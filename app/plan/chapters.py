"""章节式计划：逐章短调用、结构校验与即时落盘。"""

from __future__ import annotations

import asyncio
import json
import hashlib
import inspect
from pathlib import Path
from typing import Any, Mapping

from app.config import ChapterEngineConfig
from app.plan.model import (
    Agent, Plan, RATING_BATCH_ROWS, rated_collector_id, rating_batch_path,
    rating_rows_path,
)


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
    upstream_info: Mapping[str, Any],
    errors: list[str] | None = None,
    previous: str | None = None,
) -> str:
    history = json.dumps(
        upstream_info.get("dependency_artifacts", []),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    collection_inventory = json.dumps(
        upstream_info.get("collection_chapters", []),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    declared_inputs = [
        str(item.get("artifact")) for item in agent.inputs if item.get("artifact")
    ]
    entity = agent.entity or ""
    retry = (
        "上一轮章节结构错误（逐条修正）：" + "；".join(errors or [])
        if errors else ""
    )
    if errors and previous:
        # §PLAN-1 货 3：补丁式重试，只改报错处，不整章重写。
        retry += (
            f"上一轮本章 JSON 原文={previous}；"
            "只修改上述报错点名的字段，其余字段逐字保留，仍输出完整 JSON。"
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
        "comparison / cross_validation 章的 opening.inputs 必须逐条使用全卷采集章"
        "清单里的 output.path 原文，不得自造路径。"
        "closing.notes 必须是 JSON object，不得写字符串；竞品章 notes 必须使用 "
        "positioning/pricing/feature_differences/social_proof/strengths_weaknesses "
        f"五个字段。collection 章 closing.entities 必须恰好一个元素{entity_rule}"
        f"系统从计划树派生的上游信息（不是实际 closing）={history}"
        f"全卷采集章清单={collection_inventory}。"
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
    agent: Agent, prior_closings: Mapping[str, Any],
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


def _collection_inventory(plan: Plan) -> list[dict[str, Any]]:
    """从计划树一次性派生全卷采集章清单，不读取章 closing。"""

    return [
        {
            "goal_id": goal.goal_id,
            "agent_id": agent.agent_id,
            "output": {"path": str(agent.output.get("path", ""))},
            "entity": agent.entity,
        }
        for goal in plan.goals
        for agent in goal.agents
        if agent.capability.get("profile") == "web-collector"
        and str(agent.output.get("path", "")).strip()
    ]


def _derived_input_paths(
    goal_agents: list[Agent],
    agent: Agent,
    chapter_type: str,
    collection_inventory: list[Mapping[str, Any]],
) -> list[str]:
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
    if chapter_type in {"comparison", "cross_validation"}:
        paths.extend(
            str(item.get("output", {}).get("path", "")).strip()
            for item in collection_inventory
            if isinstance(item.get("output"), Mapping)
        )
    return list(dict.fromkeys(paths))


def _derived_upstream_info(
    goal_agents: list[Agent],
    agent: Agent,
    collection_inventory: list[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
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
    return {
        "dependency_artifacts": list(records.values()),
        "collection_chapters": [dict(item) for item in collection_inventory],
    }


def _merge_inputs(model_inputs: list[dict[str, str]], derived: list[str]) -> list[dict[str, str]]:
    paths = [*derived, *(str(item.get("path", "")) for item in model_inputs)]
    return [{"path": path} for path in dict.fromkeys(item for item in paths if item)]


def _model_inputs_for_merge(value: Mapping[str, Any]) -> list[dict[str, str]]:
    """全卷型章只认系统派生路径，丢弃模型猜测的额外路径。"""

    if value.get("chapter_type") in {"comparison", "cross_validation"}:
        return []
    return list(value["opening"]["inputs"])


def rating_chapter_value(agent: Agent, goal: Any) -> dict[str, Any] | None:
    """§RATE-1 货 2：自动排出的评级章，章规格确定性生成，不走引擎。

    按计划结构认出（只依赖一个同 goal 采集章、产物走逐条评级验证器、不是交付物章），
    输入就是那一章的产物路径。任务与验收完全由系统决定，让模型再写一遍只会引入
    漂移，还多花一次章扩写调用。
    """
    rates = rated_collector_id(
        output=agent.output or {},
        depends_on=agent.depends_on,
        deliverable_path=str((goal.deliverable or {}).get("path", "")),
        collector_ids=[
            item.agent_id for item in goal.agents
            if (item.capability or {}).get("profile") == "web-collector"
        ],
    )
    if not rates:
        return None
    source = next(
        (item for item in goal.agents if item.agent_id == rates), None,
    )
    if source is None:
        return None
    source_path = str(source.output["path"])
    # §RATE-2 货 1：输入改指**物化行文件**——源适配器直落库，采集产物只是模型顺手
    # 写下的一小撮（RATE-1 整跑：盘上 10 条 / 库里同章 50 行），评级章读产物就只
    # 评得到 15%。物化文件由 runtime 在本章起跑前按库行写出。
    rows_path = rating_rows_path(source_path)
    # §RATE-3 货 4：系统把物化文件按 ≤RATING_BATCH_ROWS 行切片、一章内分批喂入，
    # 每次会话只评一批——inputs 仍指整份物化文件（片数运行期才知道），
    # 但验收按「本批」说：条数与这一批的 .rows.<n>.json 一一对应，系统按批合并。
    batch_path = rating_batch_path(rows_path, 1).replace(".1.json", ".<n>.json")
    return {
        "chapter_type": "audit",
        "opening": {
            "inputs": [{"path": rows_path}],
            "task": agent.task,
            "acceptance": [
                f"系统把 {rows_path} 按 ≤{RATING_BATCH_ROWS} 行切成 {batch_path} "
                "分批喂入，每次会话只评这一批：本批产物按系统指定的批产物路径落盘，"
                "条数与本批文件一一对应，不新增不丢条；整章产物由系统按批合并",
                "每条带齐五维评分、rating_notes、rated_by，并原样回带原 permalink",
            ],
        },
        "closing": {
            "output": {"path": str(agent.output["path"])},
            "entities": [],
            "expected_count": None,
            "notes": {
                "rates_chapter": rates,
                "rates_output": source_path,
                "rates_rows": rows_path,
                "rates_batches": batch_path,
            },
        },
    }


async def _generate_selected_chapter_specs(
    plan: Plan,
    workspace: Any,
    adapter: Any,
    engine_config: ChapterEngineConfig,
    *,
    on_chapter: Any = None,
    prior_closings: Mapping[str, Any] | None = None,
    collection_inventory: list[Mapping[str, Any]] | None = None,
    only_agent_ids: set[str] | None = None,
    force_regenerate: bool = False,
    lint_errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """生成指定 agent 的章，调用方可对同 goal 全量并发。"""

    inventory = list(collection_inventory or [])
    history = dict(prior_closings or {
        "dependency_artifacts": [],
        "collection_chapters": inventory,
    })
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
                not force_regenerate
                and workspace.formal_path(segment_name).is_file()
                and chapter_path.is_file()
                and hash_path.is_file()
                and hash_path.read_text(encoding="utf-8").strip() == input_hash
            ):
                raw_value = json.loads(
                    workspace.formal_path(segment_name).read_text(encoding="utf-8")
                )
                value = validate_chapter_value(raw_value, agent)
                value["opening"]["inputs"] = _merge_inputs(
                    _model_inputs_for_merge(value),
                    _derived_input_paths(
                        goal.agents,
                        agent,
                        value["chapter_type"],
                        inventory,
                    ),
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
            value = rating_chapter_value(agent, goal)
            rating_chapter = value is not None
            if value is not None:
                # 评级章：不占章扩写引擎调用，直接落盘。
                value = validate_chapter_value(value, agent)
            workspace.reset_attempts(segment_name)
            semantic_errors = list(lint_errors or [])
            previous = workspace.previous_text(segment_name) if semantic_errors else None
            for _ in range(0 if value is not None else workspace.config.plan_segment_retries):
                raw = await workspace.generate(
                    segment_name,
                    _prompt(agent, history, semantic_errors, previous),
                    adapter,
                    output_schema=CHAPTER_OUTPUT_SCHEMA,
                )
                try:
                    value = validate_chapter_value(raw, agent)
                    break
                except ValueError as exc:
                    semantic_errors = [str(exc)]
                    previous = json.dumps(raw, ensure_ascii=False)
            if value is None:
                raise ValueError(
                    f"章节 {segment_name} 连续语义校验失败："
                    + "；".join(semantic_errors)
                )
            if not rating_chapter:
                # 评级章的 inputs 由系统定死，只指物化行文件；派生输入会把采集
                # 产物路径（10 条那份）加回来，正是 §RATE-2 要绕开的东西。
                value["opening"]["inputs"] = _merge_inputs(
                    _model_inputs_for_merge(value),
                    _derived_input_paths(
                        goal.agents,
                        agent,
                        value["chapter_type"],
                        inventory,
                    ),
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
    only_chapters: set[str] | None = None,
    lint_errors: list[str] | None = None,
) -> None:
    """同 goal 全部章并发生成；spec 不依赖执行期 expected_count 链。"""

    collection_inventory = _collection_inventory(plan)
    for goal in plan.goals:
        indexed = [
            (index, agent)
            for index, agent in enumerate(goal.agents, start=1)
            if only_chapters is None
            or f"{goal.goal_id}/ch-{index}" in only_chapters
        ]
        await asyncio.gather(*(
            _generate_selected_chapter_specs(
                plan,
                workspace,
                adapter,
                engine_config,
                on_chapter=on_chapter,
                prior_closings=_derived_upstream_info(
                    goal.agents, agent, collection_inventory,
                ),
                collection_inventory=collection_inventory,
                only_agent_ids={agent.agent_id},
                force_regenerate=only_chapters is not None,
                lint_errors=lint_errors,
            )
            for _, agent in indexed
        ))


__all__ = [
    "CHAPTER_TYPES", "generate_chapter_specs", "validate_chapter_value",
]
