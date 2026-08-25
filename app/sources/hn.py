"""Hacker News 信息源适配器，仅采集并返回平台基线证据。"""

from __future__ import annotations

import argparse
import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, TypedDict
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.sources.spec import SourceSpec


__all__ = ["search"]

_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
_POINTS_THRESHOLD = 50
_HITS_PER_PAGE = 1000
_WINDOW_PATTERN = re.compile(r"^([1-9]\d*)d$")
_MIN_INTERVAL_SECONDS = 0.25
_MAX_ATTEMPTS = 3
_REQUEST_TIMEOUT_SECONDS = 15
_CONTENT_EXCERPT_CHARACTER_LIMIT = 1200
_TRUNCATION_MARKER = "…[已截断]"
_throttle_lock = threading.Lock()
_last_request_at = 0.0


class Evidence(TypedDict):
    """工具层返回的证据字段；不承担入库职责。"""

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
    score_authority: int
    score_freshness: int
    score_crossref: int
    score_completeness: int
    score_independence: int
    rated_by: str


def search(query: str, window: str, *, limit: int = _HITS_PER_PAGE) -> list[Evidence]:
    """按关键词与时间窗搜索 Hacker News。"""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 必须是正整数")
    match = _WINDOW_PATTERN.fullmatch(window)
    if match is None:
        raise ValueError('window 必须形如 "90d" 或 "30d"')

    days = int(match.group(1))
    window_start = _now_epoch() - days * 86_400
    params = {
        "query": query,
        "tags": "story",
        "numericFilters": (
            f"created_at_i>{window_start},points>{_POINTS_THRESHOLD}"
        ),
        "hitsPerPage": str(limit),
    }
    payload = _fetch_json(f"{_SEARCH_URL}?{urlencode(params)}")
    hits = payload.get("hits")
    if not isinstance(hits, list):
        raise RuntimeError("HN Algolia 响应缺少 hits 数组")

    fetched_at = _utc_now_iso()
    return [_to_evidence(hit, query, fetched_at) for hit in hits]


def _now_epoch() -> int:
    return int(time.time())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_json(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Owli/0.1 Hacker-News-source",
        },
    )
    for attempt in range(_MAX_ATTEMPTS):
        _throttle()
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("HN Algolia 响应不是 JSON 对象")
            return payload
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == _MAX_ATTEMPTS - 1:
                raise RuntimeError(f"HN Algolia 请求失败：HTTP {error.code}") from error
        except (URLError, OSError, json.JSONDecodeError) as error:
            if attempt == _MAX_ATTEMPTS - 1:
                raise RuntimeError("HN Algolia 请求连续失败") from error
        time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError("HN Algolia 请求未获得可解析结果")


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait_seconds = max(
            0.0, _last_request_at + _MIN_INTERVAL_SECONDS - now
        )
        if wait_seconds:
            time.sleep(wait_seconds)
            now = time.monotonic()
        _last_request_at = now


def _to_evidence(hit: Any, query: str, fetched_at: str) -> Evidence:
    if not isinstance(hit, dict) or not hit.get("objectID"):
        raise RuntimeError("HN Algolia 命中项缺少 objectID")
    item_id = str(hit["objectID"])
    excerpt = html.unescape(hit.get("story_text") or "")
    if len(excerpt) > _CONTENT_EXCERPT_CHARACTER_LIMIT:
        excerpt = (
            excerpt[
                : _CONTENT_EXCERPT_CHARACTER_LIMIT - len(_TRUNCATION_MARKER)
            ]
            + _TRUNCATION_MARKER
        )
    return {
        "platform": "hacker_news",
        "source_type": "post",
        "platform_item_id": item_id,
        "permalink": f"https://news.ycombinator.com/item?id={item_id}",
        "title": hit.get("title"),
        "content_excerpt": excerpt or None,
        "author_name": hit.get("author"),
        "source_keyword": query,
        "fetch_method": "official_api",
        "published_at": hit.get("created_at"),
        "fetched_at": fetched_at,
        "raw_metrics": {
            "points": hit.get("points"),
            "num_comments": hit.get("num_comments"),
        },
        "score_authority": 1,
        "score_freshness": 1,
        "score_crossref": 0,
        "score_completeness": 2,
        "score_independence": 2,
        "rated_by": "baseline",
    }


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="搜索 Hacker News 证据")
    parser.add_argument("--query", required=True, help="搜索关键词")
    parser.add_argument("--window", required=True, help='时间窗，如 "90d"')
    args = parser.parse_args(argv)
    print(
        json.dumps(
            search(args.query, args.window),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


SOURCE_SPEC = SourceSpec(
    source_id="hacker_news",
    tool_name="source.hacker_news",
    entrypoint=search,
    display_name="Hacker News",
    collector_name="HN 数据抓取",
    capability_description="社区长讨论与完整评论树，适合采集真实使用反馈和技术抱怨",
    prompt_hint=(
        "Algolia 近90天，created_at_i>执行时点UTC epoch-7776000，"
        "points>50，hitsPerPage=1000"
    ),
)


if __name__ == "__main__":
    _main()
