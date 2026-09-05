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
from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
from app.adapters.ratelimit import classify_transport_error
from app.orchestrator.chapter_failure import (
    chapter_failure_reason as section_failure_reason,
)
from app.orchestrator.scheduler import CHAPTER_RETRY_INTERVAL_SECONDS, TaskRunResult
from app.report.markdown import (
    merge_section_shards,
    merge_sectioned_markdown,
    render_entity_section,
    section_conclusion_items,
)


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
#: 单节 30 个池位里，先留给本节 goal 的下限（§SRC-1 货 6）。
#: 取 20 与 `EVIDENCE_POOL_GOAL_FLOOR` 同源：一节论证需要的号数；
#: 剩下 10 个位留给跨 goal 对照证据（货 5 放开的那条用法）。
SECTION_GOAL_FLOOR = 20

#: §D-031 撰写/交叉节分片。M6-e 关账整跑九个节里八个 timeout（两个死法各半：
#: `claude.py` 300 s 适配器硬顶、节墙钟 330 s 跑满），成稿只剩占位、全库角标全空。
#: 瓶颈量过了是**产出体量**不是入参体量：可见池恒为 30 条 ≈ 15 KB，而 RATE-3 那轮
#: 写成功的节产物是 7.0/7.1/15.5 KB 中文正文——一次会话写这么多，本来就在 300 s 线上下。
#: 所以按证据池顺序切片，让「一次会话要写多少字」降下来；池 ≤ 一片就走原路。
WRITE_SHARD_ITEMS = 10
#: 单片池 JSON 字节封顶。照 RATE-3 的教训：只按条数切会被重条目击穿
#: （rating_notes / content_excerpt 长的证据条，同样条数能差出数倍字节）。
WRITE_SHARD_BYTES = 6_000
#: 一节最多切几片。片数是**天花板不是开销**（章预算按它乘），防病态计划把一节炸成十几次会话。
WRITE_SHARD_MAX = 4


def write_shard_sizes(
    items: list[Any], *, shard_items: int = WRITE_SHARD_ITEMS,
    shard_bytes: int = WRITE_SHARD_BYTES, max_shards: int = WRITE_SHARD_MAX,
) -> list[int]:
    """按池原序顺序切片：条数到 shard_items 或字节到 shard_bytes 就封一片。

    池已按 (goal_id, 评级) 排好序，连续切因此天然让第 1 片是本 goal 高等级证据、
    末片是跨 goal 对照证据，一片之内主题内聚。单条超字节预算时自成一片（不丢条）。
    片数超过 max_shards 时，**把全部条目在 max_shards 片内按字节均摊重切**
    （§D-034）：仍保持池原序、不丢条、片数恰为 max_shards，但任一片不再比
    「总字节 / max_shards」胖超过一条的权重。旧实现把溢出条目全并进末片
    （「宁可末片胖」），末片体量 = 池总量 − 前几片，双封顶在末片形同虚设——
    r-f59fdba77cd7 goal-3/ch-5/sec-2 切成 5/4/4/17，末片 305.6 s 撞 300 s 硬顶，
    为消灭超时做的分片反而在末片把超时造了回来。返回每片条数表。
    """
    max_items = max(1, int(shard_items))
    max_bytes = max(1, int(shard_bytes))
    weights = [
        len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in items
    ]
    sizes: list[int] = []
    count = 0
    used = 0
    for weight in weights:
        if count and (count >= max_items or used + weight > max_bytes):
            sizes.append(count)
            count, used = 0, 0
        count += 1
        used += weight
    if count:
        sizes.append(count)
    limit = max(1, int(max_shards))
    if len(sizes) > limit:
        sizes = _rebalanced_shard_sizes(weights, limit)
    return sizes


def _rebalanced_shard_sizes(weights: list[int], limit: int) -> list[int]:
    """把全部条目按原序均摊进 `limit` 片，返回每片条数表（§D-034）。

    贪心封片，目标字节按「剩余字节 / 剩余片数」动态取——静态目标会让前几片
    每片都欠一点、把欠账全堆到末片，正是旧实现的病。每片至少一条，也至少给
    后面的片各留一条，因此片数恰为 `limit`（前提：条数 ≥ limit，溢出时必然成立）。
    """

    total = len(weights)
    sizes: list[int] = []
    index = 0
    for remaining_shards in range(limit, 0, -1):
        if remaining_shards == 1:
            sizes.append(total - index)
            break
        # 后面每片至少留一条，本片最多能拿这么多。
        takeable = total - index - (remaining_shards - 1)
        target = sum(weights[index:]) / remaining_shards
        count = 1
        used = weights[index]
        while count < takeable:
            weight = weights[index + count]
            # 取到「离目标最近」为止：加上这条比停在这里更接近 target 才继续。
            if used + weight - target >= target - used:
                break
            used += weight
            count += 1
        sizes.append(count)
        index += count
    return sizes


def write_shard_path(section_path: Path, index: int) -> Path:
    """第 index 片的产物：`sec-1.md` → `sec-1.part.1.md`。

    片产物**不是任何 agent 的声明产物**（同 RATE-3 的 `.part.<n>.json`）；
    系统按片序合并成 `sec-<n>.md`，下游 `_assemble` 只读后者。
    """
    return section_path.with_name(
        f"{section_path.stem}.part.{int(index)}{section_path.suffix}"
    )
_EVIDENCE_SCORE_FIELDS = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence",
)
#: §RATE-1 货 4：等级高的先进池；**空等级 = 还没评到**，排在评过的后面但仍可用，
#: 否则评级章还没跑完的节一个字都写不出来。D 级不进池（真正的低质来源）。
_EVIDENCE_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "": 3, "D": 4}
_EVIDENCE_RATING_FIELDS = ("score_total", "grade", "rated_by")


def _evidence_grade(row: Mapping[str, Any]) -> str:
    return str(row.get("grade") or "").strip().upper()


def _rating_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
    """同 goal 内按真实等级排：总分降序 → 等级降序 → id 稳定兜底。"""
    total = row.get("score_total")
    scored = isinstance(total, int) and not isinstance(total, bool)
    return (
        -int(total) if scored else 1,
        _EVIDENCE_GRADE_RANK.get(_evidence_grade(row), 3),
        str(row.get("id") or ""),
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


#: §D-036：codex MCP 断连的报错原文形态。M3-a 那份 socket 词表
#: （`classify_transport_error`，在 app/adapters/ 里，本包不动）认不出它，
#: 于是 `chapter_failure_reason` 把它归到 empty_result 而不是 retry_exhausted。
#: 真跑原文（r-e74541583a05 goal-3/ch-3/sec-1 片 3）：
#: `Transport channel closed … chatgpt.com/backend-api/ps/mcp`。
_CODEX_TRANSPORT_PATTERN = re.compile(
    r"transport\s+channel\s+closed", re.IGNORECASE,
)


def _is_transport_failure(result: Any, reason: str) -> bool:
    """传输断连（socket 断开这类）才退避重试；限流 / 超时 / 结论不合法都不算。

    先看归一后的 reason —— `classify_rate_limit` 与超时兜底都排在传输判定之前，
    所以 reason 落到 retry_exhausted 时才轮得到传输判定，真 429 不会被误吞。

    §D-036 例外：codex 的 `Transport channel closed` 指纹太具体，不可能是限流或
    超时的措辞，但它带不出 retry_exhausted（报错文本里没有 socket 词表的词，
    也没有 timeout 字样，片产物又没落盘 → empty_result）。所以这一条按**文本**认，
    不过 reason 那道闸；只把 quota_exhausted 排除在外，让结构化限流信号仍然优先。
    """

    engine_error = str(getattr(result, "engine_error", "") or "")
    conclusion_error = str(getattr(result, "conclusion_error", "") or "")
    errors = " ".join(filter(None, (engine_error, conclusion_error)))
    if not errors:
        return False
    if reason != "quota_exhausted" and _CODEX_TRANSPORT_PATTERN.search(errors):
        return True
    if reason != SECTION_RETRYABLE_REASON:
        return False
    return bool(classify_transport_error(errors))


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

    remaining = _section_remaining_seconds(
        deadline, wall_clock_seconds=wall_clock_seconds,
        wall_clock_started_at=wall_clock_started_at, now=now,
    )
    if remaining is None:
        return True
    return remaining - retry_delay >= SECTION_RESUME_COST_FLOOR_SECONDS


def _section_remaining_seconds(
    deadline: float | None,
    *,
    wall_clock_seconds: float | None,
    wall_clock_started_at: Any,
    now: Any,
) -> float | None:
    """节墙钟剩余秒数；§X-1 货 2 拆出来给门槛判定与事件共用，口径不变。"""

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
    return remaining


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


def _round_robin_by_platform(
    rows: list[dict[str, Any]],
    section_goal_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """在给定行集内按平台名稳定轮转，取满 limit 为止。"""

    if limit <= 0:
        return []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("platform") or ""), []).append(row)
    for platform_rows in buckets.values():
        platform_rows.sort(key=lambda row: (
            0 if str(row.get("goal_id")) == section_goal_id else 1,
            *_rating_sort_key(row),
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


def _section_evidence_rows(
    rows: list[dict[str, Any]],
    section_goal_id: str | None,
    *,
    limit: int = SECTION_EVIDENCE_POOL_LIMIT,
) -> list[dict[str, Any]]:
    """本节 goal 先占位，剩下的名额再按平台轮转。

    §SRC-1 货 6：原实现只按平台轮转，goal 归属仅用于**平台桶内**排序。
    三个平台就是 10/10/10，与本节写谁无关——D-013 那轮
    `sec(goal-1)` 30 个池位里只有 14 个是本节能用的，
    而 `sec(goal-3)` 名下 27 条抖音也只进得去 10 条。
    现在先给本节 goal 留够 `SECTION_GOAL_FLOOR` 个位（不足则有多少给多少），
    余额再按老规矩跨平台轮转，跨 goal 对照证据仍进得来（货 5 要用）。
    """

    if section_goal_id is None:
        return _round_robin_by_platform(rows, section_goal_id, limit)

    own = [row for row in rows if str(row.get("goal_id")) == section_goal_id]
    others = [row for row in rows if str(row.get("goal_id")) != section_goal_id]
    floor = min(limit, SECTION_GOAL_FLOOR)
    selected = _round_robin_by_platform(own, section_goal_id, floor)
    taken = {id(row) for row in selected}
    remainder = [row for row in own if id(row) not in taken] + others
    selected.extend(
        _round_robin_by_platform(remainder, section_goal_id, limit - len(selected))
    )
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
        *_rating_sort_key(row),
    ))
    return selected, quotas, actual, floor_degraded


def _evidence_index(
    rows: list[dict[str, Any]],
    allowed_goal_ids: set[str],
    *,
    section_goal_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """先按全报告稳定编号，再生成不超过 30 条的本节可见子集。"""

    identified = [row for row in rows if str(row.get("id") or "").strip()]
    # §RATE-1 货 4：D 级不进池——评级要真的决定「引不引」，就必须在这里生效。
    # 全是 D 时不把写手饿死：回退全池，让它照常写、由 rating_notes 自己说明。
    keepable = [row for row in identified if _evidence_grade(row) != "D"]
    ordered = sorted(
        keepable or identified,
        key=lambda row: (str(row.get("goal_id") or ""), *_rating_sort_key(row)),
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
            # §CMT-1 货 4/5：写手要能分清「帖子作者说的」和「读者说的」。
            "kind": str(row.get("kind") or "post"),
        }
        if item["kind"] == "comment" and row.get("parent_permalink"):
            item["parent_permalink"] = row["parent_permalink"]
        for field in (*_EVIDENCE_SCORE_FIELDS, *_EVIDENCE_RATING_FIELDS):
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
        # §OBS-1 货 1：被 D 闸拦掉的条数；全 D 回退全池时闸没生效，计 0。
        "d_gate_filtered": (len(identified) - len(keepable)) if keepable else 0,
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


def _envelope_payload(text):
    """整文本解析为节级信封；形状不合返回 None。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        isinstance(payload, Mapping)
        and isinstance(payload.get("markdown"), str)
        and isinstance(payload.get("claims"), list)
    ):
        return payload
    return None


def _coerce_section_envelope(section_path: Path) -> str | None:
    """把可救的非信封节产物在盘上规范成信封，返回兜底方式；救不了返回 None。

    只救三种形状：``` 围栏包信封 / 信封前后带说明文字 / 合格裸 Markdown 正文。
    JSON 语法坏（以 { 起头却解析不了）不救，由池校验带错误位置退回（D-025）。
    """
    try:
        raw = section_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if _envelope_payload(raw) is not None:
        return None
    stripped = raw.strip()
    candidates: list[tuple[str, str]] = []
    if stripped.startswith("```") and stripped.endswith("```"):
        first_nl = stripped.find("\n")
        if first_nl != -1:
            candidates.append(("fence_stripped", stripped[first_nl + 1 : -3]))
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(("braces_extracted", raw[start : end + 1]))
    payload = note = None
    for name, text in candidates:
        payload = _envelope_payload(text)
        if payload is not None:
            note = name
            break
    if payload is None:
        if stripped.startswith("{"):
            return None
        if "## 结论" not in raw and "## 信息源" not in raw:
            return None
        payload, note = {"markdown": raw, "claims": []}, "bare_markdown_wrapped"
    section_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    return note


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
    parse_error = None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        payload = None
        if raw_text.lstrip().startswith(("{", "[")):
            parse_error = (
                f"JSON 解析失败：{exc.msg}"
                f"（line {exc.lineno} column {exc.colno} char {exc.pos}）"
            )
    except TypeError:
        payload = None
    if (
        not isinstance(payload, Mapping)
        or not isinstance(payload.get("markdown"), str)
        or not isinstance(payload.get("claims"), list)
    ):
        message = "节产物必须使用 JSON 信封（markdown 正文 + claims 数组），裸 Markdown 不接受"
        if parse_error:
            message += f"；{parse_error}"
        return validation.Result(
            validation.Verdict.FAIL,
            "evidence_pool_only",
            message,
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


def _written_section_result(
    section_path: Path,
    allowed_urls: set[str],
) -> validation.Result:
    """判一个**已经写完的** done 节还算不算数——只看它自带的东西。

    §D-046：别拿本轮重算的角标编号去判已经写完的字。每节可见池是「写的时候
    的预算」，不是「写完之后的有效性契约」：
    - 池是本轮按账本 done 集合重算的，点名补一节必须先复位父章，父章一掉出
      done 集合，邻节的跨 goal 那一截就够不着了（真机：sec-2 底料池 30 条
      weibo+web_search+reddit，补节轮只剩 15 条 weibo）；
    - 全报告编号本身也会漂——评级回填改 `_rating_sort_key` 就重排，底料
      sec-2 的 20 个角标在今天的编号下有 14 个映到了别的 permalink。
    拿这两样任意一个当尺子，好稿都会被判死、复位、连片产物一起删、从头重写。
    补一节因此等于让整章 N 节重新抽签——两次现场都是这么把好稿写砸的。

    所以这里只留两条**不随本轮重算漂移**的判据：① 正文与 claims 引用的链接
    必须还在本轮研究的证据库里（引用了库外链接的稿子该重写，D-031 那一支）；
    ② 节自带的撰写格式契约（结论/信息源齐、角标在本节信息源里解析得了、
    没有孤儿角标）。角标↔来源的对应关系写在节自己的「信息源」块里，下游章级
    组装本来就按那一块解析，与本轮池编号无关。
    """

    try:
        raw_text = section_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return validation.Result(
            validation.Verdict.UNAVAILABLE,
            "written_section_intact",
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
            "written_section_intact",
            "节产物不是 JSON 信封（markdown 正文 + claims 数组）",
            ["json_envelope"],
        )
    markdown = payload["markdown"]
    offenders = sorted(_raw_urls(markdown) - allowed_urls)
    for claim in payload["claims"]:
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
                offenders.append(permalink)
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
    rules: list[str] = []
    for failure in (
        validation.sections_exist(inner_ctx, ["结论", "信息源"]),
        validation.citation_marks_resolvable(inner_ctx, []),
        validation.no_orphan_citation(inner_ctx, []),
    ):
        if failure.verdict is not validation.Verdict.PASS:
            rules.append(failure.name)
            offenders.extend(
                f"{failure.name}: {offender}"
                for offender in (failure.offenders or [failure.message])
            )
    if offenders:
        return validation.Result(
            validation.Verdict.FAIL,
            "written_section_intact",
            f"已写完的节不再成立，共 {len(offenders)} 处"
            + (f"（规则：{', '.join(rules)}）" if rules else ""),
            offenders,
        )
    return validation.Result(
        validation.Verdict.PASS,
        "written_section_intact",
        f"已写完的节仍成立，{len(markdown)} 字正文原样保留",
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


def _placeholder(
    section: dict[str, Any],
    reason: str,
    *,
    goal_id: str | None = None,
) -> str:
    # section.goal_id 表示本节对应的上游 goal；单节占位文件则必须
    # 使用实际写入目录所属的 context.goal_id，避免路径与内容自相矛盾。
    owner_goal_id = goal_id or section["goal_id"]
    return (
        f"## {owner_goal_id}｜{section['title']}\n\n"
        f"- 此处缺失：{owner_goal_id}/{section['section_id']}；原因：{reason}\n"
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
    section_path.write_text(
        _placeholder(section, "timeout", goal_id=context.goal_id),
        encoding="utf-8",
    )
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
            "timeout_kind": "wall_clock",
            "original_reason": "timeout",
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
    goal_id: str,
    output_path: Path,
    section_items: list[dict[str, Any]],
    missing_items: list[dict[str, Any]],
    claims: list[Any] | None = None,
) -> None:
    # §ENT-1 货 6：JSON 成稿也要有「研究对象」节。它不是某个写手写出来的节，
    # 是系统按计划的实体卡确定性生成的一节，排在最前面——读报告的人先知道这份
    # 报告说的是哪几个产品，再看正文。entities 为空时这一节整个不存在。
    entity_lines = render_entity_section(
        [item.to_dict() for item in getattr(plan, "entities", []) or []]
    )
    entity_section = [{
        "section_id": f"{_chapter_id(agent)}/entities",
        # goal_id 必须是真值：`sectioned_document_valid` 会逐节校验它
        # （沙盒重放实证：填 None 时整章判 conclusion_invalid，白跑三节）。
        "goal_id": goal_id,
        "title": "研究对象",
        "markdown": "\n".join(entity_lines).strip(),
    }] if entity_lines else []
    document = {
        "title": plan.title,
        "chapter_id": _chapter_id(agent),
        "sections": entity_section + [
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
    goal_id: str,
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

    D-026：`rows` 是**全 research** 的章账本行，多个 goal 的报告章同名（都叫
    `ch-4/sec-1`）。只按 `chapter_id` 找行会先命中别的 goal 那一行——本 goal 明明
    `done`，却被判未完成，写占位文、丢掉整节 claims（交叉验证维度恒空的直接原因）。
    行查找必须 `(goal_id, chapter_id)` 两项都比。
    """
    section_items: list[dict[str, Any]] = []
    chapter_claims: list[Any] = []
    for section in sections:
        path = section_root / section["filename"]
        row = next(
            (
                item for item in rows
                if item["chapter_id"] == section["section_id"]
                and item["goal_id"] == goal_id
            ),
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
                plan=plan, agent=agent, goal_id=goal_id, output_path=output_path,
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
            plan=plan, agent=agent, goal_id=goal_id, output_path=output_path,
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
            # §ENT-1 货 6：报告开头先说清这份报告说的是哪几个产品。
            entities=[item.to_dict() for item in getattr(plan, "entities", []) or []],
        ),
        encoding="utf-8",
    )


def _shard_pool(pool: Mapping[str, Any], start: int, size: int) -> dict[str, Any]:
    """本片可见的池：除 items 只留本片那几条外，其余字段原样带走。"""
    shard = dict(pool)
    shard["items"] = list(pool["items"])[start:start + size]
    return shard


def _shard_envelope(path: Path) -> tuple[str, list[Any]] | None:
    """读一份片产物；不是可解析的非空 `{markdown, claims}` 信封就当没有。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    markdown = payload.get("markdown")
    claims = payload.get("claims")
    if not isinstance(markdown, str) or not markdown.strip():
        return None
    return markdown.strip(), list(claims) if isinstance(claims, list) else []


def _shard_stale_citations(
    markdown: str, pool: Mapping[str, Any]
) -> set[str]:
    """片正文里落在**当前池外**的角标；空集 = 这片和本轮池对得上。

    §D-042。角标编号是按写这片时那一轮的证据池发的，池一换编号就作废：
    池里没有的角标合进节正文就是 `evidence_pool_only` 的越界项。判定口径与
    `_section_evidence_pool_result` 同源（都用节可见池的 `citation` 字段）。
    """

    marks = {
        str(item.get("citation") or "")
        for item in pool.get("items", [])
        if isinstance(item, Mapping)
    }
    return set(validation._CITATION.findall(markdown)) - marks


def _merge_shard_files(
    section_path: Path,
    shard_count: int,
    citation_numbers: Mapping[str, int] | None,
) -> int:
    """把盘上已有的片产物按片序合并成节产物；返回合进去的片数。

    一片都没有就不落盘——让下游 `artifact_empty` 照旧判空，不造假产物。
    """
    texts: list[str] = []
    claims: list[Any] = []
    for index in range(1, shard_count + 1):
        envelope = _shard_envelope(write_shard_path(section_path, index))
        if envelope is None:
            continue
        texts.append(envelope[0])
        claims.extend(envelope[1])
    if not texts:
        return 0
    section_path.write_text(
        json.dumps(
            {
                "markdown": merge_section_shards(
                    texts, citation_numbers=citation_numbers,
                ),
                "claims": claims,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return len(texts)


def _shard_prior_digest(section_path: Path, upto: int) -> str:
    """前面几片已写的结论条目摘要（每条首 40 字），给后续片防重复。"""
    digests: list[str] = []
    for index in range(1, upto + 1):
        envelope = _shard_envelope(write_shard_path(section_path, index))
        if envelope is None:
            continue
        digests.extend(item[:40] for item in section_conclusion_items(envelope[0]))
    return "\n".join(f"- {item}" for item in digests)


async def _emit(
    on_event: Any, event_type: str, data: dict[str, Any], *, is_error: bool = False,
) -> None:
    """发一条事件；on_event 同步/异步两种都认（本模块其余处逐个手写的那段）。"""
    result = on_event({"type": event_type, "data": data, "is_error": is_error})
    if inspect.isawaitable(result):
        await result


def _shard_notice(
    index: int, total: int, items: int, section_number: int, prior_digest: str,
) -> str:
    """【本片】段：把「只写本片这几条」「别重复前面写过的」讲死。

    §D-031：不这么讲，写手会拿到十条证据仍照章任务写整节，产出体量降不下来——
    分片就白切了。缺口/假设/风险这类**全节口径**的段落只让第 1 片写，
    否则 K 片各写一份，合并后是三段重复。
    """
    notice = (
        f"\n\n【本片】本节的证据池已按 {total} 片切开，你现在写第 {index}/{total} 片。"
        "上面那份『本节可引用证据池 JSON』只列了本片的条目——"
        "**只就这几条写**：`## 结论` 列表项与 `## 信息源` 条目都只覆盖它们，"
        "不要提及也不要引用不在本片池里的角标。\n"
        f"本片的 `## 结论` 列表项**不超过 {items} 条**（本片证据就这么多条，"
        "一条证据撑一条结论足够；写超了不会更全面，只会挤掉后面几片的时间）。\n"
        "本片不写节标题行、不写总起或收束段落；系统会把各片按片序合并成一节，"
        "合并时信息源按链接去重、角标沿用全局编号（同一条证据在哪一片都是同一个号）。\n"
        f"本片断言 id 固定使用 c-{section_number:02d}{index:02d}01、"
        f"c-{section_number:02d}{index:02d}02… 的区间（覆盖上文给的区间口径）。"
        "**节号也要带上**：只带片号的话，两个不同节的第 1 片会撞同一个 id，"
        "而断言 id 要求报告内唯一。\n"
        # §FIX-2 货 1「乙」：真机 250 条断言里有 20 条多写了闭集外的键（claim 顶层
        # `stance`、evidence 条目 `fetched_at`），登记是整批退回，一处不合规全丢。
        # 装配层已机械剥离兜底，这句是双保险——别让写手一开始就写多。
        "断言的键只许这四个：`id`／`text`／`evidence`／`conflict_note`；"
        "`evidence` 每条只许 `permalink`／`stance`／`firsthand`／`origin_url`。"
        "多写别的键（如把 `stance` 写到断言顶层、给证据加 `fetched_at`）会被登记退回。\n"
        "读者看到的是合并后的整节，不知道也不需要知道分片：正文、结论、缺口里"
        "**一律不要出现「本片」「分片」「第 N 片」这类字样**，"
        "要说范围就说「本节」「本次样本」。\n"
    )
    if index == 1:
        notice += (
            "『证据缺口』『假设与不确定性』『适用人群与风险提示』这类**全节口径**的段落"
            "只在本片（第 1 片）写一次，后续片不要再写；"
            "**这三段合计不超过 6 行**——第 1 片本来就比别的片多扛这三段，"
            "写长了它会是全节最慢的一片。\n"
        )
    else:
        notice += (
            "『证据缺口』『假设与不确定性』『适用人群与风险提示』已由第 1 片写过，"
            "本片不要再写。\n"
        )
    if prior_digest:
        notice += (
            "前面几片已经写过下列结论条目，**不要重复、不要再引它们的角标**：\n"
            f"{prior_digest}\n"
        )
    # 硬约束放在正文最末：第三轮重放实证，同样一句话写在中段时被无视了——
    # 5 条证据的片照写 10 条结论、5.3 KB 正文、288 s（离 300 s 硬顶只剩 12 s）。
    notice += (
        "\n【本片硬约束，写之前再读一遍】\n"
        f"1. `## 结论` 列表项**最多 {items} 条**，一条也不能多。"
        "本片只有这几条证据，多写的条目没有新证据支撑，只会挤掉后面几片的时间。\n"
        "2. 只引本片池里的角标，一个池外角标都不许出现。\n"
        "3. 不写节标题行、不写总起段与收束段。\n"
    )
    if index == 1:
        notice += "4. 全节口径的三段合计不超过 6 行。\n"
    return notice


def _merged_shard_result(
    section_task: EngineTask, runs_root: Path, store: Any,
) -> EngineRunResult:
    """全片跳过时，用盘上那份合并稿当本次节尝试的结果（§D-035）。

    不造终态、不绕校验：产物验证照旧跑 section_task 自己那串 validators
    （file_exists / sections_exist / citation_marks_resolvable / no_orphan_citation），
    验不过就仍是失败结果，交回节循环按既有闭集判 reason。也不起引擎——
    这一步是纯出口修正，零新增会话、零新增循环。
    """
    return EngineRunResult(
        conclusion=OwliResult(
            "done", str(section_task.output_path),
            "本次尝试全片跳过，节产物取盘上合并稿", [], [], [], None,
        ),
        conclusion_error=None,
        validation=validation.validate(
            _ctx(section_task, runs_root, store), section_task.validators,
        ),
        events=[],
        permission_denials=[],
    )


async def _run_section_shards(
    *,
    adapter: Any,
    section_task: EngineTask,
    section_path: Path,
    section_body: Any,
    evidence_pool: Mapping[str, Any],
    shard_sizes: list[int],
    citation_numbers: Mapping[str, int] | None,
    runs_root: Path,
    store: Any,
    on_event: Any,
    section_deadline: Any,
    section_wall_clock: float | None,
    resume_session_id: str | None,
    context: Any,
    section: dict[str, Any],
    section_number: int,
    section_attempt: int,
) -> tuple[Any, EngineTask]:
    """一节切 K 片串行跑，跑完按片序合并成节产物。

    §D-031。返回 (用来判本次节尝试成败的结果, 产生它的那个任务)——
    有片失败就返回**第一个失败片**的结果，让节循环照旧判失败、照旧按节级
    attempts 重试；重试时盘上已成的片会被跳过，只补失败那片。
    """
    total = len(shard_sizes)
    start = 0
    resume_for = resume_session_id
    last: tuple[Any, EngineTask] | None = None
    first_failure: tuple[Any, EngineTask] | None = None
    wall_clock_expired: SectionWallClockExpired | None = None
    ran_any = False
    for index, size in enumerate(shard_sizes, start=1):
        shard_path = write_shard_path(section_path, index)
        existing = _shard_envelope(shard_path)
        if existing is not None:
            stale_marks = _shard_stale_citations(existing[0], evidence_pool)
            if stale_marks:
                # §D-042 第二道闸：盘上这片是**上一轮**写的，角标按当时的池编号；
                # 本轮池换了（重放复位、证据重采），跳过复用它合出来的节正文必撞
                # 证据池唯一引用源契约 → conclusion_invalid。作废重写这一片，
                # 并把越界角标报出来，别让它静默毒死整节。
                await _emit(on_event, "write_shard_stale", {
                    "goal_id": context.goal_id,
                    "chapter_id": section["section_id"],
                    "shard": index, "shards": total,
                    "citations": sorted(stale_marks)[:10],
                    "citations_total": len(stale_marks),
                    "pool_items": len(evidence_pool.get("items", [])),
                }, is_error=True)
                shard_path.unlink(missing_ok=True)
                existing = None
        if existing is not None:
            # 上一次节尝试已经写成的片：不重跑、不重写，已写的字不白丢。
            await _emit(on_event, "write_shard_skipped", {
                "goal_id": context.goal_id,
                "chapter_id": section["section_id"],
                "shard": index, "shards": total,
            })
            start += size
            continue
        body = section_body(_shard_pool(evidence_pool, start, size), shard_path)
        body += _shard_notice(
            index, total, size, section_number,
            _shard_prior_digest(section_path, index - 1),
        )
        shard_task = replace(
            section_task, body=body, output_path=shard_path,
            agent_id=f"{section_task.agent_id}-part-{index}",
        )
        await _emit(on_event, "write_shard_started", {
            "goal_id": context.goal_id,
            "chapter_id": section["section_id"],
            "shard": index, "shards": total, "pool_items": size,
            "attempt": section_attempt,
        })
        ran_any = True
        # 片墙钟：**每片一份自己的**（口径同 RATE-3 评级片），不共用节那一个
        # 绝对时刻——共用的话第 1 片跑掉 221 s，剩下三片分 109 s，必全灭。
        # 章预算已按 节数 × WRITE_SHARD_MAX 放大，覆盖得住。
        # §D-033：这一份再夹到节那个绝对时刻里——D-033 放开节级重试后，
        # 最后一次重试可能在只剩 136 s 时放行，而片墙钟原来不看节剩余时间，
        # 单节最坏耗时会从 330×片数 涨到 330×片数×2。夹住就封回 330×片数。
        # 夹的是**上界**不是共用：片各自还是从自己起点算 330 s，只在节预算
        # 快见底时才被截短（那时本来也跑不完）。
        started_at = asyncio.get_running_loop().time()
        shard_deadline = (
            started_at + section_wall_clock
            if section_wall_clock is not None
            else section_deadline
        )
        if shard_deadline is not None and section_deadline is not None:
            shard_deadline = min(shard_deadline, section_deadline)
        shard_attempt = 0
        engine_attempts = 0
        offpool_rewrites = 0
        while True:
            shard_attempt += 1
            engine_attempts += 1
            try:
                result = await _run_before_section_deadline(
                    adapter, shard_task,
                    _ctx(shard_task, runs_root, store, resume_session_id=resume_for),
                    on_event, shard_deadline,
                )
            except SectionWallClockExpired as expired:
                # 这一片跑满了**自己那份**墙钟。原样往上抛会掀掉整节，
                # 后面的片连跑都跑不上——与「一片失败后面照跑」相反。
                # 记下来、当这片失败、继续下一片；末尾若一个片都没跑出结果，
                # 再把它抛出去交给既有的节超时收尾。
                wall_clock_expired = expired
                result = None
                succeeded = False
                break
            resume_for = None
            succeeded = (
                bool(getattr(result, "succeeded", False))
                and _shard_envelope(shard_path) is not None
            )
            if succeeded:
                envelope = _shard_envelope(shard_path)
                offpool_marks = (
                    _shard_stale_citations(envelope[0], evidence_pool)
                    if envelope is not None else set()
                )
                if offpool_marks and offpool_rewrites < 2:
                    # §D-045 货 1：新写片也可能幻觉池外角标。不要等合并后把
                    # 其余好片一起作废；只删这一片，用同一 prompt 定向重写。
                    offpool_rewrites += 1
                    await _emit(on_event, "write_shard_offpool", {
                        "goal_id": context.goal_id,
                        "chapter_id": section["section_id"],
                        "shard": index, "shards": total,
                        "citations": sorted(offpool_marks)[:10],
                        "citations_total": len(offpool_marks),
                        "attempt": offpool_rewrites,
                    }, is_error=True)
                    shard_path.unlink(missing_ok=True)
                    pool_marks = [
                        str(item.get("citation") or "").strip("[]")
                        for item in evidence_pool.get("items", [])
                        if isinstance(item, Mapping) and item.get("citation")
                    ]
                    pool_label = (
                        f"{pool_marks[0]}–{pool_marks[-1]}" if pool_marks else ""
                    )
                    shard_task = replace(
                        shard_task,
                        body=(
                            f"{body}\n\n【角标越池重写】上一稿引用了池外角标；"
                            f"只能引用池内角标 {pool_label}，一个池外角标都不许出现。\n"
                        ),
                    )
                    shard_attempt = 0
                    resume_for = None
                    continue
                break
            if shard_attempt >= SECTION_RETRY_MAX_ATTEMPTS:
                break
            reason = section_failure_reason(result, shard_path)
            if not _is_transport_failure(result, reason):
                break
            # 片内传输断连：在**本片墙钟内**原地重试，不占节级 attempts。
            # 实测断连水位约 0.3 次/分钟，一节四片跑近十分钟必撞好几次；
            # 让它掉到节级重试去，3 次节尝试会被断连烧光（第二轮重放实证）。
            remaining = (
                shard_deadline - asyncio.get_running_loop().time()
                if shard_deadline is not None
                else None
            )
            if remaining is not None and remaining < SECTION_RESUME_COST_FLOOR_SECONDS:
                break
            resume_for = str(getattr(result, "session_id", None) or "") or None
            await _emit(on_event, "write_shard_retry", {
                "goal_id": context.goal_id,
                "chapter_id": section["section_id"],
                "shard": index, "shards": total, "attempt": shard_attempt + 1,
                "resume": bool(resume_for), "remaining_seconds": remaining,
            })
        await _emit(on_event, "write_shard_finished", {
            "goal_id": context.goal_id,
            "chapter_id": section["section_id"],
            "shard": index, "shards": total, "succeeded": succeeded,
            "reason": None if succeeded else (
                "timeout" if result is None
                else section_failure_reason(result, shard_path)
            ),
            "attempts": engine_attempts,
            "elapsed_seconds": asyncio.get_running_loop().time() - started_at,
            "engine_error": getattr(result, "engine_error", None),
        }, is_error=not succeeded)
        if result is not None:
            last = (result, shard_task)
            if not succeeded and first_failure is None:
                first_failure = (result, shard_task)
        start += size
    merged = _merge_shard_files(section_path, total, citation_numbers)
    if wall_clock_expired is not None:
        # 有片跑满了自己那份墙钟：本次节尝试按 timeout 收尾（交回既有那段，
        # 不自造终态）。**先把本轮跑成的片合并落盘**再抛——它们的字留在片产物里，
        # 下一次节尝试直接跳过，不用重写。
        raise wall_clock_expired
    await _emit(on_event, "write_shards_merged", {
        "goal_id": context.goal_id,
        "chapter_id": section["section_id"],
        "shards": total, "done": merged,
    })
    if (
        not ran_any
        and merged == total
        and merged > 0
        and _shard_envelope(section_path) is not None
    ):
        # §D-035：这一次节尝试一片都没跑——因为所有片在上一次尝试里已经落盘，
        # 合并稿也已经在盘上（22.7 KB 真稿）。原来这里返回 (None, …)，节循环
        # 把 None 判成 empty_result，把刚合出来的整节挪进 .rejected.md。
        # 盘上有完整合并稿 = 本次尝试的产物已经具备，按成功出口交回节循环，
        # 由它照旧跑 conclusion / 证据池格式契约那两道闸。
        return _merged_shard_result(section_task, runs_root, store), section_task
    return first_failure or last or (None, section_task)


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
            section_path = section_root / section["filename"]
            reentry_note = _coerce_section_envelope(section_path)
            if reentry_note:
                reentry_event = on_event({
                    "type": "section_envelope_coerced",
                    "data": {
                        "goal_id": context.goal_id,
                        "chapter_id": section["section_id"],
                        "kind": reentry_note,
                    },
                    "is_error": False,
                })
                if inspect.isawaitable(reentry_event):
                    await reentry_event
            # §D-046：按「已写完的节」判，不拿本轮重算的每节可见池当尺子。
            if _written_section_result(
                section_path, all_evidence_urls,
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
                    section_path = section_root / section["filename"]
                    section_path.unlink(missing_ok=True)
                    # §D-031：这一支是「已 done 的节角标失效、必须重写」。
                    # 片产物在这里**必须一起删**——别处（节级重试）留着它们是为了
                    # 不重写已写的字，但这里正文本身过期了，留着只会被原样合并回来。
                    for stale in section_root.glob(f"{section_path.stem}.part.*.md"):
                        stale.unlink(missing_ok=True)
            existing = {
                row["chapter_id"]: row
                for row in store.list_chapters(plan.research_id)
                if row["goal_id"] == context.goal_id
            }
    truncation_event_emitted = False
    # §OBS-1 货 1：每节只发一条组池组成事件；重试重组池不重复发。
    pool_composed_section_ids: set[str] = set()
    for section_number, section in enumerate(sections, start=1):
        row = existing.get(section["section_id"])
        if row and row["status"] in {"done", "missing"}:
            if row["status"] == "done":
                # §D-046：已写完的节一个字都不重写、不调引擎。点名补一节必须先
                # 复位父章（否则节化撰写压根不会被走到），复位之后同章的好稿就
                # 全靠这一支保住——不然补一节 = 整章 N 节重新抽签，每签都有违
                # 契约的概率，两次现场就是这么把 19 637 B / 24 142 B 的好稿写成
                # 一百多字节占位的。跳过要发事件，判据才落得到库上。
                done_path = section_root / section["filename"]
                skipped = on_event({
                    "type": "write_section_skipped",
                    "data": {
                        "goal_id": context.goal_id,
                        "chapter_id": section["section_id"],
                        "bytes": (
                            done_path.stat().st_size if done_path.is_file() else 0
                        ),
                    },
                    "is_error": False,
                })
                if inspect.isawaitable(skipped):
                    await skipped
            continue
        section_attempt = 0
        section_deadline = (
            asyncio.get_running_loop().time() + section_wall_clock
            if section_wall_clock is not None
            else None
        )
        section_wall_clock_started_at = now() if callable(now) else None
        # §D-031：节级重试的那道闸（「剩余节墙钟够不够再跑一次」）用的是**节**墙钟。
        # 分片后每片各拿一份自己的墙钟，四片跑完早就超出原来那一份，于是闸永远关着——
        # 真跑实证：ch-5/sec-1 四片写成三片，只因一片断连就直接落 missing、
        # 一次节级重试都没有（timeout_kind=resume_floor）。所以节这边也要按片数放大。
        # 放大只做一次（`shard_budget_applied`），且只在真的切了多片时做——
        # 池 ≤ 一片装得下的节一秒不多给，行为与分片前逐字相同。
        section_wall_clock_effective = section_wall_clock
        shard_budget_applied = False
        resume_session_id: str | None = None
        envelope_retry_source: str | None = None

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
            # §OBS-1 货 1：组池出口发组成事件（只加事件，零语义改动）。
            if section["section_id"] not in pool_composed_section_ids:
                pool_items = evidence_pool["items"]
                own_count = sum(
                    1 for item in pool_items
                    if str(item.get("goal_id")) == str(section["goal_id"])
                )
                platform_counts: dict[str, int] = {}
                grade_counts: dict[str, int] = {}
                for item in pool_items:
                    platform_key = str(item.get("platform") or "")
                    platform_counts[platform_key] = (
                        platform_counts.get(platform_key, 0) + 1
                    )
                    grade_key = str(item.get("grade") or "unrated")
                    grade_counts[grade_key] = grade_counts.get(grade_key, 0) + 1
                pool_event = on_event({
                    "type": "section_pool_composed",
                    "data": {
                        "research_id": plan.research_id,
                        "section_id": section["section_id"],
                        "goal_id": str(section["goal_id"]),
                        "pool_size": len(pool_items),
                        "own_goal_count": own_count,
                        "cross_goal_count": len(pool_items) - own_count,
                        "platform_distribution": platform_counts,
                        "grade_distribution": grade_counts,
                        "d_gate_filtered": int(evidence_pool["d_gate_filtered"]),
                    },
                    "is_error": False,
                })
                if inspect.isawaitable(pool_event):
                    await pool_event
                pool_composed_section_ids.add(section["section_id"])
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

            def _section_body(
                prompt_pool: dict[str, Any], target_path: Path,
            ) -> str:
                """§D-031：正文按传进来的池与产物路径组装。

                整节一次写时传全池与节路径；分片时传本片那几条与**本片的路径**——
                路径这句必须跟着片走：第一次重放实测，正文里还写着节路径时，
                写手照它把整份信封写进了 `sec-1.md`，片产物落空、片判失败。
                """
                body = (
                    f"{base_task.body}\n\n"
                    "本次只写一个报告节；禁止生成整份报告。\n"
                    "本节须包含一个『结论』小节与一个『信息源』小节（标题逐字使用），"
                    "Markdown 标题分别写为 `## 结论` 与 `## 信息源`，且两个小节正文均不得为空。\n"
                    # §SRC-1 货 5：原文是「只覆盖本节范围」，写手照办后把池里
                    # 其他 goal 的证据全部跳过——D-013 那轮 sec-1 可见池里躺着 10 条
                    # 抖音（S73–S82）却一条没引，正文还专门声明「与本 goal 新增的
                    # douyin、x 采集不重叠」。写手没错，是这句话把它挡住了。
                    "本节以『节目标』给出的目标为主线；结论与信息源不得替其他报告节做总结，"
                    "但**可以引用本节证据池里其他目标的证据做对照或佐证**——"
                    "引用时须在句中点明它来自哪个目标（例如「goal-3 的抖音证据显示……」），"
                    "且这类对照不得喧宾夺主，占本节结论的少数。\n"
                    "本次产物路径（写文件与 owli-result.output_path 都必须逐字使用）："
                    f"{target_path}\n"
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
                    # §CMT-1 货 4/5：评论二跳把读者反应也放进了池子。
                    "证据池里 kind=\"comment\" 的条目是**读者在该帖下的评论**，"
                    "不是帖子作者的说法，parent_permalink 指向它的父帖。"
                    "引用这类条目时正文要点明是评论（如『该帖评论区有用户提到…』），"
                    "不得当成作者本人或平台官方的表述。"
                    "若某条评论的观点与它父帖正文相反，在 claims 里给这条 evidence "
                    "写 stance=contradicts——这正是交叉验证要看见的分歧。\n"
                    "本节可引用证据池 JSON（唯一引用源）：\n"
                    f"{json.dumps(prompt_pool, ensure_ascii=False, indent=2)}"
                )
                if (
                    base_task.agent_kind in SECTIONED_KINDS
                    and _declared_shape(agent) != "array"
                ):
                    body += (
                        # §FE-1 货 2：原文只说「放独立的『证据缺口』段」，没说几级标题。
                        # 写手照办却写成 `### 证据缺口` 挂在 `## 结论` 底下，落进
                        # citation_marks_resolvable 的结论子树，于是「本节没有官方保修
                        # 数据」这类**天然无源可引**的话被逐条要求带 [Sxx]，必挂。
                        # D-025 重放 6 节里 5 节栽在这，19 个被点名项 18 个是这么来的；
                        # 同章唯一通过的那节把这几段写成了 `##` 同级——只差一个井号。
                        "\n缺口/限制性陈述（『本节可见证据不足』『未覆盖 X』这类）"
                        "不许进『结论』列表，放独立的『证据缺口』段。"
                        "『证据缺口』『假设与不确定性』『适用人群与风险提示』这类段落"
                        "**必须写成 `##` 二级标题，与 `## 结论` 同级**；"
                        "`## 结论` 段内不得出现任何 `###` 及更深的子标题——"
                        "写成子标题会被判成结论条目，而缺口陈述本就没有证据可引，必被退。\n"
                        "『结论』列表每一项必须带至少一个 [Sxx]。"
                        "角标一律写成 `[S01]` 这样的**半角方括号**，多个并排写 `[S01][S02]`；"
                        "禁止写成 `（S01、S02）`、`(S01, S02)` 这类括号内联形式——"
                        "括号形式识别不出来，同样会被判成没带角标。\n"
                        "**先按证据池里的 grade 分层**（A/B 已通过可靠度审计、C 存疑、"
                        "空 = 本节写作时还没评到；D 级已被系统挡在池外）：A/B 可以直陈、"
                        "逐条当事实引；C 与空等级只作旁证，必须带上限定语（『有用户反馈…』"
                        "『尚待其他来源印证』），不得单独支撑结论。同层内再按来源类型区分——"
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
                        "若本节确无可证否结论，claims 写空数组。\n"
                        "输出骨架示例（照此形状写，只换内容）：\n"
                        # §FE-1 货 2：示例里补上 `## 证据缺口`，让「独立一段」有形可依——
                        # 此前示例只有结论与信息源两段，写手只能自己猜层级。
                        '{"markdown": "# 节标题\\n\\n## 结论\\n\\n- 结论一 [S01][S02]\\n\\n'
                        '## 证据缺口\\n\\n- 未覆盖 X：本节可见证据里没有 …\\n\\n'
                        '## 信息源\\n\\n- [S01] [标题一](permalink1)\\n- [S02] [标题二](permalink2)", '
                        '"claims": [{"id": "c-0101", "text": "断言原文", '
                        '"evidence": [{"permalink": "https://…"}]}]}\n'
                        "整份节产物的第一个字符必须是 `{`；不得使用 ``` 代码围栏，"
                        "不得在 JSON 之外附加任何说明文字。"
                    )
                if envelope_retry_source is not None:
                    # D-025 货 4：定向重试只要求补包信封，不重写正文。
                    body += (
                        "\n上一次尝试的节产物因缺 JSON 信封被退。"
                        "下面是被退原文：只需把它原样包进 JSON 信封"
                        "（markdown 字段放正文、claims 数组按上述规则补），"
                        "不要重写正文；若原文本身是语法损坏的 JSON，"
                        "修正语法后输出完整信封。\n"
                        f"被退原文：\n{envelope_retry_source}"
                    )
                return body

            # §D-031：按证据池切片。切完只得一片（池 ≤ 一片装得下）时
            # shard_sizes 长度为 1，下面 shard_count == 1，走的仍是分片前那条路。
            shard_sizes = write_shard_sizes(evidence_pool["items"])
            shard_count = max(1, len(shard_sizes))
            if (
                shard_count > 1
                and section_wall_clock is not None
                and not shard_budget_applied
            ):
                extra = section_wall_clock * (shard_count - 1)
                if section_deadline is not None:
                    section_deadline += extra
                section_wall_clock_effective = section_wall_clock * shard_count
                shard_budget_applied = True
            body = _section_body(evidence_pool, section_path)
            section_task = replace(
                base_task,
                body=body,
                output_path=section_path,
                output_format="markdown",
                agent_id=f"{base_task.agent_id}-{section['filename'].removesuffix('.md')}",
                validators=["file_exists"],
                capability=base_task.capability,
            )
            # §D-031：engine_task 是「真正跑出这个结果的那个任务」——不分片时
            # 就是 section_task，分片时是失败/最后那一片的任务。下面两条既有的
            # 定向重试（resume 失败重跑、结论块不合法）都对着它重发，才不会打到空处。
            engine_task = section_task
            try:
                if shard_count > 1:
                    result, engine_task = await _run_section_shards(
                        adapter=adapter,
                        section_task=section_task,
                        section_path=section_path,
                        section_body=_section_body,
                        evidence_pool=evidence_pool,
                        shard_sizes=shard_sizes,
                        citation_numbers=citation_numbers,
                        runs_root=runs_root,
                        store=store,
                        on_event=on_event,
                        section_deadline=section_deadline,
                        section_wall_clock=section_wall_clock,
                        resume_session_id=resume_session_id,
                        context=context,
                        section=section,
                        section_number=section_number,
                        section_attempt=section_attempt,
                    )
                else:
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
                # §D-033：分片节里这个异常来自**一片**跑满自己那份墙钟，不等于
                # 整节预算用完——已成片就在盘上（`_run_section_shards` 抛之前已经
                # 合并落盘），一次节级重试只补坏片、其余片 `write_shard_skipped`，
                # 比整节作废便宜一个数量级。真跑实证 goal-2 ch-6/sec-2：四片写成
                # 三片、节 attempts 只用了 1/3 就整节 missing，与 §D-031「已写的
                # 字不白丢」相悖。闸沿用节级重试那一套、不新造：attempts 未满 +
                # 剩余节墙钟（已按片数放大）还够一次 resume 成本下限。不分片的节
                # （shard_count == 1）走的仍是原路：那时异常本就意味着节墙钟到点，
                # 这道闸必然关着。resume=True 指**片级** resume——不续引擎会话，
                # 靠盘上已成的片跳过。
                if (
                    shard_count > 1
                    and section_attempt < attempt_budget
                    and _section_resume_within_deadline(
                        section_deadline,
                        retry_delay=retry_delay,
                        wall_clock_seconds=section_wall_clock_effective,
                        wall_clock_started_at=section_wall_clock_started_at,
                        now=now,
                    )
                ):
                    await _emit_section_retry(
                        on_event,
                        context=context,
                        section=section,
                        attempt=section_attempt + 1,
                        resume=True,
                        session_id=None,
                    )
                    await _wait_before_section_retry(timer, retry_delay)
                    resume_session_id = None
                    continue
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
            if shard_count > 1 and engine_task is not section_task:
                # §D-031：上面两条既有重试（resume 失败重跑 / 结论块定向重试）
                # 打在**片任务**上，重跑成功也只更新片产物；不再合并一次的话，
                # 节产物还是重试前那一份。合并是幂等的，白跑一次也没副作用。
                _merge_shard_files(section_path, shard_count, citation_numbers)
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
            pool_result = None
            if persist_goal_evidence is not None and not artifact_empty:
                coerce_note = _coerce_section_envelope(section_path)
                if coerce_note:
                    coerced_event = on_event({
                        "type": "section_envelope_coerced",
                        "data": {
                            "goal_id": context.goal_id,
                            "chapter_id": section["section_id"],
                            "kind": coerce_note,
                        },
                        "is_error": False,
                    })
                    if inspect.isawaitable(coerced_event):
                        await coerced_event
                pool_result = _section_evidence_pool_result(
                    section_path, evidence_pool, all_evidence_urls,
                )
            pool_failed = bool(
                pool_result is not None
                and pool_result.verdict is not validation.Verdict.PASS
            )
            merged_envelope = (
                _shard_envelope(section_path)
                if shard_count > 1 and pool_failed else None
            )
            merged_offpool = bool(
                merged_envelope is not None
                and _shard_stale_citations(merged_envelope[0], evidence_pool)
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
                # §X-1 货 2：timeout 分三种——引擎单次超时 / 136s 门槛 / 节墙钟到点。
                original_reason = reason
                timeout_kind = "engine_timeout" if reason == "timeout" else None
                if section_attempt < attempt_budget and transport_failure:
                    # 传输断连不是「这一节问不出来」，只是链路断了：原地退避重试，
                    # 不落 missing、不发 section_error、不换引擎（引擎选择归适配层）。
                    if _section_resume_within_deadline(
                        section_deadline,
                        retry_delay=retry_delay,
                        wall_clock_seconds=section_wall_clock_effective,
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
                    # §X-1 货 2：改写前把真实原因留住并发 section_retry_skipped，
                    # 让「被 136s 门槛挡住」与真墙钟超时在事件里分得开（闭集不动）。
                    original_reason = reason
                    timeout_kind = "resume_floor"
                    remaining_seconds = _section_remaining_seconds(
                        section_deadline,
                        wall_clock_seconds=section_wall_clock_effective,
                        wall_clock_started_at=section_wall_clock_started_at,
                        now=now,
                    )
                    skipped = on_event({
                        "type": "section_retry_skipped",
                        "data": {
                            "goal_id": context.goal_id,
                            "chapter_id": section["section_id"],
                            "attempt": section_attempt,
                            "original_reason": original_reason,
                            "remaining_seconds": remaining_seconds,
                            "retry_delay": retry_delay,
                            "resume_floor_seconds": SECTION_RESUME_COST_FLOOR_SECONDS,
                        },
                        "is_error": False,
                    })
                    if inspect.isawaitable(skipped):
                        await skipped
                    reason = "timeout"
                if (
                    shard_count > 1
                    and (not pool_failed or merged_offpool)
                    and section_attempt < attempt_budget
                    and _section_resume_within_deadline(
                        section_deadline,
                        retry_delay=retry_delay,
                        wall_clock_seconds=section_wall_clock_effective,
                        wall_clock_started_at=section_wall_clock_started_at,
                        now=now,
                    )
                ):
                    # §D-036：分片节里 result 是**第一个失败片**的结果，它的失败
                    # 不等于整节问不出来——其余片的字已经合并落盘了。D-033 只给
                    # 片墙钟到点（`SectionWallClockExpired`）开了这条补坏片的路，
                    # 引擎硬顶 300 s、codex 传输断、codex 无产物这三种片级失败各自
                    # 落 missing，一片坏就作废 17–23 KB 好稿（真跑 r-e74541583a05
                    # §g #3/#4/#5，节 attempts 只用了 1/3、剩余预算还有 ~580 s）。
                    # 这里把判定统一到同一道闸上：不论片级失败的 reason 是什么，
                    # attempts 未满且剩余节墙钟（已按片数放大）还够一次 resume
                    # 成本下限，就只补坏片、已成片 `write_shard_skipped`。
                    # 闸沿用既有那一套、不新造，常量一个不动。
                    # 三处不动：① 不分片的节（shard_count == 1）逐字照旧；
                    # ② pool_failed 通常是**合并后**节产物的格式/信封失败，仍走
                    #    下面 D-025 定向重试；§D-045 只给其中角标越池这一种开门，
                    #    让下一次节尝试借 D-042 守卫只删掉污染片再写；
                    # ③ attempts 用尽或余量不足时 reason / timeout_kind 原样落账，
                    #    这里只决定「要不要再来一次」，不改写失败原因。
                    # resume=True 指**片级** resume——不续引擎会话，靠盘上已成的片跳过。
                    await _emit_section_retry(
                        on_event,
                        context=context,
                        section=section,
                        attempt=section_attempt + 1,
                        resume=True,
                        session_id=None,
                    )
                    await _wait_before_section_retry(timer, retry_delay)
                    resume_session_id = None
                    continue
                if (
                    pool_failed
                    and pool_result is not None
                    and list(pool_result.offenders) == ["json_envelope"]
                    and section_attempt < attempt_budget
                    and envelope_retry_source is None
                ):
                    # D-025 货 4：信封失败给一次定向重试（正文多半合格，
                    # 只要求补包信封）；不加时间，剩余节墙钟不足一次
                    # 会话成本下限就不给，直接如实落终态。
                    envelope_remaining = _section_remaining_seconds(
                        section_deadline,
                        wall_clock_seconds=section_wall_clock_effective,
                        wall_clock_started_at=section_wall_clock_started_at,
                        now=now,
                    )
                    if (
                        envelope_remaining is None
                        or envelope_remaining
                        >= SECTION_RESUME_COST_FLOOR_SECONDS
                    ):
                        try:
                            envelope_retry_source = section_path.read_text(
                                encoding="utf-8",
                            )
                        except (OSError, UnicodeError):
                            envelope_retry_source = ""
                        retry_event = on_event({
                            "type": "section_envelope_retry",
                            "data": {
                                "goal_id": context.goal_id,
                                "chapter_id": section["section_id"],
                                "attempt": section_attempt + 1,
                                "remaining_seconds": envelope_remaining,
                            },
                            "is_error": False,
                        })
                        if inspect.isawaitable(retry_event):
                            await retry_event
                        continue
                rejected_path = _preserve_rejected_artifact(section_path)
                conclusion_error = _conclusion_error_with_rejected_path(
                    conclusion_error, rejected_path,
                )
                section_path.write_text(
                    _placeholder(section, reason, goal_id=context.goal_id),
                    encoding="utf-8",
                )
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
                        "timeout_kind": timeout_kind,
                        "original_reason": original_reason,
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
            goal_id=context.goal_id,
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
