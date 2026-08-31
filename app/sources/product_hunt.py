"""Product Hunt GraphQL 信息源适配器。"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.adapters.events import NormalizedEvent
from app.adapters.logging import DEFAULT_LOG_ROOT
from app.adapters.ratelimit import (
    RouteCause,
    RouteDecision,
    RouteState,
    publish_route_decision,
)
from app.reliability.scoring import normalize_evidence_metrics, score_evidence

try:
    from app.sources.spec import SourceSpec
except ModuleNotFoundError as error:
    if error.name != "app.sources.spec":
        raise

    @dataclass(frozen=True)
    class SourceSpec:  # type: ignore[no-redef]
        """M3-b 聚合契约尚未合入当前分支时的同构声明。"""

        source_id: str
        tool_name: str
        entrypoint: Callable[..., Any]
        display_name: str = ""
        collector_name: str = ""
        capability_description: str = ""
        prompt_hint: str = ""


__all__ = ["SOURCE_SPEC", "search"]

_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
_ENV_PATH = Path.home() / ".owli" / ".env"
_WINDOW_PATTERN = re.compile(r"^([1-9]\d*)d$")
_CREDIT_LIMIT = 6250
_WINDOW_SECONDS = 900.0
_REQUEST_COST = 100
_MAX_ATTEMPTS = 3

_POSTS_QUERY = """
query OwliProductHuntPosts($first: Int!, $after: String, $postedAfter: DateTime!) {
  posts(first: $first, after: $after, postedAfter: $postedAfter, order: VOTES) {
    edges {
      node {
        id name tagline votesCount commentsCount createdAt url
        topics { edges { node { name } } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()


class Evidence(TypedDict, total=False):
    platform: str
    source_type: str
    platform_item_id: str
    permalink: str
    title: str
    content_excerpt: str | None
    source_keyword: str
    fetch_method: str
    published_at: str
    fetched_at: str
    raw_metrics: dict[str, Any]
    extra: dict[str, Any]


@dataclass(frozen=True)
class GraphQLResponse:
    status: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


class CreditBudget:
    """进程内共享的 6250 点/15 分钟保守预算计数器。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self.limit = _CREDIT_LIMIT
        self.remaining = _CREDIT_LIMIT
        self.reset_at = clock() + _WINDOW_SECONDS

    def reserve(self, cost: int = _REQUEST_COST) -> float:
        with self._lock:
            now = self._clock()
            if now >= self.reset_at:
                self.remaining = self.limit
                self.reset_at = now + _WINDOW_SECONDS
            if self.remaining < cost:
                return max(0.0, self.reset_at - now)
            self.remaining -= cost
            return 0.0

    def sleep_until_reset(self, seconds: float) -> None:
        self._sleeper(seconds)
        self.force_reset()

    def force_reset(self) -> None:
        with self._lock:
            self.remaining = self.limit
            self.reset_at = self._clock() + _WINDOW_SECONDS

    def observe(self, headers: Mapping[str, str]) -> None:
        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        with self._lock:
            try:
                self.limit = int(lowered.get("x-rate-limit-limit", self.limit))
                self.remaining = int(
                    lowered.get("x-rate-limit-remaining", self.remaining)
                )
                reset_seconds = float(
                    lowered.get("x-rate-limit-reset", _WINDOW_SECONDS)
                )
            except ValueError as exc:
                raise RuntimeError("Product Hunt 限额响应头不是合法数字") from exc
            self.reset_at = self._clock() + max(0.0, reset_seconds)


_BUDGET = CreditBudget()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _header_seconds(headers: Mapping[str, str], default: float) -> float:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    try:
        return max(0.0, float(lowered.get("x-rate-limit-reset", default)))
    except ValueError:
        return default


def _publish_status(
    state: RouteState,
    *,
    kind: str,
    reason: str,
    raw: Mapping[str, Any],
    on_event: Callable[[NormalizedEvent], Any] | None,
    log_root: Path,
    log_clock: Callable[[], datetime] | None,
) -> None:
    publish_route_decision(
        RouteDecision(
            state,
            reason,
            {"source": "product_hunt", "kind": kind, **raw},
            cause=(
                RouteCause.RATE_LIMIT
                if kind in {"http_429", "budget_exhausted"}
                else RouteCause.NORMAL
            ),
        ),
        engine="source.product_hunt",
        on_event=on_event,
        log_root=log_root,
        log_clock=log_clock,
        publish_continue=state is RouteState.CONTINUE,
    )


def _request_page(
    token: str,
    variables: Mapping[str, Any],
    *,
    on_event: Callable[[NormalizedEvent], Any] | None,
    log_root: Path,
    log_clock: Callable[[], datetime] | None,
) -> GraphQLResponse:
    backed_off = False
    for attempt in range(_MAX_ATTEMPTS):
        wait_seconds = _BUDGET.reserve()
        if wait_seconds:
            backed_off = True
            _publish_status(
                RouteState.BACKOFF,
                kind="budget_exhausted",
                reason="Product Hunt 本地复杂度预算耗尽，等待窗口重置",
                raw={"wait_seconds": wait_seconds, "attempt": attempt + 1},
                on_event=on_event,
                log_root=log_root,
                log_clock=log_clock,
            )
            _BUDGET.sleep_until_reset(wait_seconds)

        response = _post_graphql(token, _POSTS_QUERY, variables)
        _BUDGET.observe(response.headers)
        if response.status != 429:
            if backed_off:
                _publish_status(
                    RouteState.CONTINUE,
                    kind="recovered",
                    reason="Product Hunt 退避结束，采集已恢复",
                    raw={"attempt": attempt + 1},
                    on_event=on_event,
                    log_root=log_root,
                    log_clock=log_clock,
                )
            return response

        backed_off = True
        delay = _header_seconds(response.headers, 2 ** attempt)
        _publish_status(
            RouteState.BACKOFF,
            kind="http_429",
            reason="Product Hunt API 429，等待额度窗口重置",
            raw={"http_status": 429, "wait_seconds": delay, "attempt": attempt + 1},
            on_event=on_event,
            log_root=log_root,
            log_clock=log_clock,
        )
        time.sleep(delay)
        _BUDGET.force_reset()
    raise RuntimeError("Product Hunt API 连续 429，退避后仍未恢复")


def search(
    query: str,
    window: str,
    *,
    limit: int = 20,
    page_size: int = 20,
    on_event: Callable[[NormalizedEvent], Any] | None = None,
    store: Any | None = None,
    report_id: str | None = None,
    goal_id: str | None = None,
    agent_name: str | None = None,
    log_root: Path = DEFAULT_LOG_ROOT,
    log_clock: Callable[[], datetime] | None = None,
) -> list[Evidence]:
    """拉取时间窗内按票数排序的 Product Hunt 发布条目。"""

    if not isinstance(query, str):
        raise TypeError("query 必须是字符串；空字符串表示不做本地关键词过滤")
    match = _WINDOW_PATTERN.fullmatch(window)
    if match is None:
        raise ValueError('window 必须形如 "7d" 或 "90d"')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 必须是正整数")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 20:
        raise ValueError("page_size 必须是 1–20 的整数")
    if store is not None and (not report_id or not goal_id):
        raise ValueError("入库时 report_id 与 goal_id 必填")

    now = _utc_now()
    posted_after = now - timedelta(days=int(match.group(1)))
    token = _load_token()
    nodes: list[Mapping[str, Any]] = []
    cursor: str | None = None
    has_next_page = True

    while has_next_page and len(nodes) < limit:
        response = _request_page(
            token,
            {
                "first": min(page_size, limit - len(nodes)),
                "after": cursor,
                "postedAfter": posted_after.isoformat(),
            },
            on_event=on_event,
            log_root=log_root,
            log_clock=log_clock,
        )
        posts = _posts_payload(response)
        for edge in posts["edges"]:
            node = edge.get("node") if isinstance(edge, Mapping) else None
            if not isinstance(node, Mapping):
                raise RuntimeError("Product Hunt posts.edges 含非法 node")
            if _matches_query(node, query):
                nodes.append(node)
            if len(nodes) >= limit:
                break
        page_info = posts["pageInfo"]
        has_next_page = bool(page_info.get("hasNextPage"))
        cursor_value = page_info.get("endCursor")
        cursor = str(cursor_value) if cursor_value is not None else None
        if has_next_page and not cursor:
            raise RuntimeError("Product Hunt 声明有下一页但缺少 endCursor")

    if not nodes:
        _publish_status(
            RouteState.CONTINUE,
            kind="empty_window",
            reason="Product Hunt 指定时间窗没有匹配条目",
            raw={"query": query, "window": window},
            on_event=on_event,
            log_root=log_root,
            log_clock=log_clock,
        )
        return []

    fetched_at = now.isoformat()
    evidence = [_to_evidence(node, query, fetched_at) for node in nodes]
    normalized = normalize_evidence_metrics(
        evidence,
        computed_at=fetched_at,
        report_id=report_id or "unpersisted",
        goal_id=goal_id or "unpersisted",
        queries=[query] if query else [],
        filters=f"postedAfter={posted_after.isoformat()};order=VOTES",
    )
    scored: list[Evidence] = []
    for item in normalized:
        item.update(score_evidence(item))
        item["rated_by"] = "baseline:product_hunt@v1"
        scored.append(item)
    if store is not None:
        assert report_id is not None and goal_id is not None
        store.upsert_evidence_batch([
            {
                **item,
                "id": f"ev-{report_id}-product-hunt-{item['platform_item_id']}",
                "report_id": report_id,
                "goal_id": goal_id,
                # §M6-a 货 2（照 RATE-2 5d012a2 xhs 口径）：直落库也要带章归属，
                # 否则这一章采到的行没有任何章认领，评级章物化时看不见。
                "agent_name": agent_name,
            }
            for item in scored
        ])
    return scored


def _posts_payload(response: GraphQLResponse) -> Mapping[str, Any]:
    if response.status != 200:
        raise RuntimeError(f"Product Hunt GraphQL 请求失败：HTTP {response.status}")
    errors = response.payload.get("errors")
    if errors:
        raise RuntimeError("Product Hunt GraphQL 返回 errors")
    data = response.payload.get("data")
    posts = data.get("posts") if isinstance(data, Mapping) else None
    if not isinstance(posts, Mapping):
        raise RuntimeError("Product Hunt GraphQL 响应缺少 data.posts")
    if not isinstance(posts.get("edges"), list) or not isinstance(posts.get("pageInfo"), Mapping):
        raise RuntimeError("Product Hunt GraphQL posts 缺少 edges/pageInfo")
    return posts


def _matches_query(node: Mapping[str, Any], query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    topic_names = []
    topics = node.get("topics")
    if isinstance(topics, Mapping):
        for edge in topics.get("edges", []):
            topic_node = edge.get("node") if isinstance(edge, Mapping) else None
            if isinstance(topic_node, Mapping):
                topic_names.append(str(topic_node.get("name", "")))
    haystack = " ".join(
        (str(node.get("name", "")), str(node.get("tagline", "")), *topic_names)
    ).casefold()
    return needle in haystack


def _to_evidence(
    node: Mapping[str, Any], query: str, fetched_at: str
) -> Evidence:
    item_id = node.get("id")
    url = node.get("url")
    if item_id is None:
        raise RuntimeError("Product Hunt post 缺少 id")
    if not isinstance(url, str) or not _official_permalink(url):
        raise RuntimeError("Product Hunt post 缺少官方 url")
    votes = node.get("votesCount")
    comments = node.get("commentsCount")
    if not isinstance(votes, int) or isinstance(votes, bool):
        raise RuntimeError("Product Hunt post 缺少 votesCount 整数")
    if not isinstance(comments, int) or isinstance(comments, bool):
        raise RuntimeError("Product Hunt post 缺少 commentsCount 整数")
    return {
        "platform": "product_hunt",
        "source_type": "post",
        "platform_item_id": str(item_id),
        "permalink": url,
        "title": str(node.get("name") or ""),
        "content_excerpt": str(node.get("tagline") or "") or None,
        "source_keyword": query,
        "fetch_method": "official_api",
        "published_at": str(node.get("createdAt") or ""),
        "fetched_at": fetched_at,
        "raw_metrics": {
            "votesCount": votes,
            "commentsCount": comments,
            "votes_count": votes,
            "comments_count": comments,
        },
        "extra": {
            "authority_kind": "verified_principal",
            "content_kind": "product_launch",
            "interest_relation": "disclosed_interest",
        },
    }


def _official_permalink(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and (
        host == "producthunt.com" or host.endswith(".producthunt.com")
    ) and bool(parsed.path.strip("/"))


def _load_token() -> str:
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("未找到 ~/.owli/.env 中的 PRODUCT_HUNT_TOKEN") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "PRODUCT_HUNT_TOKEN":
            token = value.strip().strip("\"'")
            if token:
                return token
    raise RuntimeError("~/.owli/.env 缺少 PRODUCT_HUNT_TOKEN")


def _post_graphql(
    token: str, query: str, variables: Mapping[str, Any]
) -> GraphQLResponse:
    body = json.dumps(
        {"query": query, "variables": dict(variables)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        _GRAPHQL_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Owli/0.1 Product-Hunt-source",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
            headers = dict(response.headers.items())
            status = int(response.status)
    except HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"errors": [{"message": "HTTP error"}]}
        headers = dict(error.headers.items()) if error.headers is not None else {}
        status = error.code
    except (URLError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Product Hunt GraphQL 网络请求失败") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("Product Hunt GraphQL 响应不是 JSON 对象")
    return GraphQLResponse(status=status, headers=headers, payload=payload)


SOURCE_SPEC = SourceSpec(
    source_id="product_hunt",
    tool_name="source.product_hunt",
    entrypoint=search,
    display_name="Product Hunt",
    collector_name="Product Hunt 数据抓取",
    capability_description="产品 launch、maker 自述、投票与发布评论",
    prompt_hint="postedAfter 圈定时间窗并按 VOTES 排序",
)
