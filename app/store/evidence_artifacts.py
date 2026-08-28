"""把已通过 goal 闸门的 JSON 证据产物投影为 Store 冻结列。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

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


# 域名即平台的那几家：引擎把 platform 写成发布方名时，链接本身仍然指得出平台。
# 只收「域名唯一对应一个平台」的条目——sohu.com/36kr.com 这类站点不是平台，
# 它们本来就该归 web_search。
_PLATFORM_DOMAINS = {
    "xiaohongshu.com": "xhs", "xhslink.com": "xhs",
    "douyin.com": "douyin", "iesdouyin.com": "douyin",
    "reddit.com": "reddit", "redd.it": "reddit",
    "ycombinator.com": "hacker_news",
    "producthunt.com": "product_hunt",
    "x.com": "x", "twitter.com": "x",
}
# platform 降级时，归一化三件套按的是**降级前**那个（错的）平台算的，平台一改就不
# 再成立；留着会撞 dao._validate_normalization 的一致性校验，让整批 upsert 回滚、
# 把合格证据一起丢掉（§D-015「冲突不得中止检索」的同一个教训）。故整体撤下并留痕。
_NORMALIZATION_FIELDS = ("norm_method", "normalized_score", "norm_context")

# 降级留痕键：与本文件已有的 `artifact_source_type` 同一套写法——
# 列收进闭集，产物里的原始自由文本原样留在 extra 里，不丢信息。
ARTIFACT_PLATFORM_KEY = "artifact_platform"
ARTIFACT_NORM_CONTEXT_KEY = "artifact_norm_context"
PLATFORM_VOCABULARY = frozenset(_PLATFORM_CANON)


def normalize_platform(value: str) -> str:
    """把引擎写的平台自由文本归到适配器的词表；不认识的原样返回。

    ⚠️ 不认识的**不改写**——产物里还出现过 "36氪AI测评""搜狐号""人人都是产品经理"
    这类**发布方名**（web_search 条目），它们不是平台别名，硬映射会把来源信息抹掉。
    本函数只管「是不是同一个平台的另一种写法」，判不出来就如实说判不出来；
    要不要降级、降级到哪一个，是 `resolve_platform` 的事（D-020）。
    """
    text = str(value or "").strip()
    if not text:
        return text
    lowered = text.casefold()
    if lowered in _PLATFORM_CANON:
        return lowered
    return _PLATFORM_ALIASES.get(lowered, text)


def _platform_from_permalink(permalink: str) -> str | None:
    host = (urlsplit(str(permalink or "")).hostname or "").casefold()
    while host:
        if host in _PLATFORM_DOMAINS:
            return _PLATFORM_DOMAINS[host]
        _, _, host = host.partition(".")
    return None


def resolve_platform(
    value: Any, *, permalink: str = "", hint: str | None = None,
) -> tuple[str, str | None]:
    """把产物里的 platform 收进闭集，返回 (闭集值, 越界原值 or None)。

    D-020：引擎会把「这条内容发在哪个号/哪个站」写进 platform 列
    （实测 `36氪AI测评 / 搜狐号 / 人人都是产品经理 / 提效录`，都是 web_search 条目）。
    这类值既不能原样落库（`platform` 是闭集：下游按它分平台统计、判来源权重、
    `crossref` 按它查域名归属），也不能一抹了之（发布方信息没有别的列接得住）。
    做法是**降级 + 留痕**：列收进闭集，原值由调用方写进 `extra`。

    降级顺序（强证据在前）：
    1. 别名归一命中闭集 → 用它；
    2. permalink 的域名唯一对应某平台 → 用域名（比 agent 自报的来源更硬）；
    3. agent 只挂了一个信息源且在闭集内 → 用它；
    4. 兜底 `web_search`——不是任何已知平台域名的网页，本来就走网页搜索通道。
    """
    canonical = normalize_platform(value if isinstance(value, str) else str(value or ""))
    if canonical in PLATFORM_VOCABULARY:
        return canonical, None
    hinted = normalize_platform(hint or "")
    if not canonical:
        if not hinted:
            # 产物没写平台、agent 也没挂唯一信息源：与本包之前一致，交给调用方丢弃。
            return "", None
        if hinted in PLATFORM_VOCABULARY:
            return hinted, None
        # 提示词本身越界（计划里挂了个词表外的信息源）：按越界原值一并处理。
        canonical = hinted
    by_domain = _platform_from_permalink(permalink)
    if by_domain is not None:
        return by_domain, canonical
    if hinted in PLATFORM_VOCABULARY:
        return hinted, canonical
    return "web_search", canonical


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
        permalink = str(raw.get("permalink") or "").strip()
        platform, artifact_platform = resolve_platform(
            raw.get("platform"), permalink=permalink, hint=platform_hint,
        )
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
        if artifact_platform:
            # 越界原值原样留痕（发布方名就是从这里读回来的），并撤下按旧平台
            # 算的归一化三件套——它们已经不成立，留着会让整批 upsert 回滚。
            extra[ARTIFACT_PLATFORM_KEY] = artifact_platform
            if payload.get("norm_context") is not None:
                extra[ARTIFACT_NORM_CONTEXT_KEY] = payload["norm_context"]
            for field in _NORMALIZATION_FIELDS:
                payload[field] = None
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
            permalink = str(raw.get("permalink") or "").strip()
            # 与落库同一把尺：发布方名在这里也不该被当成一个独立「平台」，
            # 否则 validation 的「已消费平台都要在信息源清单里露面」会按发布方名要人。
            platform, _ = resolve_platform(raw.get("platform"), permalink=permalink)
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


__all__ = [
    "ARTIFACT_NORM_CONTEXT_KEY", "ARTIFACT_PLATFORM_KEY",
    "PLATFORM_VOCABULARY", "consumed_platform_index", "load_evidence_payloads",
    "normalize_platform", "resolve_platform",
]
