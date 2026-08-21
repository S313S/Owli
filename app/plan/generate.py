"""M2-b single 模式计划生成：引擎给骨架，系统补齐全部执行字段。"""

from __future__ import annotations

import inspect
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from app.adapters import validation
from app.adapters.events import ItemKind, NormalizedEvent
from app.adapters.routing import pick_engine
from app.config import ResilienceConfig, load_resilience_config
from app.plan.lint import lint
from app.plan.model import DEFAULT_RETRY_POLICY, Plan
from app.plan.question import make_questions
from app.plan.segments import PlanSegmentError, PlanSegmentWorkspace
from app.sources.registry import planning_catalog


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
    "x 数据抓取": ("data_collection", "web-collector"),
    "mediacrawler": ("browser_automation", "sandboxed-runner"),
    "浏览器自动化": ("browser_automation", "sandboxed-runner"),
    "代码执行": ("code_execution", "sandboxed-runner"),
    "excel 生成": ("excel_generation", "sandboxed-runner"),
    "数据清洗": ("data_cleaning", "sandboxed-runner"),
}


def _source_specs() -> dict[str, Any]:
    return {spec.collector_name.casefold(): spec for spec in planning_catalog()}


def _classify(name: str, task: str) -> tuple[str, str]:
    del task
    normalized = " ".join(name.casefold().split())
    if normalized in _source_specs():
        return "data_collection", "web-collector"
    try:
        return _ROLE_MAP[normalized]
    except KeyError as exc:
        raise ValueError(f"未知 agent 职能名称：{name}") from exc


def _skeleton_prompt(query: str, errors: list[str]) -> str:
    retry = ""
    if errors:
        retry = "\n上一轮整计划 lint 错误原文（逐条修正结构）：\n" + "\n".join(errors)
    return (
        f"目标：为《{query}》生成 3–7 个 goal 的短骨架。\n"
        "方法要点：按证据链自然断点拆分；goal 之间只用 depends_on 表达有向无环依赖，"
        "禁止按搜索/阅读/总结工种拆 goal。\n"
        "产物结构：只输出 JSON object，顶层只能有 goals；每个 goal 只能含 title、"
        "objective、depends_on。depends_on 只能引用在它之前的 goal-<n>。\n"
        "边界与降级：信息不足时做明确假设并继续，不输出 Markdown 围栏、说明或任何"
        f"执行字段。{retry}"
    )


def _goal_prompt(
    query: str,
    goal_id: str,
    scaffold: Mapping[str, Any],
    errors: list[str],
) -> str:
    retry = ""
    if errors:
        retry = "\n上一轮整计划 lint 错误原文（只修正本 goal 结构）：\n" + "\n".join(errors)
    sources = "、".join(
        f"{item.display_name}（{item.collector_name}）"
        for item in planning_catalog()
    )
    return (
        f"目标：扩展《{query}》中的 {goal_id}；骨架字段固定为："
        f"{json.dumps(dict(scaffold), ensure_ascii=False)}。\n"
        "方法要点：为这个 goal 选择能形成独立产物的执行链；信息源采集角色只从共享"
        f"注册表选择：{sources}；采集 agent 的 name 必须唯一确定 capability.sources "
        "与 source.* 工具。\n"
        "产物结构：只输出一个 JSON object，只含 deliverable、acceptance、agents。"
        "deliverable 含 format/path/description，path 只写文件名；acceptance 是逐条"
        "可判定字符串数组；agents 每项只含 name、task，不得输出 id、engine、"
        "capability、prompt、状态、重试或时间字段。\n"
        "边界与降级：采集 JSON 顶层必须为数组且每条含 permalink、fetched_at；"
        "HN 查询固定使用 created_at_i>执行时点UTC epoch-7776000、points>50、"
        "hitsPerPage=1000；"
        "数据不足时用结构化缺口口径，不得写死上游无法保证的实体最小条数；"
        f"不输出 Markdown 围栏或说明。{retry}"
    )


def _skeleton_scaffolds(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("goals"), list):
        raise ValueError("骨架顶层必须是含 goals 数组的 object")
    goals = value["goals"]
    if not 3 <= len(goals) <= 7:
        raise ValueError(f"goal 数必须在 3–7，实际为 {len(goals)}")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(goals, start=1):
        goal_id = f"goal-{index}"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{goal_id} 骨架必须是 object")
        title = str(raw.get("title", "")).strip()
        objective = str(raw.get("objective", "")).strip()
        depends_on = raw.get("depends_on", [])
        if not title or not objective:
            raise ValueError(f"{goal_id} 的 title/objective 不能为空")
        if not isinstance(depends_on, list) or not all(
            isinstance(item, str) for item in depends_on
        ):
            raise ValueError(f"{goal_id}.depends_on 必须是字符串数组")
        allowed = {f"goal-{number}" for number in range(1, index)}
        unknown = set(depends_on) - allowed
        if unknown:
            raise ValueError(f"{goal_id}.depends_on 含非法前向或未知依赖：{sorted(unknown)}")
        result.append({
            "title": title,
            "objective": objective,
            "depends_on": list(depends_on),
        })
    return result


def _capability(
    profile: str,
    goal_id: str,
    upstream: list[str],
    *,
    source_id: str = "hacker_news",
) -> dict[str, Any]:
    upstream_paths = [f"goals/{item}/**" for item in upstream]
    current = f"goals/{goal_id}/**"
    if profile == "web-collector":
        return {
            "profile": profile,
            "tools": [f"source.{source_id}", "fs.write", "db.write"],
            "sources": [source_id],
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
            "validators": [
                "file_exists",
                "no_item_missing_rating",
                "field_domain_whitelist:reliability_closed_set",
                "rating_notes_matches_regex",
                "rating_notes_scores_match_columns",
            ],
        }
    elif agent_kind == "excel_generation":
        result = {
            "format": "excel", "path": f"{base}.xlsx",
            "validators": ["file_exists", "openpyxl_reload_ok"],
        }
    else:
        validators = ["file_exists", "sections_exist:结论"]
        if agent_kind in {"report", "report_writing"}:
            validators = [
                "file_exists",
                "sections_exist:结论,信息源",
                "citation_marks_resolvable",
                "no_orphan_citation",
            ]
        result = {"format": "markdown", "path": f"{base}.md", "validators": validators}
    if target is not None:
        if target["format"] != result["format"]:
            result["validators"] = ["file_exists"]
            if target["format"] == "excel":
                result["validators"].append("openpyxl_reload_ok")
        result.update(format=target["format"], path=target["path"])
    return result


def _agent_prompt(
    query: str,
    task: str,
    output: Mapping[str, Any],
    agent_kind: str,
    *,
    source_id: str | None = None,
) -> str:
    structure = "、".join(output["validators"])
    chart_rule = ""
    if agent_kind in {"report", "report_writing", "excel_generation"}:
        chart_rule = "图表按‘结论→比较类型→图形’三步法与 8 条禁则选择；Excel 固定 6 sheet。"
    if agent_kind in {"report", "report_writing"}:
        chart_rule += (
            "引用契约：「结论」章节必须用 Markdown 列表，每条结论列表项末尾带 [SNN] 角标"
            "（S01 起编号）；「信息源」章节逐条以“- [SNN] [标题](permalink)（fetched_at=…）”"
            "列出；每条继续写“ · 五维=权威N/时效N/交叉N/完整N/无关N · "
            "rating_notes=<五段式原文>”；正文角标与信息源条目双向一致，"
            "不得有悬空角标或未被引用的信息源。"
        )
    if agent_kind in {"data_collection", "browser_automation"}:
        spec = next(
            (item for item in planning_catalog() if item.source_id == source_id),
            None,
        )
        method = (
            f"查询式={query}；调用 {spec.tool_name}；{spec.prompt_hint}。"
            if spec is not None
            else f"查询式={query}；按 capability 声明的信息源执行采集。"
        )
        evidence_rule = (
            "所有事实保留 permalink 与 fetched_at。"
            "本文件顶层必须是 JSON 数组，每个元素为一条命中记录；"
            "goal 验收若描述对象结构（如顶层含 queries/hits 等键），"
            "那是清洗类产物的口径，不适用于本文件，不要为此自报 partial。"
        )
    else:
        method = (
            f"仅消费上游产物，不发起新抓取；输入口径为查询式={query}、"
            "HN Algolia 近90天、created_at_i>执行时点UTC epoch-7776000、"
            "points>50、hitsPerPage=1000。"
        )
        evidence_rule = "事实须反向引用上游 permalink 与 fetched_at。"
        if "no_item_missing_rating" in output["validators"]:
            evidence_rule += (
                "本文件顶层必须是 JSON 数组；每个元素为一条评级条目，必须逐条带齐 "
                "score_authority、score_freshness、score_crossref、score_completeness、"
                "score_independence、rating_notes、rated_by 七个字段（评分为整数，"
                "rating_notes 说明依据，rated_by 填 agent_id）；"
                "extra.authority_kind 只能取 first_party_official、verified_principal、"
                "institutional_primary、named_secondary、community_high_signal、"
                "anonymous_or_unverifiable、content_farm；判据分别是主体官方域名、"
                "认证议题当事方、具名机构一手披露、具名二手来源、社区热度达批内P75或"
                "作者历史可查、作者不可核验、内容农场。"
                "extra.interest_relation 只能取 arms_length、disclosed_interest、"
                "undisclosed_interest；判据分别是无可见利益关系、利益关系已披露、"
                "利益关系明显但未披露。不得输出闭集外近义词；"
                "goal 验收若描述对象结构，属于其他产物，不适用本文件。"
            )
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
        if isinstance(acceptance, str) and acceptance.strip():
            # 生成器漂移实锤（r-825ec6b5228a）：整组验收写成「；」分隔长串。
            # 确定性归一成数组，后续 lint 照常逐条把关。
            acceptance = [
                item.strip() for item in re.split(r"[；;]", acceptance) if item.strip()
            ]
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
    normalized_name = " ".join(name.casefold().split())
    source_spec = _source_specs().get(normalized_name)
    source_id = source_spec.source_id if source_spec is not None else "hacker_news"
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
        "capability": _capability(
            profile, goal_id, upstream, source_id=source_id
        ),
        "prompt": {
            "preamble_ref": "common/v1",
            "body": _agent_prompt(
                query,
                task,
                output,
                agent_kind,
                source_id=source_id if profile == "web-collector" else None,
            ),
            "assumptions_policy": "assume_and_declare",
        },
        "output": output,
        "extra_quota_credits": None,
        "origin": _origin(),
        "status": "queued",
    }


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


async def generate_plan(
    query: str,
    store: Any,
    adapter: Any,
    resilience_config: ResilienceConfig | None = None,
    *,
    segment_retry_sleep: Any = None,
) -> Plan:
    """按骨架、逐 goal、整计划 lint 三阶段生成并原子保存计划。"""

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
    config = resilience_config or load_resilience_config()
    workspace_kwargs = (
        {"retry_sleep": segment_retry_sleep}
        if segment_retry_sleep is not None
        else {}
    )
    workspace = PlanSegmentWorkspace(
        runs_root / research_id,
        config,
        **workspace_kwargs,
    )
    skeleton_errors: list[str] = []
    scaffolds: list[dict[str, Any]] | None = None
    for skeleton_attempt in range(1, config.plan_segment_retries + 1):
        try:
            skeleton = await workspace.generate(
                "skeleton",
                _skeleton_prompt(normalized_query, skeleton_errors),
                adapter,
                on_retry=lambda retry, error: _emit(
                    store,
                    _retry_event(
                        research_id,
                        retry,
                        [f"[段 skeleton] {error}"],
                    ),
                ),
            )
            scaffolds = _skeleton_scaffolds(skeleton)
            break
        except PlanSegmentError as exc:
            raise PlanGenerationError(str(exc)) from exc
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            skeleton_errors = [f"[结构] {type(exc).__name__}: {exc}"]
            if skeleton_attempt < config.plan_segment_retries:
                await _emit(
                    store,
                    _retry_event(research_id, skeleton_attempt + 1, skeleton_errors),
                )
    if scaffolds is None:
        raise PlanGenerationError(
            f"规划骨架连续 {config.plan_segment_retries} 次仍有 error：\n"
            + "\n".join(skeleton_errors)
        )

    expansions: dict[str, dict[str, Any]] = {}

    async def generate_goal(index: int, errors: list[str]) -> None:
        scaffold = scaffolds[index - 1]
        goal_id = f"goal-{index}"
        expansion = await workspace.generate(
            goal_id,
            _goal_prompt(normalized_query, goal_id, scaffold, errors),
            adapter,
            on_retry=lambda retry, error: _emit(
                store,
                _retry_event(
                    research_id,
                    retry,
                    [f"[段 {goal_id}] {error}"],
                ),
            ),
        )
        if (
            expansion.get("deliverable") is None
            and isinstance(expansion.get("goals"), list)
            and len(expansion["goals"]) >= index
            and isinstance(expansion["goals"][index - 1], Mapping)
        ):
            # 兼容只会写旧整份骨架的测试/部署替身；真实 Claude 短流
            # 仍按本段契约返回单 goal，规划路由不会因此回退成长调用。
            expansion = dict(expansion["goals"][index - 1])
        expansions[goal_id] = expansion

    try:
        for index in range(1, len(scaffolds) + 1):
            await generate_goal(index, [])
    except PlanSegmentError as exc:
        raise PlanGenerationError(str(exc)) from exc

    errors: list[str] = []
    for attempt in range(1, config.plan_segment_retries + 1):
        try:
            expanded_goals = []
            for index, scaffold in enumerate(scaffolds, start=1):
                expansion = expansions[f"goal-{index}"]
                expanded_goals.append({
                    "title": scaffold["title"],
                    "objective": scaffold["objective"],
                    "depends_on": list(scaffold["depends_on"]),
                    "deliverable": expansion.get("deliverable"),
                    "acceptance": expansion.get("acceptance"),
                    "agents": expansion.get("agents"),
                })
            assembled = {"goals": expanded_goals}
            assembled_path = workspace.root / "assembled.json"
            assembled_path.write_text(
                json.dumps(assembled, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            plan = _build_plan(
                assembled,
                query=normalized_query,
                research_id=research_id,
                timestamp=timestamp,
            )
            errors = lint(plan)["errors"]
        except PlanSegmentError as exc:
            raise PlanGenerationError(str(exc)) from exc
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors = [f"[结构] {type(exc).__name__}: {exc}"]
        if not errors:
            store.save_plan_snapshot(
                research_id, snapshot=plan.to_dict(), expected_rev=0
            )
            return plan
        if attempt < config.plan_segment_retries:
            await _emit(store, _retry_event(research_id, attempt + 1, errors))
            joined = "\n".join(errors)
            affected = [
                index
                for index in range(1, len(scaffolds) + 1)
                if f"goal-{index}" in joined
            ]
            for index in affected or list(range(1, len(scaffolds) + 1)):
                try:
                    await generate_goal(index, errors)
                except PlanSegmentError as exc:
                    raise PlanGenerationError(str(exc)) from exc

    raise PlanGenerationError(
        f"计划生成连续 {config.plan_segment_retries} 次仍有 error，计划未保存：\n"
        + "\n".join(errors)
    )


__all__ = ["PlanGenerationError", "generate_plan"]
