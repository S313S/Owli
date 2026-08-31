"""Exa 主、Tavily 备的通用网页搜索信息源。"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.adapters.events import ItemKind, NormalizedEvent
from app.adapters.logging import DEFAULT_LOG_ROOT, append_routing_event
from app.reliability import normalize_evidence_metrics, score_evidence
from app.sources.spec import SourceSpec, WindowParam


__all__ = ["search", "collect_and_store"]

DEFAULT_ENV_PATH = Path("~/.owli/.env").expanduser()
_EXA_URL = "https://api.exa.ai/search"
_TAVILY_URL = "https://api.tavily.com/search"
_EXA_KEY_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_WINDOW_PATTERN = re.compile(r"^([1-9]\d*)d$")
_WINDOW_PARAM = WindowParam()
_REQUEST_TIMEOUT_SECONDS = 20.0

HttpPost = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]
EventSink = Callable[[NormalizedEvent], Any]


class CredentialError(RuntimeError):
    """网页搜索凭证缺失或归属格式错误。"""


class ProviderRequestError(RuntimeError):
    """只保留供应商与 HTTP 状态，不携带响应体或认证信息。"""

    def __init__(self, provider: str, status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else "传输或响应错误"
        super().__init__(f"{provider} 请求失败：{detail}")


@dataclass(frozen=True)
class Credentials:
    exa_api_key: str | None = field(repr=False)
    tavily_api_key: str | None = field(repr=False)


class Evidence(TypedDict):
    platform: str
    permalink: str
    fetched_at: str
    raw_metrics: dict[str, Any]
    source_keyword: str
    source_type: str
    platform_item_id: str
    title: str | None
    content_excerpt: str | None
    author_name: str | None
    fetch_method: str
    published_at: str | None
    extra: dict[str, Any]
    score_authority: int
    score_freshness: int
    score_crossref: int
    score_completeness: int
    score_independence: int
    score_total: int
    grade: str
    rating_notes: str
    rated_by: str


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_credentials(path: str | Path) -> Credentials:
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise CredentialError(f"网页搜索凭证文件不存在：{env_path}")
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        if name in {"EXA_API_KEY", "TAVILY_API_KEY"}:
            values[name] = _unquote(value.strip())

    exa = values.get("EXA_API_KEY") or None
    tavily = values.get("TAVILY_API_KEY") or None
    if exa is not None and _EXA_KEY_PATTERN.fullmatch(exa) is None:
        raise CredentialError("EXA_API_KEY 格式错误：应为无前缀的 36 位 UUID")
    if tavily is not None and not tavily.startswith("tvly-"):
        raise CredentialError("TAVILY_API_KEY 格式错误：应以 tvly- 开头")
    if exa is None and tavily is None:
        raise CredentialError("网页搜索不可用：~/.owli/.env 未配置 EXA_API_KEY 或 TAVILY_API_KEY")
    return Credentials(exa, tavily)


def _http_post(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    provider = "exa" if url == _EXA_URL else "tavily"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ProviderRequestError(provider, error.code) from error
    except (URLError, OSError, HTTPException, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderRequestError(provider) from error
    if not isinstance(decoded, Mapping):
        raise ProviderRequestError(provider)
    return decoded


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _url(value: Any) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("网页搜索结果缺少绝对 HTTP(S) permalink")
    return raw


def _exa_evidence(item: Any, query: str, fetched_at: str) -> Evidence:
    if not isinstance(item, Mapping):
        raise RuntimeError("Exa 命中项不是 JSON 对象")
    permalink = _url(item.get("url"))
    text = item.get("text")
    excerpt = text[:1200] if isinstance(text, str) and text else None
    return {
        "platform": "web_search",
        "source_type": "article",
        "platform_item_id": str(item.get("id") or permalink),
        "permalink": permalink,
        "title": item.get("title") if isinstance(item.get("title"), str) else None,
        "content_excerpt": excerpt,
        "author_name": item.get("author") if isinstance(item.get("author"), str) else None,
        "source_keyword": query,
        "fetch_method": "search_index",
        "published_at": _iso(item.get("publishedDate")),
        "fetched_at": fetched_at,
        "raw_metrics": {},
        "extra": {"provider": "exa"},
    }


def _tavily_evidence(item: Any, query: str, fetched_at: str) -> Evidence:
    if not isinstance(item, Mapping):
        raise RuntimeError("Tavily 命中项不是 JSON 对象")
    permalink = _url(item.get("url"))
    body = item.get("raw_content") or item.get("content")
    excerpt = body[:1200] if isinstance(body, str) and body else None
    return {
        "platform": "web_search",
        "source_type": "article",
        "platform_item_id": permalink,
        "permalink": permalink,
        "title": item.get("title") if isinstance(item.get("title"), str) else None,
        "content_excerpt": excerpt,
        "author_name": None,
        "source_keyword": query,
        "fetch_method": "search_index",
        # Tavily 的 published_date 实测恒空：冻结列保持真实缺失，
        # 时效兜底仅由 freshness_degraded_source 驱动 M3-a 纯函数完成。
        "published_at": None,
        "fetched_at": fetched_at,
        "raw_metrics": {},
        "extra": {
            "provider": "tavily",
            "freshness_degraded_source": "fetched_at",
        },
    }


def _rate(items: list[Evidence]) -> list[Evidence]:
    """让工具层返回值也满足 HN 同构的五维证据契约。"""

    for item in items:
        item.update(score_evidence(item))
        item["rated_by"] = "rule:reliability@v1"
    return items


def _emit_empty(on_event: EventSink | None, provider: str) -> None:
    if on_event is None:
        return
    raw = {
        "source_id": "web_search",
        "provider": provider,
        "outcome": "empty",
        "count": 0,
    }
    on_event(NormalizedEvent(
        engine="Owli",
        thread_id=None,
        turn_id=None,
        item_kind=ItemKind.DONE,
        text=f"{provider} 查询正常但没有命中",
        is_error=False,
        raw=raw,
        outcome="empty",
    ))


def _failure_reason(error: BaseException | None) -> tuple[str, int | None]:
    if error is None:
        return "EXA_API_KEY 缺失", None
    if isinstance(error, ProviderRequestError):
        if error.status_code == 429:
            return "Exa 请求 HTTP 429，额度或限流触发降级", 429
        if error.status_code is not None:
            return f"Exa 请求 HTTP {error.status_code}，触发降级", error.status_code
    return f"Exa 请求发生 {type(error).__name__}，触发降级", None


def _emit_failover(
    on_event: EventSink | None,
    *,
    error: BaseException | None,
    log_root: Path,
) -> None:
    reason, status_code = _failure_reason(error)
    event = NormalizedEvent(
        engine="OwliSource",
        thread_id=None,
        turn_id=None,
        item_kind=ItemKind.ERROR,
        text=reason,
        is_error=True,
        raw={
            "source_id": "web_search",
            "provider": "exa",
            "fallback_provider": "tavily",
            "status_code": status_code,
            "reason": reason,
        },
        route_state="FAILOVER",
        failover_target="tavily",
        scope="source.web_search",
    )
    append_routing_event(event, log_root=log_root)
    if on_event is not None:
        on_event(event)


def _emit_answer_lead(on_event: EventSink | None, answer: Any) -> None:
    if on_event is None or not isinstance(answer, str) or not answer.strip():
        return
    on_event(NormalizedEvent(
        engine="OwliSource",
        thread_id=None,
        turn_id=None,
        item_kind=ItemKind.OUTPUT,
        text="Tavily answer 仅作线索，不进入证据库",
        is_error=False,
        raw={"source_id": "web_search", "provider": "tavily", "answer": answer},
        outcome="lead",
    ))


def search(
    query: str,
    window: str,
    *,
    max_results: int = 10,
    env_path: str | Path = DEFAULT_ENV_PATH,
    http_post: HttpPost = _http_post,
    on_event: EventSink | None = None,
    log_root: Path = DEFAULT_LOG_ROOT,
    clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
) -> list[Evidence]:
    """用 Exa 搜索网页；仅在主源报错或缺凭证时降级 Tavily。"""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results 必须是正整数")
    # §SRC-1 货 2（解禁：仅时间窗归一一处）：先折算人话时间窗再校验。
    matched = _WINDOW_PATTERN.fullmatch(_WINDOW_PARAM.normalize(window))
    if matched is None:  # normalize 的输出恒为 Nd，这里只是兜底
        raise ValueError(_WINDOW_PARAM.rejection_message(window))
    credentials = _load_credentials(env_path)
    fetched_at = _iso(clock())
    if fetched_at is None:
        raise ValueError("clock 必须返回 ISO 8601 时间")
    start = datetime.fromisoformat(fetched_at) - timedelta(days=int(matched.group(1)))
    exa_payload = {
        "query": query.strip(),
        "type": "neural",
        "numResults": max_results,
        "startPublishedDate": start.isoformat(),
        "contents": {"text": {"maxCharacters": 1200}},
    }
    primary_error: BaseException | None = None
    if credentials.exa_api_key is not None:
        try:
            response = http_post(
                _EXA_URL,
                {
                    "Content-Type": "application/json",
                    "x-api-key": credentials.exa_api_key,
                },
                exa_payload,
                _REQUEST_TIMEOUT_SECONDS,
            )
            results = response.get("results")
            if not isinstance(results, list):
                raise RuntimeError("Exa 响应缺少 results 数组")
            evidence = [
                _exa_evidence(item, query.strip(), fetched_at) for item in results
            ]
        # 注入 HTTP 层也可能抛出解码/协议异常；所有 Exa 请求与响应错误
        # 都在此边界分类降级，且后续事件只记录异常类型、不记录异常消息。
        except Exception as error:
            primary_error = error
        else:
            if not evidence:
                _emit_empty(on_event, "exa")
            return _rate(evidence)

    if credentials.tavily_api_key is None:
        reason, _ = _failure_reason(primary_error)
        raise CredentialError(f"{reason}；TAVILY_API_KEY 未配置，无法降级")
    _emit_failover(on_event, error=primary_error, log_root=log_root)
    tavily_payload = {
        "query": query.strip(),
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": "text",
        "max_results": max_results,
        "start_date": start.date().isoformat(),
    }
    response = http_post(
        _TAVILY_URL,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credentials.tavily_api_key}",
        },
        tavily_payload,
        _REQUEST_TIMEOUT_SECONDS,
    )
    results = response.get("results")
    if not isinstance(results, list):
        raise ProviderRequestError("tavily")
    _emit_answer_lead(on_event, response.get("answer"))
    evidence = [
        _tavily_evidence(item, query.strip(), fetched_at) for item in results
    ]
    if not evidence:
        _emit_empty(on_event, "tavily")
    return _rate(evidence)


def _evidence_id() -> str:
    return f"ev-{uuid.uuid4()}"


def collect_and_store(
    query: str,
    window: str,
    *,
    report_id: str,
    goal_id: str,
    store: Any,
    agent_name: str | None = None,
    env_path: str | Path = DEFAULT_ENV_PATH,
    http_post: HttpPost = _http_post,
    on_event: EventSink | None = None,
    log_root: Path = DEFAULT_LOG_ROOT,
    clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
    id_factory: Callable[[], str] = _evidence_id,
) -> list[dict[str, Any]]:
    """采集、经 M3-a 纯函数计算后，通过具名 Store 接口整批入库。"""

    raw_items = search(
        query,
        window,
        env_path=env_path,
        http_post=http_post,
        on_event=on_event,
        log_root=log_root,
        clock=clock,
    )
    if not raw_items:
        return []
    items = [
        {
            **item,
            "id": id_factory(),
            "report_id": report_id,
            "goal_id": goal_id,
            "agent_name": agent_name,
        }
        for item in raw_items
    ]
    computed_at = items[0]["fetched_at"]
    normalized = normalize_evidence_metrics(
        items,
        computed_at=computed_at,
        report_id=report_id,
        goal_id=goal_id,
        queries=[query],
        filters="Exa neural；Tavily 仅错误降级",
    )
    for item in normalized:
        if item.get("extra", {}).get("provider") == "tavily":
            item["norm_context"]["degraded"] = {
                "provider": "tavily",
                "field": "published_at",
                "source": "fetched_at",
            }
        item.update(score_evidence(item))
        item["rated_by"] = "rule:reliability@v1"
    store.upsert_evidence_batch(normalized)
    return normalized


SOURCE_SPEC = SourceSpec(
    source_id="web_search",
    tool_name="source.web_search",
    entrypoint=search,
    display_name="网页搜索",
    collector_name="网页搜索数据抓取",
    capability_description="跨站官方文档、评测与报道原文，Exa 主、Tavily 错误降级",
    prompt_hint="按时间窗检索并保留落地页 permalink，不把搜索摘要当原文",
    limit_parameter="max_results",
)
