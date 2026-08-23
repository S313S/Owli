"""执行期判断章节化：短调用落节文件，确定性拼装父章产物。"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app.adapters import validation
from app.adapters.contracts import EngineTask
from app.orchestrator.chapter_failure import (
    chapter_failure_reason as section_failure_reason,
)
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
    for section in sections:
        path = section_root / section["filename"]
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
        else:
            chapter_id = section["section_id"]
            row = next(
                (item for item in rows if item["chapter_id"] == chapter_id), None,
            )
            reason = str(row["reason"] if row else "empty_result")
            text = _placeholder(section, reason).strip()
        section_items.append({
            "section_id": section["section_id"],
            "goal_id": section["goal_id"],
            "title": section["title"],
            "markdown": text,
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
    if output_format == "json":
        document = {
            "title": plan.title,
            "chapter_id": _chapter_id(agent),
            "sections": section_items,
            "缺失清单": missing_items,
        }
        output_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return
    blocks = [f"# {plan.title}", ""]
    for item in section_items:
        blocks.append(item["markdown"])
        blocks.append("")
    blocks.append("## 缺失清单")
    if missing_items:
        blocks.extend(f"- {item['text']}" for item in missing_items)
    else:
        blocks.append("- 无。")
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
        output_format=base_task.output_format,
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
