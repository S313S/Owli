"""执行期判断章节化：短调用落节文件，确定性拼装父章产物。"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app.adapters import validation
from app.adapters.contracts import EngineTask
from app.adapters.ratelimit import classify_transport_error
from app.orchestrator.scheduler import TaskRunResult


SECTIONED_KINDS = {"cross_validation", "summary", "report", "report_writing"}


def should_section(kind: str, output_format: str) -> bool:
    return kind in SECTIONED_KINDS and output_format in {"markdown", "json"}


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


def section_failure_reason(
    result: Any, output_path: Path | None = None,
) -> str:
    """把适配器失败归一为章账本闭集，不用统一的重试耗尽掩盖死因。"""

    conclusion = getattr(result, "conclusion", None)
    declared = str(getattr(conclusion, "reason", "") or "").casefold()
    denials = [
        *list(getattr(result, "permission_denials", None) or []),
        *list(getattr(conclusion, "capability_denials", None) or []),
    ]
    if declared == "tool_unavailable" or denials:
        return "tool_unavailable"
    if declared in {"empty_result", "quota_exhausted", "retry_exhausted"}:
        return declared
    errors = " ".join(filter(None, (
        str(getattr(result, "engine_error", "") or ""),
        str(getattr(result, "conclusion_error", "") or ""),
    )))
    if re.search(r"quota|rate.?limit|429|额度|限流", errors, re.IGNORECASE):
        return "quota_exhausted"
    if errors and classify_transport_error(errors):
        return "retry_exhausted"
    if output_path is not None:
        try:
            if (
                not output_path.is_file()
                or not output_path.read_text(encoding="utf-8").strip()
            ):
                return "empty_result"
        except (OSError, UnicodeError):
            return "empty_result"
    conclusion_error = str(getattr(result, "conclusion_error", "") or "")
    report = getattr(result, "validation", None)
    if (
        conclusion is None
        and conclusion_error
        and getattr(report, "verdict", None) is validation.Verdict.PASS
    ):
        return "conclusion_invalid"
    output_path = str(getattr(conclusion, "output_path", "") or "").strip()
    if conclusion is None or not output_path:
        return "empty_result"
    return "retry_exhausted"


def _assemble(
    *,
    plan: Any,
    agent: Any,
    output_path: Path,
    section_root: Path,
    sections: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    section_texts: list[str] = []
    for section in sections:
        path = section_root / section["filename"]
        if path.is_file():
            section_texts.append(path.read_text(encoding="utf-8").strip())
        else:
            chapter_id = section["section_id"]
            row = next(
                (item for item in rows if item["chapter_id"] == chapter_id), None,
            )
            reason = str(row["reason"] if row else "empty_result")
            section_texts.append(_placeholder(section, reason).strip())
    blocks = [f"# {plan.title}", ""]
    for text in section_texts:
        blocks.append(text)
        blocks.append("")
    blocks.append("## 缺失清单")
    missing = [
        row for row in rows
        if row["status"] == "missing" or (
            row["status"] == "deferred" and "/" in str(row["chapter_id"])
        )
    ]
    if missing:
        for row in missing:
            blocks.append(
                f"- 此处缺失：{row['goal_id']}/{row['chapter_id']}；原因：{row['reason']}"
            )
    else:
        blocks.append("- 无。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")


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
    for section in sections:
        row = existing.get(section["section_id"])
        if row and row["status"] in {"done", "missing"}:
            continue
        started = store.start_chapter(
            plan.research_id,
            context.goal_id,
            section["section_id"],
            engine=context.engine,
            updated_at=now_iso(),
        )
        if not started:
            continue
        current_rows = store.list_chapters(plan.research_id)
        inputs = _ledger_inputs(current_rows, section["goal_id"])
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
            "节输入只含该 goal 下 done 章 output.path 与 missing 章 reason：\n"
            f"{json.dumps(inputs, ensure_ascii=False, indent=2)}"
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
        else:
            reason = section_failure_reason(result, section_path)
            engine_error = getattr(result, "engine_error", None)
            conclusion_error = getattr(result, "conclusion_error", None)
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
    rows = store.list_chapters(plan.research_id)
    section_ids = {section["section_id"] for section in sections}
    done_count = sum(
        1
        for row in rows
        if row["goal_id"] == context.goal_id
        and row["chapter_id"] in section_ids
        and row["status"] == "done"
    )
    _assemble(
        plan=plan,
        agent=agent,
        output_path=base_task.output_path,
        section_root=section_root,
        sections=sections,
        rows=rows,
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
            else "retry_exhausted"
        )
        return TaskRunResult(
            False,
            engine=context.engine,
            failure_feedback="所有报告节均未完成；占位报告已落盘",
            chapter_status="missing",
            reason=reason,
            actual_output_path=str(base_task.output_path),
            actual_count=0,
        )
    final_validation = validation.validate(
        _ctx(base_task, runs_root, store), base_task.validators,
    )
    if final_validation.verdict is not validation.Verdict.PASS:
        failures = [
            {
                "name": item.name,
                "message": item.message,
                "offenders": list(item.offenders),
            }
            for item in final_validation.failures
        ]
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
