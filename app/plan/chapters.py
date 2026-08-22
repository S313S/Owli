"""章节式计划：逐章短调用、结构校验与即时落盘。"""

from __future__ import annotations

import asyncio
import json
import hashlib
import inspect
from pathlib import Path
from typing import Any, Mapping

from app.config import ChapterEngineConfig
from app.plan.model import Agent, Plan


CHAPTER_TYPES = frozenset({
    "collection", "transport", "data_cleaning", "code_execution",
    "excel_generation", "comparison", "cross_validation", "audit",
    "report", "summary", "tagging",
})

CHAPTER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chapter_type", "opening", "closing"],
    "properties": {
        "chapter_type": {"type": "string", "enum": sorted(CHAPTER_TYPES)},
        "opening": {
            "type": "object",
            "additionalProperties": False,
            "required": ["inputs", "task", "acceptance"],
            "properties": {
                "inputs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path"],
                        "properties": {"path": {"type": "string", "minLength": 1}},
                    },
                },
                "task": {"type": "string", "minLength": 1},
                "acceptance": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
            },
        },
        "closing": {
            "type": "object",
            "additionalProperties": False,
            "required": ["output", "entities", "expected_count", "notes"],
            "properties": {
                "output": {
                    "type": "object", "additionalProperties": False,
                    "required": ["path"],
                    "properties": {"path": {"type": "string", "minLength": 1}},
                },
                "entities": {
                    "type": "array", "items": {"type": "string", "minLength": 1},
                },
                "expected_count": {"type": ["integer", "null"], "minimum": 0},
                "notes": {"type": "object"},
            },
        },
    },
}


def _input_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("chapter.opening.inputs 必须是 object 数组")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise ValueError("chapter.opening.inputs 每项必须含 path")
        path = str(item["path"]).strip()
        if not path:
            raise ValueError("chapter.opening.inputs.path 不能为空")
        paths.append(path)
    return paths


def validate_chapter_value(value: Any, agent: Agent) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("章节 JSON 顶层必须是 object")
    unknown = set(value) - {"chapter_type", "opening", "closing"}
    if unknown:
        raise ValueError(f"章节含未知字段：{sorted(unknown)}")
    chapter_type = str(value.get("chapter_type", ""))
    if chapter_type not in CHAPTER_TYPES:
        raise ValueError(f"chapter_type 不在闭集：{chapter_type!r}")
    opening = value.get("opening")
    closing = value.get("closing")
    if not isinstance(opening, Mapping) or not isinstance(closing, Mapping):
        raise ValueError("章节 opening/closing 必须是 object")
    input_paths = _input_paths(opening.get("inputs"))
    task = str(opening.get("task", "")).strip()
    acceptance = opening.get("acceptance")
    if isinstance(acceptance, str) and acceptance.strip():
        # 无歧义同义写法归一化：单条验收写成字符串视作单元素数组
        # （r-99fdccf53cae goal-3-ch-2 首轮因此被退回重写）。
        acceptance = [acceptance]
    if not task or not isinstance(acceptance, list) or not acceptance:
        raise ValueError("章节 task 不能为空且 acceptance 至少一条")
    if not all(isinstance(item, str) and item.strip() for item in acceptance):
        raise ValueError("章节 acceptance 必须是非空字符串数组")
    output = closing.get("output")
    if not isinstance(output, Mapping) or output.get("path") != agent.output["path"]:
        raise ValueError(
            "章节 closing.output.path 必须等于系统声明路径 "
            f"{agent.output['path']}"
        )
    entities = closing.get("entities")
    if not isinstance(entities, list) or not all(
        isinstance(item, str) and item.strip() for item in entities
    ):
        raise ValueError("章节 closing.entities 必须是字符串数组")
    if chapter_type == "collection" and len(entities) != 1:
        raise ValueError(
            "collection 章 closing.entities 必须恰含一个实体，"
            "以落实竞品 × 信息源的单章颗粒度"
        )
    if (
        chapter_type == "collection"
        and agent.entity is not None
        and entities != [agent.entity]
    ):
        raise ValueError(
            "collection 章 closing.entities 必须逐字等于 agent.entity："
            f"{agent.entity}"
        )
    expected_count = closing.get("expected_count")
    if expected_count is not None and (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 0
    ):
        raise ValueError("章节 closing.expected_count 必须是非负整数或 null")
    notes = closing.get("notes")
    if not isinstance(notes, Mapping):
        raise ValueError("章节 closing.notes 必须是 object")
    return {
        "chapter_type": chapter_type,
        "opening": {
            "inputs": [{"path": item} for item in input_paths],
            "task": task,
            "acceptance": [str(item) for item in acceptance],
        },
        "closing": {
            "output": {"path": str(output["path"])},
            "entities": [str(item) for item in entities],
            "expected_count": expected_count,
            "notes": dict(notes),
        },
    }


def _prompt(
    agent: Agent,
    upstream_info: list[Mapping[str, Any]],
    errors: list[str] | None = None,
) -> str:
    history = json.dumps(upstream_info, ensure_ascii=False, separators=(",", ":"))
    declared_inputs = [
        str(item.get("artifact")) for item in agent.inputs if item.get("artifact")
    ]
    entity = agent.entity or ""
    retry = (
        "上一轮章节结构错误（逐条修正）：" + "；".join(errors or [])
        if errors else ""
    )
    entity_rule = (
        f"，且逐字为 {json.dumps(entity, ensure_ascii=False)}。"
        if entity else "。"
    )
    return (
        "为一个独立执行章填写结构化开头与结尾，只输出 JSON object。"
        "顶层只含 chapter_type/opening/closing；chapter_type 从闭集选择："
        f"{','.join(sorted(CHAPTER_TYPES))}。opening 只含 inputs/task/acceptance，"
        "inputs 每项只含 path；closing 只含 output/entities/expected_count/notes。"
        f"系统声明 task={json.dumps(agent.task, ensure_ascii=False)}；"
        f"系统声明 inputs={json.dumps(declared_inputs, ensure_ascii=False)}；"
        f"系统声明 output.path={json.dumps(agent.output['path'], ensure_ascii=False)}。"
        "对比或交叉验证章的 inputs 必须覆盖此前全部 collection 章 output.path。"
        "closing.notes 必须是 JSON object，不得写字符串；竞品章 notes 必须使用 "
        "positioning/pricing/feature_differences/social_proof/strengths_weaknesses "
        f"五个字段。collection 章 closing.entities 必须恰好一个元素{entity_rule}"
        f"系统从计划树派生的上游信息（不是实际 closing）={history}"
        f"{retry}"
    )


def _render_chapter(chapter: Mapping[str, Any], agent: Agent) -> str:
    opening = json.dumps(chapter["opening"], ensure_ascii=False, indent=2)
    body = json.dumps(agent.to_dict(), ensure_ascii=False, indent=2)
    closing = json.dumps(chapter["closing"], ensure_ascii=False, indent=2)
    return (
        f"# {chapter['chapter_id']}\n\n"
        f"## 开头\n\n```json\n{opening}\n```\n\n"
        f"## 正文\n\n```json\n{body}\n```\n\n"
        f"## 结尾\n\n```json\n{closing}\n```\n"
    )


def _chapter_input_hash(
    agent: Agent, prior_closings: list[dict[str, Any]],
) -> str:
    data = agent.to_dict()
    data.pop("chapter", None)
    data.pop("engine", None)
    encoded = json.dumps(
        {"agent": data, "prior_closings": prior_closings},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _derived_input_paths(goal_agents: list[Agent], agent: Agent) -> list[str]:
    outputs = {
        item.agent_id: str(item.output.get("path", ""))
        for item in goal_agents
        if item.output.get("path")
    }
    paths: list[str] = []
    for dependency in agent.depends_on:
        path = outputs.get(str(dependency))
        if path:
            paths.append(path)
    for item in agent.inputs:
        artifact = str(item.get("artifact", "")).strip()
        if artifact:
            paths.append(artifact)
    return list(dict.fromkeys(paths))


def _derived_upstream_info(
    goal_agents: list[Agent], agent: Agent,
) -> list[dict[str, Any]]:
    """从依赖与声明输入派生 spec 所需上游摘要，不读取已生成 closing。"""

    by_id = {item.agent_id: item for item in goal_agents}
    records: dict[str, dict[str, Any]] = {}
    pending = list(agent.depends_on)
    visited: set[str] = set()
    while pending:
        dependency = str(pending.pop())
        if dependency in visited:
            continue
        visited.add(dependency)
        upstream = by_id.get(dependency)
        if upstream is None:
            continue
        path = str(upstream.output.get("path", "")).strip()
        if path:
            records[path] = {
                "agent_id": upstream.agent_id,
                "output": {"path": path},
                "entities": [upstream.entity] if upstream.entity else [],
                "expected_count": None,
                "notes": {"derived_from": "plan"},
            }
        pending.extend(upstream.depends_on)
    for item in agent.inputs:
        path = str(item.get("artifact", "")).strip()
        if path and path not in records:
            records[path] = {
                "from_goal": str(item.get("from_goal", "")),
                "output": {"path": path},
                "entities": [],
                "expected_count": None,
                "notes": {"derived_from": "plan"},
            }
    return list(records.values())


def _merge_inputs(model_inputs: list[dict[str, str]], derived: list[str]) -> list[dict[str, str]]:
    paths = [*derived, *(str(item.get("path", "")) for item in model_inputs)]
    return [{"path": path} for path in dict.fromkeys(item for item in paths if item)]


async def _generate_selected_chapter_specs(
    plan: Plan,
    workspace: Any,
    adapter: Any,
    engine_config: ChapterEngineConfig,
    *,
    on_chapter: Any = None,
    prior_closings: list[dict[str, Any]] | None = None,
    only_agent_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """生成指定 agent 的章，调用方可对同 goal 全量并发。"""

    history = list(prior_closings or [])
    generated_closings: list[dict[str, Any]] = []
    research_root = Path(workspace.root).parent
    for goal in plan.goals:
        for index, agent in enumerate(goal.agents, start=1):
            if only_agent_ids is not None and agent.agent_id not in only_agent_ids:
                continue
            segment_name = f"{goal.goal_id}-ch-{index}"
            chapter_path = research_root / f"goals/{goal.goal_id}/ch-{index}.md"
            hash_path = research_root / f"goals/{goal.goal_id}/.{segment_name}.sha256"
            input_hash = _chapter_input_hash(agent, history)
            if (
                workspace.formal_path(segment_name).is_file()
                and chapter_path.is_file()
                and hash_path.is_file()
                and hash_path.read_text(encoding="utf-8").strip() == input_hash
            ):
                raw_value = json.loads(
                    workspace.formal_path(segment_name).read_text(encoding="utf-8")
                )
                value = validate_chapter_value(raw_value, agent)
                value["opening"]["inputs"] = _merge_inputs(
                    value["opening"]["inputs"],
                    _derived_input_paths(goal.agents, agent),
                )
                chapter = {
                    "chapter_id": f"ch-{index}",
                    "chapter_type": value["chapter_type"],
                    "plan_path": f"goals/{goal.goal_id}/ch-{index}.md",
                    "opening": value["opening"],
                    "closing": value["closing"],
                }
                shell = str((agent.capability or {}).get("shell", "none"))
                agent.engine = (
                    "codex" if shell != "none"
                    else engine_config.engine_for(value["chapter_type"])
                )
                agent.chapter = chapter
                generated_closings.append({
                    "goal_id": goal.goal_id,
                    "chapter_id": chapter["chapter_id"],
                    **value["closing"],
                })
                if on_chapter is not None:
                    callback_result = on_chapter(goal.goal_id, f"ch-{index}")
                    if inspect.isawaitable(callback_result):
                        await callback_result
                continue
            workspace.reset_attempts(segment_name)
            semantic_errors: list[str] = []
            value = None
            for _ in range(workspace.config.plan_segment_retries):
                raw = await workspace.generate(
                    segment_name,
                    _prompt(agent, history, semantic_errors),
                    adapter,
                    output_schema=CHAPTER_OUTPUT_SCHEMA,
                )
                try:
                    value = validate_chapter_value(raw, agent)
                    break
                except ValueError as exc:
                    semantic_errors = [str(exc)]
            if value is None:
                raise ValueError(
                    f"章节 {segment_name} 连续语义校验失败："
                    + "；".join(semantic_errors)
                )
            value["opening"]["inputs"] = _merge_inputs(
                value["opening"]["inputs"],
                _derived_input_paths(goal.agents, agent),
            )
            chapter = {
                "chapter_id": f"ch-{index}",
                "chapter_type": value["chapter_type"],
                "plan_path": f"goals/{goal.goal_id}/ch-{index}.md",
                "opening": value["opening"],
                "closing": value["closing"],
            }
            # 规则 7（V2-D2）是硬约束：有 shell 能力的 agent 必须 codex，
            # 章级默认引擎只对无此约束的章生效（r-1e339b0180ca 取证：
            # excel-generation 章被判为 comparison → claude → lint 必死）。
            shell = str((agent.capability or {}).get("shell", "none"))
            agent.engine = (
                "codex" if shell != "none"
                else engine_config.engine_for(value["chapter_type"])
            )
            agent.chapter = chapter
            path = research_root / chapter["plan_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render_chapter(chapter, agent), encoding="utf-8")
            hash_path.parent.mkdir(parents=True, exist_ok=True)
            hash_path.write_text(input_hash + "\n", encoding="utf-8")
            generated_closings.append({
                "goal_id": goal.goal_id,
                "chapter_id": chapter["chapter_id"],
                **value["closing"],
            })
            if on_chapter is not None:
                callback_result = on_chapter(goal.goal_id, f"ch-{index}")
                if inspect.isawaitable(callback_result):
                    await callback_result
    return generated_closings


async def generate_chapter_specs(
    plan: Plan,
    workspace: Any,
    adapter: Any,
    engine_config: ChapterEngineConfig,
    *,
    on_chapter: Any = None,
) -> None:
    """同 goal 全部章并发生成；spec 不依赖执行期 expected_count 链。"""

    for goal in plan.goals:
        indexed = list(enumerate(goal.agents, start=1))
        await asyncio.gather(*(
            _generate_selected_chapter_specs(
                plan,
                workspace,
                adapter,
                engine_config,
                on_chapter=on_chapter,
                prior_closings=_derived_upstream_info(goal.agents, agent),
                only_agent_ids={agent.agent_id},
            )
            for _, agent in indexed
        ))


__all__ = [
    "CHAPTER_TYPES", "generate_chapter_specs", "validate_chapter_value",
]
