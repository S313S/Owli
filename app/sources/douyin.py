"""抖音 TikHub 搜索与评论信息源适配器。"""

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
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.reliability.scoring import normalize_evidence_metrics
from app.sources import comments as comment_shape
from app.sources.spec import SourceSpec


__all__ = [
    "FORBIDDEN_TIKHUB_PATHS", "SOURCE_SPEC", "fetch_comments", "search",
]

_API_BASE = "https://api.tikhub.io"
# §D-038：v5 为主、v4 为备。2026-09-03 傍晚 v5 连官方演示参数都恒 400（FIX-2 货 3 据此
# 判「端点报废」），五小时后同一把 key 三次全 200——是**上游临时故障**不是端点死亡。
# 单版本依赖在那几小时里让抖音 0 产出，所以保留 v5 主路，另接 v4 兜底：v5 请求失败时
# 自动改打 v4，并把 v4 的 PascalCase 返回归一成 v5 形态，下游映射一行不用改。
_SEARCH_PATH_V5 = "/api/v1/douyin/search/fetch_video_search_v5"
_SEARCH_PATH_V4 = "/api/v1/douyin/search/fetch_video_search_v4"
_SEARCH_PATH = _SEARCH_PATH_V5
_COMMENTS_PATH = "/api/v1/douyin/app/v3/fetch_video_comments"
_ENV_PATH = Path.home() / ".owli" / ".env"

FORBIDDEN_TIKHUB_PATHS = frozenset({
    "/api/v1/douyin/app/v3/fetch_multi_video_high_quality_play_url",
    "/api/v1/douyin/app/v3/fetch_multi_video_v2",
    "/api/v1/douyin/app/v3/fetch_multi_video_statistics",
})


_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{24,}")
_BODY_SUMMARY_LIMIT = 200


class TikHubError(RuntimeError):
    """带上「谁拒绝了我们」的 TikHub 失败。

    §SRC-1 诊断：此前这里只抛裸 `RuntimeError`，`_unavailable` 又把它抹平成一个
    笼统的 `tikhub_request_failed`，于是整轮跑完，「抖音为什么失败」在事件、
    日志里都查不到状态码——B 类原因（供应商/网络瞬时失败）永远无法定性。
    这里把端点、HTTP 状态、上游 code 与响应体摘要一路带到事件里。
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
            f"TikHub 抖音{kind}失败：endpoint={self.endpoint} "
            f"http={http_status} code={upstream_code} {detail or ''}".strip()
        )

    @property
    def closed_reason(self) -> str:
        """事件里的失败分类；细到能直接分诊，不再是一个笼统的字符串。"""

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
        # HTTP 200 但上游 code 非 200：TikHub 自己在响应体里说不行。
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


HttpRequest = Callable[
    [str, str, Mapping[str, str], bytes | None, float], HttpResponse
]
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


def _assert_allowed_path(path: str) -> None:
    if path in FORBIDDEN_TIKHUB_PATHS:
        raise ValueError(f"禁止调用高价 TikHub 接口：{path}")


def _default_http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    request = Request(url, data=body, method=method, headers=dict(headers))
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
        on_event({"type": event_type, "data": {"source": "douyin", **data}})


def _request_json(
    path: str,
    *,
    method: str,
    token: str,
    http_request: HttpRequest,
    timeout_seconds: float,
    rate_gate: RateGate,
    query: Mapping[str, Any] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    _assert_allowed_path(path)
    suffix = f"?{urlencode(query)}" if query else ""
    encoded = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if body is not None else None
    )
    rate_gate.wait()
    response = http_request(
        method,
        f"{_API_BASE}{path}{suffix}",
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Owli/0.1 Douyin-source",
        },
        encoded,
        timeout_seconds,
    )
    upstream_code = response.payload.get("code")
    if response.status != 200 or upstream_code != 200:
        raise TikHubError(
            "http", endpoint=path, http_status=response.status,
            upstream_code=upstream_code,
            detail=_body_summary(response.payload),
        )
    data = response.payload.get("data")
    if not isinstance(data, Mapping):
        raise TikHubError(
            "bad_response", endpoint=path, http_status=response.status,
            detail="响应缺少 data",
        )
    return data


def _integer(item: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _video_items_v4(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """v4 的列表在 `data.data.itemList[].AwemeInfo`，比 v5 多一层 `data`。"""

    inner = data.get("data")
    values = inner.get("itemList") if isinstance(inner, Mapping) else None
    if not isinstance(values, list):
        raise TikHubError(
            "bad_response", endpoint=_SEARCH_PATH_V4, detail="搜索响应缺少 itemList",
        )
    result: list[Mapping[str, Any]] = []
    for wrapper in values:
        if not isinstance(wrapper, Mapping):
            continue
        aweme = wrapper.get("AwemeInfo")
        video = _v4_video(aweme) if isinstance(aweme, Mapping) else None
        if video is not None:
            result.append(video)
    return result


def _v4_next_cursor(data: Mapping[str, Any], current: int) -> int | None:
    """v4 翻页用 `hasMore` + `cursor`，没有 v5 的 search_id/backtrace。"""

    inner = data.get("data")
    if not isinstance(inner, Mapping) or not inner.get("hasMore"):
        return None
    cursor = inner.get("cursor")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= current:
        return None
    return cursor


def _video_items(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = data.get("items")
    if not isinstance(values, list):
        raise TikHubError(
            "bad_response", endpoint=_SEARCH_PATH_V5, detail="搜索响应缺少 items",
        )
    result = []
    for wrapper in values:
        if not isinstance(wrapper, Mapping):
            continue
        video = wrapper.get("aweme_info") or wrapper.get("aweme") or wrapper
        if isinstance(video, Mapping) and video.get("aweme_id"):
            result.append(video)
    return result


def _published_at(value: Any) -> str | None:
    """把 create_time 转成 ISO 时间。

    §D-038 两处真机读数，都会静默出错：
    ① v5 给的是**秒**（`1775107082`），v4 给的是**毫秒**（`1782789498000`）——直接
    `fromtimestamp` 会把 v4 的时间算到公元 58 万年；按量级判断，2001-09-09 之后的
    秒级时间戳都小于 1e11、毫秒级都大于它。
    ② v4 的 `CreateTime` 是**字符串** `'1782789498000'` 不是数字——只认 int/float
    的话真机跑出来整列 `published_at` 全是 None（本包第一版就是这么绿着错的）。
    """

    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000 if value >= 1e11 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _v4_video(aweme: Mapping[str, Any]) -> dict[str, Any] | None:
    """把 v4 的 PascalCase 视频体归一成 v5 形态，供 `_to_evidence` 原样消费。

    v4 比 v5 少两样，都按「取不到就是 0/False」处理，不编造：
    `Statistics` 没有 `CollectCount`（收藏数落 0）、`Author` 没有 `Uid` 与认证位
    （`uid` 退用 `ShortId`，`is_verified` 恒 False）。
    """

    aweme_id = str(aweme.get("AwemeId") or "")
    if not aweme_id:
        return None
    author = aweme.get("Author") if isinstance(aweme.get("Author"), Mapping) else {}
    statistics = (
        aweme.get("Statistics")
        if isinstance(aweme.get("Statistics"), Mapping) else {}
    )
    return {
        "aweme_id": aweme_id,
        "desc": aweme.get("Desc") or "",
        "create_time": aweme.get("CreateTime"),
        "author": {
            "nickname": author.get("Nickname") or "",
            "uid": str(author.get("ShortId") or ""),
            "sec_uid": str(author.get("SecUid") or ""),
            "is_verified": False,
        },
        "statistics": {
            "digg_count": _integer(statistics, "DiggCount"),
            "comment_count": _integer(statistics, "CommentCount"),
            "share_count": _integer(statistics, "ShareCount"),
        },
    }


def _fetch_comments(
    aweme_id: str,
    *,
    declared_total: int,
    max_pages: int,
    token: str,
    http_request: HttpRequest,
    timeout_seconds: float,
    rate_gate: RateGate,
) -> tuple[list[Mapping[str, Any]], bool, int]:
    comments: list[Mapping[str, Any]] = []
    cursor = 0
    calls = 0
    complete = False
    for _ in range(max_pages):
        data = _request_json(
            _COMMENTS_PATH,
            method="GET",
            token=token,
            http_request=http_request,
            timeout_seconds=timeout_seconds,
            rate_gate=rate_gate,
            query={"aweme_id": aweme_id, "cursor": cursor, "count": 20},
        )
        calls += 1
        values = data.get("comments")
        if not isinstance(values, list):
            raise TikHubError(
                "bad_response", endpoint=_COMMENTS_PATH,
                detail="评论响应缺少 comments",
            )
        comments.extend(item for item in values if isinstance(item, Mapping))
        has_more = bool(data.get("has_more"))
        response_total = _integer(data, "total")
        expected = response_total or declared_total
        if not has_more and len(comments) >= expected:
            complete = True
            break
        if not has_more:
            break
        next_cursor = data.get("cursor")
        if not isinstance(next_cursor, int) or next_cursor == cursor:
            break
        cursor = next_cursor
    return comments, complete, calls


def _to_comment(
    item: Mapping[str, Any], *, parent_permalink: str
) -> comment_shape.Comment:
    user = item.get("user") if isinstance(item.get("user"), Mapping) else {}
    return comment_shape.Comment(
        parent_permalink=parent_permalink,
        # 抖音同样没有公开的单条评论链接，入库时由调用方合成锚点。
        permalink="",
        author=str(user.get("nickname") or item.get("nickname") or "").strip(),
        text=str(item.get("text") or item.get("content") or "").strip(),
        likes=comment_shape.integer(item, "digg_count", "like_count"),
        published_at=comment_shape.published_at(item.get("create_time")),
        platform="douyin",
        comment_id=str(item.get("cid") or item.get("comment_id") or "").strip(),
    )


def fetch_comments(
    aweme_id: str,
    *,
    parent_permalink: str,
    limit: int = 20,
    token: str | None = None,
    http_request: HttpRequest = _default_http_request,
    timeout_seconds: float = 45.0,
    rate_gate: RateGate = _RATE_GATE,
    max_pages: int = 4,
) -> comment_shape.CommentBatch:
    """把既有 `_fetch_comments` 的分页拉取包成三源统一形状对外暴露。"""

    identifier = str(aweme_id).strip()
    if not identifier:
        raise ValueError("aweme_id 必须是非空字符串")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1-100 整数")
    api_token = token or _load_token()
    raw, _complete, calls = _fetch_comments(
        identifier,
        declared_total=limit,
        max_pages=max_pages,
        token=api_token,
        http_request=http_request,
        timeout_seconds=timeout_seconds,
        rate_gate=rate_gate,
    )
    kept, dropped = comment_shape.clean(
        [_to_comment(item, parent_permalink=parent_permalink) for item in raw],
        limit=limit,
    )
    return comment_shape.CommentBatch(comments=kept, dropped_short=dropped, calls=calls)


def _comment_texts(comments: list[Mapping[str, Any]]) -> list[str]:
    return [
        str(item.get("text") or "").strip()
        for item in comments
        if str(item.get("text") or "").strip()
    ]


def _to_evidence(
    video: Mapping[str, Any],
    *,
    query: str,
    fetched_at: str,
    comments: list[Mapping[str, Any]],
    comments_complete: bool,
) -> dict[str, Any]:
    aweme_id = str(video.get("aweme_id") or "")
    if not aweme_id:
        raise RuntimeError("抖音视频缺少 aweme_id")
    author = video.get("author") if isinstance(video.get("author"), Mapping) else {}
    statistics = (
        video.get("statistics")
        if isinstance(video.get("statistics"), Mapping) else {}
    )
    description = str(video.get("desc") or "").strip()
    texts = _comment_texts(comments)
    content = description
    if texts:
        content += "\n\n评论：" + " | ".join(texts)
    complete_score = 2 if comments_complete and bool(description) else 1
    complete_reason = (
        f"评论区全取{len(comments)}条" if complete_score == 2 else "文案及部分评论"
    )
    return {
        "platform": "douyin",
        "source_type": "video",
        "platform_item_id": aweme_id,
        "permalink": f"https://www.douyin.com/video/{aweme_id}",
        "title": description[:80] or f"抖音视频 {aweme_id}",
        "content_excerpt": content[:8000] or None,
        "author_name": str(author.get("nickname") or "") or None,
        "author_meta": {
            "uid": str(author.get("uid") or ""),
            "sec_uid": str(author.get("sec_uid") or ""),
            "verified": bool(author.get("is_verified")),
        },
        "source_keyword": query,
        "fetch_method": "third_party_api",
        "published_at": _published_at(video.get("create_time")),
        "fetched_at": fetched_at,
        "raw_metrics": {
            "digg_count": _integer(statistics, "digg_count"),
            "comments_count": _integer(statistics, "comment_count"),
            "share_count": _integer(statistics, "share_count"),
            "collect_count": _integer(statistics, "collect_count"),
            "comments_fetched": len(comments),
        },
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": None,
        "score_completeness": complete_score,
        "score_independence": 1,
        "rating_notes": (
            "权威1:平台社区基线 · 时效2:搜索近期视频 · "
            f"交叉?:缺断言血缘簇 · 完整{complete_score}:{complete_reason} · "
            "无关1:商单密度较高"
        ),
        "rated_by": "baseline:douyin@v1",
        "extra": {
            "content_kind": "user_opinion",
            "provider": "tikhub",
            "comments_complete": comments_complete,
            "comment_texts": texts,
            "declared_comment_count": _integer(statistics, "comment_count"),
        },
    }


def _collect_v5(
    query: str, *, limit: int, request: Callable[..., Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    videos: list[Mapping[str, Any]] = []
    pagination: Mapping[str, Any] = {
        "offset": 0, "search_id": "", "backtrace": "", "has_more": 1,
    }
    page = 1
    while len(videos) < limit and pagination.get("has_more"):
        data = request(_SEARCH_PATH_V5, body={
            "keyword": query,
            "offset": int(pagination.get("offset") or 0),
            "page": page,
            "search_id": str(pagination.get("search_id") or ""),
            "backtrace": str(pagination.get("backtrace") or ""),
        })
        page_items = _video_items(data)
        videos.extend(page_items)
        next_pagination = data.get("pagination")
        if not isinstance(next_pagination, Mapping) or not page_items:
            break
        pagination = next_pagination
        page += 1
    return videos


def _collect_v4(
    query: str, *, limit: int, request: Callable[..., Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    videos: list[Mapping[str, Any]] = []
    cursor = 0
    while len(videos) < limit:
        data = request(_SEARCH_PATH_V4, body={
            "keyword": query, "cursor": cursor, "sort_type": "0",
            "publish_time": "0", "filter_duration": "0", "content_type": "0",
        })
        page_items = _video_items_v4(data)
        videos.extend(page_items)
        next_cursor = _v4_next_cursor(data, cursor)
        if next_cursor is None or not page_items:
            break
        cursor = next_cursor
    return videos


def _unavailable(
    on_event: EventCallback | None,
    *,
    reason: str,
    forced: bool,
    error: TikHubError | None = None,
    extra: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    _emit(
        on_event,
        "source_unavailable",
        reason="tool_unavailable",
        closed_reason=reason,
        provider="tikhub",
        forced=forced,
        task_continues=True,
        **(error.event_fields() if error is not None else {}),
        **(dict(extra) if extra else {}),
    )
    return []


def search(
    query: str,
    window: str = "",
    *,
    limit: int = 10,
    comment_video_limit: int = 3,
    max_comment_pages: int = 5,
    store: Any | None = None,
    report_id: str | None = None,
    goal_id: str | None = None,
    agent_name: str | None = None,
    on_event: EventCallback | None = None,
    force_unavailable: bool = False,
    token: str | None = None,
    http_request: HttpRequest = _default_http_request,
    timeout_seconds: float = 45.0,
    rate_gate: RateGate = _RATE_GATE,
    now: Callable[[], datetime] = _utc_now,
) -> list[dict[str, Any]]:
    """搜索抖音视频，并为优先候选拉取评论正文。

    §SRC-1 货 3：`window` **不参与检索**——TikHub 的抖音视频搜索没有时间窗
    参数，此前它只被一条正则校验然后丢掉，却打回了 25% 的调用。现在
    `SOURCE_SPEC.window = None`，工具 schema 里不再向模型索取这个参数；
    形参保留且默认空串，只是为了与 `SourceToolAdapter.call` 的位置参数对齐，
    传什么都不影响结果。
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100 整数")
    if not 1 <= comment_video_limit <= limit:
        raise ValueError("comment_video_limit 必须为 1–limit 整数")
    if not 1 <= max_comment_pages <= 10:
        raise ValueError("max_comment_pages 必须为 1–10 整数")
    if store is not None and (not report_id or not goal_id):
        raise ValueError("入库时 report_id 与 goal_id 必填")
    if force_unavailable:
        return _unavailable(on_event, reason="tikhub_forced_unavailable", forced=True)
    try:
        api_token = token or _load_token()
    except RuntimeError:
        return _unavailable(on_event, reason="tikhub_credential_missing", forced=False)

    request_counts = {"video_search_v5": 0, "video_search_v4": 0, "video_comments": 0}

    def counted_http_request(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        if _COMMENTS_PATH in url:
            key = "video_comments"
        elif _SEARCH_PATH_V4 in url:
            key = "video_search_v4"
        else:
            key = "video_search_v5"
        request_counts[key] += 1
        return http_request(method, url, headers, body, timeout)

    def request(path: str, *, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return _request_json(
            path,
            method="POST",
            token=api_token,
            http_request=counted_http_request,
            timeout_seconds=timeout_seconds,
            rate_gate=rate_gate,
            body=body,
        )

    keyword = query.strip()
    search_version = "v5"
    try:
        videos = _collect_v5(keyword, limit=limit, request=request)
    except TikHubError as primary_error:
        # §D-038：v5 抽风过整整几小时，那期间抖音 0 产出。这里不再直接判源不可用，
        # 先改打 v4；兜底路平时不走，所以**每次触发都留痕**，别让它静默成功也静默失败。
        _emit(
            on_event,
            "source_search_fallback",
            provider="tikhub",
            from_version="v5",
            to_version="v4",
            closed_reason=primary_error.closed_reason,
            task_continues=True,
            **primary_error.event_fields(),
        )
        search_version = "v4"
        try:
            videos = _collect_v4(keyword, limit=limit, request=request)
        except TikHubError as fallback_error:
            # 两条路都断了才算源不可用。头条报**主路**的分诊（SRC-1 的契约：
            # `source_unavailable` 的 endpoint/status 指向主要死因），兜底那条的
            # 分诊挂在 `fallback_*` 字段上，两个状态码都查得到。
            return _unavailable(
                on_event,
                reason=primary_error.closed_reason,
                forced=False,
                error=primary_error,
                extra={
                    "fallback_version": "v4",
                    "fallback_closed_reason": fallback_error.closed_reason,
                    "fallback_endpoint": fallback_error.endpoint,
                    "fallback_http_status": fallback_error.http_status,
                },
            )

    selected = videos[:limit]
    statistics = [
        item.get("statistics") if isinstance(item.get("statistics"), Mapping) else {}
        for item in selected
    ]
    candidate_indices = sorted(
        range(len(selected)),
        key=lambda index: (
            _integer(statistics[index], "comment_count") == 0,
            _integer(statistics[index], "comment_count"),
        ),
    )[:comment_video_limit]
    comments_by_index: dict[int, list[Mapping[str, Any]]] = {}
    completeness_by_index: dict[int, bool] = {}
    for index in candidate_indices:
        declared = _integer(statistics[index], "comment_count")
        try:
            comments, complete, _calls = _fetch_comments(
                str(selected[index].get("aweme_id")),
                declared_total=declared,
                max_pages=max_comment_pages,
                token=api_token,
                http_request=counted_http_request,
                timeout_seconds=timeout_seconds,
                rate_gate=rate_gate,
            )
            comments_by_index[index] = comments
            completeness_by_index[index] = complete
        except TikHubError as error:
            _emit(
                on_event,
                "source_comment_partial",
                aweme_id=str(selected[index].get("aweme_id") or ""),
                reason=error.closed_reason,
                task_continues=True,
                **error.event_fields(),
            )

    fetched_at = now().astimezone(timezone.utc).isoformat()
    evidence = [
        _to_evidence(
            item,
            query=query.strip(),
            fetched_at=fetched_at,
            comments=comments_by_index.get(index, []),
            comments_complete=completeness_by_index.get(index, False),
        )
        for index, item in enumerate(selected)
    ]
    normalized = normalize_evidence_metrics(
        evidence,
        computed_at=fetched_at,
        report_id=report_id or "unpersisted",
        goal_id=goal_id or "unpersisted",
        queries=[query.strip()],
        # 不写 window：抖音视频搜索没有时间窗过滤，写进参照集描述是假的。
        # §D-038：参照集要记**实际走的那个版本**，否则兜底跑了一轮，描述还印着 v5。
        filters=f"video_search_{search_version};comments_app_v3",
    )
    if store is not None:
        assert report_id is not None and goal_id is not None
        store.upsert_evidence_batch([
            {
                **item,
                "id": f"ev-{report_id}-douyin-{item['platform_item_id']}",
                "report_id": report_id,
                "goal_id": goal_id,
                # §M6-a 货 2（照 RATE-2 5d012a2 xhs 口径）：直落库也要带章归属，
                # 否则这一章采到的行没有任何章认领，评级章物化时看不见。
                "agent_name": agent_name,
            }
            for item in normalized
        ])
    _emit(
        on_event,
        "source_usage_reconciled",
        provider="tikhub",
        calls={
            "video_search_v5": request_counts["video_search_v5"],
            "video_search_v4": request_counts["video_search_v4"],
            "video_comments": request_counts["video_comments"],
        },
        search_version=search_version,
        returned=len(normalized),
        completeness_2=sum(
            item.get("score_completeness") == 2 for item in normalized
        ),
        task_continues=True,
    )
    return normalized


SOURCE_SPEC = SourceSpec(
    source_id="douyin",
    tool_name="source.douyin",
    entrypoint=search,
    display_name="抖音",
    collector_name="抖音数据抓取",
    capability_description="TikHub 视频搜索（V5 主 / V4 兜底）、视频文案、互动指标与评论正文",
    prompt_hint="优先选择评论量可全取的视频，完整评论区可把完整度上探到 2",
    # 抖音搜索没有时间窗，别向模型要一个用不上的参数（§SRC-1 货 3）。
    window=None,
    comment_fetcher=fetch_comments,
)
