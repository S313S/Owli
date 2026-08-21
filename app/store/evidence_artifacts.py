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
_SOURCE_TYPES = frozenset({
    "post", "comment", "video", "article", "search_snippet",
    "ranking_item", "profile", "other",
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
        platform = str(raw.get("platform") or platform_hint or "").strip()
        permalink = str(raw.get("permalink") or "").strip()
        fetched_at = str(raw.get("fetched_at") or "").strip()
        if not platform or not permalink or not fetched_at:
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
