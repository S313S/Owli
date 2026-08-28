"""小红书 TikHub App V2 信息源适配器。"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.reliability.scoring import normalize_evidence_metrics
from app.sources.spec import SourceSpec, WindowParam


__all__ = ["SOURCE_SPEC", "search"]

_API_BASE = "https://api.tikhub.io"
_SEARCH_PATH = "/api/v1/xiaohongshu/app_v2/search_notes"
_ENV_PATH = Path.home() / ".owli" / ".env"
_WINDOW_PATTERN = re.compile(r"^([1-9]\d*)d$")
_WINDOW_PARAM = WindowParam()
_SORT_TYPES = {
    "general", "time_descending", "popularity_descending",
    "comment_descending", "collect_descending",
}
_NOTE_TYPES = {"不限", "视频笔记", "普通笔记", "直播笔记"}

# 第二供应商仅保留接缝形态；当前未实现，也不会读取其凭证。
_FALLBACK_PROVIDER: Callable[..., Any] | None = None


_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{24,}")
_BODY_SUMMARY_LIMIT = 200


class TikHubError(RuntimeError):
    """带上「谁拒绝了我们」的 TikHub 失败（与 `app.sources.douyin` 同构）。

    §SRC-1 诊断：此前只抛裸 `RuntimeError`，事件里被抹平成一个笼统的
    `tikhub_request_failed`，整轮跑完查不到状态码。这里把端点、HTTP 状态、
    上游 code 与响应体摘要一路带到事件里。
    """

    def __init__(
        self,
        kind: str,
        *,
        endpoint: str,
        http_status: int | None = None,
        upstream_code: Any = None,
        detail: str | None = None,
    ) -> None:
        self.kind = kind
        self.endpoint = _endpoint_path(endpoint)
        self.http_status = http_status
        self.upstream_code = upstream_code
        self.detail = detail
        super().__init__(
            f"TikHub 小红书{kind}失败：endpoint={self.endpoint} "
            f"http={http_status} code={upstream_code} {detail or ''}".strip()
        )

    @property
    def closed_reason(self) -> str:
        if self.kind == "transport":
            return "tikhub_transport"
        if self.kind == "bad_response":
            return "tikhub_bad_response"
        status = self.http_status
        if status in {401, 403}:
            return "tikhub_auth"
        if status == 429:
            return "tikhub_http_429"
        if isinstance(status, int) and 500 <= status < 600:
            return "tikhub_http_5xx"
        if isinstance(status, int) and status != 200:
            return f"tikhub_http_{status}"
        return "tikhub_upstream_code"

    def event_fields(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "upstream_code": self.upstream_code,
            "detail": self.detail,
        }


def _endpoint_path(url: str) -> str:
    """只留路径，别把带签名的完整 URL 写进事件。"""

    text = str(url or "")
    return text.split("?", 1)[0].replace(_API_BASE, "") or text[:80]


def _body_summary(payload: Mapping[str, Any]) -> str:
    """响应体摘要：截断并抹掉任何长串，避免把凭证写进事件与日志。"""

    for key in ("message", "detail", "msg", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _SECRETISH.sub("<REDACTED>", value.strip())[:_BODY_SUMMARY_LIMIT]
    return _SECRETISH.sub(
        "<REDACTED>", json.dumps(payload, ensure_ascii=False)
    )[:_BODY_SUMMARY_LIMIT]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: Mapping[str, Any]


HttpGet = Callable[[str, Mapping[str, str], float], HttpResponse]
EventCallback = Callable[[dict[str, Any]], None]


class RateGate:
    """TikHub 进程内 10 QPS 串行间隔闸门。"""

    def __init__(self, interval_seconds: float = 0.1) -> None:
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        with self._lock:
            delay = self._interval - (clock() - self._last_request_at)
            if delay > 0:
                sleeper(delay)
            self._last_request_at = clock()


_RATE_GATE = RateGate()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_token(env_path: Path = _ENV_PATH) -> str:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError("未找到 ~/.owli/.env 中的 TIKHUB_API_KEY") from error
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "TIKHUB_API_KEY" and value.strip().strip("\"'"):
            return value.strip().strip("\"'")
    raise RuntimeError("~/.owli/.env 缺少 TIKHUB_API_KEY")


def _default_http_get(
    url: str, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    request = Request(url, headers=dict(headers))
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(response.status)
    except HTTPError as error:
        raw = error.read()
        status = error.code
    except (URLError, OSError) as error:
        raise TikHubError(
            "transport", endpoint=url, detail=type(error).__name__,
        ) from error
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TikHubError(
            "bad_response", endpoint=url, http_status=status,
            detail="响应不是合法 JSON",
        ) from error
    if not isinstance(payload, Mapping):
        raise TikHubError(
            "bad_response", endpoint=url, http_status=status,
            detail="响应不是 JSON 对象",
        )
    return HttpResponse(status=status, payload=payload)


def _emit(on_event: EventCallback | None, event_type: str, **data: Any) -> None:
    if on_event is not None:
        on_event({"type": event_type, "data": {"source": "xhs", **data}})


def _time_filter(days: int) -> str:
    if days <= 1:
        return "一天内"
    if days <= 7:
        return "一周内"
    if days <= 183:
        return "半年内"
    return "不限"


def build_xhs_permalink(note_id: str, xsec_token: str = "") -> str:
    """保留签名参数；否则从搜索结果点回原文会 404。"""

    normalized_id = str(note_id).strip()
    if not normalized_id:
        raise RuntimeError("小红书笔记缺少 id")
    base = f"https://www.xiaohongshu.com/explore/{quote(normalized_id)}"
    token = str(xsec_token).strip()
    if not token:
        return base
    return f"{base}?{urlencode({'xsec_token': token, 'xsec_source': 'pc_feed'})}"


def _integer(item: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _response_data(response: HttpResponse) -> Mapping[str, Any]:
    payload = response.payload
    upstream_code = payload.get("code")
    if response.status != 200 or upstream_code != 200:
        raise TikHubError(
            "http", endpoint=_SEARCH_PATH, http_status=response.status,
            upstream_code=upstream_code, detail=_body_summary(payload),
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise TikHubError(
            "bad_response", endpoint=_SEARCH_PATH,
            http_status=response.status, detail="响应缺少 data",
        )
    if data.get("success") is False or data.get("code") not in {None, 0, 200}:
        raise TikHubError(
            "http", endpoint=_SEARCH_PATH, http_status=response.status,
            upstream_code=data.get("code"), detail="上游返回失败",
        )
    return data


def _notes(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nested = data.get("data")
    values = nested.get("items") if isinstance(nested, Mapping) else data.get("items")
    if not isinstance(values, list):
        raise TikHubError(
            "bad_response", endpoint=_SEARCH_PATH, detail="响应缺少 data.items",
        )
    notes: list[Mapping[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping) or item.get("model_type") != "note":
            continue
        note = item.get("note")
        if isinstance(note, Mapping) and note.get("id"):
            notes.append(note)
    return notes


def _to_evidence(
    note: Mapping[str, Any], *, query: str, fetched_at: str, time_filter: str
) -> dict[str, Any]:
    user = note.get("user") if isinstance(note.get("user"), Mapping) else {}
    note_id = str(note.get("id") or "")
    token = str(note.get("xsec_token") or "")
    note_kind = str(note.get("type") or "")
    title = str(note.get("title") or "").strip()
    description = str(note.get("desc") or "").strip()
    author = str(
        user.get("nickname") or user.get("red_id") or user.get("userid") or ""
    ).strip()
    return {
        "platform": "xhs",
        "source_type": "video" if note_kind == "video" else "post",
        "platform_item_id": note_id,
        "permalink": build_xhs_permalink(note_id, token),
        "title": title or description[:80] or f"小红书笔记 {note_id}",
        "content_excerpt": description or title or None,
        "author_name": author or None,
        "author_meta": {
            "user_id": str(user.get("userid") or ""),
            "red_id": str(user.get("red_id") or ""),
            "verified": bool(user.get("red_official_verified")),
        },
        "source_keyword": query,
        "fetch_method": "third_party_api",
        # 搜索只给“3天前”等相对文本；时间窗在请求端限定，不伪造绝对时间。
        "published_at": None,
        "fetched_at": fetched_at,
        "raw_metrics": {
            "liked_count": _integer(note, "liked_count", "likedCount"),
            "comments_count": _integer(note, "comments_count", "commentsCount"),
            "collected_count": _integer(note, "collected_count", "collectedCount"),
            "share_count": _integer(note, "share_count", "shareCount"),
        },
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": None,
        "score_completeness": 1,
        "score_independence": 1,
        "rating_notes": (
            "权威1:平台社区基线 · 时效2:请求时间窗限定 · "
            "交叉?:缺断言血缘簇 · 完整1:正文摘要可回溯 · 无关1:商单密度较高"
        ),
        "rated_by": "baseline:xhs@v1",
        "extra": {
            "authority_kind": "community_high_signal",
            "content_kind": "user_opinion",
            "interest_relation": "disclosed_interest",
            "provider": "tikhub",
            "native_time_filter": time_filter,
            "relative_publish_time_omitted": True,
        },
    }


def _unavailable(
    on_event: EventCallback | None,
    *,
    reason: str,
    forced: bool,
    error: TikHubError | None = None,
) -> list[dict[str, Any]]:
    _emit(
        on_event,
        "source_unavailable",
        reason="tool_unavailable",
        closed_reason=reason,
        provider="tikhub",
        fallback_available=False,
        forced=forced,
        task_continues=True,
        **(error.event_fields() if error is not None else {}),
    )
    return []


def search(
    query: str,
    window: str,
    *,
    limit: int = 20,
    sort_type: str = "general",
    note_type: str = "不限",
    store: Any | None = None,
    report_id: str | None = None,
    goal_id: str | None = None,
    on_event: EventCallback | None = None,
    force_unavailable: bool = False,
    token: str | None = None,
    http_get: HttpGet = _default_http_get,
    timeout_seconds: float = 45.0,
    rate_gate: RateGate = _RATE_GATE,
    now: Callable[[], datetime] = _utc_now,
) -> list[dict[str, Any]]:
    """按 TikHub 原生排序、类型和时间窗搜索小红书笔记。"""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    # §SRC-1 货 2：先按说明书折算人话时间窗，折不出来才报错，
    # 且报错要连「该怎么写」一起说（引擎此前传 all / 不限时间 / recent_1_year）。
    matched = _WINDOW_PATTERN.fullmatch(_WINDOW_PARAM.normalize(window))
    if matched is None:  # normalize 的输出恒为 Nd，这里只是兜底
        raise ValueError(_WINDOW_PARAM.rejection_message(window))
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100 整数")
    if sort_type not in _SORT_TYPES:
        raise ValueError(f"sort_type 不在闭集：{sort_type}")
    if note_type not in _NOTE_TYPES:
        raise ValueError(f"note_type 不在闭集：{note_type}")
    if store is not None and (not report_id or not goal_id):
        raise ValueError("入库时 report_id 与 goal_id 必填")
    if force_unavailable:
        return _unavailable(on_event, reason="tikhub_forced_unavailable", forced=True)

    try:
        api_token = token or _load_token()
    except RuntimeError:
        return _unavailable(on_event, reason="tikhub_credential_missing", forced=False)

    requested_filter = _time_filter(int(matched.group(1)))
    request_count = 0
    page = 1
    search_id = ""
    session_id = ""
    collected: list[Mapping[str, Any]] = []
    try:
        while len(collected) < limit:
            params = {
                "keyword": query.strip(),
                "page": page,
                "sort_type": sort_type,
                "note_type": note_type,
                "time_filter": requested_filter,
            }
            if page > 1:
                params.update({
                    "search_id": search_id,
                    "search_session_id": session_id,
                })
            rate_gate.wait()
            response = http_get(
                f"{_API_BASE}{_SEARCH_PATH}?{urlencode(params)}",
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_token}",
                    "User-Agent": "Owli/0.1 XHS-source",
                },
                timeout_seconds,
            )
            request_count += 1
            data = _response_data(response)
            page_notes = _notes(data)
            collected.extend(page_notes)
            search_id = str(data.get("search_id") or search_id)
            session_id = str(data.get("search_session_id") or session_id)
            next_page = data.get("next_page")
            if not page_notes or not next_page or len(collected) >= limit:
                break
            if not search_id or not session_id:
                raise TikHubError(
                    "bad_response", endpoint=_SEARCH_PATH,
                    detail="翻页缺少 search_id/session_id",
                )
            page = int(next_page) if isinstance(next_page, int) else page + 1
    except TikHubError as error:
        return _unavailable(
            on_event, reason=error.closed_reason, forced=False, error=error,
        )

    fetched_at = now().astimezone(timezone.utc).isoformat()
    evidence = [
        _to_evidence(
            note, query=query.strip(), fetched_at=fetched_at,
            time_filter=requested_filter,
        )
        for note in collected[:limit]
    ]
    normalized = normalize_evidence_metrics(
        evidence,
        computed_at=fetched_at,
        report_id=report_id or "unpersisted",
        goal_id=goal_id or "unpersisted",
        queries=[query.strip()],
        filters=(
            f"sort_type={sort_type};note_type={note_type};"
            f"time_filter={requested_filter}"
        ),
    )
    if store is not None:
        assert report_id is not None and goal_id is not None
        store.upsert_evidence_batch([
            {
                **item,
                "id": f"ev-{report_id}-xhs-{item['platform_item_id']}",
                "report_id": report_id,
                "goal_id": goal_id,
            }
            for item in normalized
        ])
    _emit(
        on_event,
        "source_usage_reconciled",
        provider="tikhub",
        calls={"search_notes": request_count},
        returned=len(normalized),
        task_continues=True,
    )
    return normalized


SOURCE_SPEC = SourceSpec(
    source_id="xhs",
    tool_name="source.xhs",
    entrypoint=search,
    display_name="小红书",
    collector_name="小红书数据抓取",
    capability_description="TikHub App V2 笔记搜索；原生排序、类型与时间窗过滤",
    prompt_hint="相对发布时间不落 published_at；翻页回传双搜索会话 ID",
)
