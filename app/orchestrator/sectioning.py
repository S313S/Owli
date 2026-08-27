"""执行期判断章节化：短调用落节文件，确定性拼装父章产物。"""

from __future__ import annotations

import asyncio
import inspect
import json
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

#: 拿不到适配器超时时的「一次引擎超时」兜底口径，与两个引擎的默认值同档。
FALLBACK_ENGINE_TIMEOUT_SECONDS = 300.0


class SectionAssemblyShapeError(ValueError):
    """确定性的组装失败：节产物形状与章声明的 shape 对不上。"""


def _section_attempt_budget() -> int:
    return SECTION_RETRY_MAX_ATTEMPTS


def _declared_shape(agent: Any) -> str:
    output = getattr(agent, "output", None)
    if isinstance(output, Mapping):
        return str(output.get("shape", "") or "").strip().casefold()
    return ""


def _retry_within_deadline(
    now: Any, deadline_at: Any, engine_timeout_seconds: float,
) -> bool:
    """剩余墙钟不足一次引擎超时就不再派：节级重试总和受章墙钟约束（期望 c）。"""

    if deadline_at is None or now is None:
        return True
    try:
        remaining = (deadline_at - now()).total_seconds()
    except (AttributeError, TypeError):
        return True
    return remaining >= engine_timeout_seconds


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


def _ctx(task: EngineTask, runs_root: Path, store: Any) -> validation.Ctx:
    return validation.Ctx(
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
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if output_format == "json" and _declared_shape(agent) != "array":
                try:
                    fragment = json.loads(text)
                except (json.JSONDecodeError, UnicodeError):
                    fragment = None
                if isinstance(fragment, Mapping) and "markdown" in fragment:
                    markdown = fragment.get("markdown")
                    claims = fragment.get("claims", [])
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
            "done": bool(
                path.is_file() and (row is None or row["status"] == "done")
            ),
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
    if deadline_at is None:
        deadline_at = getattr(context, "deadline_at", None)
    engine_timeout = float(
        engine_timeout_seconds
        if engine_timeout_seconds is not None
        else FALLBACK_ENGINE_TIMEOUT_SECONDS
    )
    retry_delay = float(
        CHAPTER_RETRY_INTERVAL_SECONDS.get(getattr(plan, "scale", ""), 0.0)
    )
    for section_number, section in enumerate(sections, start=1):
        row = existing.get(section["section_id"])
        if row and row["status"] in {"done", "missing"}:
            continue
        section_attempt = 0
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
            current_rows = store.list_chapters(plan.research_id)
            inputs = _ledger_inputs(current_rows, section["goal_id"])
            chapter = agent.chapter if isinstance(agent.chapter, Mapping) else {}
            opening = chapter.get("opening", {})
            declared_inputs = (
                opening.get("inputs", []) if isinstance(opening, Mapping) else []
            )
            inputs = _merge_declared_done_inputs(
                inputs,
                current_rows,
                declared_inputs,
                research_root=runs_root / plan.research_id,
            )
            section_path = section_root / section["filename"]
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
                "产物 path 只用于定位，不是 permalink；角标 permalink 必须逐字取自 done 产物内容。"
                "结构化派生产物若同时给出实体条目及该实体的 permalink 来源，"
                "可逐字复用该 permalink 支撑同一实体条目中的判断；不得跨实体挪用。"
                "实体对应可由显式字段，或 permalink 中无歧义的品牌或模型标识确认；"
                "不得只按 sources 数组顺序猜测实体对应。"
                "若内容没有可支撑判断的 permalink，原位标注缺失；"
                "不得把本地路径改写成 file:// 角标，也不得编造 URL。\n"
                f"{json.dumps(inputs, ensure_ascii=False, indent=2)}"
            )
            if base_task.output_format == "json" and _declared_shape(agent) != "array":
                body += (
                    "\n本章最终产物是 JSON 节化文档信封。本节须显式写 JSON object："
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
            result = adapter.run(
                section_task, _ctx(section_task, runs_root, store), on_event=on_event,
            )
            if inspect.isawaitable(result):
                result = await result
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
                result = adapter.run(
                    retry_task,
                    _ctx(retry_task, runs_root, store),
                    on_event=on_event,
                )
                if inspect.isawaitable(result):
                    result = await result
            succeeded = bool(getattr(result, "succeeded", False)) and not artifact_empty
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
                reason = section_failure_reason(result, section_path)
                engine_error = getattr(result, "engine_error", None)
                conclusion_error = getattr(result, "conclusion_error", None)
                if (
                    section_attempt < attempt_budget
                    and _is_transport_failure(result, reason)
                ):
                    # 传输断连不是「这一节问不出来」，只是链路断了：原地退避重试，
                    # 不落 missing、不发 section_error、不换引擎（引擎选择归适配层）。
                    if _retry_within_deadline(now, deadline_at, engine_timeout):
                        await _wait_before_section_retry(timer, retry_delay)
                        continue
                    # 剩余墙钟不足一次引擎超时：停派并按 timeout 定终态（期望 c）。
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
