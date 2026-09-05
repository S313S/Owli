"""入库后独立补评：读 Store、批量审计、幂等 upsert，并把报告角标落回 Store。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.claude import OwliResultError, parse_owli_result
from app.adapters.contracts import EngineTask
from app.reliability.audit import AUTHORITY_KINDS, INTEREST_RELATIONS, MAX_ATTEMPTS
from app.reliability.scoring import (
    CROSSREF_SCORES,
    FRESHNESS_WINDOWS,
    PLATFORM_BASELINES,
    PRIMARY_METRICS,
    RATING_NOTES_PATTERN,
    SCORE_FIELDS,
    normalize_evidence_metrics,
    claim_support_is_valid,
    engagement_percentiles,
    is_comment_row,
    score_evidence_partial,
)
from app.reliability.crossref import build_claim_clusters
from app.report.markdown import render_source_list
from app.store.dao import normalize_permalink


CONTENT_KINDS = frozenset((*FRESHNESS_WINDOWS, "reference"))
AGENT_ID = "reliability-auditor"
LABEL_SCORE_FIELDS = frozenset({
    "score_authority", "score_freshness", "score_independence",
})
_RECOVERABLE_TRANSPORT_MARKERS = (
    "tls handshake eof", "stream disconnected", "reconnecting",
)
_MARK = re.compile(r"\[S(?P<number>\d{2})\]")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"\((https?://[^)\s]+)\)")
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class BackfillResult:
    report_id: str
    before_rows: int
    after_rows: int
    attempted: int
    rated: int
    failed: int
    complete_rows: int
    complete_cells: int
    total_cells: int
    citations: int
    summary_line: str | None
    weak_claims: list[str]


_CROSSREF_LIFT_KEYS = (
    "story_id", "conversation_id", "video_id", "note_id", "thread_key",
    "parent_permalink", "top_level_permalink", "root_permalink",
    "ancestor_permalinks", "is_top_level_comment", "institution_key",
    "canonical_url",
)


def _claim_crossref_item(
    row: Mapping[str, Any], claim: Mapping[str, Any]
) -> dict[str, Any]:
    """组装审计前的簇计算输入，并提升库存线程/血缘键。"""

    item = dict(row)
    # 簇判定先于本轮交叉维回写。D-013：上一轮的 score_crossref 与两个生成列
    # 不得成为下一轮 _grade() 的输入，否则 verdict↔grade 成环、补评两遍读数漂移。
    # §XSEM-1 条 3（C-1）：权威/时效/完整/无关四维不在这条环上（§3.5 第③步先写
    # 四维实值、后算 crossref），保留它们让 _grade() 能算四维先验等级。
    for key in ("score_crossref", "score_total", "grade"):
        item.pop(key, None)
    extra_value = item.get("extra")
    extra = dict(extra_value) if isinstance(extra_value, Mapping) else {}
    item["extra"] = extra
    for key in _CROSSREF_LIFT_KEYS:
        if key in extra:
            item[key] = extra[key]
    claim_id = str(claim["id"])
    evidence_id = str(item["id"])
    stance = claim.get("stance")
    stance_value = (
        stance.get(evidence_id, "supports")
        if isinstance(stance, Mapping)
        else "supports"
    )
    item["stance_by_claim"] = {claim_id: stance_value}
    firsthand = claim.get("firsthand")
    if isinstance(firsthand, list):
        item_firsthand = evidence_id in firsthand
    else:
        # §CMT-1 货 4：评论行按「用户直述」起评——写手没逐条登记 firsthand 时，
        # 一条读者自己写的评论本来就是一手材料，起评点不该和转载稿一样是 False。
        # 写手显式给了 firsthand 列表就以它为准，这里只兜没登记的那一路。
        item_firsthand = is_comment_row(item)
    item["firsthand_by_claim"] = {claim_id: item_firsthand}
    origins = claim.get("origin_overrides")
    if isinstance(origins, Mapping) and origins.get(evidence_id):
        item["explicit_origin_by_claim"] = {
            claim_id: origins[evidence_id]
        }
    return item


def _report_claims(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    extra = report.get("extra")
    value = extra.get("claims") if isinstance(extra, Mapping) else None
    if not isinstance(value, list):
        return []
    return [dict(claim) for claim in value if isinstance(claim, Mapping)]


def _firsthand_settled(claim: Mapping[str, Any]) -> bool:
    """已审计且逐条留痕的断言不再重烧引擎——防 D-013 那类重复回写。"""

    if claim.get("firsthand_source") != "audited":
        return False
    audit = claim.get("firsthand_audit")
    evidence_ids = claim.get("evidence_ids")
    if not isinstance(audit, Mapping) or not isinstance(evidence_ids, list):
        return False
    return all(str(evidence_id) in audit for evidence_id in evidence_ids)


def _firsthand_pairs(
    claims: Sequence[Mapping[str, Any]], rows_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """展开成 (断言, 证据) 对——§3.2 明说同两条证据在断言 A/B 上可以不同。"""

    pairs: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim.get("id")
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(claim_id, str) or not isinstance(evidence_ids, list):
            continue
        declared = claim.get("firsthand")
        declared_ids = set(declared) if isinstance(declared, list) else set()
        for evidence_id in evidence_ids:
            row = rows_by_id.get(str(evidence_id))
            if row is None:
                continue
            excerpt = str(row.get("content_excerpt") or "")
            pairs.append({
                "claim_id": claim_id,
                "evidence_id": str(evidence_id),
                "claim_text": str(claim.get("text") or ""),
                "goal_id": str(row.get("goal_id") or "goal-1"),
                "platform": row.get("platform"),
                "source_type": row.get("source_type"),
                "permalink": row.get("permalink"),
                "title": row.get("title"),
                "published_at": row.get("published_at"),
                "author_present": bool(row.get("author_name")),
                "content_excerpt": excerpt[:600],
                "declared_by_writer": str(evidence_id) in declared_ids,
            })
    return pairs


def _backfill_claim_clusters(
    store: Any,
    report: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """按 reports.extra.claims 顺序累积主/次断言簇并回写两端。"""

    report_extra = report.get("extra")
    claims_value = (
        report_extra.get("claims")
        if isinstance(report_extra, Mapping)
        else None
    )
    if not isinstance(claims_value, list) or not claims_value:
        return [dict(row) for row in rows], set()
    claims = [dict(claim) for claim in claims_value if isinstance(claim, Mapping)]
    accumulated = {str(row["id"]): dict(row) for row in rows}
    referenced_ids: set[str] = set()
    aliases = (
        report_extra.get("author_aliases")
        if isinstance(report_extra, Mapping)
        else None
    )
    for claim in claims:
        claim_id = claim.get("id")
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(claim_id, str) or not isinstance(evidence_ids, list):
            continue
        items = []
        for evidence_id in evidence_ids:
            row = accumulated.get(str(evidence_id))
            if row is None:
                continue
            referenced_ids.add(str(evidence_id))
            items.append(_claim_crossref_item(row, claim))
        if not items:
            continue
        result = build_claim_clusters(
            items,
            claim_id,
            author_aliases=aliases,
            conflict_explained=bool(claim.get("conflict_note")),
        )
        for evidence_id, computed_extra in result["evidence_extra"].items():
            row = accumulated[evidence_id]
            existing_extra = row.get("extra")
            merged = (
                dict(existing_extra)
                if isinstance(existing_extra, Mapping)
                else {}
            )
            merged.update(computed_extra)
            row["extra"] = merged
        claim.update(
            clusters=result["clusters"],
            k=result["k"],
            verdict=result["verdict"],
        )

    payloads = [
        {
            key: value for key, value in accumulated[evidence_id].items()
            if key not in {"score_total", "grade"}
        }
        for evidence_id in sorted(referenced_ids)
    ]
    if payloads:
        store.upsert_evidence_batch(payloads)
    store.set_report_claims(str(report["id"]), claims)
    return store.list_evidence(str(report["id"])), referenced_ids


def _firsthand_prompt(
    pairs: Sequence[Mapping[str, Any]], *, output_path: Path
) -> str:
    return (
        "目标：按 §3.2 第 5 项「一手性」，逐 (断言, 证据) 对判定该条证据对该条断言是否"
        "提供了**独立获取的一手信息**（自测数据、自述经历、自有统计、当事方一手披露），"
        "而不只是复述别人的结论。判定只依据输入，不补造正文、作者或时间。\n"
        "判否的典型：转述官方公告、综述他人评测、聚合站汇编、纯标题片段。\n"
        "判是的典型：作者写自己实测的数字、亲历使用体验、自有样本统计、"
        "被讨论主体自己首次披露的事实。\n"
        "同两条证据在断言 A 上可以判是、在断言 B 上判否——请对着这一条断言的原文判，"
        "不要对着证据本身泛泛判。\n"
        "输入里的 declared_by_writer 是撰写方的自述，**只作参考，不得直接照抄**；"
        "判断与它不一致是正常的。\n"
        "每项输出 claim_id、evidence_id、firsthand（布尔）、reason（1–20 字依据）。"
        "不接受「可能」「疑似」这类不表态的写法；reason 必填且不得为空。\n"
        "输出顶层数组，条数与顺序必须与输入完全一致，不要输出 Markdown。\n"
        f"必须把结果写到此精确路径：{output_path}。不得改用其他文件名。\n"
        "输入 (断言, 证据) 对：" + json.dumps(
            list(pairs), ensure_ascii=False, separators=(",", ":")
        )
    )


def _firsthand_errors(
    value: Any, inputs: Sequence[Mapping[str, Any]]
) -> list[str]:
    if not isinstance(value, list):
        return ["一手性审计产物顶层必须是数组"]
    if len(value) != len(inputs):
        return [f"审计条数应为 {len(inputs)}，实际 {len(value)}"]
    errors: list[str] = []
    for index, (item, expected) in enumerate(zip(value, inputs)):
        location = f"[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{location} 不是 object")
            continue
        for key in ("claim_id", "evidence_id"):
            if str(item.get(key) or "") != str(expected[key]):
                errors.append(f"{location}.{key} 与输入不一致，顺序不得改动")
        if not isinstance(item.get("firsthand"), bool):
            errors.append(f"{location}.firsthand 必须是 true/false，不接受不表态")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{location}.reason 缺失或为空")
        elif len(reason.strip()) > 20:
            errors.append(f"{location}.reason 超过 20 字")
    return errors


def _prompt(items: Sequence[Mapping[str, Any]], *, output_path: Path) -> str:
    return (
        "目标：对已入库证据做可靠度闭集判定。只依据输入，不补造作者、时间、正文或利益关系。\n"
        "每项输出 id、authority_kind、content_kind、interest_relation、missing_dimensions。"
        "前三个标签分别只能取既有闭集；若信息不足，标签填 null，并在 missing_dimensions "
        "用对应 score 字段写 1–14 字明确原因。允许登记缺失的字段仅为 "
        f"{','.join(sorted(LABEL_SCORE_FIELDS))}。score_crossref 与 score_completeness "
        "由本地规则计算，不得写入 missing_dimensions。不得为了非空率猜测标签。\n"
        "只有对应标签本身为 null 时才能登记该缺失维度；标签已判定则不得同时声称缺失。"
        "例如，缺少 published_at 但 content_kind 可判定时，时效由本地规则保守评 0，"
        "不得将 score_freshness 登记为缺失。\n"
        "authority_kind 判据：first_party_official=被讨论主体自己的官方域名；"
        "verified_principal=平台官方认证且主体是议题当事方；"
        "institutional_primary=具名机构的一手披露；named_secondary=具名二手报道、分析或评测；"
        "community_high_signal=社区热度达批内 P75 以上或作者历史可查；"
        "anonymous_or_unverifiable=作者匿名或不可核验；content_farm=SEO 聚合或正文不支撑标题。"
        "作者信号不存在=anonymous_or_unverifiable，这是保守闭集结论，不是缺失分数。\n"
        "content_kind 判据：product_launch=产品发布；market_data=市场或运行数据；"
        "user_opinion=用户、社区观点；reference=现行文档、定价、许可或帮助页；"
        "industry_view=行业分析、新闻或其他观点。\n"
        "interest_relation 判据：arms_length=输入公开信号中无可见利益关系；"
        "disclosed_interest=官方自述或其他已披露利益关系；"
        "undisclosed_interest=利益关系明显但未披露。无可见利益关系=arms_length，"
        "只判定可见披露状态，不猜隐藏动机。\n"
        "只有输入连来源身份或可见关系都无法识别，仍无法落入任一闭集时，"
        "才将对应标签置 null 并登记诚实缺失；不得把已有保守闭集入口的情形写成缺失。\n"
        "输出顶层数组，顺序与输入一致，不要输出 Markdown。\n"
        f"必须把结果写到此精确路径：{output_path}。不得改用 output.json "
        "或其他文件名。\n"
        "输入证据：" + json.dumps(list(items), ensure_ascii=False, separators=(",", ":"))
    )


def _ctx(path: Path, report_id: str, goal_id: str) -> validation.Ctx:
    runs_root = path.parents[4]
    return validation.Ctx(
        output_path=path,
        output_format="json",
        research_id=report_id,
        goal_id=goal_id,
        agent_id=AGENT_ID,
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
        runs_root=runs_root,
    )


def _label_errors(value: Any, inputs: Sequence[Mapping[str, Any]]) -> list[str]:
    if not isinstance(value, list):
        return ["补评产物顶层必须是数组"]
    if len(value) != len(inputs):
        return [f"补评条数应为 {len(inputs)}，实际 {len(value)}"]
    errors: list[str] = []
    label_specs = (
        ("authority_kind", AUTHORITY_KINDS, "score_authority"),
        ("content_kind", CONTENT_KINDS, "score_freshness"),
        ("interest_relation", INTEREST_RELATIONS, "score_independence"),
    )
    for index, (item, source) in enumerate(zip(value, inputs)):
        if not isinstance(item, Mapping):
            errors.append(f"items[{index}] 必须是 object")
            continue
        if item.get("id") != source.get("id"):
            errors.append(f"items[{index}].id 与输入不一致")
        missing = item.get("missing_dimensions", {})
        if not isinstance(missing, Mapping):
            errors.append(f"items[{index}].missing_dimensions 必须是 object")
            continue
        unknown = set(missing) - LABEL_SCORE_FIELDS
        if unknown:
            errors.append(
                f"items[{index}].missing_dimensions 含本地计算维度或越界字段："
                f"{sorted(unknown)}"
            )
        for field, reason in missing.items():
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 14:
                errors.append(f"items[{index}].missing_dimensions.{field} 原因应为 1–14 字")
        for label, allowed, score_field in label_specs:
            selected = item.get(label)
            if selected is None:
                if score_field not in missing:
                    errors.append(f"items[{index}].{label} 为空但未登记缺失原因")
            elif selected not in allowed:
                errors.append(f"items[{index}].{label} 越界：{selected!r}")
            elif score_field in missing:
                errors.append(
                    f"items[{index}].{label} 标签已判定，不得同时登记 {score_field} 缺失"
                )
    return errors


def _conclusion_path(output_path: Path) -> Path:
    return output_path.parent / f".{AGENT_ID}-codex-last-message.json"


def _recover_transport_completion(
    result: Any, output_path: Path, conclusion_path: Path
) -> bool:
    """仅在 Codex 短暂传输告警后重验两条落盘腿，不信退出码。"""

    error = str(getattr(result, "engine_error", "") or "").casefold()
    if not any(marker in error for marker in _RECOVERABLE_TRANSPORT_MARKERS):
        return False
    if not output_path.is_file() or not conclusion_path.is_file():
        return False
    try:
        raw = conclusion_path.read_text(encoding="utf-8")
        value = json.loads(raw)
        wrapped = (
            "```json owli-result\n"
            f"{json.dumps(value, ensure_ascii=False)}\n"
            "```"
        )
        conclusion = parse_owli_result(wrapped)
    except (OSError, UnicodeError, json.JSONDecodeError, OwliResultError):
        return False
    actual = Path(conclusion.output_path).expanduser().resolve(strict=False)
    expected = output_path.resolve(strict=False)
    return (
        actual == expected
        and (
            conclusion.status == "done"
            or (conclusion.status == "partial" and bool(conclusion.unmet))
        )
    )


def _engine_input(item: Mapping[str, Any]) -> dict[str, Any]:
    meta = item.get("author_meta")
    author_meta = meta if isinstance(meta, Mapping) else {}
    extra_value = item.get("extra")
    extra = extra_value if isinstance(extra_value, Mapping) else {}
    signal_keys = (
        "artifact_source_type", "entity", "dimension",
        "authority_kind", "content_kind", "interest_relation",
    )
    return {
        "id": item.get("id"),
        "goal_id": item.get("goal_id"),
        "platform": item.get("platform"),
        "source_type": item.get("source_type"),
        "permalink": item.get("permalink"),
        "title": item.get("title"),
        "published_at": item.get("published_at"),
        "fetched_at": item.get("fetched_at"),
        "normalized_score": item.get("normalized_score"),
        "author_signals": {
            "present": bool(item.get("author_name")),
            "verified": bool(author_meta.get("verified")),
            "affiliation_present": bool(author_meta.get("affiliation")),
        },
        "signals": {
            key: extra[key] for key in signal_keys if extra.get(key) is not None
        },
    }


async def _firsthand_batch(
    pairs: Sequence[Mapping[str, Any]], *, adapter: Any, output_path: Path,
    report_id: str, goal_id: str, engine_preference: str | None,
) -> list[dict[str, Any]] | None:
    """§3.2 第 5 项的 auditor 判定；结构与 _classify_batch 同源，只换提示词与校验。"""

    errors: list[str] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    conclusion_path = _conclusion_path(output_path)
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        output_path.unlink(missing_ok=True)
        conclusion_path.unlink(missing_ok=True)
        body = _firsthand_prompt(pairs, output_path=output_path)
        if errors:
            body += "\n上一轮错误：" + "；".join(errors)
        task = EngineTask(
            body=body,
            output_path=output_path,
            output_format="json",
            research_id=report_id,
            goal_id=goal_id,
            agent_id=AGENT_ID,
            agent_kind="reliability_audit",
            validators=["file_exists"],
            user_override=engine_preference,
            runs_root=output_path.parents[4],
            capability=Capability(
                profile="readonly-analyst",
                tools=("fs.write",),
                fs=FileSystemScope(write=(f"goals/{goal_id}/**",)),
            ),
        )
        result = await adapter.run(
            task, _ctx(output_path, report_id, goal_id), on_event=None
        )
        if not bool(getattr(result, "succeeded", False)) and not _recover_transport_completion(
            result, output_path, conclusion_path
        ):
            errors = ["适配器双腿判定未通过"]
            continue
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors = [f"一手性审计产物无法解析：{type(exc).__name__}"]
            continue
        errors = _firsthand_errors(value, pairs)
        if not errors:
            return [dict(item) for item in value]
    return None


async def _audit_firsthand(
    store: Any, report: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *,
    adapter: Any, runs_root: Path, batch_size: int, engine_preference: str,
    force: bool, on_event: Any,
) -> dict[str, int]:
    """§XSEM-1 条 1：逐 (证据, 断言) 对把 §3.2 第 5 项的闸门交回 auditor。

    一手性是交叉维唯一闸门，改前由撰写方自己声明。引擎整批失败时**保留撰写方
    声明、不升级 firsthand_source**，也不回落成「全否」——全否会把交叉维直接
    打到零分，那是拿失败当结论。
    """

    report_id = str(report["id"])
    claims = _report_claims(report)
    pending = [claim for claim in claims if force or not _firsthand_settled(claim)]
    rows_by_id = {str(row["id"]): row for row in rows}
    pairs = _firsthand_pairs(pending, rows_by_id) if pending else []
    stats = {"pairs": len(pairs), "audited": 0, "failed": 0, "claims": 0}
    if not pairs:
        return stats

    verdicts: dict[tuple[str, str], dict[str, Any]] = {}

    def settle() -> None:
        """把已判到的 (断言, 证据) 对结算进 claims；缺一对的断言原样不改口。"""
        for claim in pending:
            claim_id = str(claim.get("id"))
            evidence_ids = [
                str(value) for value in (claim.get("evidence_ids") or [])
                if str(value) in rows_by_id
            ]
            keys = [(claim_id, evidence_id) for evidence_id in evidence_ids]
            if not keys or any(key not in verdicts for key in keys):
                continue          # 缺一对就整条不改口，撰写方声明原样留着。
            audit = {key[1]: verdicts[key] for key in keys}
            claim["firsthand_audit"] = audit
            claim["firsthand"] = [
                evidence_id for evidence_id in evidence_ids
                if audit[evidence_id]["firsthand"]
            ]
            claim["firsthand_source"] = "audited"
            stats["claims"] += 1
        if stats["claims"]:
            store.set_report_claims(report_id, claims)

    try:
        await _audit_firsthand_batches(
            pairs, verdicts, stats, adapter=adapter, runs_root=runs_root,
            batch_size=batch_size, engine_preference=engine_preference,
            report_id=report_id, on_event=on_event,
        )
    except asyncio.CancelledError:
        # §D-043：/stop 掐进来时，已经付过钱判完的批次照常结算落库——
        # 「缺一对就整条不改口」那条规矩本来就守着一致性，丢掉才是白烧。
        settle()
        raise
    settle()
    return stats


async def _audit_firsthand_batches(
    pairs: Sequence[Mapping[str, Any]],
    verdicts: dict[tuple[str, str], dict[str, Any]],
    stats: dict[str, int],
    *, adapter: Any, runs_root: Path, batch_size: int, engine_preference: str,
    report_id: str, on_event: Any,
) -> None:
    """逐 goal 逐批把一手性判读灌进 `verdicts`（取消时上抛，结算交调用方）。"""
    for goal_id in sorted({str(pair["goal_id"]) for pair in pairs}):
        goal_pairs = [pair for pair in pairs if str(pair["goal_id"]) == goal_id]
        batch_total = (len(goal_pairs) + batch_size - 1) // batch_size
        for batch_number, start in enumerate(
            range(0, len(goal_pairs), batch_size), 1
        ):
            batch = goal_pairs[start:start + batch_size]
            batch_started = time.monotonic()
            audited = await _firsthand_batch(
                batch, adapter=adapter,
                output_path=_batch_output_path(
                    runs_root, report_id, goal_id, batch_number,
                    folder="firsthand-audit",
                ),
                report_id=report_id, goal_id=goal_id,
                engine_preference=engine_preference,
            )
            if audited is None:
                stats["failed"] += len(batch)
            else:
                stats["audited"] += len(audited)
                for item in audited:
                    verdicts[(str(item["claim_id"]), str(item["evidence_id"]))] = {
                        "firsthand": bool(item["firsthand"]),
                        "reason": str(item["reason"]).strip(),
                    }
            if on_event is not None:
                event = on_event({
                    "type": "firsthand_audit_progress",
                    "data": {
                        "report_id": report_id, "goal_id": goal_id,
                        "batch_number": batch_number, "batch_total": batch_total,
                        "batch_pairs": len(batch),
                        "audited_total": stats["audited"],
                        "failed_total": stats["failed"],
                        "batch_seconds": round(time.monotonic() - batch_started, 3),
                    },
                })
                if inspect.isawaitable(event):
                    await event


async def _classify_batch(
    items: Sequence[Mapping[str, Any]], *, adapter: Any, output_path: Path,
    report_id: str, goal_id: str, engine_preference: str | None,
) -> list[dict[str, Any]] | None:
    errors: list[str] = []
    compact = [_engine_input(item) for item in items]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    conclusion_path = _conclusion_path(output_path)
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        output_path.unlink(missing_ok=True)
        conclusion_path.unlink(missing_ok=True)
        body = _prompt(compact, output_path=output_path)
        if errors:
            body += "\n上一轮错误：" + "；".join(errors)
        task = EngineTask(
            body=body,
            output_path=output_path,
            output_format="json",
            research_id=report_id,
            goal_id=goal_id,
            agent_id=AGENT_ID,
            agent_kind="reliability_audit",
            validators=["file_exists"],
            user_override=engine_preference,
            runs_root=output_path.parents[4],
            capability=Capability(
                profile="readonly-analyst",
                tools=("fs.write",),
                fs=FileSystemScope(write=(f"goals/{goal_id}/**",)),
            ),
        )
        result = await adapter.run(
            task, _ctx(output_path, report_id, goal_id), on_event=None
        )
        if not bool(getattr(result, "succeeded", False)) and not _recover_transport_completion(
            result, output_path, conclusion_path
        ):
            errors = ["适配器双腿判定未通过"]
            continue
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors = [f"补评产物无法解析：{type(exc).__name__}"]
            continue
        errors = _label_errors(value, compact)
        if not errors:
            return [dict(item) for item in value]
    return None


def _metric_enriched(item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    raw_metrics = dict(result.get("raw_metrics") or {})
    extra = result.get("extra") if isinstance(result.get("extra"), Mapping) else {}
    metric = PRIMARY_METRICS.get(str(result.get("platform")))
    if metric and metric not in raw_metrics and isinstance(extra.get(metric), (int, float)):
        raw_metrics[metric] = extra[metric]
    result["raw_metrics"] = raw_metrics
    return result


def _crossref_verdict(extra: Mapping[str, Any]) -> str | None:
    verdict = extra.get("crossref_verdict")
    count = extra.get("crossref_n_clusters")
    claim_ids = extra.get("claim_ids")
    if (
        verdict in CROSSREF_SCORES
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 1
        and isinstance(claim_ids, list)
        and bool(claim_ids)
        and all(isinstance(value, str) and value.strip() for value in claim_ids)
    ):
        return str(verdict)
    return None


def _scoring_view(
    item: Mapping[str, Any], label: Mapping[str, Any],
    *, engagement_percentile: float | None = None,
) -> dict[str, Any]:
    result = dict(item)
    # §RATE-4 货 1：批内互动量分位只在打分时用，不进回写载荷（`evidence` 没有
    # 这一列）；载荷是从 `item` 起的，所以挂在这份视图上就够。
    if engagement_percentile is not None:
        result["engagement_percentile"] = engagement_percentile
    extra = dict(result.get("extra") or {})
    for key in ("authority_kind", "content_kind", "interest_relation"):
        if label.get(key) is not None:
            extra[key] = label[key]
        else:
            extra.pop(key, None)
    if _crossref_verdict(extra) is None:
        extra.pop("crossref_verdict", None)
    result["extra"] = extra
    author_meta_value = result.get("author_meta")
    author_meta = author_meta_value if isinstance(author_meta_value, Mapping) else {}
    reachability = extra.get("permalink_reachable")
    author_history = extra.get(
        "author_history_verified", author_meta.get("history_verified")
    )
    has_body = bool(
        result.get("content_excerpt")
        or extra.get("evidence")
        or extra.get("facts")
    )
    result.update(
        has_body=has_body,
        summary_sufficient=has_body,
        permalink_reachable=(reachability if isinstance(reachability, bool) else None),
        author_history_verified=(
            author_history if isinstance(author_history, bool) else None
        ),
    )
    return result


def _safe_component(value: str, field: str) -> str:
    if value in {".", ".."} or _SAFE_PATH_COMPONENT.fullmatch(value) is None:
        raise ValueError(f"{field} 不是安全路径身份：{value!r}")
    return value


def _batch_output_path(
    runs_root: Path, report_id: str, goal_id: str, batch_number: int,
    *, folder: str = "reliability-backfill",
) -> Path:
    safe_report = _safe_component(report_id, "report_id")
    safe_goal = _safe_component(goal_id, "goal_id")
    safe_folder = _safe_component(folder, "folder")
    root = runs_root.resolve(strict=False)
    research_root = (root / safe_report).resolve(strict=False)
    goal_root = (research_root / "goals" / safe_goal).resolve(strict=False)
    output_path = (
        goal_root / safe_folder / f"batch-{batch_number:03d}.json"
    ).resolve(strict=False)
    if (
        not research_root.is_relative_to(root)
        or not goal_root.is_relative_to(research_root)
        or not output_path.is_relative_to(goal_root)
    ):
        raise ValueError("补评产物路径越界")
    return output_path


def _normalize_report(items: Sequence[Mapping[str, Any]], computed_at: str) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    goal_ids = sorted({str(item.get("goal_id") or "goal-1") for item in items})
    for goal_id in goal_ids:
        group = [
            _metric_enriched(item) for item in items
            if str(item.get("goal_id") or "goal-1") == goal_id
        ]
        for item in normalize_evidence_metrics(
            group,
            computed_at=computed_at,
            report_id=str(group[0]["report_id"]),
            goal_id=goal_id,
        ):
            normalized[str(item["id"])] = item
    return normalized


def _stored_labels(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    labels: list[dict[str, Any]] = []
    specifications = (
        ("authority_kind", AUTHORITY_KINDS, "score_authority", 2),
        ("content_kind", CONTENT_KINDS, "score_freshness", 4),
        ("interest_relation", INTEREST_RELATIONS, "score_independence", 10),
    )
    for item in items:
        extra_value = item.get("extra")
        extra = extra_value if isinstance(extra_value, Mapping) else {}
        notes = item.get("rating_notes")
        matched_notes = (
            RATING_NOTES_PATTERN.fullmatch(notes)
            if isinstance(notes, str)
            else None
        )
        label: dict[str, Any] = {
            "id": item.get("id"), "missing_dimensions": {},
        }
        for field, allowed, score_field, reason_group in specifications:
            value = extra.get(field)
            if value in allowed:
                label[field] = value
                continue
            if (
                item.get(score_field) is None
                and matched_notes is not None
                and matched_notes.group(reason_group - 1) == "?"
            ):
                label[field] = None
                label["missing_dimensions"][score_field] = matched_notes.group(
                    reason_group
                )
                continue
            return None
        labels.append(label)
    return labels


def _rating_provenance(
    item: Mapping[str, Any], *, used_engine: bool, engine_preference: str
) -> str:
    """区分本轮真实引擎判定、旧引擎产物与纯本地重算，避免伪造来源。"""

    canonical = f"agent:{AGENT_ID}@"
    if used_engine:
        return f"{canonical}{engine_preference}"
    existing = str(item.get("rated_by") or "")
    if existing.startswith(canonical):
        return existing
    legacy = "agent:reliability-backfill@"
    if existing.startswith(legacy):
        routed = existing.removeprefix(legacy)
        if routed in {"claude", "codex"}:
            return f"{canonical}{routed}"
    if existing:
        return existing
    return "rule:reliability-backfill@v1"


def _resolve_report_path(report: Mapping[str, Any], runs_root: Path) -> Path | None:
    value = report.get("report_path")
    if not value:
        return None
    raw = Path(str(value))
    root = runs_root.resolve(strict=False)
    allowed = (root / _safe_component(str(report["id"]), "report_id")).resolve()
    if not allowed.is_relative_to(root):
        raise ValueError("报告产物根目录越界")
    candidates = [raw] if raw.is_absolute() else [
        runs_root.parent / raw,
        runs_root / raw,
        runs_root / str(report["id"]) / raw,
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(allowed) and resolved.is_file():
            return resolved
    return None


def _summary_line(markdown: str) -> str | None:
    in_conclusion = False
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            in_conclusion = heading.group(1).strip() == "结论"
            continue
        if not in_conclusion:
            continue
        matched = _BULLET.match(line)
        if matched is None:
            continue
        text = _MARK.sub("", matched.group(1))
        text = _LINK.sub(r"\1", text)
        text = re.sub(r"[`*_]", "", text)
        text = " ".join(text.split()).strip("。；; ")
        if text:
            return text[:120]
    return None


def _source_entries(markdown: str) -> list[tuple[int, str]]:
    """兼容历史报告的三种来源行，返回角标与首个 HTTP(S) 链接。"""

    entries: list[tuple[int, str]] = []
    in_sources = False
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            in_sources = "信息源" in heading.group(1)
            continue
        if not in_sources:
            continue
        mark = _MARK.search(line)
        url = _URL.search(line)
        if mark is None or url is None:
            continue
        entries.append((int(mark.group("number")), normalize_permalink(url.group(1))))
    return entries


def _replace_source_section(markdown: str, rendered: str) -> str:
    lines = markdown.splitlines()
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading and "信息源" in heading.group(1):
            start = index + 1
            continue
        if start is not None and heading:
            end = index
            break
    if start is None:
        return markdown
    replacement = ["", *rendered.splitlines(), ""]
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join([*lines[:start], *replacement, *lines[end:]]).rstrip() + suffix


def _citation_mark_sets(markdown: str) -> tuple[set[int], set[int]]:
    body_marks: set[int] = set()
    source_marks: set[int] = set()
    in_sources = False
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            in_sources = "信息源" in heading.group(1)
            continue
        target = source_marks if in_sources else body_marks
        target.update(int(value) for value in _MARK.findall(line))
    return body_marks, source_marks


def _assert_body_citations_resolvable(markdown: str) -> set[int]:
    body_marks, source_marks = _citation_mark_sets(markdown)
    unresolved = body_marks - source_marks
    if unresolved:
        raise ValueError(
            "报告正文存在无法解析角标："
            f"{sorted(unresolved)}"
        )
    return body_marks


def _assert_citation_bijection(markdown: str) -> None:
    """拒绝正文无法解析角标和来源孤立角标，避免重编号改变语义。"""

    body_marks, source_marks = _citation_mark_sets(markdown)
    if body_marks != source_marks:
        raise ValueError(
            "报告正文与信息源角标不双向："
            f"body={sorted(body_marks)}, sources={sorted(source_marks)}"
        )


def _rendered_evidence(
    evidence_by_url: Mapping[str, Mapping[str, Any]],
    urls: Sequence[str],
    citations: Mapping[str, int],
) -> str:
    items = [
        {**dict(evidence_by_url[url]), "citation_no": citations[url]}
        for url in urls
    ]
    return render_source_list(items)


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{AGENT_ID}.tmp")
    try:
        temporary.unlink(missing_ok=True)
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_artifact_then_citations(
    store: Any,
    report_id: str,
    path: Path,
    *,
    original_text: str,
    rewritten_text: str,
    citations: Mapping[str, int],
) -> None:
    _atomic_write(path, rewritten_text)
    try:
        store.replace_evidence_citations(report_id, dict(citations))
    except Exception:
        _atomic_write(path, original_text)
        raise


def _sync_report_artifact(
    store: Any, report_id: str, path: Path
) -> tuple[int, str | None]:
    evidence = store.list_evidence(report_id)
    stored_urls = {str(item["permalink"]) for item in evidence}
    evidence_by_url = {str(item["permalink"]): item for item in evidence}
    original_text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".json":
        text = original_text
        body_marks = _assert_body_citations_resolvable(text)
        entries = [
            entry for entry in _source_entries(text) if entry[0] in body_marks
        ]
        citations = {
            url: number for number, url in entries if url in stored_urls
        }
        text = _replace_source_section(
            text,
            _rendered_evidence(
                evidence_by_url, list(citations), citations
            ),
        )
        _assert_citation_bijection(text)
        _write_artifact_then_citations(
            store,
            report_id,
            path,
            original_text=original_text,
            rewritten_text=text,
            citations=citations,
        )
        return len(citations), _summary_line(text)

    document = json.loads(original_text)
    sections = document.get("sections") if isinstance(document, Mapping) else None
    if not isinstance(sections, list):
        return 0, None
    global_citations: dict[str, int] = {}
    summaries: list[str] = []
    section_urls: list[list[str]] = []
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("markdown"), str):
            section_urls.append([])
            continue
        markdown = section["markdown"]
        body_marks = _assert_body_citations_resolvable(markdown)
        local = [
            entry
            for entry in _source_entries(markdown)
            if entry[0] in body_marks
        ]
        local_marks: dict[int, int] = {}
        urls: list[str] = []
        for local_number, permalink in local:
            if permalink not in stored_urls:
                raise KeyError(f"报告引用未入库：{permalink}")
            global_number = global_citations.setdefault(
                permalink, len(global_citations) + 1
            )
            local_marks.setdefault(local_number, global_number)
            if permalink not in urls:
                urls.append(permalink)
        section_urls.append(urls)
        section["markdown"] = _MARK.sub(
            lambda matched: f"[S{local_marks.get(int(matched.group('number')), int(matched.group('number'))):02d}]",
            markdown,
        )
        summary = _summary_line(section["markdown"])
        if summary:
            summaries.append(summary)
    for section, urls in zip(sections, section_urls):
        if not isinstance(section, dict) or not isinstance(section.get("markdown"), str):
            continue
        if urls:
            section["markdown"] = _replace_source_section(
                section["markdown"],
                _rendered_evidence(
                    evidence_by_url, urls, global_citations
                ),
            )
        _assert_citation_bijection(section["markdown"])
    rewritten = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    _write_artifact_then_citations(
        store,
        report_id,
        path,
        original_text=original_text,
        rewritten_text=rewritten,
        citations=global_citations,
    )
    return len(global_citations), summaries[0] if summaries else None


def _scored_payloads(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], bool]],
    engine_preference: str,
    *, percentiles: Mapping[str, float] | None = None,
    keep_crossref: bool = False,
) -> list[dict[str, Any]]:
    """(证据, 标签, 是否本轮引擎判定) → 五维回写载荷；口径与拆分前逐字相同。"""

    pool = dict(percentiles or {})
    payloads: list[dict[str, Any]] = []
    for item, label, used_engine in pairs:
        scoring_view = _scoring_view(
            item, label, engagement_percentile=pool.get(str(item.get("id"))),
        )
        missing_dimensions = dict(label.get("missing_dimensions") or {})
        crossref_verdict = _crossref_verdict(scoring_view["extra"])
        cluster_stats = None
        baseline = None
        if crossref_verdict in CROSSREF_SCORES:
            cluster_stats = {"verdict": crossref_verdict}
        elif keep_crossref and isinstance(item.get("score_crossref"), int):
            # §RATE-4 货 2：换尺子那一轮只改第一维。这些行的交叉维是写作前评级章
            # 按平台基线填的（全库 477 行 extra 里根本没有 crossref_verdict），
            # 补评口径会把它们诚实改成 NULL——那不是重算分，那是替另一维重下判断，
            # 而 grade 一旦变 NULL，D 闸和池排序就跟着塌。原值原样留着。
            baseline = {
                **PLATFORM_BASELINES.get(
                    str(item.get("platform") or ""),
                    PLATFORM_BASELINES["web_search"],
                ),
                "score_crossref": int(item["score_crossref"]),
            }
        else:
            missing_dimensions["score_crossref"] = "缺断言血缘簇"
        scored = score_evidence_partial(
            scoring_view,
            missing_dimensions=missing_dimensions,
            baseline=baseline,
            cluster_stats=cluster_stats,
        )
        payload = {
            key: value for key, value in item.items()
            if key not in {"score_total", "grade"}
        }
        payload.update(scored)
        payload["extra"] = scoring_view["extra"]
        payload["rated_by"] = _rating_provenance(
            item, used_engine=used_engine, engine_preference=engine_preference,
        )
        payloads.append(payload)
    return payloads


def _wants_representativeness(item: Mapping[str, Any]) -> bool:
    """这一行该用「代表性」尺子，但库里那条理由还是「权威」写法。"""

    extra = item.get("extra") if isinstance(item.get("extra"), Mapping) else {}
    if extra.get("content_kind") != "user_opinion":
        return False
    if extra.get("authority_kind") == "content_farm":
        return False
    return not str(item.get("rating_notes") or "").startswith("代表性")


def _already_agent_rated(item: Mapping[str, Any]) -> bool:
    """§RATE-1 货 5：写作前的评级章已经逐条评过、五维齐全的行，收尾不再重评。

    收尾回填从此只是**兜底**：补写作前没评到的那部分。此前入选条件完全不看
    `rated_by`——claims 为空时「crossref 缺」几乎把全表圈进来，X-1 那轮 623 条
    重评了 103 分钟。交叉维缺失本身由 `_backfill_claim_clusters` 在上面的本地
    路径已经算过一遍，不需要再过一次引擎。
    """
    return (
        str(item.get("rated_by") or "").startswith("agent:")
        and all(item.get(field) is not None for field in SCORE_FIELDS)
    )


async def backfill_report(
    store: Any,
    report_id: str,
    *,
    adapter: Any,
    runs_root: str | Path,
    batch_size: int = 25,
    force: bool = False,
    engine_preference: str = "claude",
    on_event: Any = None,
    rescore_only: bool = False,
) -> BackfillResult:
    """对单报告做可重复补评；引擎失败的批次保持原 NULL，不写平台基线。

    `rescore_only`：只拿库里已有的闭集标签把五维重算一遍，一次引擎都不过
    （§RATE-4 货 2）。评分口径改了之后要给既有语料换尺子，标签本身没变，
    没有理由再付一遍判定的钱。没有可复用标签的行本轮不动，也不进 attempted。
    """

    if not 1 <= batch_size <= 50:
        raise ValueError("补评 batch_size 必须在 1–50 之间")
    if rescore_only and force:
        raise ValueError("rescore_only 与 force 互斥：只重算分就不会过引擎")
    if engine_preference not in {"claude", "codex"}:
        raise ValueError("补评 engine_preference 只能是 claude 或 codex")
    _safe_component(report_id, "report_id")
    report = store.get_report(report_id)
    if report is None:
        raise KeyError(f"报告不存在：{report_id}")
    rows = store.list_evidence(report_id)
    before_rows = len(rows)
    # §XSEM-1 条 1：一手性审计必须排在簇计算之前——它是 §3.2 五项里唯一的闸门，
    # 而 §3.2 又明说同两条证据在不同断言上结论可以不同，所以只能逐 (证据, 断言)
    # 对判，也就只能等断言登记之后（评级章跑在断言产生之前，放不下这一步）。
    firsthand_audit = (
        {"pairs": 0, "audited": 0, "failed": 0, "claims": 0}
        if rescore_only
        else await _audit_firsthand(
            store, report, rows, adapter=adapter, runs_root=Path(runs_root),
            batch_size=batch_size, engine_preference=engine_preference,
            force=force, on_event=on_event,
        )
    )
    if firsthand_audit["claims"]:
        report = store.get_report(report_id) or report
    rows, clustered_ids = _backfill_claim_clusters(store, report, rows)
    report = store.get_report(report_id) or report
    computed_at = str(report.get("completed_at") or max(
        (str(item.get("fetched_at") or "") for item in rows), default=""
    ))
    normalized = _normalize_report(rows, computed_at) if rows else {}
    # §RATE-4 货 1：分位要按**整份研究**的平台池算，不能按 25 条一批算——
    # 同一条证据在不同批里会得到不同分位，两次补评就对不上（D-013 判据）。
    percentiles = engagement_percentiles(normalized.values())
    restale = {
        str(item["id"]) for item in rows
        if str(item["id"]) in percentiles and _wants_representativeness(item)
    }
    targets = [
        item for item in rows
        # §RATE-4 货 2：只重算分的一轮，凡是库里有闭集标签的行都要重算——
        # 换尺子改的是分不是标签，没标签的行本轮不动（它们要过引擎，另说）。
        if _stored_labels([item]) is not None
    ] if rescore_only else [
        item for item in rows
        if force or (
            not _already_agent_rated(item)
            and (
                str(item.get("id")) in clustered_ids
                or any(item.get(field) is None for field in SCORE_FIELDS)
                or _crossref_verdict(
                    item.get("extra") if isinstance(item.get("extra"), Mapping) else {}
                ) is None
                # 评级章按闭集打过的 UGC 行五维是齐的，`_already_agent_rated`
                # 会把它们挡在外面——那样代表性尺子在真跑里永远落不到库上。
                or str(item["id"]) in restale
            )
        )
    ]
    rated = 0
    failed = 0
    root = Path(runs_root)
    target_goals = sorted({str(item.get("goal_id") or "goal-1") for item in targets})
    for goal_id in target_goals:
        goal_targets = [
            normalized[str(item["id"])] for item in targets
            if str(item.get("goal_id") or "goal-1") == goal_id
        ]
        # §X-1 货 1b：先分「本地可复用」与「要进引擎」，只有后者切批——
        # 此前按全部 targets 每 25 条切，再挑没标签的送引擎，26 条会散成 7 次调用。
        reusable: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
        pending: list[dict[str, Any]] = []
        for item in goal_targets:
            stored = None if force else _stored_labels([item])
            assert not (rescore_only and stored is None), "只重算分的一轮不该有待判行"
            if stored is None:
                pending.append(item)
            else:
                reusable.append((item, stored[0], False))
        if reusable:
            payloads = _scored_payloads(
                reusable, engine_preference, percentiles=percentiles,
                keep_crossref=rescore_only,
            )
            store.upsert_evidence_batch(payloads)
            rated += len(payloads)
        batch_total = (len(pending) + batch_size - 1) // batch_size
        for batch_number, start in enumerate(range(0, len(pending), batch_size), 1):
            batch = pending[start:start + batch_size]
            output_path = _batch_output_path(root, report_id, goal_id, batch_number)
            batch_started = time.monotonic()
            engine_labels = await _classify_batch(
                batch, adapter=adapter, output_path=output_path,
                report_id=report_id, goal_id=goal_id,
                engine_preference=engine_preference,
            )
            if engine_labels is None:
                failed += len(batch)
            else:
                payloads = _scored_payloads(
                    [(item, label, True) for item, label in zip(batch, engine_labels)],
                    engine_preference, percentiles=percentiles,
                    keep_crossref=rescore_only,
                )
                store.upsert_evidence_batch(payloads)
                rated += len(payloads)
            # §OBS-1 货 2：每批完成发进度事件（成功失败都发；只加事件不改语义）。
            if on_event is not None:
                progress_event = on_event({
                    "type": "reliability_backfill_progress",
                    "data": {
                        "report_id": report_id,
                        "goal_id": goal_id,
                        "batch_number": batch_number,
                        "batch_total": batch_total,
                        "batch_rows": len(batch),
                        "rated_total": rated,
                        "failed_total": failed,
                        "batch_seconds": round(time.monotonic() - batch_started, 3),
                    },
                })
                if inspect.isawaitable(progress_event):
                    await progress_event

    # §XSEM-1 条 3：本轮刚写上的四维实值会让簇的先验等级变一次。同一次调用里再收敛
    # 一轮——重算簇、按新 verdict 重打交叉维——否则「补评两遍逐字段 0 差异」（D-013
    # 判据）会在「第一遍才评上分」这条路径上先红一次。四维自身此后不再变，故一轮即定；
    # 收敛轮只用库存标签本地重算，不再过引擎。§3.5 要求的「先四维、后 crossref」
    # 由此才真正成立——此前簇计算跑在同一次调用的评分之前。
    if rated and clustered_ids:
        report = store.get_report(report_id) or report
        rows, clustered_ids = _backfill_claim_clusters(
            store, report, store.list_evidence(report_id)
        )
        settled = _normalize_report(rows, computed_at) if rows else {}
        resettle = [
            settled[str(item["id"])] for item in rows
            if str(item["id"]) in clustered_ids and str(item["id"]) in settled
        ]
        settled_labels = _stored_labels(resettle) if resettle else None
        if settled_labels is not None:
            store.upsert_evidence_batch(_scored_payloads(
                [
                    (item, label, False)
                    for item, label in zip(resettle, settled_labels)
                ],
                engine_preference, percentiles=percentiles,
                keep_crossref=rescore_only,
            ))

    refreshed_report = store.get_report(report_id) or report
    report_path = _resolve_report_path(refreshed_report, root)
    citations = 0
    summary = None
    if report_path is not None and failed == 0:
        citations, summary = _sync_report_artifact(store, report_id, report_path)
        if summary and refreshed_report.get("status") == "completed":
            store.finish_report(
                report_id,
                status="completed",
                completed_at=str(refreshed_report.get("completed_at") or computed_at),
                summary_line=summary,
            )

    after = store.list_evidence(report_id)
    complete_rows = sum(
        all(item.get(field) is not None for field in SCORE_FIELDS) for item in after
    )
    complete_cells = sum(
        item.get(field) is not None for item in after for field in SCORE_FIELDS
    )
    claims_value = (
        (store.get_report(report_id) or {}).get("extra", {}).get("claims", [])
    )
    after_by_id = {str(item["id"]): item for item in after}
    weak_claims = []
    for claim in claims_value if isinstance(claims_value, list) else []:
        if not isinstance(claim, Mapping) or not isinstance(claim.get("id"), str):
            continue
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            weak_claims.append(str(claim["id"]))
            continue
        grades = [
            after_by_id[str(evidence_id)].get("grade")
            for evidence_id in evidence_ids
            if str(evidence_id) in after_by_id
            and isinstance(after_by_id[str(evidence_id)].get("grade"), str)
        ]
        if not claim_support_is_valid(grades):
            weak_claims.append(str(claim["id"]))
    return BackfillResult(
        report_id=report_id,
        before_rows=before_rows,
        after_rows=len(after),
        attempted=len(targets),
        rated=rated,
        failed=failed,
        complete_rows=complete_rows,
        complete_cells=complete_cells,
        total_cells=len(after) * len(SCORE_FIELDS),
        citations=citations,
        summary_line=summary,
        weak_claims=weak_claims,
    )


__all__ = ["BackfillResult", "backfill_report"]
