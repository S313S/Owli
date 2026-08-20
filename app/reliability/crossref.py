"""断言级血缘簇、五项独立性排除与验证结论纯函数。"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

from publicsuffix2 import get_sld

from .scoring import PLATFORM_BASELINES, grade_for_total


GRADE_ORDER = {"D": 0, "C": 1, "B": 2, "A": 3}


def normalize_origin_url(value: str) -> str:
    """去 scheme/www/追踪查询串/锚点；保留 HN 等身份参数 id。"""
    raw = value.strip()
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/").lower()
    identity_query = [(key.lower(), val.lower()) for key, val in parse_qsl(parsed.query) if key.lower() == "id"]
    query = f"?{urlencode(identity_query)}" if identity_query else ""
    return f"{host}{path}{query}"


def origin_key(evidence: Mapping[str, Any], claim_id: str) -> str:
    extra = evidence.get("extra") or {}
    secondary = extra.get("crossref_secondary") or {}
    if isinstance(secondary.get(claim_id), Mapping) and secondary[claim_id].get("origin_key"):
        return str(secondary[claim_id]["origin_key"])
    explicit_by_claim = evidence.get("explicit_origin_by_claim") or {}
    if isinstance(explicit_by_claim, Mapping) and explicit_by_claim.get(claim_id):
        return normalize_origin_url(str(explicit_by_claim[claim_id]))
    claim_ids = extra.get("claim_ids") or []
    persisted = extra.get("origin_key")
    if claim_ids and claim_ids[0] == claim_id and persisted:
        return str(persisted)
    explicit = evidence.get("explicit_origin_url") or evidence.get("canonical_url")
    if explicit:
        return normalize_origin_url(str(explicit))
    parent = evidence.get("parent_permalink")
    firsthand = bool((evidence.get("firsthand_by_claim") or {}).get(claim_id))
    if parent and not (evidence.get("is_top_level_comment") and firsthand):
        return normalize_origin_url(str(parent))
    return normalize_origin_url(str(evidence.get("permalink", "")))


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    normalized = str(value)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tokens(value: Any) -> set[str]:
    text = str(value).lower()
    tokens = set(re.findall(r"[a-z0-9_]+", text))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(sequence) == 1:
            tokens.add(sequence)
        else:
            tokens.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return tokens


def _title_similarity(left: Any, right: Any) -> float:
    a, b = _tokens(left), _tokens(right)
    return len(a & b) / len(a | b) if a or b else 0.0


def _author_key(name: Any, aliases: Mapping[str, str] | Sequence[Sequence[str]] | None) -> str:
    def normalize(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "")).casefold()

    normalized = normalize(name)
    if isinstance(aliases, Mapping):
        mapping = {normalize(key): normalize(value) for key, value in aliases.items()}
        return mapping.get(normalized, normalized)
    for group in aliases or ():
        members = {normalize(item) for item in group}
        if normalized in members:
            return sorted(members)[0]
    return normalized


def _registered_domain(host: str) -> str:
    return str(get_sld(host.lower()) or host.lower())


def _institution_parts(evidence: Mapping[str, Any]) -> tuple[str | None, str]:
    explicit = evidence.get("institution_key")
    if explicit:
        return re.sub(r"[^a-z0-9一-鿿]", "", str(explicit).casefold()), ""
    meta = evidence.get("author_meta")
    affiliation = None
    if isinstance(meta, Mapping) and meta.get("affiliation"):
        affiliation = re.sub(
            r"[^a-z0-9一-鿿]", "", str(meta["affiliation"]).casefold()
        )
    host = (urlsplit(str(evidence.get("permalink", ""))).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return affiliation, _registered_domain(host)


def _institutions_differ(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_affiliation, left_domain = _institution_parts(left)
    right_affiliation, right_domain = _institution_parts(right)
    if left_affiliation and right_affiliation:
        return left_affiliation != right_affiliation
    if not left_affiliation and not right_affiliation:
        platform_domains = {
            "hacker_news": {"ycombinator.com"}, "x": {"x.com", "twitter.com"},
            "xhs": {"xiaohongshu.com", "xhslink.com"}, "douyin": {"douyin.com"},
            "bilibili": {"bilibili.com"}, "reddit": {"reddit.com"},
        }
        platform = left.get("platform") if left.get("platform") == right.get("platform") else None
        if platform and {left_domain, right_domain} <= platform_domains.get(str(platform), set()):
            return True
        return bool(left_domain and right_domain and left_domain != right_domain)
    affiliation = left_affiliation or right_affiliation or ""
    domain = right_domain if left_affiliation else left_domain
    domain_label = re.sub(r"[^a-z0-9一-鿿]", "", domain.split(".", 1)[0])
    return not bool(affiliation and domain_label and (
        affiliation in domain_label or domain_label in affiliation
    ))


def independence_checks(
    left: Mapping[str, Any], right: Mapping[str, Any], claim_id: str, *,
    author_aliases: Mapping[str, str] | Sequence[Sequence[str]] | None = None,
) -> dict[str, bool]:
    """返回 §3.2 五项排除法的逐项布尔结果。"""
    lineage = origin_key(left, claim_id) != origin_key(right, claim_id)
    left_author = _author_key(left.get("author_name"), author_aliases)
    right_author = _author_key(right.get("author_name"), author_aliases)
    authors = bool(left_author and right_author and left_author != right_author)
    institutions = _institutions_differ(left, right)

    left_time, right_time = _parse_time(left.get("published_at")), _parse_time(right.get("published_at"))
    threshold = 0.45 if {left.get("platform"), right.get("platform")} & {"xhs", "douyin"} else 0.6
    same_batch = bool(
        left_time and right_time
        and abs((left_time - right_time).total_seconds()) <= 48 * 3600
        and _title_similarity(left.get("title"), right.get("title")) >= threshold
    )
    firsthand = bool(
        (left.get("firsthand_by_claim") or {}).get(claim_id)
        or (right.get("firsthand_by_claim") or {}).get(claim_id)
    )
    return {
        "citation_lineage": lineage,
        "author_subject": authors,
        "institution_subject": institutions,
        "repost_batch": not same_batch,
        "firsthand": firsthand,
    }


def _independent(
    left: Mapping[str, Any], right: Mapping[str, Any], claim_id: str, *,
    author_aliases: Mapping[str, str] | Sequence[Sequence[str]] | None,
) -> bool:
    left_url = normalize_origin_url(str(left.get("permalink", "")))
    right_url = normalize_origin_url(str(right.get("permalink", "")))

    def ancestors(item: Mapping[str, Any]) -> set[str]:
        values: list[Any] = [
            item.get("parent_permalink"), item.get("top_level_permalink"),
            item.get("root_permalink"),
        ]
        extra_ancestors = item.get("ancestor_permalinks") or []
        if isinstance(extra_ancestors, Sequence) and not isinstance(extra_ancestors, (str, bytes)):
            values.extend(extra_ancestors)
        return {normalize_origin_url(str(value)) for value in values if value}

    if left_url in ancestors(right) or right_url in ancestors(left):
        return False
    checks = independence_checks(
        left, right, claim_id, author_aliases=author_aliases
    )
    if all(checks.values()):
        return True
    # 一手性豁免只覆盖引用血缘；作者、机构与通稿批次仍须独立。
    return checks["firsthand"] and all(
        checks[key] for key in ("author_subject", "institution_subject", "repost_batch")
    )


def _grade(evidence: Mapping[str, Any]) -> str:
    direct = evidence.get("grade")
    if direct in GRADE_ORDER:
        return str(direct)
    scores = [evidence.get(field) for field in (
        "score_authority", "score_freshness", "score_crossref",
        "score_completeness", "score_independence",
    )]
    if all(isinstance(value, int) and not isinstance(value, bool) for value in scores):
        return grade_for_total(sum(scores))
    baseline = PLATFORM_BASELINES.get(str(evidence.get("platform")), PLATFORM_BASELINES["web_search"])
    return grade_for_total(sum(baseline.values()))


def _thread_key(evidence: Mapping[str, Any]) -> str | None:
    for key in ("thread_key", "conversation_id", "story_id", "video_id", "note_id"):
        if evidence.get(key) is not None:
            return f"{evidence.get('platform')}:{evidence[key]}"
    return None


def build_claim_clusters(
    evidence_items: Iterable[Mapping[str, Any]], claim_id: str, *,
    author_aliases: Mapping[str, str] | Sequence[Sequence[str]] | None = None,
    conflict_explained: bool = False,
) -> dict[str, Any]:
    """为单一断言构簇，并返回可直接合入 evidence.extra 的 §3.4 结构。"""
    items = [dict(item) for item in evidence_items]
    if not items:
        raise ValueError("断言至少需要一条证据")
    identifiers = [str(item.get("id", "")) for item in items]
    if any(not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("证据 id 必须非空且唯一")

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if not _independent(
                items[left], items[right], claim_id, author_aliases=author_aliases
            ):
                union(left, right)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(items)):
        grouped[find(index)].append(index)
    ordered_groups = sorted(grouped.values(), key=lambda group: min(group))
    cluster_ids = {index: f"cl-{number:02d}" for number, group in enumerate(ordered_groups, 1) for index in group}

    def representative(group: list[int]) -> int:
        return max(group, key=lambda index: (GRADE_ORDER[_grade(items[index])], -index))

    support_groups = [
        group for group in ordered_groups
        if any((items[index].get("stance_by_claim") or {}).get(claim_id, "supports") != "contradicts" for index in group)
    ]
    conflict_groups = [
        group for group in ordered_groups
        if all((items[index].get("stance_by_claim") or {}).get(claim_id, "supports") == "contradicts" for index in group)
    ]

    ranked_supports = sorted(
        support_groups,
        key=lambda group: (-GRADE_ORDER[_grade(items[representative(group)])], min(group)),
    )
    selected_supports: list[list[int]] = []
    thread_counts: dict[str, int] = defaultdict(int)
    x_selected = False
    for group in ranked_supports:
        rep = representative(group)
        if items[rep].get("platform") == "x":
            if x_selected:
                continue
            x_selected = True
        thread = _thread_key(items[rep])
        if thread and thread_counts[thread] >= 2:
            continue
        if thread:
            thread_counts[thread] += 1
        selected_supports.append(group)
    selected_supports.sort(key=lambda group: min(group))

    k = len(selected_supports)
    selected_reps = [representative(group) for group in selected_supports]
    conflict_reps = [representative(group) for group in conflict_groups]
    strong_conflict = any(GRADE_ORDER[_grade(items[index])] >= GRADE_ORDER["B"] for index in conflict_reps)
    def verdict_for(index: int) -> str:
        if strong_conflict and not conflict_explained:
            return "CONFLICT"
        if strong_conflict and conflict_explained:
            return "WEAK"
        if k <= 1:
            return "SINGLE"
        own_cluster = cluster_ids[index]
        best_other = max(
            (
                GRADE_ORDER[_grade(items[rep])]
                for rep in selected_reps
                if cluster_ids[rep] != own_cluster
            ),
            default=-1,
        )
        return "PASS" if best_other >= GRADE_ORDER["B"] else "WEAK"

    primary_index = selected_reps[0] if selected_reps else 0
    verdict = verdict_for(primary_index)
    score = {"PASS": 2, "WEAK": 1, "SINGLE": 0, "CONFLICT": 0}[verdict]
    extras: dict[str, dict[str, Any]] = {}
    primary_keys = (
        "claim_ids", "origin_key", "crossref_cluster", "crossref_n_clusters",
        "crossref_peers", "crossref_conflicts", "crossref_verdict",
    )
    for index, item in enumerate(items):
        existing_extra = item.get("extra") or {}
        existing_primary = {
            key: existing_extra[key] for key in primary_keys if key in existing_extra
        }
        stance = (item.get("stance_by_claim") or {}).get(claim_id, "supports")
        if stance == "contradicts":
            extras[identifiers[index]] = existing_primary
            continue
        peers = [identifiers[rep] for rep in selected_reps if cluster_ids[rep] != cluster_ids[index]]
        existing_claims = list(existing_extra.get("claim_ids", []))
        claim_ids = existing_claims if claim_id in existing_claims else [*existing_claims, claim_id]
        item_verdict = verdict_for(index)
        claim_result = {
            "origin_key": origin_key(item, claim_id),
            "cluster": cluster_ids[index],
            "n_clusters": k,
            "peers": peers,
            "conflicts": [identifiers[rep] for rep in conflict_reps],
            "verdict": item_verdict,
        }
        if claim_ids[0] == claim_id:
            extras[identifiers[index]] = {
                "claim_ids": claim_ids,
                "origin_key": claim_result["origin_key"],
                "crossref_cluster": claim_result["cluster"],
                "crossref_n_clusters": claim_result["n_clusters"],
                "crossref_peers": claim_result["peers"],
                "crossref_conflicts": claim_result["conflicts"],
                "crossref_verdict": claim_result["verdict"],
            }
        else:
            secondary = dict(existing_extra.get("crossref_secondary") or {})
            secondary[claim_id] = claim_result
            extras[identifiers[index]] = {
                **existing_primary,
                "claim_ids": claim_ids,
                "crossref_secondary": secondary,
            }
    return {
        "claim_id": claim_id,
        "k": k,
        "verdict": verdict,
        "score_crossref": score,
        "clusters": [cluster_ids[group[0]] for group in selected_supports],
        "evidence_extra": extras,
    }
