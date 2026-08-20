"""Exa 主、Tavily 备的通用网页搜索信息源。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.adapters.events import ItemKind, NormalizedEvent
from app.sources.spec import SourceSpec


__all__ = ["search"]

DEFAULT_ENV_PATH = Path("~/.owli/.env").expanduser()
_EXA_URL = "https://api.exa.ai/search"
_TAVILY_URL = "https://api.tavily.com/search"
_EXA_KEY_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_WINDOW_PATTERN = re.compile(r"^([1-9]\d*)d$")
_REQUEST_TIMEOUT_SECONDS = 20.0

HttpPost = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]
EventSink = Callable[[NormalizedEvent], Any]


class CredentialError(RuntimeError):
    """网页搜索凭证缺失或归属格式错误。"""


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
        raise RuntimeError(f"HTTP {error.code}") from error
    except (URLError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("HTTP 请求或 JSON 解析失败") from error
    if not isinstance(decoded, Mapping):
        raise RuntimeError("供应商响应不是 JSON 对象")
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
        "fetch_method": "exa_api",
        "published_at": _iso(item.get("publishedDate")),
        "fetched_at": fetched_at,
        "raw_metrics": {},
        "extra": {"provider": "exa"},
    }


def _emit_empty(on_event: EventSink | None) -> None:
    if on_event is None:
        return
    raw = {
        "source_id": "web_search",
        "provider": "exa",
        "outcome": "empty",
        "count": 0,
    }
    on_event(NormalizedEvent(
        engine="Owli",
        thread_id=None,
        turn_id=None,
        item_kind=ItemKind.DONE,
        text="Exa 查询正常但没有命中",
        is_error=False,
        raw=raw,
        outcome="empty",
    ))


def search(
    query: str,
    window: str,
    *,
    env_path: str | Path = DEFAULT_ENV_PATH,
    http_post: HttpPost = _http_post,
    on_event: EventSink | None = None,
    clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
) -> list[Evidence]:
    """用 Exa 搜索网页；正常空命中不触发备源。"""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    matched = _WINDOW_PATTERN.fullmatch(window)
    if matched is None:
        raise ValueError('window 必须形如 "90d" 或 "30d"')
    credentials = _load_credentials(env_path)
    if credentials.exa_api_key is None:
        raise CredentialError("EXA_API_KEY 缺失，主源无法启动")
    fetched_at = _iso(clock())
    if fetched_at is None:
        raise ValueError("clock 必须返回 ISO 8601 时间")
    start = datetime.fromisoformat(fetched_at) - timedelta(days=int(matched.group(1)))
    payload = {
        "query": query.strip(),
        "type": "neural",
        "numResults": 10,
        "startPublishedDate": start.isoformat(),
        "contents": {"text": {"maxCharacters": 1200}},
    }
    response = http_post(
        _EXA_URL,
        {"Content-Type": "application/json", "x-api-key": credentials.exa_api_key},
        payload,
        _REQUEST_TIMEOUT_SECONDS,
    )
    results = response.get("results")
    if not isinstance(results, list):
        raise RuntimeError("Exa 响应缺少 results 数组")
    if not results:
        _emit_empty(on_event)
        return []
    return [_exa_evidence(item, query.strip(), fetched_at) for item in results]


SOURCE_SPEC = SourceSpec(
    source_id="web_search",
    tool_name="source.web_search",
    entrypoint=search,
)
