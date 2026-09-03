"""Reddit 信息源：Prowlo MCP 主路径，Apify 异步兜底。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from app.reliability.scoring import normalize_evidence_metrics
from app.sources import comments as comment_shape
from app.sources.spec import SourceSpec


__all__ = ["SOURCE_SPEC", "fetch_comments", "search"]

_PROWLO_MCP_URL = "https://api.prowlo.com/mcp"
_APIFY_API_URL = "https://api.apify.com/v2"
_APIFY_ACTOR = "trudax~reddit-scraper-lite"
_ENV_PATH = Path.home() / ".owli" / ".env"
_WINDOW_PATTERN = re.compile(r"^([1-9]\d*)d$")
_TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HttpRequest = Callable[
    [str, str, Mapping[str, str], bytes | None, float], HttpResponse
]
EventCallback = Callable[[dict[str, Any]], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_token(name: str, env_path: Path = _ENV_PATH) -> str | None:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            token = value.strip().strip("\"'")
            return token or None
    return None


def _default_http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    """显式绕过系统代理，避免本机代理把 Prowlo 伪装成 403。"""

    request = Request(url, data=body, method=method, headers=dict(headers))
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                int(response.status), dict(response.headers.items()), response.read()
            )
    except HTTPError as error:
        return HttpResponse(
            error.code,
            dict(error.headers.items()) if error.headers else {},
            error.read(),
        )
    except (URLError, OSError) as error:
        raise RuntimeError("Reddit 第三方 API 网络请求失败") from error


def _json_body(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _decode_json(body: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} 响应不是合法 JSON") from error
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"{label} 响应不是 JSON 对象")
    return payload


def _decode_mcp(body: bytes) -> Mapping[str, Any]:
    """同时接受 MCP JSON 与 `event: message` SSE 帧。"""

    text = body.decode("utf-8")
    candidates = [
        line.removeprefix("data: ")
        for line in text.splitlines()
        if line.startswith("data: ")
    ]
    raw = candidates[-1] if candidates else text
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("Prowlo MCP 响应不是合法 JSON/SSE") from error
    if not isinstance(payload, Mapping):
        raise RuntimeError("Prowlo MCP 响应不是 JSON 对象")
    if payload.get("error"):
        raise RuntimeError("Prowlo MCP 返回 JSON-RPC error")
    return payload


class ProwloClient:
    def __init__(
        self,
        token: str,
        *,
        http_request: HttpRequest,
        timeout_seconds: float,
    ) -> None:
        self._token = token
        self._http_request = http_request
        self._timeout_seconds = timeout_seconds
        self._session_id: str | None = None
        self._next_id = 1

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": "Owli/0.1 Reddit-source",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        response = self._http_request(
            "POST", _PROWLO_MCP_URL, self._headers(), _json_body(payload),
            self._timeout_seconds,
        )
        lowered = {str(key).casefold(): str(value) for key, value in response.headers.items()}
        self._session_id = lowered.get("mcp-session-id", self._session_id)
        if response.status == 202:
            return None
        if response.status != 200:
            raise RuntimeError(f"Prowlo MCP 请求失败：HTTP {response.status}")
        return _decode_mcp(response.body)

    def initialize(self) -> None:
        payload = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "owli", "version": "0.1"},
            },
        })
        self._next_id += 1
        if payload is None or not self._session_id:
            raise RuntimeError("Prowlo MCP initialize 缺少会话")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        })
        self._next_id += 1
        result = payload.get("result") if isinstance(payload, Mapping) else None
        content = result.get("content") if isinstance(result, Mapping) else None
        if not isinstance(content, list):
            raise RuntimeError(f"Prowlo {name} 响应缺少 result.content")
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, Mapping):
                continue
            if decoded.get("status") == "limit":
                raise RuntimeError(f"Prowlo {name} 达到服务限制")
            return decoded
        raise RuntimeError(f"Prowlo {name} 未返回结构化结果")


def _prowlo_time(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 366:
        return "year"
    return "all"


def _prowlo_items(
    query: str,
    *,
    days: int,
    limit: int,
    client: ProwloClient,
) -> tuple[list[Mapping[str, Any]], dict[str, int]]:
    client.initialize()
    dataset = client.call("search_dataset", {
        "query": query, "mode": "keyword", "platforms": ["reddit"],
        "limit": min(limit, 40),
    })
    raw_dataset = dataset.get("items", [])
    dataset_items = [item for item in raw_dataset if isinstance(item, Mapping)]
    expanded: list[Mapping[str, Any]] = []
    get_record_calls = 0
    for item in dataset_items[:limit]:
        item_id = item.get("id")
        if not item_id:
            expanded.append(item)
            continue
        try:
            get_record_calls += 1
            record = client.call("get_record", {"id": str(item_id)})
        except RuntimeError:
            expanded.append(item)
            continue
        detail = record.get("record") if isinstance(record.get("record"), Mapping) else record
        expanded.append(detail if isinstance(detail, Mapping) else item)

    remaining = max(0, limit - len(expanded))
    live_items: list[Mapping[str, Any]] = []
    if remaining:
        live = client.call("social_search", {
            "platform": "reddit", "query": query, "sort": "relevance",
            "time": _prowlo_time(days), "limit": min(limit, 20),
        })
        values = live.get("items", [])
        live_items = [item for item in values if isinstance(item, Mapping)]
    return expanded + live_items, {
        "dataset_search": 1,
        "dataset_get_record": get_record_calls,
        "live_read": 1 if remaining else 0,
    }


def _apify_url(path: str, token: str, **params: Any) -> str:
    query = urlencode({"token": token, **params})
    return f"{_APIFY_API_URL}{path}?{query}"


def _apify_items(
    query: str,
    *,
    days: int,
    limit: int,
    token: str,
    http_request: HttpRequest,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[list[Mapping[str, Any]], dict[str, int]]:
    actor_input = {
        "searches": [query],
        "searchPosts": True,
        "searchComments": False,
        "sort": "relevance",
        "time": _prowlo_time(days),
        "maxItems": limit,
        "maxPostCount": limit,
        "skipComments": True,
        "includeMediaLinks": True,
    }
    start = http_request(
        "POST",
        _apify_url(f"/acts/{_APIFY_ACTOR}/runs", token),
        {"Accept": "application/json", "Content-Type": "application/json",
         "User-Agent": "Owli/0.1 Reddit-source"},
        _json_body(actor_input),
        30.0,
    )
    if start.status not in {200, 201}:
        raise RuntimeError(f"Apify 启动 Reddit actor 失败：HTTP {start.status}")
    start_payload = _decode_json(start.body, label="Apify 启动 run")
    run = start_payload.get("data")
    if not isinstance(run, Mapping) or not run.get("id"):
        raise RuntimeError("Apify 启动响应缺少 run id")
    run_id = str(run["id"])
    deadline = clock() + timeout_seconds
    polls = 0

    while True:
        if clock() > deadline:
            raise RuntimeError("Apify Reddit actor 轮询超时")
        status_response = http_request(
            "GET", _apify_url(f"/actor-runs/{quote(run_id)}", token),
            {"Accept": "application/json", "User-Agent": "Owli/0.1 Reddit-source"},
            None, 30.0,
        )
        polls += 1
        if status_response.status != 200:
            raise RuntimeError(f"Apify 查询 run 失败：HTTP {status_response.status}")
        status_payload = _decode_json(status_response.body, label="Apify run")
        current = status_payload.get("data")
        if not isinstance(current, Mapping):
            raise RuntimeError("Apify run 响应缺少 data")
        status = str(current.get("status") or "")
        if status in _TERMINAL_RUN_STATES:
            run = current
            break
        sleeper(poll_interval_seconds)

    if run.get("status") != "SUCCEEDED" or not run.get("defaultDatasetId"):
        raise RuntimeError(f"Apify Reddit actor 未成功：{run.get('status') or 'UNKNOWN'}")
    dataset_id = quote(str(run["defaultDatasetId"]))
    dataset = http_request(
        "GET",
        _apify_url(f"/datasets/{dataset_id}/items", token, clean="true", limit=limit),
        {"Accept": "application/json", "User-Agent": "Owli/0.1 Reddit-source"},
        None,
        60.0,
    )
    if dataset.status != 200:
        raise RuntimeError(f"Apify 读取 Reddit dataset 失败：HTTP {dataset.status}")
    try:
        values = json.loads(dataset.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Apify Reddit dataset 不是合法 JSON") from error
    if not isinstance(values, list):
        raise RuntimeError("Apify Reddit dataset 不是数组")
    return [item for item in values if isinstance(item, Mapping)], {
        "actor_start": 1, "actor_poll": polls, "dataset_read": 1,
    }


def _reddit_permalink(item: Mapping[str, Any]) -> str:
    raw = str(
        item.get("permalink") or item.get("postUrl") or item.get("url") or ""
    ).strip()
    if raw.startswith("/"):
        raw = f"https://www.reddit.com{raw}"
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").casefold()
    if host not in {"reddit.com", "www.reddit.com", "old.reddit.com"}:
        item_id = str(
            item.get("redditId") or item.get("id") or item.get("postId") or ""
        ).removeprefix("t3_")
        if not item_id:
            raise RuntimeError("Reddit 条目缺少可追溯 permalink")
        return f"https://www.reddit.com/comments/{quote(item_id)}"
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", "www.reddit.com", path, "", ""))


def _integer(item: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return 0


def _published_at(item: Mapping[str, Any]) -> str | None:
    value = item.get("redditCreatedAt") or item.get("createdAt")
    if isinstance(value, str) and value.strip():
        return value.strip()
    timestamp = item.get("createdUtc") or item.get("created_utc")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    return None


def _platform_item_id(item: Mapping[str, Any], permalink: str) -> str:
    for name in ("redditId", "externalId", "postId", "id"):
        value = item.get(name)
        if value:
            return str(value).removeprefix("t3_")
    match = re.search(r"/comments/([^/]+)", urlsplit(permalink).path)
    if match:
        return match.group(1)
    raise RuntimeError("Reddit 条目缺少平台 ID")


def _to_evidence(
    item: Mapping[str, Any],
    *,
    query: str,
    fetched_at: str,
    provider: str,
) -> dict[str, Any]:
    permalink = _reddit_permalink(item)
    body = item.get("body") or item.get("text") or item.get("snippet")
    excerpt = str(body).strip() if body is not None else ""
    author = item.get("author") or item.get("username")
    status = item.get("status")
    removed = item.get("removedByCategory") or item.get("removed_by_category")
    return {
        "platform": "reddit",
        "source_type": "search_snippet",
        "platform_item_id": _platform_item_id(item, permalink),
        "permalink": permalink,
        "title": str(item.get("title") or "") or None,
        "content_excerpt": excerpt[:4000] or None,
        "author_name": str(author) if author else None,
        "author_meta": {"subreddit": str(item.get("subreddit") or "")} if item.get("subreddit") else None,
        "source_keyword": query,
        "fetch_method": "third_party_api",
        "published_at": _published_at(item),
        "fetched_at": fetched_at,
        "raw_metrics": {
            "score": _integer(item, "score", "upVotes", "upvotes"),
            "num_comments": _integer(
                item, "numComments", "numberOfComments", "commentsCount"
            ),
            "upvote_ratio": item.get("upvote_ratio") or item.get("upVoteRatio"),
        },
        "score_authority": 0,
        "score_freshness": 1,
        "score_crossref": None,
        "score_completeness": 0,
        "score_independence": 2,
        "rating_notes": (
            "权威0:作者身份不可核验 · 时效1:索引时间基线 · "
            "交叉?:缺断言血缘簇 · 完整0:索引缺评论树 · 无关2:社区讨论无投放"
        ),
        "rated_by": "baseline:reddit@v1",
        "extra": {
            "authority_kind": "anonymous_or_unverifiable",
            "content_kind": "user_opinion",
            "interest_relation": "arms_length",
            "provider": provider,
            "status": str(status) if status is not None else "unknown",
            "removed_by_category": str(removed) if removed is not None else "",
            "external_url": str(item.get("url") or ""),
            "link_flair_text": str(item.get("link_flair_text") or ""),
            "domain": str(item.get("domain") or ""),
        },
    }


def _deduplicate(items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    by_permalink: dict[str, Mapping[str, Any]] = {}
    for item in items:
        try:
            permalink = _reddit_permalink(item)
        except RuntimeError:
            continue
        existing = by_permalink.get(permalink)
        if existing is None or len(str(item.get("body") or item.get("snippet") or "")) > len(
            str(existing.get("body") or existing.get("snippet") or "")
        ):
            by_permalink[permalink] = item
    return list(by_permalink.values())


def _comment_permalink(item: Mapping[str, Any], parent_permalink: str) -> str:
    """Prowlo 每条评论回的 permalink 是父帖的；单条评论链接要用 t1 id 拼。"""

    comment_id = str(item.get("id") or "").removeprefix("t1_").strip()
    raw = str(item.get("permalink") or "").strip()
    base = raw or parent_permalink
    if not base:
        return ""
    if comment_id and not base.rstrip("/").endswith(comment_id):
        return f"{base.rstrip('/')}/{quote(comment_id)}"
    return base


def _to_comment(
    item: Mapping[str, Any], *, parent_permalink: str
) -> comment_shape.Comment:
    return comment_shape.Comment(
        parent_permalink=parent_permalink,
        permalink=_comment_permalink(item, parent_permalink),
        author=str(item.get("author") or "").strip(),
        text=str(item.get("body") or "").strip(),
        likes=comment_shape.integer(item, "score", "upVotes", "ups"),
        published_at=comment_shape.published_at(
            item.get("createdUtc") or item.get("created_utc")
            or item.get("redditCreatedAt")
        ),
        platform="reddit",
        comment_id=str(item.get("id") or "").strip(),
    )


def fetch_comments(
    post_id: str,
    *,
    parent_permalink: str,
    limit: int = 20,
    prowlo_token: str | None = None,
    http_request: HttpRequest = _default_http_request,
    timeout_seconds: float = 60.0,
    client: ProwloClient | None = None,
) -> comment_shape.CommentBatch:
    """用 Prowlo social_get_post 取一帖的评论树顶层（每次算一次 live read）。"""

    identifier = str(post_id).strip()
    if not identifier:
        raise ValueError("post_id 必须是非空字符串")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("limit 必须为 1-20 整数")
    if client is None:
        token = prowlo_token or _load_token("PROWLO_API_KEY")
        if not token:
            raise RuntimeError("缺少 PROWLO_API_KEY")
        client = ProwloClient(
            token, http_request=http_request, timeout_seconds=timeout_seconds
        )
        client.initialize()
    payload = client.call("social_get_post", {
        "platform": "reddit", "id": identifier,
        "includeComments": True, "commentLimit": limit,
    })
    values = payload.get("comments")
    if not isinstance(values, list):
        raise RuntimeError("Prowlo social_get_post 响应缺少 comments")
    post = payload.get("post") if isinstance(payload.get("post"), Mapping) else {}
    parent = parent_permalink or str(post.get("permalink") or "")
    kept, dropped = comment_shape.clean(
        [
            _to_comment(item, parent_permalink=parent)
            for item in values if isinstance(item, Mapping)
        ],
        limit=limit,
    )
    return comment_shape.CommentBatch(comments=kept, dropped_short=dropped, calls=1)


def _emit(on_event: EventCallback | None, event_type: str, **data: Any) -> None:
    if on_event is not None:
        on_event({"type": event_type, "data": {"source": "reddit", **data}})


def search(
    query: str,
    window: str,
    *,
    limit: int = 20,
    store: Any | None = None,
    report_id: str | None = None,
    goal_id: str | None = None,
    agent_name: str | None = None,
    on_event: EventCallback | None = None,
    prowlo_token: str | None = None,
    apify_token: str | None = None,
    http_request: HttpRequest = _default_http_request,
    timeout_seconds: float = 420.0,
    poll_interval_seconds: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utc_now,
) -> list[dict[str, Any]]:
    """先查 Prowlo 自有 Dataset，再按需 live read；失败则异步跑 Apify。"""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    matched = _WINDOW_PATTERN.fullmatch(window)
    if matched is None:
        raise ValueError('window 必须形如 "7d" 或 "90d"')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("limit 必须为 1–20 整数")
    if store is not None and (not report_id or not goal_id):
        raise ValueError("入库时 report_id 与 goal_id 必填")
    if timeout_seconds <= 0 or poll_interval_seconds < 0:
        raise ValueError("timeout_seconds 必须大于 0，poll_interval_seconds 不得为负")

    days = int(matched.group(1))
    primary_token = prowlo_token or _load_token("PROWLO_API_KEY")
    fallback_token = apify_token or _load_token("APIFY_TOKEN")
    items: list[Mapping[str, Any]] = []
    provider = ""
    usage: dict[str, int] = {}
    failures: list[str] = []

    if primary_token:
        try:
            client = ProwloClient(
                primary_token, http_request=http_request,
                timeout_seconds=min(timeout_seconds, 60.0),
            )
            items, usage = _prowlo_items(
                query.strip(), days=days, limit=limit, client=client
            )
            provider = "prowlo"
        except RuntimeError:
            failures.append("prowlo_unavailable")
            _emit(on_event, "source_route", provider="prowlo", state="fallback")
    else:
        failures.append("prowlo_credential_missing")

    if not items and fallback_token:
        try:
            items, usage = _apify_items(
                query.strip(), days=days, limit=limit, token=fallback_token,
                http_request=http_request, timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds, sleeper=sleeper,
                clock=monotonic,
            )
            provider = "apify"
        except RuntimeError:
            failures.append("apify_unavailable")
    elif not items:
        failures.append("apify_credential_missing")

    untitled_count = sum(
        1 for item in items if not str(item.get("title") or "").strip()
    )
    if untitled_count:
        items = [
            item for item in items if str(item.get("title") or "").strip()
        ]
        _emit(
            on_event, "source_items_dropped", provider=provider,
            reason="missing_title", dropped=untitled_count,
            task_continues=True,
        )

    unique = _deduplicate(items)[:limit]
    if not unique:
        if provider:
            _emit(
                on_event, "source_empty", provider=provider,
                reason="empty_result", task_continues=True,
            )
        else:
            _emit(
                on_event, "source_unavailable", reason="all_providers_unavailable",
                failures=failures, task_continues=True,
            )
        return []

    fetched_at = now().astimezone(timezone.utc).isoformat()
    evidence = [
        _to_evidence(
            item, query=query.strip(), fetched_at=fetched_at, provider=provider
        )
        for item in unique
    ]
    normalized = normalize_evidence_metrics(
        evidence,
        computed_at=fetched_at,
        report_id=report_id or "unpersisted",
        goal_id=goal_id or "unpersisted",
        queries=[query.strip()],
        filters=f"window={window};sort=relevance;provider={provider}",
    )
    if store is not None:
        assert report_id is not None and goal_id is not None
        store.upsert_evidence_batch([
            {
                **item,
                "id": (
                    f"ev-{report_id}-reddit-"
                    f"{re.sub(r'[^A-Za-z0-9_-]', '-', str(item['platform_item_id']))}"
                ),
                "report_id": report_id,
                "goal_id": goal_id,
                # §M6-a 货 2（照 RATE-2 5d012a2 xhs 口径）：直落库也要带章归属，
                # 否则这一章采到的行没有任何章认领，评级章物化时看不见。
                "agent_name": agent_name,
            }
            for item in normalized
        ])
    _emit(
        on_event, "source_usage_reconciled", provider=provider,
        calls=usage, returned=len(normalized), task_continues=True,
    )
    return normalized


SOURCE_SPEC = SourceSpec(
    source_id="reddit",
    tool_name="source.reddit",
    entrypoint=search,
    display_name="Reddit",
    collector_name="Reddit 数据抓取",
    capability_description="全站社区帖子；Prowlo Dataset/live read 主路径，Apify 异步兜底",
    prompt_hint="先查免费 Dataset，再对不足样本做 relevance live search",
)
