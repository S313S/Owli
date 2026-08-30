"""入库后独立补评：读 Store、批量审计、幂等 upsert，并把报告角标落回 Store。"""

from __future__ import annotations

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
    PRIMARY_METRICS,
    RATING_NOTES_PATTERN,
    SCORE_FIELDS,
    normalize_evidence_metrics,
    claim_support_is_valid,
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
    # 簇判定先于本轮五维实值回写。上一轮补评的生成列与
    # 五维分不得成为下一轮 _grade() 的输入，否则会从平台基线漂移。
    for key in (*SCORE_FIELDS, "score_total", "grade"):
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
    item["firsthand_by_claim"] = {
        claim_id: evidence_id in firsthand
        if isinstance(firsthand, list)
        else False
    }
    origins = claim.get("origin_overrides")
    if isinstance(origins, Mapping) and origins.get(evidence_id):
        item["explicit_origin_by_claim"] = {
            claim_id: origins[evidence_id]
        }
    return item


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


def _scoring_view(item: Mapping[str, Any], label: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
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
    runs_root: Path, report_id: str, goal_id: str, batch_number: int
) -> Path:
    safe_report = _safe_component(report_id, "report_id")
    safe_goal = _safe_component(goal_id, "goal_id")
    root = runs_root.resolve(strict=False)
    research_root = (root / safe_report).resolve(strict=False)
    goal_root = (research_root / "goals" / safe_goal).resolve(strict=False)
    output_path = (
        goal_root / "reliability-backfill" / f"batch-{batch_number:03d}.json"
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
) -> list[dict[str, Any]]:
    """(证据, 标签, 是否本轮引擎判定) → 五维回写载荷；口径与拆分前逐字相同。"""

    payloads: list[dict[str, Any]] = []
    for item, label, used_engine in pairs:
        scoring_view = _scoring_view(item, label)
        missing_dimensions = dict(label.get("missing_dimensions") or {})
        crossref_verdict = _crossref_verdict(scoring_view["extra"])
        cluster_stats = None
        if crossref_verdict in CROSSREF_SCORES:
            cluster_stats = {"verdict": crossref_verdict}
        else:
            missing_dimensions["score_crossref"] = "缺断言血缘簇"
        scored = score_evidence_partial(
            scoring_view,
            missing_dimensions=missing_dimensions,
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
) -> BackfillResult:
    """对单报告做可重复补评；引擎失败的批次保持原 NULL，不写平台基线。"""

    if not 1 <= batch_size <= 50:
        raise ValueError("补评 batch_size 必须在 1–50 之间")
    if engine_preference not in {"claude", "codex"}:
        raise ValueError("补评 engine_preference 只能是 claude 或 codex")
    _safe_component(report_id, "report_id")
    report = store.get_report(report_id)
    if report is None:
        raise KeyError(f"报告不存在：{report_id}")
    rows = store.list_evidence(report_id)
    before_rows = len(rows)
    rows, clustered_ids = _backfill_claim_clusters(store, report, rows)
    report = store.get_report(report_id) or report
    computed_at = str(report.get("completed_at") or max(
        (str(item.get("fetched_at") or "") for item in rows), default=""
    ))
    normalized = _normalize_report(rows, computed_at) if rows else {}
    targets = [
        item for item in rows
        if force or (
            not _already_agent_rated(item)
            and (
                str(item.get("id")) in clustered_ids
                or any(item.get(field) is None for field in SCORE_FIELDS)
                or _crossref_verdict(
                    item.get("extra") if isinstance(item.get("extra"), Mapping) else {}
                ) is None
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
            if stored is None:
                pending.append(item)
            else:
                reusable.append((item, stored[0], False))
        if reusable:
            payloads = _scored_payloads(reusable, engine_preference)
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
                    engine_preference,
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
