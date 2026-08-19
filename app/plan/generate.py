"""M2-b single 模式计划生成：引擎给骨架，系统补齐全部执行字段。"""

from __future__ import annotations

import inspect
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineTask
from app.adapters.events import ItemKind, NormalizedEvent
from app.adapters.routing import pick_engine
from app.plan.lint import lint
from app.plan.model import DEFAULT_RETRY_POLICY, Plan
from app.plan.question import make_questions


MAX_ATTEMPTS = 3
_FORMATS = {"table", "markdown", "excel", "json"}


class PlanGenerationError(RuntimeError):
    """规划引擎或计划产物未通过协议。"""


_ROLE_MAP = {
    "规划": ("goal_planning", "readonly-analyst"),
    "计划仲裁": ("plan_arbitration", "readonly-analyst"),
    "可靠度审计": ("reliability_audit", "readonly-analyst"),
    "交叉验证": ("cross_validation", "readonly-analyst"),
    "一致性检查": ("consistency_check", "readonly-analyst"),
    "报告撰写": ("report_writing", "report-writer"),
    "摘要": ("summary", "report-writer"),
    "标签": ("tagging", "report-writer"),
    "api 数据抓取": ("data_collection", "web-collector"),
    "hn 数据抓取": ("data_collection", "web-collector"),
    "mediacrawler": ("browser_automation", "sandboxed-runner"),
    "浏览器自动化": ("browser_automation", "sandboxed-runner"),
    "代码执行": ("code_execution", "sandboxed-runner"),
    "excel 生成": ("excel_generation", "sandboxed-runner"),
    "数据清洗": ("data_cleaning", "sandboxed-runner"),
}


def _classify(name: str, task: str) -> tuple[str, str]:
    del task
    normalized = " ".join(name.casefold().split())
    try:
        return _ROLE_MAP[normalized]
    except KeyError as exc:
        raise ValueError(f"未知 agent 职能名称：{name}") from exc


def _planning_prompt(query: str, output_path: Path, errors: list[str]) -> str:
    retry = ""
    if errors:
        retry = "\n上一轮 plan_lint 错误原文（逐条修正）：\n" + "\n".join(errors)
    return (
        f"目标：为用户原始需求《{query}》生成一棵 3–7 个 goal 的三层计划骨架，"
        f"写入 {output_path}；"
        "按证据链自然断点拆分，每个 goal 同时满足独立产物、验收可判定、值得干预、失败可局部化。\n"
        "方法要点：当前 M2 只使用 Hacker News；固定查询策略为 HN Algolia /api/v1/search，"
        "tags=story，numericFilters=created_at_i>执行时点UTC epoch-7776000,points>50，"
        "hitsPerPage=1000。禁止按搜索/阅读/总结工种拆 goal。\n"
        f"产物结构：只输出 JSON object 到 {output_path}，顶层只能有 goals。每个 goal 只能含 "
        "title、objective、depends_on、deliverable、acceptance、agents；agents 每项只能含 "
        "name、task，name 应从规划、计划仲裁、可靠度审计、交叉验证、一致性检查、报告撰写、"
        "摘要、标签、API 数据抓取、MediaCrawler、浏览器自动化、代码执行、Excel 生成、数据清洗中选。"
        "depends_on 用 goal-<n>；deliverable 含 format/path/description，format 只能取 "
        "table、markdown、excel、json，path 只写文件名；"
        "不得输出 id、engine、capability、prompt、状态、重试或时间字段。\n"
        "边界与降级：信息不足时做明确假设并继续；仍须保留 3–7 个 goal、每个 goal 至少一个 agent、"
        "每个 acceptance 都含数量、字段、文件或集合等可直接判定条件；"
        "最终结构化结论的 summary 固定填写‘计划骨架已写入’。"
        f"{retry}"
    )


def _capability(profile: str, goal_id: str, upstream: list[str]) -> dict[str, Any]:
    upstream_paths = [f"goals/{item}/**" for item in upstream]
    current = f"goals/{goal_id}/**"
    if profile == "web-collector":
        return {
            "profile": profile,
            "tools": ["source.hacker_news", "fs.write", "db.write"],
            "sources": ["hacker_news"],
            "fs": {"read": upstream_paths, "write": [current]},
            "network": "sources_only",
            "shell": "none",
        }
    if profile == "sandboxed-runner":
        return {
            "profile": profile,
            "tools": ["shell.exec", "fs.read", "fs.write"],
            "sources": [],
            "fs": {"read": [*upstream_paths, current], "write": [current]},
            "network": "none",
            "shell": "workspace",
        }
    if profile == "report-writer":
        return {
            "profile": profile,
            "tools": ["fs.read", "fs.write", "db.read", "db.write", "report.render"],
            "sources": [],
            "fs": {"read": ["**"], "write": [current]},
            "network": "none",
            "shell": "none",
        }
    return {
        "profile": "readonly-analyst",
        "tools": ["fs.read", "fs.write", "db.read"],
        "sources": [],
        "fs": {"read": ["**"], "write": [current]},
        "network": "none",
        "shell": "none",
    }


def _output(
    agent_kind: str,
    goal_id: str,
    agent_id: str,
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = f"goals/{goal_id}/{agent_id}"
    if agent_kind in {"data_collection", "browser_automation"}:
        result = {
            "format": "json",
            "path": f"{base}.json",
            "validators": [
                "file_exists", "json_array_min_items:1",
                "each_item_has:permalink,fetched_at",
            ],
        }
    elif agent_kind in {"audit", "reliability_audit", "cross_validation"}:
        result = {
            "format": "json", "path": f"{base}.json",
            "validators": ["file_exists", "no_item_missing_rating"],
        }
    elif agent_kind == "excel_generation":
        result = {
            "format": "excel", "path": f"{base}.xlsx",
            "validators": ["file_exists", "openpyxl_reload_ok"],
        }
    else:
        validators = ["file_exists", "sections_exist:结论"]
        if agent_kind in {"report", "report_writing"}:
            validators = ["file_exists", "sections_exist:结论,信息源"]
        result = {"format": "markdown", "path": f"{base}.md", "validators": validators}
    if target is not None:
        if target["format"] != result["format"]:
            result["validators"] = ["file_exists"]
            if target["format"] == "excel":
                result["validators"].append("openpyxl_reload_ok")
        result.update(format=target["format"], path=target["path"])
    return result


def _agent_prompt(query: str, task: str, output: Mapping[str, Any], agent_kind: str) -> str:
    structure = "、".join(output["validators"])
    chart_rule = ""
    if agent_kind in {"report", "report_writing", "excel_generation"}:
        chart_rule = "图表按‘结论→比较类型→图形’三步法与 8 条禁则选择；Excel 固定 6 sheet。"
    if agent_kind in {"data_collection", "browser_automation"}:
        method = (
            f"查询式={query}；HN Algolia 时间窗=近90天，"
            "numericFilters=created_at_i>执行时点UTC epoch-7776000,points>50，"
            "hitsPerPage=1000。"
        )
        evidence_rule = "所有事实保留 permalink 与 fetched_at。"
    else:
        method = (
            f"仅消费上游产物，不发起新抓取；输入口径为查询式={query}、"
            "HN Algolia 近90天、created_at_i>执行时点UTC epoch-7776000、"
            "points>50、hitsPerPage=1000。"
        )
        evidence_rule = "事实须反向引用上游 permalink 与 fetched_at。"
    return (
        f"目标：{task}；把可复核结果写入 {output['path']}。\n"
        f"方法要点：{method}{chart_rule}\n"
        f"产物结构：format={output['format']}；校验约束={structure}；{evidence_rule}\n"
        "边界与降级：命中不足时保留实际小数组并写明数量，不放宽时间窗或 points 阈值；"
        "无法满足某条验收时在结构化结论 unmet 逐条列明。"
    )


def _deliverable(raw: Any, goal_id: str) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{goal_id}.deliverable 必须是 object")
    format_name = str(raw.get("format", ""))
    if format_name not in _FORMATS:
        raise ValueError(f"{goal_id}.deliverable.format 非法：{format_name!r}")
    original = PurePosixPath(str(raw.get("path", "result.md")))
    leaf = original.name
    if not leaf or leaf in {".", ".."}:
        raise ValueError(f"{goal_id}.deliverable.path 缺少文件名")
    description = str(raw.get("description", "")).strip()
    if not description:
        raise ValueError(f"{goal_id}.deliverable.description 不能为空")
    return {
        "format": format_name,
        "path": f"goals/{goal_id}/{leaf}",
        "description": description,
    }


def _origin() -> dict[str, str]:
    fields = (
        "_node", "display_name", "task", "depends_on", "inputs", "engine",
        "model", "capability", "prompt.body", "output", "extra_quota_credits",
    )
    return {field: "generated" for field in fields}


def _build_plan(
    skeleton: Any,
    *,
    query: str,
    research_id: str,
    timestamp: str,
) -> Plan:
    if not isinstance(skeleton, Mapping) or not isinstance(skeleton.get("goals"), list):
        raise ValueError("规划产物顶层必须是含 goals 数组的 object")
    raw_goals = skeleton["goals"]
    if not 3 <= len(raw_goals) <= 7:
        raise ValueError(f"goal 数必须在 3–7，实际为 {len(raw_goals)}")
    for index, raw_goal in enumerate(raw_goals, start=1):
        if not isinstance(raw_goal, Mapping):
            raise ValueError(f"goal-{index} 骨架必须是 object")
    deliverables = {
        f"goal-{index}": _deliverable(raw_goal.get("deliverable"), f"goal-{index}")
        for index, raw_goal in enumerate(raw_goals, start=1)
    }
    counters: Counter[str] = Counter()
    goals: list[dict[str, Any]] = []
    for index, raw_goal in enumerate(raw_goals, start=1):
        goal_id = f"goal-{index}"
        title = str(raw_goal.get("title", "")).strip()
        objective = str(raw_goal.get("objective", "")).strip()
        depends_on = raw_goal.get("depends_on", [])
        acceptance = raw_goal.get("acceptance", [])
        raw_agents = raw_goal.get("agents", [])
        if not title or not objective:
            raise ValueError(f"{goal_id} 的 title/objective 不能为空")
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            raise ValueError(f"{goal_id}.depends_on 必须是字符串数组")
        if not isinstance(acceptance, list) or not acceptance:
            raise ValueError(f"{goal_id}.acceptance 至少需要 1 条")
        if not isinstance(raw_agents, list) or not raw_agents:
            raise ValueError(f"{goal_id}.agents 至少需要 1 项")
        agents: list[dict[str, Any]] = []
        previous_agent_id: str | None = None
        upstream_artifacts = {
            item: deliverables[item]["path"]
            for item in depends_on
            if item in deliverables
        }
        for agent_index, item in enumerate(raw_agents):
            agent = _build_agent(
                item,
                goal_id,
                list(depends_on),
                query,
                counters,
                previous_agent_id=previous_agent_id,
                upstream_artifacts=upstream_artifacts if agent_index == 0 else {},
                target=deliverables[goal_id] if agent_index == len(raw_agents) - 1 else None,
            )
            agents.append(agent)
            previous_agent_id = agent["agent_id"]
        goals.append({
            "goal_id": goal_id,
            "title": title[:24],
            "objective": objective,
            "depends_on": list(depends_on),
            "deliverable": deliverables[goal_id],
            "acceptance": [str(item) for item in acceptance],
            "intervention": {"on_complete": True, "prompt": f"请核对《{title[:24]}》产物，是否继续？"},
            "retry_policy": dict(DEFAULT_RETRY_POLICY),
            "on_upstream_failure": "skip",
            "agents": agents,
            "status": "pending",
        })
    use_case = "other"
    if any(word in query for word in ("竞品", "优缺点", "对比")):
        use_case = "product_competitor"
    if any(word in query for word in ("社媒", "小红书", "抖音", "舆情")):
        use_case = "social_competitor"
    plan = Plan.from_dict({
        "research_id": research_id,
        "plan_rev": 1,
        "title": query.strip()[:40],
        "research_question": query,
        "use_case": use_case,
        "status": "awaiting_review",
        "approved_at": None,
        "decision_balance": [],
        "expert_panel": None,
        "goals": goals,
        "change_log": [],
        "baseline": None,
        "baseline_source": "generated",
        "created_at": timestamp,
        "updated_at": timestamp,
    })
    plan.decision_balance = make_questions(plan, query)
    return plan


def _build_agent(
    raw: Any,
    goal_id: str,
    upstream: list[str],
    query: str,
    counters: Counter[str],
    *,
    previous_agent_id: str | None,
    upstream_artifacts: Mapping[str, str],
    target: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{goal_id}.agents 的元素必须是 object")
    name = str(raw.get("display_name") or raw.get("name") or "").strip()
    task = str(raw.get("task", "")).strip()
    if not name or not task:
        raise ValueError(f"{goal_id}.agents 的 name/task 不能为空")
    agent_kind, profile = _classify(name, task)
    counters[agent_kind] += 1
    suffix = "" if counters[agent_kind] == 1 else f"-{counters[agent_kind]}"
    agent_id = f"{agent_kind.replace('_', '-')}{suffix}"
    output = _output(agent_kind, goal_id, agent_id, target)
    return {
        "agent_id": agent_id,
        "display_name": name,
        "task": task[:200],
        "depends_on": [] if previous_agent_id is None else [previous_agent_id],
        "inputs": [
            {"from_goal": item, "artifact": upstream_artifacts[item]}
            for item in upstream
            if item in upstream_artifacts
        ],
        "engine": pick_engine(agent_kind, None).engine,
        "model": None,
        "capability": _capability(profile, goal_id, upstream),
        "prompt": {
            "preamble_ref": "common/v1",
            "body": _agent_prompt(query, task, output, agent_kind),
            "assumptions_policy": "assume_and_declare",
        },
        "output": output,
        "extra_quota_credits": None,
        "origin": _origin(),
        "status": "queued",
    }


def _ctx(path: Path, research_id: str, store: Any) -> validation.Ctx:
    return validation.Ctx(
        output_path=path,
        output_format="json",
        research_id=research_id,
        goal_id="goal-1",
        agent_id="plan-generator",
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=store,
        source_domains=frozenset(),
    )


async def _emit(store: Any, event: NormalizedEvent) -> None:
    sink = getattr(store, "on_plan_event", None)
    if sink is None:
        return
    result = sink(event)
    if inspect.isawaitable(result):
        await result


def _retry_event(research_id: str, attempt: int, errors: list[str]) -> NormalizedEvent:
    return NormalizedEvent(
        engine="Owli",
        thread_id=research_id,
        turn_id=f"plan-attempt-{attempt}",
        item_kind=ItemKind.ERROR,
        text="\n".join(errors),
        is_error=True,
        raw={"attempt": attempt, "errors": list(errors)},
        outcome="retrying",
    )


def _adapter_error(result: Any) -> str:
    messages: list[str] = []
    for field in ("engine_error", "conclusion_error"):
        value = getattr(result, field, None)
        if value:
            messages.append(str(value))
    report = getattr(result, "validation", None)
    for item in getattr(report, "results", []):
        verdict = getattr(getattr(item, "verdict", None), "value", None)
        if verdict == "pass":
            continue
        detail = str(getattr(item, "message", "")).strip()
        offenders = [str(value) for value in getattr(item, "offenders", [])]
        if offenders:
            detail = f"{detail}；offenders={offenders}"
        if detail:
            messages.append(detail)
    return "；".join(messages) or "规划产物与结构化结论未同时通过"


async def generate_plan(query: str, store: Any, adapter: Any) -> Plan:
    """从待规划报告生成、校验并原子保存 awaiting_review 计划。"""

    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("需求文本不能为空")
    report = store.get_drafting_report(normalized_query)
    if report is None:
        raise PlanGenerationError("找不到由调用方预先创建的待规划报告")
    research_id = str(report["id"])
    extra = report.get("extra") if isinstance(report.get("extra"), Mapping) else {}
    timestamp = str(extra.get("plan_generated_at") or report["created_at"])
    runs_root = Path(getattr(store, "runs_root", validation.RUNS_ROOT))
    skeleton_path = runs_root / research_id / "goals" / "goal-1" / "plan-skeleton.json"
    skeleton_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        task = EngineTask(
            body=_planning_prompt(normalized_query, skeleton_path, errors),
            output_path=skeleton_path,
            output_format="json",
            research_id=research_id,
            goal_id="goal-1",
            agent_id="plan-generator",
            agent_kind="planning",
            validators=["file_exists"],
            capability=Capability(
                profile="custom",
                tools=("fs.write",),
                fs=FileSystemScope(write=("goals/goal-1/**",)),
            ),
        )
        result = await adapter.run(
            task,
            _ctx(skeleton_path, research_id, store),
            on_event=lambda event: _emit(store, event),
        )
        if not bool(getattr(result, "succeeded", False)):
            errors = [f"[规划双腿判定] {_adapter_error(result)}"]
            if attempt < MAX_ATTEMPTS:
                await _emit(store, _retry_event(research_id, attempt + 1, errors))
            continue
        try:
            skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
            plan = _build_plan(
                skeleton,
                query=normalized_query,
                research_id=research_id,
                timestamp=timestamp,
            )
            errors = lint(plan)["errors"]
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors = [f"[结构] {type(exc).__name__}: {exc}"]
        if not errors:
            store.save_plan_snapshot(
                research_id, snapshot=plan.to_dict(), expected_rev=0
            )
            return plan
        if attempt < MAX_ATTEMPTS:
            await _emit(store, _retry_event(research_id, attempt + 1, errors))

    raise PlanGenerationError(
        "计划生成连续 3 次仍有 error，计划未保存：\n" + "\n".join(errors)
    )


__all__ = ["PlanGenerationError", "generate_plan"]
