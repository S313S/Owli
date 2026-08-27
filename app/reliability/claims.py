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


@dataclass(frozen=True)
class ClaimsRegistrationError(ValueError):
    """断言登记失败；offenders 可直接进入报告校验事件。"""

    message: str
    offenders: list[str]

    def __str__(self) -> str:
        return self.message


def _error(message: str, offenders: Iterable[str]) -> ClaimsRegistrationError:
    return ClaimsRegistrationError(message, list(offenders))


def claims_from_documents(documents: Iterable[Mapping[str, Any]]) -> list[Any]:
    """按章产物顺序收集可选顶层 claims；不从正文推断。"""

    result: list[Any] = []
    for document in documents:
        claims = document.get("claims")
        if claims is None:
            continue
        if not isinstance(claims, list):
            raise _error("章产物 claims 必须是数组", ["claims"])
        result.extend(claims)
    return result


def prepare_claim_registration(
    evidence_rows: Sequence[Mapping[str, Any]],
    raw_claims: Sequence[Any],
    *,
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """校验断言并把 permalink 联接成 evidence id。"""

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
            evidence_id = evidence_by_url.get(normalized)
            if evidence_id is None:
                offenders.append(f"{claim_id} 悬空 permalink：{normalized}")
                continue
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
        registered.append(claim)

    if offenders:
        raise _error(f"断言登记失败，共 {len(offenders)} 处", offenders)
    return registered, mapping


def register_claims(
    store: Any,
    report_id: str,
    raw_claims: Sequence[Any],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """两条生产路径共用的固定落库入口。"""

    claims, mapping = prepare_claim_registration(
        store.list_evidence(report_id), raw_claims, source=source
    )
    store.set_report_claims(report_id, claims)
    store.attach_claim_ids(report_id, mapping)
    return claims


__all__ = [
    "CLAIM_ID_PATTERN",
    "ClaimsRegistrationError",
    "claims_from_documents",
    "prepare_claim_registration",
    "register_claims",
]
