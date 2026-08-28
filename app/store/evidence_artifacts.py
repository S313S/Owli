"""把已通过 goal 闸门的 JSON 证据产物投影为 Store 冻结列。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from app.store.dao import normalize_permalink


_FROZEN_FIELDS = frozenset({
    "platform", "source_type", "platform_item_id", "permalink", "title",
    "content_excerpt", "author_name", "author_meta", "source_keyword",
    "fetch_method", "published_at", "fetched_at", "raw_metrics",
    "normalized_score", "norm_method", "norm_context", "score_authority",
    "score_freshness", "score_crossref", "score_completeness",
    "score_independence", "rating_notes", "rated_by", "citation_no", "extra",
})
_SYSTEM_FIELDS = frozenset({
    "id", "evidence_id", "report_id", "goal_id", "agent_name", "engine",
    "score_total", "grade",
})
_EXTRA_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_CONSUMED_FIELDS = frozenset({
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence", "rating_notes",
})
# 引擎会把平台名写成自由文本（第 5 轮真实产物里 16 条全是 "xiaohongshu"，
# 而采集期适配器写的是 "xhs"）。两套词让 dao._evidence_identity 的
# native-identity 查认不出同一行，落到 INSERT 上撞 UNIQUE(report_id, permalink)——
# 即 D-019。词表以 app/sources/*.py 里适配器写的值为准，别名一律归到那一侧。
_PLATFORM_CANON = ("xhs", "douyin", "web_search", "reddit", "product_hunt",
                   "hacker_news", "x")
_PLATFORM_ALIASES = {
    "xiaohongshu": "xhs", "xiao_hong_shu": "xhs", "xiaohongshu.com": "xhs",
    "redbook": "xhs", "red_book": "xhs", "rednote": "xhs",
    "littleredbook": "xhs", "little_red_book": "xhs", "小红书": "xhs",
    "douyin.com": "douyin", "dou_yin": "douyin", "抖音": "douyin",
    "websearch": "web_search", "web-search": "web_search",
    "producthunt": "product_hunt", "product-hunt": "product_hunt",
    "hackernews": "hacker_news", "hacker-news": "hacker_news",
    "hn": "hacker_news",
    "twitter": "x", "twitter.com": "x", "x.com": "x",
}


def normalize_platform(value: str) -> str:
    """把引擎写的平台自由文本归到适配器的词表；不认识的原样返回。

    ⚠️ 不认识的**不改写**——产物里还出现过 "36氪AI测评""搜狐号""人人都是产品经理"
    这类**发布方名**（web_search 条目），它们不是平台别名，硬映射会把来源信息抹掉。
    这类值单独登记，不在本函数里猜。
    """
    text = str(value or "").strip()
    if not text:
        return text
    lowered = text.casefold()
    if lowered in _PLATFORM_CANON:
        return lowered
    return _PLATFORM_ALIASES.get(lowered, text)


_SOURCE_TYPES = frozenset({
    "post", "comment", "video", "article", "search_snippet",
    "ranking_item", "profile", "other",
})
_TEXT_ALIASES = ("text", "body", "content", "description", "summary", "snippet")
_AUTHOR_ALIASES = ("author", "creator", "user_name", "username", "nickname")
_METRIC_ALIASES = frozenset({
    "like_count", "liked_count", "digg_count", "comment_count",
    "comments_count", "collect_count", "collected_count", "share_count",
    "view_count", "play_count", "points", "num_comments", "votes_count",
})


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("evidence"), list):
        return list(value["evidence"])
    return []


def _evidence_id(
    report_id: str,
    platform: str,
    platform_item_id: str | None,
    permalink: str,
) -> str:
    identity = platform_item_id or permalink
    digest = hashlib.sha256(
        f"{report_id}\0{platform}\0{identity}".encode("utf-8")
    ).hexdigest()[:24]
    return f"ev-{digest}"


def _first_text(raw: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _content_contract(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """把规划器常见同义键收敛到四个冻结内容字段；全空行拒收。"""

    excerpt = _first_text(raw, ("content_excerpt", *_TEXT_ALIASES))
    title = _first_text(raw, ("title", "headline", "name"))
    if title is None and excerpt is not None:
        title = excerpt[:120]
    if excerpt is None and title is not None:
        excerpt = title
    author = _first_text(raw, ("author_name", *_AUTHOR_ALIASES))
    raw_metrics = (
        dict(raw.get("raw_metrics") or {})
        if isinstance(raw.get("raw_metrics"), Mapping)
        else {}
    )
    for key in _METRIC_ALIASES:
        value = raw.get(key)
        if value not in (None, ""):
            raw_metrics.setdefault(key, value)
    if title is None and excerpt is None and author is None and not raw_metrics:
        return None
    return {
        "title": title,
        "content_excerpt": excerpt,
        "author_name": author,
        "raw_metrics": raw_metrics,
    }


def load_evidence_payloads(
    path: str | Path,
    *,
    report_id: str,
    goal_id: str,
    agent_name: str,
    platform_hint: str | None = None,
) -> list[dict[str, Any]]:
    """读取单个成功产物；非 evidence JSON 返回空列表。"""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    payloads: list[dict[str, Any]] = []
    for raw in _items(value):
        if not isinstance(raw, Mapping):
            continue
        platform = normalize_platform(raw.get("platform") or platform_hint or "")
        permalink = str(raw.get("permalink") or "").strip()
        fetched_at = str(raw.get("fetched_at") or "").strip()
        if not platform or not permalink or not fetched_at:
            continue
        content = _content_contract(raw)
        if content is None:
            continue
        normalized = normalize_permalink(permalink)
        item_id_value = raw.get("platform_item_id")
        platform_item_id = (
            str(item_id_value).strip() if item_id_value not in (None, "") else None
        )
        extra = (
            dict(raw.get("extra") or {})
            if isinstance(raw.get("extra"), Mapping) else {}
        )
        source_type = str(raw.get("source_type") or "").strip()
        if source_type and source_type not in _SOURCE_TYPES:
            extra["artifact_source_type"] = source_type
        for key, item in raw.items():
            normalized_key = str(key)
            if (
                key not in _FROZEN_FIELDS | _SYSTEM_FIELDS
                and _EXTRA_KEY.fullmatch(normalized_key)
            ):
                extra[normalized_key] = item
        payload = {
            key: raw[key] for key in _FROZEN_FIELDS
            if key in raw and key not in {"extra", "citation_no"}
        }
        payload.update({
            "id": str(raw.get("id") or raw.get("evidence_id") or _evidence_id(
                report_id, platform, platform_item_id, normalized
            )),
            "report_id": report_id,
            "goal_id": goal_id,
            "agent_name": agent_name,
            "platform": platform,
            "platform_item_id": platform_item_id,
            "permalink": normalized,
            "fetched_at": fetched_at,
            "extra": extra,
            **content,
        })
        if source_type and source_type not in _SOURCE_TYPES:
            payload["source_type"] = "other"
        payloads.append(payload)
    return payloads


def consumed_platform_index(research_root: str | Path) -> dict[str, str]:
    """汇总已完成五维处理的 permalink -> platform，不识别信息源实现。"""

    root = Path(research_root)
    index: dict[str, str] = {}
    if not root.is_dir():
        return index
    for path in sorted(root.glob("goals/**/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for raw in _items(value):
            if not isinstance(raw, Mapping):
                continue
            platform = str(raw.get("platform") or "").strip()
            permalink = str(raw.get("permalink") or "").strip()
            if (
                not platform
                or not permalink
                or not _CONSUMED_FIELDS <= set(raw)
            ):
                continue
            try:
                index[normalize_permalink(permalink)] = platform
            except ValueError:
                continue
    return index


__all__ = ["consumed_platform_index", "load_evidence_payloads"]
