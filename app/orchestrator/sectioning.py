"""执行期判断章节化：短调用落节文件，确定性拼装父章产物。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app.adapters import validation
from app.adapters.contracts import EngineTask
from app.adapters.ratelimit import classify_transport_error
from app.orchestrator.chapter_failure import (
    chapter_failure_reason as section_failure_reason,
)
from app.orchestrator.scheduler import CHAPTER_RETRY_INTERVAL_SECONDS, TaskRunResult
from app.report.markdown import merge_sectioned_markdown


SECTIONED_KINDS = {"cross_validation", "summary", "report", "report_writing"}

#: 节级传输断连可重试的唯一闭集 reason。其余闭集原因（quota_exhausted /
#: tool_unavailable / timeout / conclusion_invalid / empty_result）行为不变。
SECTION_RETRYABLE_REASON = "retry_exhausted"


def should_section(kind: str, output_format: str) -> bool:
    return kind in SECTIONED_KINDS and output_format in {"markdown", "json"}


#: 节级重试次数上限：独立常量，不再沿用 `retry_policy.max_attempts_per_round`
#: （D-008 期望 c —— 那个值是章级轮内次数，默认 10，节级照抄会把一章拖成黑洞）。
SECTION_RETRY_MAX_ATTEMPTS = 3

#: 上一轮基线整跑 `r-0f92790bfe37` 中，全部 done 且只在同父章内相邻作差的
#: 撰写节耗时为 136 / 159 / 201 / 213 / 244 / 248 / 267 / 275 / 283 秒；
#: 最小值 136 s = `goal-1/ch-4/sec-2`（report-writing），首节无可靠起点不硬算。
#: 唯一的真实 resume 样本（`r-6215aa582053`/`goal-3/ch-4/sec-3`）跑满约 336 s 未完成，
#: 预算内重试大概率救不回；136 s 只保证不做明显跑不完的重试。
#: 改这个数必须带新的实测来源，优先等一个 done 的 resume 样本。
SECTION_RESUME_COST_FLOOR_SECONDS = 136.0

EVIDENCE_POOL_LIMIT = 99
#: 四个对比主体分在两个采集 goal 里；每个非空 goal 至少要拿到足以支撑
#: 一节论证的 20 个号。空 goal 不占保底额，保底总量超过 S01-S99 时按比例退化。
EVIDENCE_POOL_GOAL_FLOOR = 20
SECTION_EVIDENCE_POOL_LIMIT = 30
_EVIDENCE_SCORE_FIELDS = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence",
)
_HTTP_URL = re.compile(
    r"https?://[^\s<>\"'()（）\[\]{}，。；：！？]+",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


class SectionAssemblyShapeError(ValueError):
    """确定性的组装失败：节产物形状与章声明的 shape 对不上。"""


class SectionWallClockExpired(TimeoutError):
    """单节墙钟到点；与适配器自身抛出的 TimeoutError 分开。"""


def _section_attempt_budget() -> int:
    return SECTION_RETRY_MAX_ATTEMPTS


def _declared_shape(agent: Any) -> str:
    output = getattr(agent, "output", None)
    if isinstance(output, Mapping):
        return str(output.get("shape", "") or "").strip().casefold()
    return ""


def _is_transport_failure(result: Any, reason: str) -> bool:
    """传输断连（socket 断开这类）才退避重试；限流 / 超时 / 结论不合法都不算。

    先看归一后的 reason —— `classify_rate_limit` 与超时兜底都排在传输判定之前，
    所以 reason 落到 retry_exhausted 时才轮得到传输判定，真 429 不会被误吞。
    """

    if reason != SECTION_RETRYABLE_REASON:
        return False
    engine_error = str(getattr(result, "engine_error", "") or "")
    conclusion_error = str(getattr(result, "conclusion_error", "") or "")
    errors = " ".join(filter(None, (engine_error, conclusion_error)))
    return bool(errors) and classify_transport_error(errors)


async def _wait_before_section_retry(timer: Any, delay: float) -> None:
    """退避沿用章级口径（`CHAPTER_RETRY_INTERVAL_SECONDS[scale]`）。"""

    if delay <= 0:
        return
    if timer is None:
        await asyncio.sleep(delay)
        return
    ready = asyncio.get_running_loop().create_future()

    def release() -> None:
        if not ready.done():
            ready.set_result(None)

    timer(delay, release)
    await ready


async def _emit_section_retry(
    on_event: Any,
    *,
    context: Any,
    section: Mapping[str, Any],
    attempt: int,
    resume: bool,
    session_id: str | None,
) -> None:
    event_result = on_event({
        "type": "section_retry",
        "data": {
            "goal_id": context.goal_id,
            "chapter_id": section["section_id"],
            "attempt": attempt,
            "resume": resume,
            "session_id": session_id,
        },
        "is_error": False,
    })
    if inspect.isawaitable(event_result):
        await event_result


async def _run_before_section_deadline(
    adapter: Any,
    task: EngineTask,
    ctx: Any,
    on_event: Any,
    deadline: float | None,
) -> Any:
    """在单节绝对墙钟内完成一次引擎调用；到点会取消进行中的调用。"""

    async def invoke() -> Any:
        result = adapter.run(task, ctx, on_event=on_event)
        return await result if inspect.isawaitable(result) else result

    if deadline is None:
        return await invoke()
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise SectionWallClockExpired
    run = asyncio.create_task(invoke())
    try:
        done, _ = await asyncio.wait({run}, timeout=remaining)
    except asyncio.CancelledError:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        raise
    if run not in done:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        raise SectionWallClockExpired
    return run.result()


def _section_resume_within_deadline(
    deadline: float | None,
    *,
    retry_delay: float,
    wall_clock_seconds: float | None,
    wall_clock_started_at: Any,
    now: Any,
) -> bool:
    """退避照常吃节墙钟；退避后至少还剩一次 resume 的实测成本下限。"""

    remaining: float | None = None
    if wall_clock_seconds is not None and wall_clock_started_at is not None and callable(now):
        try:
            remaining = wall_clock_seconds - (
                now() - wall_clock_started_at
            ).total_seconds()
        except (AttributeError, TypeError):
            remaining = None
    if remaining is None and deadline is not None:
        remaining = deadline - asyncio.get_running_loop().time()
    if remaining is None:
        return True
    return remaining - retry_delay >= SECTION_RESUME_COST_FLOOR_SECONDS


def _chapter_id(agent: Any) -> str:
    chapter = agent.chapter if isinstance(agent.chapter, Mapping) else {}
    return str(chapter.get("chapter_id") or agent.agent_id)


def _section_specs(plan: Any, agent: Any) -> list[dict[str, Any]]:
    parent = _chapter_id(agent)
    specs: list[dict[str, Any]] = []
    for index, goal in enumerate(plan.goals, start=1):
        specs.append({
            "section_id": f"{parent}/sec-{index}",
            "filename": f"sec-{index}.md",
            "title": goal.title,
            "goal_id": goal.goal_id,
        })
    return specs


def _ledger_inputs(rows: list[dict[str, Any]], goal_id: str) -> dict[str, list[dict[str, Any]]]:
    done = []
    missing = []
    for row in rows:
        if row["goal_id"] != goal_id:
            continue
        if "/" in str(row["chapter_id"]):
            continue
        if row["status"] == "done":
            done.append({
                "goal_id": row["goal_id"],
                "chapter_id": row["chapter_id"],
                "path": row["actual_output_path"],
                "actual_count": row["actual_count"],
            })
        elif row["status"] in {"missing", "deferred"}:
            missing.append({
                "goal_id": row["goal_id"],
                "chapter_id": row["chapter_id"],
                "reason": row["reason"],
            })
    return {"done": done, "missing": missing}


def _artifact_key(raw_path: Any, research_root: Path) -> str | None:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        path = research_root / path
    return str(path.resolve(strict=False))


def _merge_declared_done_inputs(
    inputs: dict[str, list[dict[str, Any]]],
    rows: list[dict[str, Any]],
    declared_inputs: Any,
    *,
    research_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    """只把章级声明且账本已 done 的路径并入节输入。"""

    done_rows_by_path = {}
    for row in rows:
        if row["status"] != "done":
            continue
        key = _artifact_key(row.get("actual_output_path"), research_root)
        if key is not None:
            done_rows_by_path.setdefault(key, row)
    seen = {
        key
        for item in inputs["done"]
        if (key := _artifact_key(item.get("path"), research_root)) is not None
    }
    for item in declared_inputs if isinstance(declared_inputs, list) else []:
        if not isinstance(item, Mapping):
            continue
        key = _artifact_key(item.get("path"), research_root)
        row = done_rows_by_path.get(key)
        if row is None or key in seen:
            continue
        inputs["done"].append({
            "goal_id": row["goal_id"],
            "chapter_id": row["chapter_id"],
            "path": row["actual_output_path"],
            "actual_count": row["actual_count"],
        })
        seen.add(key)
    return inputs


def _declared_done_goal_closure(
    plan: Any,
    rows: list[dict[str, Any]],
    inputs: Mapping[str, Any],
    *,
    research_root: Path,
) -> set[str]:
    """沿已完成产物的 opening.inputs 传递追溯其可引用 goal。"""

    done_rows_by_path: dict[str, dict[str, Any]] = {}
    done_rows_by_chapter: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("status") != "done":
            continue
        goal_id = str(row.get("goal_id") or "")
        chapter_id = str(row.get("chapter_id") or "")
        done_rows_by_chapter[(goal_id, chapter_id)] = row
        key = _artifact_key(row.get("actual_output_path"), research_root)
        if key is not None:
            done_rows_by_path.setdefault(key, row)

    agents_by_chapter: dict[tuple[str, str], Any] = {}
    for goal in getattr(plan, "goals", []):
        goal_id = str(getattr(goal, "goal_id", "") or "")
        for candidate in getattr(goal, "agents", []):
            chapter = getattr(candidate, "chapter", None)
            if not isinstance(chapter, Mapping):
                continue
            chapter_id = str(chapter.get("chapter_id") or "")
            if chapter_id:
                agents_by_chapter[(goal_id, chapter_id)] = candidate

    closure: set[str] = set()
    pending: list[dict[str, Any]] = []
    for item in inputs.get("done", []):
        if not isinstance(item, Mapping):
            continue
        goal_id = str(item.get("goal_id") or "")
        chapter_id = str(item.get("chapter_id") or "")
        if goal_id:
            closure.add(goal_id)
        row = done_rows_by_chapter.get((goal_id, chapter_id))
        if row is None:
            key = _artifact_key(item.get("path"), research_root)
            row = done_rows_by_path.get(key) if key is not None else None
        if row is not None:
            pending.append(row)

    visited: set[tuple[str, str]] = set()
    while pending:
        row = pending.pop()
        row_key = (str(row.get("goal_id") or ""), str(row.get("chapter_id") or ""))
        if row_key in visited:
            continue
        visited.add(row_key)
        agent = agents_by_chapter.get(row_key)
        chapter = getattr(agent, "chapter", None)
        if not isinstance(chapter, Mapping):
            continue
        opening = chapter.get("opening", {})
        declared = opening.get("inputs", []) if isinstance(opening, Mapping) else []
        for declared_item in declared if isinstance(declared, list) else []:
            if not isinstance(declared_item, Mapping):
                continue
            artifact_key = _artifact_key(declared_item.get("path"), research_root)
            upstream = (
                done_rows_by_path.get(artifact_key)
                if artifact_key is not None
                else None
            )
            if upstream is None:
                continue
            upstream_goal_id = str(upstream.get("goal_id") or "")
            if upstream_goal_id:
                closure.add(upstream_goal_id)
            pending.append(upstream)
    return closure


def _allowed_evidence_goal_ids(
    plan: Any,
    rows: list[dict[str, Any]],
    inputs: Mapping[str, Any],
    section_goal_id: str,
    *,
    research_root: Path,
) -> set[str]:
    return {
        str(section_goal_id),
        *_declared_done_goal_closure(
            plan, rows, inputs, research_root=research_root,
        ),
    }


def _section_evidence_rows(
    rows: list[dict[str, Any]],
    section_goal_id: str | None,
    *,
    limit: int = SECTION_EVIDENCE_POOL_LIMIT,
) -> list[dict[str, Any]]:
    """goal 相关项在各平台内优先，再按平台名稳定轮转。"""

    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        platform = str(row.get("platform") or "")
        buckets.setdefault(platform, []).append(row)
    for platform_rows in buckets.values():
        platform_rows.sort(key=lambda row: (
            0 if str(row.get("goal_id")) == section_goal_id else 1,
            str(row.get("id") or ""),
        ))

    selected: list[dict[str, Any]] = []
    offsets = {platform: 0 for platform in buckets}
    platforms = sorted(buckets)
    while len(selected) < limit:
        advanced = False
        for platform in platforms:
            offset = offsets[platform]
            platform_rows = buckets[platform]
            if offset >= len(platform_rows):
                continue
            selected.append(platform_rows[offset])
            offsets[platform] += 1
            advanced = True
            if len(selected) == limit:
                break
        if not advanced:
            break
    return selected


def _proportional_evidence_slots(
    counts: Mapping[str, int],
    capacities: Mapping[str, int],
    slots: int,
) -> dict[str, int]:
    """按条数权重用最大余数法分配；容量不足时确定性地继续分剩余名额。"""

    allocated = {goal_id: 0 for goal_id in sorted(capacities)}
    remaining_slots = min(max(0, slots), sum(max(0, value) for value in capacities.values()))
    while remaining_slots:
        eligible = [
            goal_id
            for goal_id in sorted(capacities)
            if allocated[goal_id] < max(0, capacities[goal_id])
        ]
        if not eligible:
            break
        weight_total = sum(max(0, counts.get(goal_id, 0)) for goal_id in eligible)
        if weight_total <= 0:
            weight_total = len(eligible)
            weights = {goal_id: 1 for goal_id in eligible}
        else:
            weights = {
                goal_id: max(0, counts.get(goal_id, 0))
                for goal_id in eligible
            }
        round_slots = remaining_slots
        raw = {
            goal_id: round_slots * weights[goal_id] / weight_total
            for goal_id in eligible
        }
        whole = {
            goal_id: min(
                capacities[goal_id] - allocated[goal_id],
                int(raw[goal_id]),
            )
            for goal_id in eligible
        }
        distributed = sum(whole.values())
        for goal_id, value in whole.items():
            allocated[goal_id] += value
        remaining_slots -= distributed
        if not remaining_slots:
            break
        ranked = sorted(
            eligible,
            key=lambda goal_id: (-(raw[goal_id] - int(raw[goal_id])), goal_id),
        )
        gave_remainder = False
        for goal_id in ranked:
            if allocated[goal_id] >= capacities[goal_id]:
                continue
            allocated[goal_id] += 1
            remaining_slots -= 1
            gave_remainder = True
            if not remaining_slots:
                break
        if not gave_remainder:
            break
    return allocated


def _numbered_evidence_rows(
    ordered: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int], bool]:
    """先按非空 goal 分 S01-S99 配额，再保持旧的全局编号顺序。"""

    by_goal: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for row in ordered:
        goal_id = str(row.get("goal_id") or "")
        if goal_id:
            by_goal.setdefault(goal_id, []).append(row)
        else:
            unassigned.append(row)
    counts = {goal_id: len(goal_rows) for goal_id, goal_rows in by_goal.items()}
    if len(ordered) <= EVIDENCE_POOL_LIMIT:
        quotas = dict(sorted(counts.items()))
        return ordered, quotas, dict(quotas), False

    floor_degraded = len(counts) * EVIDENCE_POOL_GOAL_FLOOR > EVIDENCE_POOL_LIMIT
    if floor_degraded:
        base = {goal_id: 0 for goal_id in counts}
    else:
        base = {
            goal_id: min(count, EVIDENCE_POOL_GOAL_FLOOR)
            for goal_id, count in counts.items()
        }
    remaining = EVIDENCE_POOL_LIMIT - sum(base.values())
    capacities = {
        goal_id: counts[goal_id] - base[goal_id]
        for goal_id in counts
    }
    extras = _proportional_evidence_slots(counts, capacities, remaining)
    quotas = {
        goal_id: base[goal_id] + extras[goal_id]
        for goal_id in sorted(counts)
    }

    selected: list[dict[str, Any]] = []
    actual: dict[str, int] = {}
    for goal_id in sorted(by_goal):
        goal_selected = _section_evidence_rows(
            by_goal[goal_id], goal_id, limit=quotas[goal_id],
        )
        selected.extend(goal_selected)
        actual[goal_id] = len(goal_selected)
    unfilled = EVIDENCE_POOL_LIMIT - len(selected)
    if unfilled > 0 and unassigned:
        selected.extend(_section_evidence_rows(unassigned, None, limit=unfilled))
    selected.sort(key=lambda row: (
        str(row.get("goal_id") or ""),
        str(row["id"]),
    ))
    return selected, quotas, actual, floor_degraded


def _evidence_index(
    rows: list[dict[str, Any]],
    allowed_goal_ids: set[str],
    *,
    section_goal_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """先按全报告稳定编号，再生成不超过 30 条的本节可见子集。"""

    ordered = sorted(
        (
            row for row in rows
            if str(row.get("id") or "").strip()
        ),
        key=lambda row: (
            str(row.get("goal_id") or ""),
            str(row["id"]),
        ),
    )
    numbered, goal_quotas, goal_selected_counts, goal_floor_degraded = (
        _numbered_evidence_rows(ordered)
    )
    citation_by_id = {
        str(row["id"]): index
        for index, row in enumerate(numbered, start=1)
    }
    citations = {
        str(row["permalink"]): index
        for index, row in enumerate(numbered, start=1)
    }
    eligible = [
        row for row in numbered
        if str(row.get("goal_id") or "") in allowed_goal_ids
    ]
    eligible_count = sum(
        1
        for row in ordered
        if str(row.get("goal_id") or "") in allowed_goal_ids
    )
    if not eligible and numbered:
        # evidence 已存在却被 goal 过滤成空集时，回退到本 research 全池。
        eligible = numbered
        eligible_count = len(ordered)
    visible = _section_evidence_rows(eligible, section_goal_id)
    visible.sort(key=lambda row: citation_by_id[str(row["id"])])
    items: list[dict[str, Any]] = []
    for row in visible:
        index = citation_by_id[str(row["id"])]
        excerpt = row.get("content_excerpt")
        excerpt_text = None if excerpt is None else str(excerpt)
        truncated = bool(excerpt_text is not None and len(excerpt_text) > 120)
        item = {
            "citation": f"[S{index:02d}]",
            "permalink": row.get("permalink"),
            "title": row.get("title"),
            "content_excerpt": (
                excerpt_text[:120] if excerpt_text is not None else None
            ),
            "content_excerpt_truncated": truncated,
            "author_name": row.get("author_name"),
            "platform": row.get("platform"),
            "evidence_id": row.get("id"),
            "goal_id": row.get("goal_id"),
            "fetched_at": row.get("fetched_at"),
        }
        for field in _EVIDENCE_SCORE_FIELDS:
            if row.get(field) is not None:
                item[field] = row[field]
        if row.get("rating_notes") not in (None, ""):
            item["rating_notes"] = row["rating_notes"]
        items.append(item)
    return {
        "items": items,
        "omitted_count": max(0, eligible_count - len(items)),
        "goal_quotas": goal_quotas,
        "goal_selected_counts": goal_selected_counts,
        "goal_floor_degraded": goal_floor_degraded,
    }, citations


async def _project_accessible_evidence(
    *,
    plan: Any,
    goal_ids: set[str],
    persist_goal_evidence: Any,
    projected_goal_ids: set[str],
) -> None:
    if persist_goal_evidence is None:
        return
    for goal in plan.goals:
        goal_id = str(goal.goal_id)
        if goal_id not in goal_ids or goal_id in projected_goal_ids:
            continue
        result = persist_goal_evidence(plan, goal)
        if inspect.isawaitable(result):
            await result
        projected_goal_ids.add(goal_id)


def _raw_urls(text: str) -> set[str]:
    """提取原样 URL；撰写契约要求逐字取自证据池，不做归一化。"""

    urls: set[str] = set()
    for matched in _HTTP_URL.findall(text):
        urls.add(matched.rstrip(")]},.;:!?，。；：！？"))
    return urls


def _section_evidence_pool_result(
    section_path: Path,
    pool: Mapping[str, Any],
    allowed_urls: set[str],
) -> validation.Result:
    """角标按本节可见子集解析，URL 与 claims 按 research 全量证据池判定。"""

    items = list(pool.get("items", []))
    if not items:
        return validation.Result(
            validation.Verdict.FAIL,
            "evidence_pool_only",
            "本节无可引用证据，不能登记正文角标或信息源",
            [],
        )
    try:
        raw_text = section_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return validation.Result(
            validation.Verdict.UNAVAILABLE,
            "evidence_pool_only",
            f"无法读取节产物：{type(exc).__name__}: {exc}",
            [],
        )
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if (
        not isinstance(payload, Mapping)
        or not isinstance(payload.get("markdown"), str)
        or not isinstance(payload.get("claims"), list)
    ):
        return validation.Result(
            validation.Verdict.FAIL,
            "evidence_pool_only",
            "节产物必须使用 JSON 信封（markdown 正文 + claims 数组），裸 Markdown 不接受",
            ["json_envelope"],
        )
    markdown = payload["markdown"]
    claims = list(payload["claims"])

    by_mark = {str(item["citation"]): str(item["permalink"]) for item in items}
    pool_offenders: list[str] = []
    used_marks = set(validation._CITATION.findall(markdown))
    pool_offenders.extend(sorted(used_marks - set(by_mark)))
    pool_offenders.extend(sorted(_raw_urls(markdown) - allowed_urls))

    in_sources = False
    for line in markdown.splitlines():
        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            in_sources = "信息源" in heading.group(1)
            continue
        if not in_sources:
            continue
        mark_match = validation._CITATION.search(line)
        if mark_match is None:
            continue
        mark = mark_match.group(0)
        line_urls = _raw_urls(line)
        expected = by_mark.get(mark)
        if expected is None or line_urls != {expected}:
            pool_offenders.append(f"{mark} 未逐字映射到证据池 permalink")

    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        links = claim.get("evidence", [])
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, Mapping):
                continue
            permalink = str(link.get("permalink") or "")
            if permalink not in allowed_urls:
                pool_offenders.append(permalink)

    inner_ctx = validation.Ctx(
        output_path=section_path,
        output_format="markdown",
        research_id="",
        goal_id="",
        agent_id="report-writing",
        read_text=lambda: markdown,
        read_json=lambda: {},
        store=None,
        source_domains=frozenset(),
    )
    inner_results = (
        validation.sections_exist(inner_ctx, ["结论", "信息源"]),
        validation.citation_marks_resolvable(inner_ctx, []),
        validation.no_orphan_citation(inner_ctx, []),
    )
    format_offenders: list[str] = []
    format_rules: list[str] = []
    for failure in inner_results:
        if failure.verdict is not validation.Verdict.PASS:
            format_rules.append(failure.name)
            format_offenders.extend(
                f"{failure.name}: {offender}"
                for offender in (failure.offenders or [failure.message])
            )
    if pool_offenders or format_offenders:
        messages: list[str] = []
        if pool_offenders:
            messages.append(
                "节正文违反证据池唯一引用源契约，"
                f"共 {len(pool_offenders)} 处"
            )
        if format_offenders:
            messages.append(
                f"节正文违反撰写格式契约，共 {len(format_offenders)} 处"
                f"（规则：{', '.join(format_rules)}）"
            )
        return validation.Result(
            validation.Verdict.FAIL,
            "evidence_pool_only",
            "。".join(messages),
            [*pool_offenders, *format_offenders],
        )
    return validation.Result(
        validation.Verdict.PASS,
        "evidence_pool_only",
        f"正文引用均来自本节 {len(items)} 条可引用证据",
        [],
    )


def _ctx(
    task: EngineTask,
    runs_root: Path,
    store: Any,
    *,
    resume_session_id: str | None = None,
) -> validation.Ctx:
    context = validation.Ctx(
        output_path=task.output_path,
        output_format=task.output_format,
        research_id=task.research_id,
        goal_id=task.goal_id,
        agent_id=task.agent_id,
        read_text=lambda: task.output_path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(task.output_path.read_text(encoding="utf-8")),
        store=store,
        source_domains=frozenset({"news.ycombinator.com"}),
        runs_root=runs_root,
    )
    if resume_session_id:
        # Ctx 仍是统一验证上下文；Claude 适配器只选读这个扩展属性，
        # Codex 与其他适配器无需参与，编排层也不分支引擎。
        context.resume_session_id = resume_session_id
    return context


def _placeholder(section: dict[str, Any], reason: str) -> str:
    return (
        f"## {section['goal_id']}｜{section['title']}\n\n"
        f"- 此处缺失：{section['goal_id']}/{section['section_id']}；原因：{reason}\n"
    )


def _preserve_rejected_artifact(section_path: Path) -> Path | None:
    """非空失败产物改名留存；空文件与缺失文件不制造 rejected 副本。"""

    try:
        if not section_path.is_file():
            return None
        if not section_path.read_text(encoding="utf-8").strip():
            return None
    except (OSError, UnicodeError):
        return None
    rejected_path = section_path.with_name(
        f"{section_path.stem}.rejected{section_path.suffix}"
    )
    section_path.replace(rejected_path)
    return rejected_path


def _conclusion_error_with_rejected_path(
    conclusion_error: str | None, rejected_path: Path | None,
) -> str | None:
    if rejected_path is None:
        return conclusion_error
    path_note = f"rejected_path={rejected_path}"
    return f"{conclusion_error}\n{path_note}" if conclusion_error else path_note


async def _finish_section_timeout(
    *,
    plan: Any,
    context: Any,
    section: dict[str, Any],
    section_path: Path,
    store: Any,
    now_iso: Any,
    on_event: Any,
) -> None:
    """单节到点落闭集终态，并隔离可能存在的半截产物。"""

    rejected_path = _preserve_rejected_artifact(section_path)
    conclusion_error = _conclusion_error_with_rejected_path(None, rejected_path)
    section_path.write_text(_placeholder(section, "timeout"), encoding="utf-8")
    store.finish_chapter(
        plan.research_id,
        context.goal_id,
        section["section_id"],
        status="missing",
        reason="timeout",
        actual_output_path=str(section_path),
        actual_count=0,
        conclusion_error=conclusion_error,
        updated_at=now_iso(),
    )
    event_result = on_event({
        "type": "section_error",
        "data": {
            "goal_id": context.goal_id,
            "chapter_id": section["section_id"],
            "reason": "timeout",
            "engine_error": None,
            "conclusion_error": conclusion_error,
        },
        "is_error": True,
    })
    if inspect.isawaitable(event_result):
        await event_result


def _invalid_conclusion_source(result: Any) -> str:
    """优先回喂模型刚生成的原始结论块，使定向重试保留全部字段。"""

    conclusion_error = str(getattr(result, "conclusion_error", "") or "")
    for event in reversed(list(getattr(result, "events", None) or [])):
        text = str(getattr(event, "text", "") or "")
        if "owli-result" in text:
            return f"{conclusion_error}\n原 owli-result 块：\n{text}"
    return conclusion_error


def _write_object_document(
    *,
    plan: Any,
    agent: Any,
    output_path: Path,
    section_items: list[dict[str, Any]],
    missing_items: list[dict[str, Any]],
    claims: list[Any] | None = None,
) -> None:
    document = {
        "title": plan.title,
        "chapter_id": _chapter_id(agent),
        "sections": [
            {key: value for key, value in item.items() if key != "done"}
            for item in section_items
        ],
        "缺失清单": missing_items,
    }
    if claims:
        document["claims"] = claims
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _assemble(
    *,
    plan: Any,
    agent: Any,
    output_path: Path,
    output_format: str,
    section_root: Path,
    sections: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    citation_numbers: Mapping[str, int] | None = None,
) -> None:
    """把各节产物拼成父章产物；**按声明的 output.format 落盘**。

    以前无论声明什么格式都写 Markdown，声明 json 的报告章因此得到一份「假 json」
    （后缀是 .json、内容是 Markdown），下游按 json 解析必然失败。
    """
    section_items: list[dict[str, Any]] = []
    chapter_claims: list[Any] = []
    for section in sections:
        path = section_root / section["filename"]
        row = next(
            (item for item in rows if item["chapter_id"] == section["section_id"]),
            None,
        )
        section_done = bool(
            path.is_file() and (row is None or row["status"] == "done")
        )
        if section_done:
            text = path.read_text(encoding="utf-8").strip()
            try:
                fragment = json.loads(text)
            except (json.JSONDecodeError, UnicodeError):
                if (
                    output_format == "json"
                    and _declared_shape(agent) != "array"
                    and text.lstrip().casefold().startswith(("{", "[", "```json"))
                ):
                    raise SectionAssemblyShapeError(
                        f"{section['section_id']} 的 JSON 节产物不完整或不可解析"
                    )
                fragment = None
            if isinstance(fragment, Mapping) and "markdown" in fragment:
                markdown = fragment.get("markdown")
                claims = fragment.get("claims")
                if not isinstance(markdown, str) or not markdown.strip():
                    raise SectionAssemblyShapeError(
                        f"{section['section_id']} 的 markdown 缺失或为空"
                    )
                if not isinstance(claims, list):
                    raise SectionAssemblyShapeError(
                        f"{section['section_id']} 的 claims 必须是数组"
                    )
                text = markdown.strip()
                chapter_claims.extend(claims)
        else:
            reason = str(row["reason"] if row else "empty_result")
            text = _placeholder(section, reason).strip()
        section_items.append({
            "section_id": section["section_id"],
            "goal_id": section["goal_id"],
            "title": section["title"],
            "markdown": text,
            # 有产物且账本没判它失败，就按「本节有内容」参与组装。
            "done": section_done,
        })
    missing = [
        row for row in rows
        if row["status"] == "missing" or (
            row["status"] == "deferred" and "/" in str(row["chapter_id"])
        )
    ]
    missing_items = [
        {
            "goal_id": row["goal_id"],
            "chapter_id": row["chapter_id"],
            "reason": row["reason"],
            "text": (
                f"此处缺失：{row['goal_id']}/{row['chapter_id']}；原因：{row['reason']}"
            ),
        }
        for row in missing
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json" and _declared_shape(agent) == "array":
        # 章声明 shape=array（其 validators 按顶层数组写）：节产物逐条并成数组，
        # 缺失清单另置到同目录的 `<stem>.missing.json`，不塞进数组里污染条目（期望 d）。
        done_items = [item for item in section_items if item["done"]]
        parsed: list[tuple[dict[str, Any], Any]] = []
        unparsed: list[str] = []
        for item in done_items:
            try:
                parsed.append((item, json.loads(item["markdown"])))
            except ValueError:
                unparsed.append(item["section_id"])
        if done_items and unparsed and len(unparsed) < len(done_items):
            # 同一章里既有 JSON 节又有非 JSON 节：确定性的形状冲突，重来一轮也一样。
            raise SectionAssemblyShapeError(
                f"节产物形状不一致，无法按 shape=array 组装：{sorted(unparsed)} 不是合法 JSON"
            )
        if unparsed:
            # 整章的节都是叙述体（声明 json 只是产物后缀）：沿用对象文档，不硬拗数组。
            _write_object_document(
                plan=plan, agent=agent, output_path=output_path,
                section_items=section_items, missing_items=missing_items,
            )
            return
        items: list[Any] = []
        for _item, payload in parsed:
            if isinstance(payload, list):
                items.extend(payload)
            else:
                items.append(payload)
        output_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        output_path.with_name(f"{output_path.stem}.missing.json").write_text(
            json.dumps(
                {
                    "chapter_id": _chapter_id(agent),
                    "缺失清单": missing_items,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return
    if output_format == "json":
        _write_object_document(
            plan=plan, agent=agent, output_path=output_path,
            section_items=section_items, missing_items=missing_items,
            claims=chapter_claims,
        )
        return
    # Markdown 整卷报告做确定性归并：单结论、单信息源、全卷统一角标
    #（M4-a 改法 3，worklog report-module §10.4）。
    output_path.write_text(
        merge_sectioned_markdown(
            plan.title,
            [item["markdown"] for item in section_items],
            [item["text"] for item in missing_items],
            citation_numbers=citation_numbers,
        ),
        encoding="utf-8",
    )


async def run_sectioned_task(
    *,
    plan: Any,
    agent: Any,
    context: Any,
    base_task: EngineTask,
    adapter: Any,
    store: Any,
    runs_root: Path,
    now_iso: Any,
    on_event: Any,
    timer: Any = None,
    now: Any = None,
    deadline_at: Any = None,
    engine_timeout_seconds: float | None = None,
    persist_goal_evidence: Any = None,
) -> TaskRunResult:
    sections = _section_specs(plan, agent)
    store.ensure_chapters(
        plan.research_id,
        [
            {"goal_id": context.goal_id, "chapter_id": section["section_id"]}
            for section in sections
        ],
        updated_at=now_iso(),
        reset_running=False,
    )
    existing = {
        row["chapter_id"]: row
        for row in store.list_chapters(plan.research_id)
        if row["goal_id"] == context.goal_id
    }
    section_root = base_task.output_path.parent / Path(base_task.output_path.stem)
    section_root.mkdir(parents=True, exist_ok=True)
    # 章级重试（本函数每被调用一次 = 父章一次尝试）先把上一轮传输耗尽的节复位成
    # pending 重新派活；done 节与其它闭集 reason 的 missing 节仍旧跳过。
    reset_ids = set(store.reset_retry_exhausted_chapters(
        plan.research_id,
        context.goal_id,
        [section["section_id"] for section in sections],
        updated_at=now_iso(),
    ))
    if reset_ids:
        for section in sections:
            if section["section_id"] in reset_ids:
                # 上一轮写下的占位正文不能留着冒充产物，否则重派后引擎不落盘也会判 done。
                (section_root / section["filename"]).unlink(missing_ok=True)
        existing = {
            row["chapter_id"]: row
            for row in store.list_chapters(plan.research_id)
            if row["goal_id"] == context.goal_id
        }
    attempt_budget = _section_attempt_budget()
    section_wall_clock = getattr(context, "section_deadline_seconds", None)
    if section_wall_clock is not None:
        section_wall_clock = float(section_wall_clock)
    retry_delay = float(
        CHAPTER_RETRY_INTERVAL_SECONDS.get(getattr(plan, "scale", ""), 0.0)
    )
    report_goal_ids = {str(goal.goal_id) for goal in plan.goals}
    # 父章一次尝试内冻结可读账本快照：若其它 goal 在写节期间刚完成，
    # 它既不会半途进 done，也不会造成“读得到、引不得”或已用 S 号漂移。
    input_rows = store.list_chapters(plan.research_id)
    chapter = agent.chapter if isinstance(agent.chapter, Mapping) else {}
    opening = chapter.get("opening", {})
    declared_inputs = (
        opening.get("inputs", []) if isinstance(opening, Mapping) else []
    )
    if persist_goal_evidence is not None:
        projection_goal_ids: set[str] = set()
        for section in sections:
            projection_inputs = _merge_declared_done_inputs(
                _ledger_inputs(input_rows, section["goal_id"]),
                input_rows,
                declared_inputs,
                research_root=runs_root / plan.research_id,
            )
            projection_goal_ids.update(
                _allowed_evidence_goal_ids(
                    plan,
                    input_rows,
                    projection_inputs,
                    str(section["goal_id"]),
                    research_root=runs_root / plan.research_id,
                )
            )
        await _project_accessible_evidence(
            plan=plan,
            goal_ids=projection_goal_ids,
            persist_goal_evidence=persist_goal_evidence,
            projected_goal_ids=set(),
        )
    # source_mcp 可在并发 goal 中直写 evidence，因此证据行也必须与
    # input_rows 同时冻结；合并编号和每节证据池共用这一份快照。
    evidence_rows = store.list_evidence(plan.research_id)
    all_evidence_urls = {
        str(row.get("permalink") or "")
        for row in evidence_rows
        if str(row.get("permalink") or "")
    }
    _, citation_numbers = _evidence_index(
        evidence_rows, report_goal_ids,
    )
    if persist_goal_evidence is not None:
        stale_done_ids: list[str] = []
        for section in sections:
            row = existing.get(section["section_id"])
            if row is None or row["status"] != "done":
                continue
            frozen_inputs = _merge_declared_done_inputs(
                _ledger_inputs(input_rows, section["goal_id"]),
                input_rows,
                declared_inputs,
                research_root=runs_root / plan.research_id,
            )
            allowed_goal_ids = _allowed_evidence_goal_ids(
                plan,
                input_rows,
                frozen_inputs,
                str(section["goal_id"]),
                research_root=runs_root / plan.research_id,
            )
            frozen_pool, _ = _evidence_index(
                evidence_rows,
                allowed_goal_ids,
                section_goal_id=str(section["goal_id"]),
            )
            section_path = section_root / section["filename"]
            if _section_evidence_pool_result(
                section_path, frozen_pool, all_evidence_urls,
            ).verdict is not validation.Verdict.PASS:
                stale_done_ids.append(section["section_id"])
        if stale_done_ids:
            store.reset_done_chapters(
                plan.research_id,
                context.goal_id,
                stale_done_ids,
                updated_at=now_iso(),
            )
            for section in sections:
                if section["section_id"] in stale_done_ids:
                    (section_root / section["filename"]).unlink(missing_ok=True)
            existing = {
                row["chapter_id"]: row
                for row in store.list_chapters(plan.research_id)
                if row["goal_id"] == context.goal_id
            }
    truncation_event_emitted = False
    for section_number, section in enumerate(sections, start=1):
        row = existing.get(section["section_id"])
        if row and row["status"] in {"done", "missing"}:
            continue
        section_attempt = 0
        section_deadline = (
            asyncio.get_running_loop().time() + section_wall_clock
            if section_wall_clock is not None
            else None
        )
        section_wall_clock_started_at = now() if callable(now) else None
        resume_session_id: str | None = None

        while True:
            started = store.start_chapter(
                plan.research_id,
                context.goal_id,
                section["section_id"],
                engine=context.engine,
                updated_at=now_iso(),
            )
            if not started:
                break
            section_attempt += 1
            inputs = _ledger_inputs(input_rows, section["goal_id"])
            inputs = _merge_declared_done_inputs(
                inputs,
                input_rows,
                declared_inputs,
                research_root=runs_root / plan.research_id,
            )
            allowed_goal_ids = _allowed_evidence_goal_ids(
                plan,
                input_rows,
                inputs,
                str(section["goal_id"]),
                research_root=runs_root / plan.research_id,
            )
            evidence_pool, _ = _evidence_index(
                evidence_rows,
                allowed_goal_ids,
                section_goal_id=str(section["goal_id"]),
            )
            omitted_count = int(evidence_pool["omitted_count"])
            if omitted_count and not truncation_event_emitted:
                event_result = on_event({
                    "type": "evidence_pool_truncated",
                    "data": {
                        "research_id": plan.research_id,
                        "goal_id": context.goal_id,
                        "chapter_id": _chapter_id(agent),
                        "omitted_count": omitted_count,
                        "limit": SECTION_EVIDENCE_POOL_LIMIT,
                        "goal_quotas": dict(evidence_pool["goal_quotas"]),
                        "goal_selected_counts": dict(
                            evidence_pool["goal_selected_counts"]
                        ),
                        "goal_floor_degraded": bool(
                            evidence_pool["goal_floor_degraded"]
                        ),
                    },
                    "is_error": False,
                })
                if inspect.isawaitable(event_result):
                    await event_result
                truncation_event_emitted = True
            section_path = section_root / section["filename"]
            if evidence_pool["items"]:
                pool_notice = (
                    "下方证据池是本节角标的唯一来源。正文角标与『信息源』"
                    "清单里的 Sxx 必须逐字取自池；清单只列正文实际使用的角标。"
                )
            else:
                pool_notice = (
                    "本节无可引用证据；请按缺失清单如实产出，不得回退到 done "
                    "产物里的 URL，也不得编造。"
                )
            if omitted_count:
                pool_notice += (
                    f" 本节可见角标池已裁剪至 {len(evidence_pool['items'])} 条；"
                    "裁剪不缩小本 research 全量 evidence permalink 的 URL 判定面。"
                )
            body = (
                f"{base_task.body}\n\n"
                "本次只写一个报告节；禁止生成整份报告。\n"
                "本节须包含一个『结论』小节与一个『信息源』小节（标题逐字使用），"
                "Markdown 标题分别写为 `## 结论` 与 `## 信息源`，且两个小节正文均不得为空。\n"
                "本节的结论/信息源只覆盖本节范围，不总结或引用其他报告节。\n"
                "本节产物路径（写文件与 owli-result.output_path 都必须逐字使用）："
                f"{section_path}\n"
                f"节目标={json.dumps(section, ensure_ascii=False)}\n"
                "本节可用的上游产物：done = 本 goal 账本 status=done 的章 + "
                "章级 opening.inputs 声明且账本 status=done 的上游产物；"
                "每条都带 goal_id/chapter_id/path/actual_count。\n"
                "missing 仍只列本 goal 账本 missing/deferred 章及其 reason。\n"
                "只允许读取下方 done 列出的产物；不得越过节协议读取未列出的其他 goal 产物。\n"
                "done 产物只作事实与上下文来源，不作引用来源；产物 path 只用于定位，"
                "不是 permalink。池外 URL 一律不得出现在正文，包括散文里的裸链接。"
                "不得把本地路径改写成 file:// 角标，也不得编造 URL。\n"
                f"本节上游输入 JSON：\n{json.dumps(inputs, ensure_ascii=False, indent=2)}\n"
                f"{pool_notice}\n"
                "content_excerpt_truncated=true 表示该摘要已截到 120 个 Unicode 字符。\n"
                "本节可引用证据池 JSON（唯一引用源）：\n"
                f"{json.dumps(evidence_pool, ensure_ascii=False, indent=2)}"
            )
            if (
                base_task.agent_kind in {"report", "report_writing"}
                and _declared_shape(agent) != "array"
            ):
                body += (
                    "\n缺口/限制性陈述（『本节可见证据不足』『未覆盖 X』这类）"
                    "不许进『结论』列表，放独立的『证据缺口』段。"
                    "『结论』列表每一项必须带至少一个 [Sxx]。\n"
                    "权威来源（官网 / 媒体 / 评测站）：可以直陈，逐条当事实引。"
                    "社交媒体这类权重不高的来源：以汇总式、带平台与倾向的句式提及，"
                    "并挂角标，不逐条当权威引。样例：『在国内小红书平台上，"
                    "大多数……情况是……』。社媒证据要出现在正文，但以『平台 + 群体倾向』"
                    "的方式说，不设置社媒排序或配额规则。"
                    "claims 的 stance / firsthand 对这类汇总句照常登记。\n"
                    "本节产物必须使用 JSON 信封，须显式写 JSON object；裸 Markdown 不接受："
                    "markdown 为本节 Markdown 正文；claims 为本节结论断言数组。"
                    "每条断言必须含报告内唯一 id（c- 加至少两位数字）、非空 text、"
                    "至少一条 evidence；evidence 用 permalink 联接，可选 stance="
                    "contradicts、firsthand=true、origin_url。不得用 [Sxx] 代替 permalink，"
                    "不得从正文事后抽取断言。"
                    f"本节断言 id 固定使用 c-{section_number:02d}01、"
                    f"c-{section_number:02d}02…的区间，避免跨节重号。"
                    "若本节确无可证否结论，claims 写空数组。"
                )
            section_task = replace(
                base_task,
                body=body,
                output_path=section_path,
                output_format="markdown",
                agent_id=f"{base_task.agent_id}-{section['filename'].removesuffix('.md')}",
                validators=["file_exists"],
                capability=base_task.capability,
            )
            try:
                result = await _run_before_section_deadline(
                    adapter,
                    section_task,
                    _ctx(
                        section_task,
                        runs_root,
                        store,
                        resume_session_id=resume_session_id,
                    ),
                    on_event,
                    section_deadline,
                )
            except SectionWallClockExpired:
                await _finish_section_timeout(
                    plan=plan, context=context, section=section,
                    section_path=section_path, store=store,
                    now_iso=now_iso, on_event=on_event,
                )
                break
            except asyncio.CancelledError:
                cancellation_reason = getattr(
                    context, "cancellation_reason", None,
                )
                reason = (
                    cancellation_reason()
                    if callable(cancellation_reason)
                    else None
                )
                if reason == "timeout":
                    await _finish_section_timeout(
                        plan=plan, context=context, section=section,
                        section_path=section_path, store=store,
                        now_iso=now_iso, on_event=on_event,
                    )
                raise
            if resume_session_id and bool(getattr(result, "resume_failed", False)):
                # resume 未建立可用会话时，同一次节重试清空 resume 从头跑；
                # 这段仍由原始绝对节墙钟约束，不补回任何时间。
                failed_session_id = resume_session_id
                resume_session_id = None
                await _emit_section_retry(
                    on_event,
                    context=context,
                    section=section,
                    attempt=section_attempt,
                    resume=False,
                    session_id=failed_session_id,
                )
                try:
                    result = await _run_before_section_deadline(
                        adapter,
                        section_task,
                        _ctx(section_task, runs_root, store),
                        on_event,
                        section_deadline,
                    )
                except SectionWallClockExpired:
                    await _finish_section_timeout(
                        plan=plan, context=context, section=section,
                        section_path=section_path, store=store,
                        now_iso=now_iso, on_event=on_event,
                    )
                    break
                except asyncio.CancelledError:
                    cancellation_reason = getattr(
                        context, "cancellation_reason", None,
                    )
                    reason = (
                        cancellation_reason()
                        if callable(cancellation_reason)
                        else None
                    )
                    if reason == "timeout":
                        await _finish_section_timeout(
                            plan=plan, context=context, section=section,
                            section_path=section_path, store=store,
                            now_iso=now_iso, on_event=on_event,
                        )
                    raise
            try:
                artifact_empty = (
                    not section_path.is_file()
                    or not section_path.read_text(encoding="utf-8").strip()
                )
            except (OSError, UnicodeError):
                artifact_empty = True
            conclusion_invalid = (
                not artifact_empty
                and getattr(result, "conclusion", None) is None
                and bool(getattr(result, "conclusion_error", None))
                and getattr(result, "engine_error", None) is None
                and getattr(getattr(result, "validation", None), "verdict", None)
                is validation.Verdict.PASS
            )
            if conclusion_invalid:
                original_conclusion = _invalid_conclusion_source(result)
                retry_task = replace(
                    section_task,
                    body=(
                        f"结论块字段不合法：{original_conclusion}，"
                        "请只重发 owli-result 块，不要重写产物"
                    ),
                    agent_id=f"{section_task.agent_id}-conclusion-retry",
                )
                try:
                    result = await _run_before_section_deadline(
                        adapter,
                        retry_task,
                        _ctx(retry_task, runs_root, store),
                        on_event,
                        section_deadline,
                    )
                except SectionWallClockExpired:
                    await _finish_section_timeout(
                        plan=plan, context=context, section=section,
                        section_path=section_path, store=store,
                        now_iso=now_iso, on_event=on_event,
                    )
                    break
                except asyncio.CancelledError:
                    cancellation_reason = getattr(
                        context, "cancellation_reason", None,
                    )
                    reason = (
                        cancellation_reason()
                        if callable(cancellation_reason)
                        else None
                    )
                    if reason == "timeout":
                        await _finish_section_timeout(
                            plan=plan, context=context, section=section,
                            section_path=section_path, store=store,
                            now_iso=now_iso, on_event=on_event,
                        )
                    raise
            pool_result = (
                _section_evidence_pool_result(
                    section_path, evidence_pool, all_evidence_urls,
                )
                if persist_goal_evidence is not None and not artifact_empty
                else None
            )
            pool_failed = bool(
                pool_result is not None
                and pool_result.verdict is not validation.Verdict.PASS
            )
            succeeded = (
                bool(getattr(result, "succeeded", False))
                and not artifact_empty
                and not pool_failed
            )
            if succeeded:
                store.finish_chapter(
                    plan.research_id,
                    context.goal_id,
                    section["section_id"],
                    status="done",
                    reason=None,
                    actual_output_path=str(section_path),
                    actual_count=1,
                    updated_at=now_iso(),
                )
                break
            else:
                reason = (
                    "conclusion_invalid"
                    if pool_failed
                    else section_failure_reason(result, section_path)
                )
                engine_error = getattr(result, "engine_error", None)
                conclusion_error = getattr(result, "conclusion_error", None)
                if pool_failed and pool_result is not None:
                    conclusion_error = pool_result.message
                transport_failure = _is_transport_failure(result, reason)
                if section_attempt < attempt_budget and transport_failure:
                    # 传输断连不是「这一节问不出来」，只是链路断了：原地退避重试，
                    # 不落 missing、不发 section_error、不换引擎（引擎选择归适配层）。
                    if _section_resume_within_deadline(
                        section_deadline,
                        retry_delay=retry_delay,
                        wall_clock_seconds=section_wall_clock,
                        wall_clock_started_at=section_wall_clock_started_at,
                        now=now,
                    ):
                        next_session_id = str(
                            getattr(result, "session_id", None) or ""
                        ) or None
                        await _emit_section_retry(
                            on_event,
                            context=context,
                            section=section,
                            attempt=section_attempt + 1,
                            resume=bool(next_session_id),
                            session_id=next_session_id,
                        )
                        await _wait_before_section_retry(timer, retry_delay)
                        resume_session_id = next_session_id
                        continue
                    # 退避后的剩余节墙钟不足一次 resume 成本下限：如实 timeout。
                    reason = "timeout"
                rejected_path = _preserve_rejected_artifact(section_path)
                conclusion_error = _conclusion_error_with_rejected_path(
                    conclusion_error, rejected_path,
                )
                section_path.write_text(_placeholder(section, reason), encoding="utf-8")
                store.finish_chapter(
                    plan.research_id,
                    context.goal_id,
                    section["section_id"],
                    status="missing",
                    reason=reason,
                    actual_output_path=str(section_path),
                    actual_count=0,
                    engine_error=engine_error,
                    conclusion_error=conclusion_error,
                    updated_at=now_iso(),
                )
                event_result = on_event({
                    "type": "section_error",
                    "data": {
                        "goal_id": context.goal_id,
                        "chapter_id": section["section_id"],
                        "reason": reason,
                        "engine_error": engine_error,
                        "conclusion_error": conclusion_error,
                    },
                    "is_error": True,
                })
                if inspect.isawaitable(event_result):
                    await event_result
                break
    rows = store.list_chapters(plan.research_id)
    section_ids = {section["section_id"] for section in sections}
    done_count = sum(
        1
        for row in rows
        if row["goal_id"] == context.goal_id
        and row["chapter_id"] in section_ids
        and row["status"] == "done"
    )
    try:
        _assemble(
            plan=plan,
            agent=agent,
            output_path=base_task.output_path,
            output_format=base_task.output_format,
            section_root=section_root,
            sections=sections,
            rows=rows,
            citation_numbers=citation_numbers,
        )
    except SectionAssemblyShapeError as exc:
        # 形状对不上是确定性失败：换一轮也是同样结果，直接定终态、不进第二轮（期望 d）。
        event_result = on_event({
            "type": "section_assembly_error",
            "data": {
                "goal_id": context.goal_id,
                "chapter_id": _chapter_id(agent),
                "validation_failures": [
                    {"name": "section_assembly_shape", "message": str(exc),
                     "offenders": []},
                ],
            },
            "is_error": True,
        })
        if inspect.isawaitable(event_result):
            await event_result
        return TaskRunResult(
            False,
            engine=context.engine,
            failure_feedback=str(exc),
            chapter_status="missing",
            reason="conclusion_invalid",
            actual_output_path=str(base_task.output_path),
            actual_count=done_count,
        )
    if done_count == 0:
        section_reasons = {
            str(row["reason"])
            for row in rows
            if row["goal_id"] == context.goal_id
            and row["chapter_id"] in section_ids
            and row["reason"]
        }
        reason = (
            next(iter(section_reasons))
            if len(section_reasons) == 1
            else SECTION_RETRYABLE_REASON
        )
        # 节全丢时不无条件定终态：如果全是传输耗尽，这一章还值得再来一轮
        # （下一轮入口会把这些节复位重新派活）。所以不写 chapter_status，
        # 交回 Scheduler 走正常章级重试；轮次耗尽后它自会落 missing。
        retryable = bool(section_reasons) and section_reasons <= {
            SECTION_RETRYABLE_REASON,
        }
        return TaskRunResult(
            False,
            engine=context.engine,
            failure_feedback="所有报告节均未完成；占位报告已落盘",
            chapter_status=None if retryable else "missing",
            reason=reason,
            actual_output_path=str(base_task.output_path),
            actual_count=0,
        )
    final_validation = validation.validate(
        _ctx(base_task, runs_root, store), base_task.validators,
    )
    if final_validation.verdict is not validation.Verdict.PASS:
        # json 章的组装是确定性的：同样的节文件重来一轮还是同样的产物。
        # 复位已写成的节再来一轮既白烧一轮墙钟，又把三节成果冲掉（D-008 根因 4）。
        deterministic = base_task.output_format == "json"
        failures = [
            {
                "name": item.name,
                "message": item.message,
                "offenders": list(item.offenders),
            }
            for item in final_validation.failures
        ]
        if not deterministic:
            store.reset_done_chapters(
                plan.research_id,
                context.goal_id,
                [section["section_id"] for section in sections],
                updated_at=now_iso(),
            )
        event_result = on_event({
            "type": "section_assembly_error",
            "data": {
                "goal_id": context.goal_id,
                "chapter_id": _chapter_id(agent),
                "validation_failures": failures,
            },
            "is_error": True,
        })
        if inspect.isawaitable(event_result):
            await event_result
        return TaskRunResult(
            False,
            engine=context.engine,
            failure_feedback=json.dumps(failures, ensure_ascii=False),
            chapter_status="missing" if deterministic else None,
            reason="conclusion_invalid" if deterministic else None,
            actual_output_path=str(base_task.output_path),
            actual_count=done_count,
        )
    return TaskRunResult(
        True,
        engine=context.engine,
        actual_output_path=str(base_task.output_path),
        actual_count=done_count,
    )


__all__ = ["run_sectioned_task", "section_failure_reason", "should_section"]
