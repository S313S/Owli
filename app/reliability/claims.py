"""报告断言的显式登记、permalink 联接与双向落库。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.store.dao import normalize_permalink


CLAIM_ID_PATTERN = re.compile(r"^c-\d{2,}$")
CLAIM_FIELDS = frozenset({"id", "text", "evidence", "conflict_note"})
CLAIM_EVIDENCE_FIELDS = frozenset({
    "permalink", "stance", "firsthand", "origin_url",
})
FIRSTHAND_SOURCES = frozenset({
    # §XSEM-1 条 1：C-1 只做到「撰写方声明可分辨」，闸门没守住。audited = §3.2 第 5 项
    # 真正由 reliability-auditor 逐 (证据, 断言) 对判过、且留了一句依据。
    "declared_by_writer", "declared_by_backfill", "audited",
})
_FIRSTHAND_SOURCE_BY_CLAIMS_SOURCE = {
    "chapter": "declared_by_writer",
    "backfill": "declared_by_backfill",
}
CLAIM_DROP_REASONS = frozenset({
    "dangling_evidence", "all_evidence_dangling",
})


@dataclass(frozen=True)
class ClaimsRegistrationError(ValueError):
    """断言登记失败；offenders 可直接进入报告校验事件。"""

    message: str
    offenders: list[str]

    def __str__(self) -> str:
        return self.message


def _error(message: str, offenders: Iterable[str]) -> ClaimsRegistrationError:
    return ClaimsRegistrationError(message, list(offenders))


def claims_from_documents(
    documents: Iterable[Mapping[str, Any]],
    *,
    stripped: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """按章产物顺序收集可选顶层 claims；不从正文推断。

    §FIX-2 货 1（D-037）：id 由分片写手按「节序+片序+条序」生成，goal 与章两个
    维度缺席，跨章同位片必撞 `c-010101`（真机 250 条只剩 84 唯一、166 处重复）。
    这里按**文档序**给合法 id 加确定性命名空间 `c-{文档序:02d}{原数字}`——机械改写，
    不靠提示词约束写手；形态仍满足 CLAIM_ID_PATTERN。文档内重复加前缀后照旧相撞、
    id 格式违规原样透传，两条检出能力都不被掩盖。claim id 只是报告内部键：正文角标
    走 `[Sxx]`，不引用 claim id，故改写不动正文。
    """

    result: list[Any] = []
    for index, document in enumerate(documents, start=1):
        claims = document.get("claims")
        if claims is None:
            continue
        if not isinstance(claims, list):
            raise _error("章产物 claims 必须是数组", ["claims"])
        for position, claim in enumerate(claims):
            claim = _namespaced_claim(claim, index)
            result.append(_strip_unknown_keys(
                claim, location=f"claims[{len(result)}]",
                origin=f"文档 {index} 的第 {position + 1} 条", account=stripped,
            ))
    return result


def _strip_unknown_keys(
    claim: Any, *, location: str, origin: str, account: list[dict[str, Any]] | None,
) -> Any:
    """机械剥离闭集外的键（用户 09-03 拍板「甲」），剥了什么逐条记账。

    §FIX-2 货 1：写手会在 claim 顶层多写 `stance`、在 evidence 条目里多写 `fetched_at`
    这类闭集外字段，登记是「一处不合规整批退回」，于是 250 条断言全被拒、库里恒空。
    这里只剥**闭集外**的键（CLAIM_FIELDS / CLAIM_EVIDENCE_FIELDS 本身一个字不放宽），
    剥掉的键与出处进 account，由调用方落进事件；剥不动的（缺 id、类型不对）原样透传，
    照旧由 prepare_claim_registration 报错。
    """

    if not isinstance(claim, Mapping):
        return claim
    removed: dict[str, list[str]] = {}
    kept = {k: v for k, v in claim.items() if k in CLAIM_FIELDS}
    top_unknown = sorted(set(claim) - CLAIM_FIELDS)
    if top_unknown:
        removed["claim"] = top_unknown
    evidence = kept.get("evidence")
    if isinstance(evidence, list):
        cleaned: list[Any] = []
        for item in evidence:
            if not isinstance(item, Mapping):
                cleaned.append(item)
                continue
            unknown = sorted(set(item) - CLAIM_EVIDENCE_FIELDS)
            if unknown:
                removed.setdefault("evidence", [])
                removed["evidence"].extend(k for k in unknown if k not in removed["evidence"])
            cleaned.append({k: v for k, v in item.items() if k in CLAIM_EVIDENCE_FIELDS})
        kept["evidence"] = cleaned
    if not removed:
        return claim
    if account is not None:
        account.append({
            "location": location, "origin": origin,
            "claim_id": kept.get("id") if isinstance(kept.get("id"), str) else None,
            "removed": removed,
        })
    return kept


def _namespaced_claim(claim: Any, document_index: int) -> Any:
    """给合法 id 加文档命名空间；其余原样返回（含非 Mapping 与非法 id）。"""

    if not isinstance(claim, Mapping):
        return claim
    claim_id = claim.get("id")
    if not isinstance(claim_id, str) or CLAIM_ID_PATTERN.fullmatch(claim_id) is None:
        return claim
    return {**claim, "id": f"c-{document_index:02d}{claim_id[2:]}"}


def prepare_claim_registration(
    evidence_rows: Sequence[Mapping[str, Any]],
    raw_claims: Sequence[Any],
    *,
    source: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[str]],
    list[dict[str, Any]],
]:
    """校验断言并联接 evidence；只把悬空 permalink 降级为丢弃账。"""

    if source not in {"chapter", "backfill"}:
        raise ValueError("claims_source 只能是 chapter 或 backfill")
    if isinstance(raw_claims, (str, bytes)) or not isinstance(raw_claims, Sequence):
        raise TypeError("claims 必须是数组")
    evidence_by_url: dict[str, str] = {}
    for row in evidence_rows:
        evidence_id = str(row.get("id") or "")
        permalink = row.get("permalink")
        if not evidence_id or not isinstance(permalink, str):
            continue
        evidence_by_url[normalize_permalink(permalink)] = evidence_id

    registered: list[dict[str, Any]] = []
    mapping: dict[str, list[str]] = {}
    dropped: list[dict[str, Any]] = []
    seen_claim_ids: set[str] = set()
    offenders: list[str] = []
    for index, raw_claim in enumerate(raw_claims):
        location = f"claims[{index}]"
        if not isinstance(raw_claim, Mapping):
            offenders.append(f"{location} 不是 object")
            continue
        unknown = sorted(set(raw_claim) - CLAIM_FIELDS)
        if unknown:
            offenders.append(f"{location} 含未知键 {unknown}")
        claim_id = raw_claim.get("id")
        if not isinstance(claim_id, str) or CLAIM_ID_PATTERN.fullmatch(claim_id) is None:
            offenders.append(f"{location}.id 不符合 c-\\d{{2,}}")
            continue
        if claim_id in seen_claim_ids:
            offenders.append(f"{location}.id 报告内重复：{claim_id}")
            continue
        seen_claim_ids.add(claim_id)
        text = raw_claim.get("text")
        if not isinstance(text, str) or not text.strip():
            offenders.append(f"{location}.text 缺失或为空")
        raw_evidence = raw_claim.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            offenders.append(f"{location}.evidence 至少需要 1 条")
            continue

        evidence_ids: list[str] = []
        contradicts: dict[str, str] = {}
        firsthand: list[str] = []
        origins: dict[str, str] = {}
        dangling_permalinks: list[str] = []
        seen_urls: set[str] = set()
        for evidence_index, raw_link in enumerate(raw_evidence):
            link_location = f"{location}.evidence[{evidence_index}]"
            if not isinstance(raw_link, Mapping):
                offenders.append(f"{link_location} 不是 object")
                continue
            link_unknown = sorted(set(raw_link) - CLAIM_EVIDENCE_FIELDS)
            if link_unknown:
                offenders.append(f"{link_location} 含未知键 {link_unknown}")
            permalink = raw_link.get("permalink")
            try:
                normalized = normalize_permalink(str(permalink or ""))
            except ValueError:
                offenders.append(f"{link_location}.permalink 不是 HTTP(S) 绝对链接")
                continue
            if normalized in seen_urls:
                offenders.append(f"{link_location}.permalink 在断言内重复")
                continue
            seen_urls.add(normalized)
            stance = raw_link.get("stance", "supports")
            if stance not in {"supports", "contradicts"}:
                offenders.append(f"{link_location}.stance 只能是 supports/contradicts")
            if "firsthand" in raw_link and not isinstance(raw_link["firsthand"], bool):
                offenders.append(f"{link_location}.firsthand 必须是 bool")
            origin_url = raw_link.get("origin_url")
            normalized_origin: str | None = None
            if origin_url is not None:
                try:
                    normalized_origin = normalize_permalink(str(origin_url))
                except ValueError:
                    offenders.append(f"{link_location}.origin_url 不是 HTTP(S) 绝对链接")
            # 悬空只改变证据的登记去向，不能让同一 link 绕过结构契约。
            evidence_id = evidence_by_url.get(normalized)
            if evidence_id is None:
                dangling_permalinks.append(normalized)
                continue
            evidence_ids.append(evidence_id)
            mapping.setdefault(evidence_id, []).append(claim_id)
            if stance == "contradicts":
                contradicts[evidence_id] = "contradicts"
            if raw_link.get("firsthand") is True:
                firsthand.append(evidence_id)
            if normalized_origin is not None:
                origins[evidence_id] = normalized_origin

        claim: dict[str, Any] = {
            "id": claim_id,
            "text": text.strip() if isinstance(text, str) else "",
            "evidence_ids": evidence_ids,
            "claims_source": source,
            "firsthand_source": _FIRSTHAND_SOURCE_BY_CLAIMS_SOURCE[source],
        }
        conflict_note = raw_claim.get("conflict_note")
        if conflict_note is not None:
            if not isinstance(conflict_note, str):
                offenders.append(f"{location}.conflict_note 必须是字符串")
            elif conflict_note.strip():
                claim["conflict_note"] = conflict_note.strip()
        if contradicts:
            claim["stance"] = contradicts
        if firsthand:
            claim["firsthand"] = firsthand
        if origins:
            claim["origin_overrides"] = origins
        if dangling_permalinks:
            dropped.append({
                "claim_id": claim_id,
                "reason": (
                    "dangling_evidence"
                    if evidence_ids
                    else "all_evidence_dangling"
                ),
                "permalinks": dangling_permalinks,
            })
        # 全悬空断言若保留，会违反至少一条 evidence 的既有契约，也无法计算证据簇；
        # 丢弃正文登记项但保留 claims_dropped 审计记录。
        if evidence_ids:
            registered.append(claim)

    if offenders:
        raise _error(f"断言登记失败，共 {len(offenders)} 处", offenders)
    return registered, mapping, dropped


def register_claims(
    store: Any,
    report_id: str,
    raw_claims: Sequence[Any],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """两条生产路径共用的固定落库入口。"""

    claims, mapping, dropped = prepare_claim_registration(
        store.list_evidence(report_id), raw_claims, source=source
    )
    store.set_report_claims(report_id, claims, dropped=dropped)
    store.attach_claim_ids(report_id, mapping)
    return claims


__all__ = [
    "CLAIM_ID_PATTERN",
    "CLAIM_DROP_REASONS",
    "FIRSTHAND_SOURCES",
    "ClaimsRegistrationError",
    "claims_from_documents",
    "prepare_claim_registration",
    "register_claims",
]
