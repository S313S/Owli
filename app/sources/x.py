"""X API v2 recent search 信息源适配器。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.adapters.ratelimit import RouteState
from app.sources.spec import SourceSpec

from app.store.usage import (
    SourceUsageStore,
    billing_cycle_start_utc,
    week_start_utc,
)


# SOURCE_SPEC 实例在模块尾部声明（entrypoint 指向下方 search 函数）。

_WINDOW_PATTERN = re.compile(r"^([1-7])d$")
_UNSUPPORTED_ENGAGEMENT_OPERATOR = re.compile(
    r"\bmin_(?:faves|retweets|replies|quotes):", re.IGNORECASE
)
_LANG_PATTERN = re.compile(r"^[A-Za-z][A-Za-z-]{0,14}$")
_TWEET_FIELDS = "created_at,public_metrics,author_id,lang,note_tweet"
_BASELINE_RATING_NOTES = (
    "权威1:沿用平台基线 · 时效2:recent近7天 · 交叉0:采集阶段单一源 · "
    "完整1:仅取单帖正文 · 无关2:普通用户短讯"
)


@dataclass(frozen=True)
class XSourceConfig:
    api_base_url: str
    weekly_budget_usd: Decimal
    balance_usd: Decimal
    billing_cycle_cap_usd: Decimal
    billing_cycle_spent_usd: Decimal
    price_per_read_usd: Decimal
    bearer_token_env: str = "X_BEARER_TOKEN"
    warn_threshold: Decimal = Decimal("0.80")
    billing_cycle_anchor_day: int = 31
    timeout_seconds: float = 15.0
    max_backoff_attempts: int = 2

    def __post_init__(self) -> None:
        money = (
            self.weekly_budget_usd,
            self.balance_usd,
            self.billing_cycle_cap_usd,
            self.billing_cycle_spent_usd,
            self.price_per_read_usd,
        )
        if any(not isinstance(value, Decimal) for value in money):
            raise TypeError("X 金额配置必须使用 Decimal")
        if self.price_per_read_usd <= 0:
            raise ValueError("X 单价配置必须大于 0")
        if any(value < 0 for value in money[:-1]):
            raise ValueError("X 预算配置不能为负数")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class XSearchResult:
    evidence: list[dict[str, Any]]
    conclusion: dict[str, Any]


class HttpGet(Protocol):
    def __call__(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


EventCallback = Callable[[dict[str, Any]], None]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_bearer_token(
    env_path: str | Path | None = None,
    *,
    variable_name: str = "X_BEARER_TOKEN",
) -> str:
    """只从 ~/.owli/.env 形态的文件读取 token，不读取进程环境。"""

    path = Path(env_path) if env_path is not None else Path.home() / ".owli" / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"无法读取 X 凭证文件：{path}") from error
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == variable_name:
            token = value.strip().strip('"').strip("'")
            if token:
                return token
    raise RuntimeError(f"X 凭证文件缺少 {variable_name}")


def build_recent_search_params(
    query: str,
    *,
    lang: str,
    window: str,
    max_results: int,
    now: datetime,
) -> dict[str, str]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if _UNSUPPORTED_ENGAGEMENT_OPERATOR.search(query):
        raise ValueError("互动量过滤必须在本地执行，查询不得包含 min_* 操作符")
    matched = _WINDOW_PATTERN.fullmatch(window)
    if matched is None:
        raise ValueError('X recent search 的 window 必须为 "1d" 至 "7d"')
    if not _LANG_PATTERN.fullmatch(lang):
        raise ValueError("lang 必须是显式语言代码")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 10 <= max_results <= 100:
        raise ValueError("max_results 必须为 10–100 整数")
    current = now.astimezone(timezone.utc)
    start = current - timedelta(days=int(matched.group(1)))
    base = query.strip()
    if "-is:retweet" not in base:
        base += " -is:retweet"
    if "-is:reply" not in base:
        base += " -is:reply"
    if not re.search(r"(?:^|\s)lang:[^\s]+", base):
        base += f" lang:{lang}"
    if len(base) > 512:
        raise ValueError("X recent search 查询超过 512 字符")
    return {
        "query": base,
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_results": str(max_results),
        "sort_order": "relevancy",
        "tweet.fields": _TWEET_FIELDS,
    }


def _metric(metrics: Mapping[str, Any], name: str) -> int:
    value = metrics.get(name, 0)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _evidence(item: Mapping[str, Any], query: str, fetched_at: str) -> dict[str, Any]:
    item_id = str(item.get("id") or "")
    if not item_id:
        raise RuntimeError("X 响应帖子缺少 id")
    raw = item.get("public_metrics")
    metrics = raw if isinstance(raw, Mapping) else {}
    note_tweet = item.get("note_tweet")
    full_text = note_tweet.get("text") if isinstance(note_tweet, Mapping) else None
    author_id = item.get("author_id")
    return {
        "platform": "x",
        "source_type": "post",
        "platform_item_id": item_id,
        "permalink": f"https://x.com/i/status/{item_id}",
        "title": None,
        "content_excerpt": str(full_text or item.get("text") or "") or None,
        "author_name": None,
        "author_meta": {"author_id": str(author_id)} if author_id else None,
        "source_keyword": query,
        "fetch_method": "official_api",
        "published_at": item.get("created_at"),
        "fetched_at": fetched_at,
        "raw_metrics": {
            "like_count": _metric(metrics, "like_count"),
            "retweet_count": _metric(metrics, "retweet_count"),
            "reply_count": _metric(metrics, "reply_count"),
            "quote_count": _metric(metrics, "quote_count"),
        },
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": 0,
        "score_completeness": 1,
        "score_independence": 2,
        "rating_notes": _BASELINE_RATING_NOTES,
        "rated_by": "baseline:x@v1",
    }


def map_recent_search_response(
    payload: Mapping[str, Any],
    *,
    query: str,
    fetched_at: datetime,
    min_likes: int,
    min_retweets: int,
) -> XSearchResult:
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("X recent search 响应 data 不是数组")
    if min_likes < 0 or min_retweets < 0:
        raise ValueError("本地互动量阈值不能为负数")
    fetched_iso = fetched_at.astimezone(timezone.utc).isoformat()
    evidence = []
    for item in data:
        if not isinstance(item, Mapping):
            raise RuntimeError("X recent search 命中项不是对象")
        raw_metrics = item.get("public_metrics")
        metrics = raw_metrics if isinstance(raw_metrics, Mapping) else {}
        if (
            _metric(metrics, "like_count") >= min_likes
            or _metric(metrics, "retweet_count") >= min_retweets
        ):
            evidence.append(_evidence(item, query, fetched_iso))
    meta = payload.get("meta")
    api_result_count = meta.get("result_count") if isinstance(meta, Mapping) else None
    return XSearchResult(
        evidence=evidence,
        conclusion={
            "status": "completed",
            "before_filter": len(data),
            "after_filter": len(evidence),
            "filtered_out": len(data) - len(evidence),
            "api_result_count": api_result_count,
        },
    )


def prepare_evidence_batch(
    result: XSearchResult,
    *,
    report_id: str,
    goal_id: str,
    agent_name: str,
    engine: str,
) -> list[dict[str, Any]]:
    """补齐系统归属字段并复用 M3-a 平台内归一化契约。"""

    from app.reliability.scoring import normalize_evidence_metrics

    items = []
    for evidence in result.evidence:
        item_id = str(evidence["platform_item_id"])
        items.append({
            **evidence,
            "id": f"ev-{report_id}-{item_id}",
            "report_id": report_id,
            "goal_id": goal_id,
            "agent_name": agent_name,
            "engine": engine,
        })
    computed_at = (
        str(items[0]["fetched_at"])
        if items else datetime.now(timezone.utc).isoformat()
    )
    queries = tuple(
        dict.fromkeys(str(item["source_keyword"]) for item in items)
    )
    return normalize_evidence_metrics(
        items,
        computed_at=computed_at,
        report_id=report_id,
        goal_id=goal_id,
        queries=queries,
        filters="-is:retweet -is:reply + local_public_metrics",
    )


def budget_snapshot(
    config: XSourceConfig,
    usage_store: SourceUsageStore,
    *,
    now: datetime,
    estimated_reads: int,
) -> dict[str, Any]:
    """按周预算、账期 cap 剩余与 credits 余额的最小值做事前预估。"""

    if estimated_reads < 0:
        raise ValueError("estimated_reads 不能为负数")
    current = now.astimezone(timezone.utc)
    week_start = week_start_utc(current)
    cycle_start = billing_cycle_start_utc(
        current, config.billing_cycle_anchor_day
    )
    used_reads = usage_store.reads_since("x_api", week_start)
    cap_headroom = max(
        Decimal(0),
        config.billing_cycle_cap_usd - config.billing_cycle_spent_usd,
    )
    candidates = {
        "owli_weekly_budget": config.weekly_budget_usd,
        "platform_billing_cycle_cap": cap_headroom,
        "platform_credits_balance": config.balance_usd,
    }
    limiting_gate, effective_limit = min(candidates.items(), key=lambda item: item[1])
    effective_quota_reads = int(effective_limit / config.price_per_read_usd)
    threshold = Decimal(effective_quota_reads) * config.warn_threshold
    warning = (
        Decimal(used_reads) >= threshold
        or used_reads + estimated_reads > effective_quota_reads
    )
    return {
        "week_start": week_start.isoformat(),
        "billing_cycle_start": cycle_start.isoformat(),
        "weekly_reads_before": used_reads,
        "estimated_reads": estimated_reads,
        "effective_quota_reads": effective_quota_reads,
        "effective_limit_usd": str(effective_limit),
        "limiting_gate": limiting_gate,
        "warning": warning,
    }


def _budget_card(snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    timestamp = now.astimezone(timezone.utc).isoformat()
    gate = str(snapshot["limiting_gate"])
    return {
        "type": "card_update",
        "data": {
            "card": {
                "card_id": f"x-budget-{now.strftime('%Y%m%dT%H%M%S%fZ')}",
                "card_type": "EXTRA_QUOTA_CONFIRM",
                "research_id": "source-x",
                "goal_id": None,
                "agent_id": None,
                "title": "X 信息源预算提示",
                "body": (
                    f"预计读取 {snapshot['estimated_reads']} 条；有效额度受 {gate} 限制。"
                    "这是软提示，本次任务继续执行。"
                ),
                "target": {
                    "source": "x",
                    "gate": gate,
                    "estimated_reads": snapshot["estimated_reads"],
                    "weekly_reads_before": snapshot["weekly_reads_before"],
                    "effective_quota_reads": snapshot["effective_quota_reads"],
                },
                "actions": [{
                    "type": "CHOICE_2",
                    "options": ["继续本次查询", "稍后调整 X 额度"],
                }],
                "blocking": "none",
                "deadline": None,
                "status": "pending",
                "result": None,
                "created_at": timestamp,
                "resolved_at": None,
            }
        },
    }


def _platform_hard_gate(response: HttpResponse) -> str | None:
    if response.status not in {402, 403}:
        return None
    text = json.dumps(response.payload, ensure_ascii=False).casefold()
    if any(token in text for token in ("usagecapexceeded", "billing cycle cap", "spend cap")):
        return "platform_billing_cycle_cap"
    if any(token in text for token in ("creditsdepleted", "credit balance", "insufficient credits")):
        return "platform_credits_balance"
    return None


def _default_http_get(
    url: str, *, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    request = Request(url, headers=dict(headers))
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            response_headers = dict(response.headers.items())
    except HTTPError as error:
        raw = error.read()
        status = error.code
        response_headers = dict(error.headers.items()) if error.headers else {}
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(payload, Mapping):
        raise RuntimeError("X API 响应不是 JSON 对象")
    return HttpResponse(status, response_headers, payload)


def _rate_limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    lowered = {str(key).casefold(): str(value) for key, value in headers.items()}
    names = (
        "x-rate-limit-limit",
        "x-rate-limit-remaining",
        "x-rate-limit-reset",
    )
    return {name: lowered[name] for name in names if name in lowered}


@dataclass
class XRecentSearch:
    config: XSourceConfig
    usage_store: SourceUsageStore
    token_loader: Callable[[], str]
    http_get: HttpGet = _default_http_get
    clock: Clock = _now_utc
    sleeper: Sleeper = time.sleep
    on_event: EventCallback | None = None

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def search(
        self,
        query: str,
        *,
        window: str,
        lang: str,
        max_results: int,
        min_likes: int,
        min_retweets: int,
    ) -> XSearchResult:
        now = self.clock().astimezone(timezone.utc)
        params = build_recent_search_params(
            query, lang=lang, window=window, max_results=max_results, now=now
        )
        snapshot = budget_snapshot(
            self.config,
            self.usage_store,
            now=now,
            estimated_reads=max_results,
        )
        if snapshot["warning"]:
            self._emit(_budget_card(snapshot, now))
        url = f"{self.config.api_base_url.rstrip('/')}/tweets/search/recent?{urlencode(params)}"
        token = self.token_loader()
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Owli/0.1 X-source",
        }
        backoff_count = 0
        while True:
            response = self.http_get(
                url,
                headers=request_headers,
                timeout=self.config.timeout_seconds,
            )
            if response.status != 429:
                break
            self.usage_store.record_response(
                "x_api", occurred_at=now, resource_ids=[]
            )
            rate_headers = _rate_limit_headers(response.headers)
            reset_text = rate_headers.get("x-rate-limit-reset")
            try:
                resets_at = int(reset_text) if reset_text is not None else None
            except ValueError:
                resets_at = None
            if resets_at is None:
                resets_at = int(self.clock().timestamp()) + 2 ** backoff_count
            self._emit({
                "type": "source_route",
                "data": {
                    "source": "x",
                    "route_state": RouteState.BACKOFF.value,
                    "resets_at": resets_at,
                    "rate_limit_headers": rate_headers,
                    "suspend_new_tasks": True,
                },
            })
            backoff_count += 1
            if backoff_count > self.config.max_backoff_attempts:
                return XSearchResult(
                    evidence=[],
                    conclusion={
                        "status": "backoff",
                        "route_state": RouteState.BACKOFF.value,
                        "resets_at": resets_at,
                        "backoff_count": backoff_count,
                        "task_continues": True,
                        "soft_budget_warning": bool(snapshot["warning"]),
                        **snapshot,
                    },
                )
            wait_seconds = max(0, resets_at - int(self.clock().timestamp()))
            self.sleeper(wait_seconds)
        if backoff_count:
            self._emit({
                "type": "source_route",
                "data": {
                    "source": "x",
                    "route_state": RouteState.CONTINUE.value,
                    "resumed_at": self.clock().astimezone(timezone.utc).isoformat(),
                    "backoff_count": backoff_count,
                },
            })
        hard_gate = _platform_hard_gate(response)
        if hard_gate is not None:
            self.usage_store.record_response(
                "x_api", occurred_at=now, resource_ids=[]
            )
            self._emit({
                "type": "source_unavailable",
                "data": {
                    "source": "x",
                    "gate": hard_gate,
                    "supersedes": snapshot["limiting_gate"]
                    if snapshot["warning"] else None,
                    "task_continues": True,
                },
            })
            return XSearchResult(
                evidence=[],
                conclusion={
                    "status": "unavailable",
                    "before_filter": 0,
                    "after_filter": 0,
                    "filtered_out": 0,
                    "soft_budget_warning": bool(snapshot["warning"]),
                    "soft_gate": snapshot["limiting_gate"]
                    if snapshot["warning"] else None,
                    "hard_gate": hard_gate,
                    "task_continues": True,
                    **snapshot,
                },
            )
        if response.status != 200:
            raise RuntimeError(f"X recent search 请求失败：HTTP {response.status}")
        result = map_recent_search_response(
            response.payload,
            query=query,
            fetched_at=now,
            min_likes=min_likes,
            min_retweets=min_retweets,
        )
        data = response.payload.get("data", [])
        resource_ids = [str(item["id"]) for item in data if isinstance(item, Mapping) and item.get("id")]
        accounting = self.usage_store.record_response(
            "x_api", occurred_at=now, resource_ids=resource_ids
        )
        result.conclusion.update(
            actual_returned=accounting.returned,
            newly_billed=accounting.newly_billed,
            soft_budget_warning=bool(snapshot["warning"]),
            soft_gate=snapshot["limiting_gate"] if snapshot["warning"] else None,
            hard_gate=None,
            backoff_count=backoff_count,
            **snapshot,
        )
        self._emit({
            "type": "source_usage_reconciled",
            "data": {
                "source": "x",
                "actual_returned": accounting.returned,
                "newly_billed": accounting.newly_billed,
                "charged_usd": str(
                    accounting.newly_billed * self.config.price_per_read_usd
                ),
                "expansions_billed_reads": 0,
                "billing_cycle_start": snapshot["billing_cycle_start"],
            },
        })
        return result


def search(
    query: str,
    window: str,
    *,
    config: XSourceConfig,
    usage_store: SourceUsageStore,
    lang: str,
    max_results: int,
    min_likes: int,
    min_retweets: int,
    token_loader: Callable[[], str] | None = None,
    http_get: HttpGet = _default_http_get,
    clock: Clock = _now_utc,
    sleeper: Sleeper = time.sleep,
    on_event: EventCallback | None = None,
) -> XSearchResult:
    loader = token_loader or (
        lambda: load_bearer_token(variable_name=config.bearer_token_env)
    )
    return XRecentSearch(
        config=config,
        usage_store=usage_store,
        token_loader=loader,
        http_get=http_get,
        clock=clock,
        sleeper=sleeper,
        on_event=on_event,
    ).search(
        query,
        window=window,
        lang=lang,
        max_results=max_results,
        min_likes=min_likes,
        min_retweets=min_retweets,
    )


SOURCE_SPEC = SourceSpec(
    source_id="x",
    tool_name="source.x",
    entrypoint=search,
)
