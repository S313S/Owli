"""M2-b single 模式计划生成：引擎给骨架，系统补齐全部执行字段。"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from app.adapters import validation
from app.adapters.events import ItemKind, NormalizedEvent
from app.adapters.routing import pick_engine
from app.config import (
    ChapterEngineConfig,
    ResearchScaleConfig,
    ResearchScaleProfile,
    ResilienceConfig,
    load_research_scale_config,
    load_resilience_config,
)
from app.plan.allocation import (
    allocate_collections, collection_capacity, collection_plan_dict,
    per_goal_capacity,
)
from app.plan.chapters import generate_chapter_specs
from app.plan.lint import _SOURCE_MARKET_PROFILES, duplicate_collection_goal_ids, lint
from app.plan.normalize import normalize_plan
from app.plan.model import (
    DEFAULT_RETRY_POLICY, Plan, SECTIONED_CHAPTER_KINDS, rating_rows_path,
    rating_task_text,
)
from app.plan.question import make_questions
from app.plan.segments import PlanSegmentError, PlanSegmentWorkspace
from app.sources.registry import planning_catalog, source_limit_parameters


def _limit_parameter(source_id: str) -> str:
    """采集条数参数名一律问注册表（§M6-a 货 1 四表合一，不再手抄）。"""

    return source_limit_parameters().get(source_id, "limit")


_FORMATS = {"table", "markdown", "excel", "json"}
_SHAPES = {"object", "array"}
_MARKET_SOURCES = _SOURCE_MARKET_PROFILES
_LINT_GOAL_HEADER = re.compile(
    r"^\[(?:规则\d+|结构)\]\s+goal-([1-9][0-9]*)(?=[./\s：:]|$)"
)
_FINDINGS_FIELDS = frozenset({
    "competitor", "pros", "cons", "statement", "supporting_evidence",
    "evidence_id", "is_singleton", "conflicts", "gaps",
})


class PlanGenerationError(RuntimeError):
    """规划引擎或计划产物未通过协议。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


def _affected_goal_indices(errors: list[str], goal_count: int) -> list[int]:
    """只从 lint 消息头提取受影响 goal，引文中的 goal-N 不参与判定。"""

    affected: list[int] = []
    for message in errors:
        matched = _LINT_GOAL_HEADER.match(str(message))
        if matched is None:
            continue
        index = int(matched.group(1))
        if 1 <= index <= goal_count and index not in affected:
            affected.append(index)
    return affected


def _affected_chapter_refs(errors: list[str]) -> set[str]:
    """只按 lint 消息头的结构化 goal-N/ch-M 定位章，不解析正文。"""

    affected: set[str] = set()
    for message in errors:
        _, separator, body = str(message).partition("] ")
        if not separator:
            continue
        location = body.split(maxsplit=1)[0]
        goal_id, slash, chapter_id = location.partition("/")
        if (
            slash
            and goal_id.startswith("goal-")
            and goal_id.removeprefix("goal-").isdigit()
            and chapter_id.startswith("ch-")
            and chapter_id.removeprefix("ch-").isdigit()
        ):
            affected.add(f"{goal_id}/{chapter_id}")
    return affected


def _structure_errors(exc: Exception) -> list[str]:
    """把可归属的结构错误统一改成可机读的 goal 消息头。"""

    messages: list[str] = []
    for line in str(exc).splitlines() or [str(exc)]:
        matched = re.match(
            r"^\s*(goal-[1-9][0-9]*)(?=[./\s：:]|$)", line,
        )
        if matched is not None:
            messages.append(
                f"[结构] {matched.group(1)}. {type(exc).__name__}: {line.strip()}"
            )
    if messages:
        return list(dict.fromkeys(messages))
    return [f"[结构] {type(exc).__name__}: {exc}"]


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
    # collector_name 与 display_name 都唯一标识同一信息源；提示词以
    # 「display_name（collector_name）」列出，模型两种写法都该被接受
    # （6b 实跑取证：模型写「Hacker News」被拒，2026-08-21 r-e55ddfe36e51）。
    specs: dict[str, Any] = {}
    for spec in planning_catalog():
        specs[_normalize_name(spec.collector_name)] = spec
        specs[_normalize_name(spec.display_name)] = spec
    return specs


def _normalize_name(name: str) -> str:
    # 连字符/下划线与空格视为同一分隔（6b 实跑取证：模型写
    # 「product-hunt 数据抓取」被拒，2026-08-21 r-bbc15dc5c4e8）。
    return " ".join(name.casefold().replace("-", " ").replace("_", " ").split())


def _role_name(name: str) -> str:
    """取结构化 `角色·实体` 的角色部分；实体只用于章节颗粒度。"""

    return name.partition("·")[0].strip()


def _entity_name(name: str) -> str:
    """只按约定分隔符提取 `角色·实体` 的实体部分。"""

    return name.partition("·")[2].strip()


def _classify(name: str, task: str) -> tuple[str, str]:
    del task
    normalized = _normalize_name(_role_name(name))
    if normalized in _source_specs():
        return "data_collection", "web-collector"
    try:
        return _ROLE_MAP[normalized]
    except KeyError as exc:
        # 回灌必须自带闭集：只报「未知」不给合法值，模型重试轮无从自纠
        # （6b 实跑取证：hn_competitor_scope_collector 连拒三轮，2026-08-21）。
        roles = "、".join(_ROLE_MAP)
        collectors = "、".join(sorted(_source_specs()))
        raise ValueError(
            f"未知 agent 职能名称：{name}；非采集 agent 的 name 只能逐字取职能闭集："
            f"{roles}；采集 agent 的 name 只能逐字取共享注册表 collector_name："
            f"{collectors}"
        ) from exc


def _scale_profile(
    scale: str,
    scale_config: ResearchScaleConfig | None,
) -> ResearchScaleProfile:
    return (scale_config or load_research_scale_config()).profile(scale)


def _subjects_cap_rule(profile: ResearchScaleProfile) -> str:
    """§PLAN-1：有章数上限的档位把 subjects 上限讲在骨架这一步。"""

    capacity = collection_capacity(profile.max_goals, profile)
    if capacity is None:
        return ""
    return (
        f"subjects 最多 {capacity} 个（{profile.max_goals} 个 goal × 每 goal "
        f"{per_goal_capacity(profile)} 个采集位），超出会让骨架作废；"
    )


def _skeleton_prompt(
    query: str,
    errors: list[str],
    *,
    scale: str = "standard",
    scale_config: ResearchScaleConfig | None = None,
) -> str:
    profile = _scale_profile(scale, scale_config)
    retry = ""
    if errors:
        retry = "\n上一轮整计划 lint 错误原文（逐条修正结构）：\n" + "\n".join(errors)
    return (
        f"目标：为《{query}》生成 3–{profile.max_goals} 个 goal 的短骨架。\n"
        "方法要点：按证据链自然断点拆分；goal 之间只用 depends_on 表达有向无环依赖，"
        "禁止按搜索/阅读/总结工种拆 goal；同一 source_id 与 entity 的完整采集组合"
        "全计划只安排一次，同源不同实体允许分摊到多个 goal；需要复用的下游 goal "
        "必须通过 depends_on 连到已有采集链。\n"
        "产物结构：只输出 JSON object，顶层只含 market_profile、"
        "market_profile_justification、subjects、subjects_justification、goals。"
        "subjects 必须是被研究实体的非空去重字符串数组，并包含主体自身——"
        "只写能被逐个采集的**具体实体专名**（品牌、产品、机构、人物的名字）；"
        "题目本身、所属领域/行业/品类、平台名、以及「XX 社媒」「XX 矩阵」"
        "这类抽象概括词一律不得写进 subjects。下游段级校验会要求"
        "**每一个 subject 都有一个同名实体的采集章**，写不出采集对象的词"
        "放进来会让整份计划作废；题目只给了领域没点名主体时，"
        "自己点出该领域里可采集的具体主体，不要把领域名当主体。"
        f"{_subjects_cap_rule(profile)}"
        "subjects_justification 用一句可复核理由说明纳入边界；market_profile 只能取 "
        "cn_product/global_product，justification 用一句可复核理由；"
        "每个 goal 只能含 title、"
        "objective、depends_on。depends_on 只能引用在它之前的 goal-<n>。\n"
        "边界与降级：信息不足时做明确假设并继续，JSON 字符串值内部不得出现未转义"
        "的英文双引号（引用名称用中文引号「」），不输出 Markdown 围栏、说明或任何"
        f"执行字段。{retry}"
    )


def _goal_prompt(
    query: str,
    goal_id: str,
    scaffold: Mapping[str, Any],
    errors: list[str],
    *,
    upstream_collections: list[Mapping[str, str]] | None = None,
    subjects: list[str] | None = None,
    market_profile: str = "global_product",
    scale: str = "standard",
    scale_config: ResearchScaleConfig | None = None,
    collection_slots: Sequence[Mapping[str, str]] | None = None,
) -> str:
    profile = _scale_profile(scale, scale_config)
    retry = ""
    if errors:
        retry = "\n上一轮整计划 lint 错误原文（只修正本 goal 结构）：\n" + "\n".join(errors)
    slots = list(collection_slots or [])
    slots_json = json.dumps(slots, ensure_ascii=False, separators=(",", ":"))
    slot_names = "、".join(f"{s['collector_name']}·{s['entity']}" for s in slots) or "无"
    per_goal = per_goal_capacity(profile)
    non_collection_budget = (
        f"本 goal 非采集章最多 {max(profile.max_chapters_per_goal - len(slots), 1)} 个；"
        if per_goal is not None and profile.max_chapters_per_goal is not None else ""
    )
    allocation_rule = (
        "本 goal 必采清单（硬性，系统分配，逐条建采集 agent，name 逐字用「注册表原名·实体」），"
        f"必采清单 JSON={slots_json}，即 {slot_names}；清单外可加采集章但不得与清单及上游重复 "
        "(source_id, entity) 组合，且须在章预算内；{non_collection_budget}"
    ).replace("{non_collection_budget}", non_collection_budget)
    sources = "、".join(
        f"{item.display_name}（{item.collector_name}）"
        for item in planning_catalog()
    )
    inventory = json.dumps(
        list(upstream_collections or []),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if market_profile not in _MARKET_SOURCES:
        raise ValueError(f"market_profile 不在闭集：{market_profile!r}")
    applicable_sources = _MARKET_SOURCES[market_profile]
    coverage = json.dumps(
        {
            "market_profile": market_profile,
            "applicable_sources": sorted(applicable_sources),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    inventory_locations = "；".join(
        f"{item.get('goal_id')}/{item.get('agent_id')} "
        f"source_id={item.get('source_id')} entity={item.get('entity')} "
        f"output.path={item.get('output_path')}"
        for item in upstream_collections or []
    ) or "无"
    reuse_rule = (
        f"上游 goal 已采集源及产物路径={inventory}；定位={inventory_locations}；"
        "清单内相同 source_id 与 entity 的组合禁止重复采集，"
        "同源不同实体允许跨 goal 采集；重复组合必须通过 inputs 消费对应 output.path。"
    )
    if scale == "fast":
        source_limits = "、".join(
            f"{source_id} {_limit_parameter(source_id)}={limit}"
            for source_id, limit in sorted(profile.source_item_limits.items())
        )
        scale_rule = (
            f"快速档每个 goal 采集源最多 {profile.max_sources_per_goal} 个；"
            f"本 goal 章数上限为 {profile.max_chapters_per_goal}；"
            "采集清单已由系统按预算分配，超预算时先删清单外的采集章；"
            f"每源采集条数参数：{source_limits}；"
        )
        hn_rule = (
            "HN 查询固定使用 created_at_i>执行时点UTC epoch-7776000、points>50、"
            f"hitsPerPage={profile.source_item_limits['hacker_news']}；"
        )
    else:
        scale_rule = ""
        hn_rule = (
            "HN 查询固定使用 created_at_i>执行时点UTC epoch-7776000、points>50、"
            "hitsPerPage=1000；"
        )
    return (
        f"目标：扩展《{query}》中的 {goal_id}；骨架字段固定为："
        f"{json.dumps(dict(scaffold), ensure_ascii=False)}。\n"
        "方法要点：为这个 goal 选择能形成独立产物的执行链；信息源采集角色只从共享"
        f"注册表选择：{sources}；源 × 市场属性覆盖表={coverage}；"
        f"全计划研究实体闭集={json.dumps(list(subjects or scaffold.get('subjects', [])), ensure_ascii=False)}；"
        f"{allocation_rule}"
        f"采集章只能选择 applicable_sources 中的 source_id；{reuse_rule}{scale_rule}"
        "采集 agent 的 name 必须唯一确定 capability.sources "
        "与 source.* 工具；在章数预算内，优先一实体一源；name 用“注册表原名·竞品名”"
        "的结构化格式，"
        "同一采集角色可为不同竞品重复出现；"
        "非采集 agent 的 name 只能逐字取职能闭集："
        f"{'、'.join(_ROLE_MAP)}，不得自造名称。\n"
        "产物结构：只输出一个 JSON object，只含 deliverable、acceptance、agents。"
        "deliverable 含 format/path/description/shape，path 只写文件名，shape "
        "只能取 object/array；acceptance 是逐条可判定字符串数组；"
        "agents 每项只含 name、task、output，output 只含 shape 且只能取 "
        "object/array；交叉验证 / 报告撰写 / 摘要这类节化汇总章的 shape 及其"
        "所属 deliverable.shape 一律取 object（节化文档信封形）；不得输出 id、engine、"
        "capability、prompt、状态、重试或时间字段。\n"
        "边界与降级：JSON 字符串值内部不得出现未转义的英文双引号，引用名称一律"
        "用中文引号「」；采集 JSON 顶层必须为数组且每条含 permalink、fetched_at；"
        f"{hn_rule}"
        "数据不足时用结构化缺口口径，不得写死上游无法保证的实体最小条数；"
        f"不输出 Markdown 围栏或说明。{retry}"
    )


def _skeleton_scaffolds(
    value: Any,
    *,
    scale: str = "standard",
    scale_config: ResearchScaleConfig | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("goals"), list):
        raise ValueError("骨架顶层必须是含 goals 数组的 object")
    _skeleton_market_profile(value)
    subjects, subjects_justification = _skeleton_subjects(value)
    goals = value["goals"]
    profile = _scale_profile(scale, scale_config)
    if not 3 <= len(goals) <= profile.max_goals:
        raise ValueError(
            f"goal 数必须在 3–{profile.max_goals}，实际为 {len(goals)}"
        )
    capacity = collection_capacity(len(goals), profile)
    if capacity is not None and len(subjects) > capacity:
        raise ValueError(
            f"subjects 有 {len(subjects)} 个，超出 {scale} 档采集容量 {capacity}"
            f"（{len(goals)} 个 goal × 每 goal {per_goal_capacity(profile)} 个采集位）；"
            "请合并或删减研究实体"
        )
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
            "subjects": list(subjects),
            "subjects_justification": subjects_justification,
        })
    return result


def _skeleton_market_profile(value: Mapping[str, Any]) -> tuple[str, str]:
    profile = str(value.get("market_profile", ""))
    justification = str(value.get("market_profile_justification", "")).strip()
    if profile not in _MARKET_SOURCES:
        raise ValueError(
            "骨架 market_profile 只能取 cn_product 或 global_product"
        )
    if not justification:
        raise ValueError("骨架 market_profile_justification 不能为空")
    return profile, justification


def _skeleton_subjects(value: Mapping[str, Any]) -> tuple[list[str], str]:
    subjects = value.get("subjects")
    justification = str(value.get("subjects_justification", "")).strip()
    if not isinstance(subjects, list) or not subjects or not all(
        isinstance(item, str) and item.strip() for item in subjects
    ):
        raise ValueError("骨架 subjects 必须是包含主体自身的非空字符串数组")
    normalized = [str(item).strip() for item in subjects]
    if len(set(normalized)) != len(normalized):
        raise ValueError("骨架 subjects 不得含重复实体")
    if not justification:
        raise ValueError("骨架 subjects_justification 不能为空")
    return normalized, justification


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
    shape: str,
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = f"goals/{goal_id}/{agent_id}"
    if agent_kind in {"data_collection", "browser_automation"}:
        result = {
            "format": "json",
            "shape": shape,
            "path": f"{base}.json",
            "validators": [
                "file_exists", "json_array_min_items:1",
                "each_item_has:permalink,fetched_at",
            ],
        }
    elif (
        agent_kind in {"cross_validation", "reliability_audit"}
        and _target_is_findings(target)
    ):
        # §RATE-1 货 2：自动排出的评级章一律 target=None，走不到这条改判；
        # 模型自己写的「末级可靠度审计」承接 findings 交付物的老契约原样保留。
        result = {
            "format": "json", "shape": shape, "path": f"{base}.json",
            "validators": [
                "file_exists",
                "json_array_min_items:1",
                "each_item_has:competitor,pros,cons,conflicts,gaps",
            ],
        }
    elif agent_kind == "cross_validation":
        # 节化章走 §2.3.1 信封形：验证器查信封结构；逐条评级类数组验证器
        # 只属于非节化的 reliability_audit 章。shape 保留模型声明，非 object
        # 由规则 27 拒绝（与规则 17 同一「生成保真、lint 把关」口径）。
        result = {
            "format": "json", "shape": shape, "path": f"{base}.json",
            "validators": ["file_exists", "sectioned_document_valid"],
        }
    elif agent_kind in {"audit", "reliability_audit"}:
        result = {
            "format": "json", "shape": shape, "path": f"{base}.json",
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
            "format": "excel", "shape": shape, "path": f"{base}.xlsx",
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
        result = {
            "format": "markdown", "shape": shape,
            "path": f"{base}.md", "validators": validators,
        }
    if target is not None:
        if target["format"] != result["format"]:
            if agent_kind in SECTIONED_CHAPTER_KINDS and target["format"] == "json":
                # 节化章的 json 交付物是 §2.3.1 信封，不再塌成裸 file_exists。
                result["validators"] = ["file_exists", "sectioned_document_valid"]
            else:
                result["validators"] = ["file_exists"]
                if target["format"] == "excel":
                    result["validators"].append("openpyxl_reload_ok")
        result.update(format=target["format"], path=target["path"])
    return result


def _target_is_findings(target: Mapping[str, Any] | None) -> bool:
    if target is None:
        return False
    identifiers = set(re.findall(
        r"[A-Za-z][A-Za-z0-9_]*", str(target.get("description", ""))
    ))
    return len(identifiers & _FINDINGS_FIELDS) >= 2


SOURCE_HANDBOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "app" / "prompts" / "common" / "sources-v1.md"
)


def render_source_handbook(scale: str = "standard") -> str:
    """信息源手册：静态通用页 + 按注册表生成的逐源表（§SRC-1）。

    加源只改各源自己的 `SOURCE_SPEC`，逐源表自动更新——不必回来改这里，
    也不必手工维护一份会漂的清单（M6 接微信/MediaCrawler 时直接受益）。
    """

    page = SOURCE_HANDBOOK_PATH.read_text(encoding="utf-8").rstrip("\n")
    limits = load_research_scale_config().profile(scale).source_item_limits
    parameters = source_limit_parameters()
    rows = ["", "## 5. 当前可用的源", "",
            "| 工具 | 是什么 | 时间窗 | 本档名额 | 要点 |",
            "|---|---|---|---|---|"]
    for spec in planning_catalog():
        parameter = parameters.get(spec.source_id, "limit")
        limit = limits.get(spec.source_id)
        quota = f"`{parameter}={limit}`" if limit is not None else "按章任务给定"
        window = (
            " / ".join(f"`{item}`" for item in spec.window.examples)
            if spec.window is not None else "**本源不要这个参数**"
        )
        rows.append(
            f"| `{spec.tool_name}` | {spec.display_name}："
            f"{spec.capability_description} | {window} | {quota} | "
            f"{spec.prompt_hint} |"
        )
    return page + "\n" + "\n".join(rows) + "\n"


def _agent_prompt(
    query: str,
    task: str,
    output: Mapping[str, Any],
    agent_kind: str,
    *,
    source_id: str | None = None,
    source_item_limit: int | None = None,
    scale: str = "standard",
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
            "不得有悬空角标或未被引用的信息源；在满足双向一致的引用子集里，"
            "每个实际消费的平台至少选入一条证据，不得整体遗漏某个平台。"
        )
    if agent_kind in {"data_collection", "browser_automation"}:
        spec = next(
            (item for item in planning_catalog() if item.source_id == source_id),
            None,
        )
        # §SRC-1 货 8：原文只说「调用 source.xxx」，模型照样用引擎自带的
        # web_search——六轮里 source.web_search 零调用，89/76 次搜索全走自带工具。
        # 自带工具的结果只落在产物文件里，章一超时就整批作废；而 source.* 走 MCP
        # 是直接落库的。所以这里把它变成硬约束，而不是一句建议。
        if spec is not None:
            exclusive_rule = (
                f"本章证据**只能**来自 {spec.tool_name} 的返回值："
                "产物里每一条记录的 permalink 都必须是该工具这次返回过的。"
                "引擎自带的联网搜索只可用来想查询式，"
                "其结果一律不得写进产物、不得当作证据来源。"
                f"若 {spec.tool_name} 取不到，按结构化缺口口径如实记录，"
                "不得用自带搜索补位。"
            )
            method = (
                f"查询式={query}；调用 {spec.tool_name}；"
                f"{spec.prompt_hint}。{exclusive_rule}"
            )
        else:
            method = f"查询式={query}；按 capability 声明的信息源执行采集。"
        if scale == "fast" and source_id and source_item_limit is not None:
            parameter = _limit_parameter(source_id)
            method += f"快速档以 {parameter}={source_item_limit} 覆盖默认采集条数。"
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
        if "sectioned_document_valid" in output["validators"]:
            evidence_rule += (
                "本章走节化执行：你每次只写一个节的 Markdown 正文，最终产物由系统"
                "确定性组装成节化文档信封（顶层 JSON object，固定含 title、chapter_id、"
                "sections 数组与缺失清单，sections 每项含 section_id/goal_id/title/"
                "markdown）。不要自行输出顶层 JSON 数组，也不要逐条评级——"
                "五维评级校验属于可靠度审计章，不适用本文件。"
            )
        elif "no_item_missing_rating" in output["validators"]:
            evidence_rule += (
                "本文件顶层必须是 JSON 数组；每个元素为一条评级条目，"
                "**必须原样回带被评那条证据的 permalink**（逐字复制，不改写、"
                "不补全、不去参数）。本章由系统按批喂入（物化文件切成 .rows.<n>.json，"
                "每次会话只评任务里指到的**这一批**、产物写到指定的批产物路径），"
                "条数与本批输入证据一一对应：不新增、不合并、不丢条，"
                "其它批不要读也不要评；系统按 permalink 把评分贴回已入库的那一行，"
                "permalink 对不上就等于这条评级作废。每条还必须带齐 "
                "score_authority、score_freshness、score_crossref、score_completeness、"
                "score_independence、rating_notes、rated_by 七个字段"
                "（**五维评分只能是 0、1、2 三个整数之一**——不是 0–5、不是百分制、"
                "不是小数；越界的条目会被整条丢弃，等于白评。"
                "rating_notes 必须是五段式一行：'权威N:依据 · 时效N:依据 · 交叉N:依据 · "
                "完整N:依据 · 无关N:依据'——五段按此顺序、用' · '分隔，N 逐字等于对应的"
                "五维列分值，每段依据 ≤14 字、不含'可能/大概/视情况/疑似/应该是'，"
                "格式不符整条作废；rated_by 填 agent_id）；"
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
        elif any(
            str(item).startswith("each_item_has:competitor,pros,cons")
            for item in output["validators"]
        ):
            evidence_rule += (
                "本文件顶层必须是 findings JSON 数组；每个竞品对象带齐 "
                "competitor、pros、cons、conflicts、gaps。pros/cons 中每条结论"
                "必须含非空 statement、supporting_evidence；每条支撑证据保留 "
                "evidence_id 与 permalink，并显式写 is_singleton。来源矛盾写入 "
                "conflicts，证据不足写入 gaps；可靠度复核只能在该结构内补字段，"
                "不得把 findings 改写成逐证据评级数组。"
            )
    body = (
        f"目标：{task}；把可复核结果写入 {output['path']}。\n"
        f"方法要点：{method}{chart_rule}\n"
        f"产物结构：format={output['format']}；校验约束={structure}；{evidence_rule}\n"
        "边界与降级：命中不足时保留实际小数组并写明数量，不放宽时间窗或 points 阈值；"
        "无法满足某条验收时在结构化结论 unmet 逐条列明。"
    )
    if agent_kind in {"data_collection", "browser_automation"}:
        # §SRC-1：采集 agent 共用一页信息源手册（参数格式、名额、失败怎么办）。
        body += f"\n\n---\n\n{render_source_handbook(scale)}"
    return body


def _deliverable(raw: Any, goal_id: str) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{goal_id}.deliverable 必须是 object")
    format_name = str(raw.get("format", "")).strip().casefold()
    # 无歧义同义写法归一化接受；报错必须带闭集（6b 实跑取证：模型写
    # 'xlsx' 被拒且报错不给合法值，两轮未自纠，2026-08-21 r-bbc15dc5c4e8）。
    format_name = {"xlsx": "excel", "xls": "excel", "md": "markdown"}.get(
        format_name, format_name
    )
    if format_name not in _FORMATS:
        raise ValueError(
            f"{goal_id}.deliverable.format 非法：{format_name!r}；"
            f"只能取：{'、'.join(sorted(_FORMATS))}"
        )
    original = PurePosixPath(str(raw.get("path", "result.md")))
    leaf = original.name
    if not leaf or leaf in {".", ".."}:
        raise ValueError(f"{goal_id}.deliverable.path 缺少文件名")
    description = str(raw.get("description", "")).strip()
    if not description:
        raise ValueError(f"{goal_id}.deliverable.description 不能为空")
    shape = str(raw.get("shape", "")).strip().casefold()
    if shape not in _SHAPES:
        raise ValueError(
            f"{goal_id}.deliverable.shape 非法：{shape!r}；"
            "只能取 object/array"
        )
    return {
        "format": format_name,
        "shape": shape,
        "path": f"goals/{goal_id}/{leaf}",
        "description": description,
    }


def _origin() -> dict[str, str]:
    fields = (
        "_node", "display_name", "entity", "task", "depends_on", "inputs", "engine",
        "model", "capability", "prompt.body", "output", "chapter",
        "extra_quota_credits",
    )
    return {field: "generated" for field in fields}


def _align_deliverable_shape(
    raw: Any, deliverable: dict[str, Any], goal_id: str, repairs: list[str],
) -> Any:
    """[修正17] 归属 deliverable 的末位 agent 与 deliverable 的 shape 对齐。

    两者都是同一段模型写的，不一致只是漂移：deliverable 是 goal 的对外契约，
    以它为准；节化章种类（规则 27）两边一律 object。修正记进 repairs 留痕。
    """

    if not isinstance(raw, Mapping) or not isinstance(raw.get("output"), Mapping):
        return raw
    shape = str(raw["output"].get("shape", "")).strip().casefold()
    target = str(deliverable.get("shape", "")).strip().casefold()
    if shape not in _SHAPES or target not in _SHAPES or shape == target:
        return raw
    name = str(raw.get("display_name") or raw.get("name") or "").strip()
    try:
        kind, _ = _classify(name, str(raw.get("task", "")))
    except ValueError:
        return raw
    final = "object" if kind in SECTIONED_CHAPTER_KINDS else target
    if final != target:
        deliverable["shape"] = final
    repairs.append(
        f"[修正17] {goal_id}/{name} output.shape {shape}→{final}"
        f"（归属 deliverable.shape={target}{'→object' if final != target else ''}）"
    )
    return {**raw, "output": {**raw["output"], "shape": final}}


def _build_plan(
    skeleton: Any,
    *,
    query: str,
    research_id: str,
    timestamp: str,
    scale: str = "standard",
    scale_config: ResearchScaleConfig | None = None,
    market_profile: str = "global_product",
    market_profile_justification: str = "兼容旧调用的全球产品默认。",
    subjects: list[str] | None = None,
    subjects_justification: str = "兼容旧调用，未声明研究实体。",
    repairs: list[str] | None = None,
) -> Plan:
    if not isinstance(skeleton, Mapping) or not isinstance(skeleton.get("goals"), list):
        raise ValueError("规划产物顶层必须是含 goals 数组的 object")
    raw_goals = skeleton["goals"]
    profile = _scale_profile(scale, scale_config)
    if not 3 <= len(raw_goals) <= profile.max_goals:
        raise ValueError(
            f"goal 数必须在 3–{profile.max_goals}，实际为 {len(raw_goals)}"
        )
    for index, raw_goal in enumerate(raw_goals, start=1):
        if not isinstance(raw_goal, Mapping):
            raise ValueError(f"goal-{index} 骨架必须是 object")
    # 结构错误一次收集全量：逐个抛错会让段级重试打地鼠式消耗预算
    # （6b 实跑取证：goal-1/2/4 各占一轮吃光 3 次预算，2026-08-21）。
    structure_errors: list[str] = []
    deliverables: dict[str, dict[str, Any]] = {}
    for index, raw_goal in enumerate(raw_goals, start=1):
        goal_id = f"goal-{index}"
        try:
            deliverables[goal_id] = _deliverable(raw_goal.get("deliverable"), goal_id)
        except ValueError as exc:
            structure_errors.append(str(exc))
    counters: Counter[str] = Counter()
    goals: list[dict[str, Any]] = []
    collection_artifacts: list[dict[str, str]] = []
    for index, raw_goal in enumerate(raw_goals, start=1):
        goal_id = f"goal-{index}"
        if goal_id not in deliverables:
            continue
        try:
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
            dependencies_by_goal = {
                str(item["goal_id"]): list(item.get("depends_on", []))
                for item in goals
            }
            ancestors: set[str] = set()
            pending = list(depends_on)
            while pending:
                dependency = pending.pop()
                if dependency in ancestors:
                    continue
                ancestors.add(dependency)
                pending.extend(dependencies_by_goal.get(dependency, []))
            reusable_inputs = [
                {
                    "from_goal": item["goal_id"],
                    "artifact": item["output_path"],
                }
                for item in collection_artifacts
                if item["goal_id"] in ancestors
            ]
            for agent_index, item in enumerate(raw_agents):
                if agent_index == len(raw_agents) - 1 and repairs is not None:
                    item = _align_deliverable_shape(
                        item, deliverables[goal_id], goal_id, repairs,
                    )
                agent = _build_agent(
                    item,
                    goal_id,
                    list(depends_on),
                    query,
                    counters,
                    previous_agent_id=previous_agent_id,
                    upstream_artifacts=upstream_artifacts if agent_index == 0 else {},
                    reusable_inputs=reusable_inputs if agent_index == 0 else [],
                    target=deliverables[goal_id] if agent_index == len(raw_agents) - 1 else None,
                    scale=scale,
                    scale_profile=profile,
                )
                is_collection = agent["capability"]["profile"] == "web-collector"
                if is_collection:
                    agent["depends_on"] = []
                elif agents and all(
                    item["capability"]["profile"] == "web-collector"
                    for item in agents
                ):
                    # 同一重型 goal 的竞品 × 信息源采集章可并行；首个汇总章
                    # 必须等齐全部采集章，不能只依赖最后一个。
                    agent["depends_on"] = [item["agent_id"] for item in agents]
                agents.append(agent)
                previous_agent_id = agent["agent_id"]
            agents = _insert_rating_agents(
                agents, goal_id=goal_id, upstream=list(depends_on), query=query,
                counters=counters, scale=scale, scale_profile=profile,
            )
            source_ids = {
                str(source)
                for agent in agents
                for source in agent.get("capability", {}).get("sources", [])
            }
            if (
                profile.max_sources_per_goal is not None
                and len(source_ids) > profile.max_sources_per_goal
            ):
                raise ValueError(
                    f"{goal_id} 在 {scale} 档采集源最多 "
                    f"{profile.max_sources_per_goal} 个，实际为 {len(source_ids)}："
                    f"{sorted(source_ids)}"
                )
            for agent in agents:
                for source_id in agent.get("capability", {}).get("sources", []):
                    collection_artifacts.append({
                        "goal_id": goal_id,
                        "agent_id": str(agent["agent_id"]),
                        "source_id": str(source_id),
                        "output_path": str(agent["output"]["path"]),
                    })
            retry_policy = dict(DEFAULT_RETRY_POLICY)
            if profile.chapter_wall_clock_seconds is not None:
                retry_policy["chapter_deadline_seconds"] = (
                    profile.chapter_wall_clock_seconds
                )
            goals.append({
                "goal_id": goal_id,
                "title": title[:24],
                "objective": objective,
                "depends_on": list(depends_on),
                "deliverable": deliverables[goal_id],
                "acceptance": [str(item) for item in acceptance],
                "intervention": {"on_complete": True, "prompt": f"请核对《{title[:24]}》产物，是否继续？"},
                "retry_policy": retry_policy,
                "on_upstream_failure": "skip",
                "agents": agents,
                "status": "pending",
            })
        except ValueError as exc:
            structure_errors.append(str(exc))
    if structure_errors:
        raise ValueError("\n".join(structure_errors))
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
        "market_profile": market_profile,
        "market_profile_justification": market_profile_justification,
        "subjects": list(subjects or []),
        "subjects_justification": subjects_justification,
        "scale": scale,
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
    reusable_inputs: list[Mapping[str, str]] | None = None,
    target: Mapping[str, Any] | None = None,
    scale: str = "standard",
    scale_profile: ResearchScaleProfile | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{goal_id}.agents 的元素必须是 object")
    name = str(raw.get("display_name") or raw.get("name") or "").strip()
    task = str(raw.get("task", "")).strip()
    if not name or not task:
        raise ValueError(f"{goal_id}.agents 的 name/task 不能为空")
    raw_output = raw.get("output")
    if not isinstance(raw_output, Mapping) or set(raw_output) != {"shape"}:
        raise ValueError(f"{goal_id}.agents 的 output 必须是仅含 shape 的 object")
    shape = str(raw_output.get("shape", "")).strip().casefold()
    if shape not in _SHAPES:
        raise ValueError(
            f"{goal_id}.agents.output.shape 非法：{shape!r}；"
            "只能取 object/array"
        )
    try:
        agent_kind, profile = _classify(name, task)
    except ValueError as exc:
        # 带上 goal_id 才能让段级重试只重跑涉事段，而不是整份计划全重跑。
        raise ValueError(f"{goal_id} {exc}") from exc
    normalized_name = _normalize_name(_role_name(name))
    source_spec = _source_specs().get(normalized_name)
    source_id = source_spec.source_id if source_spec is not None else "hacker_news"
    entity = (_entity_name(name) or None) if agent_kind == "data_collection" else None
    counters[agent_kind] += 1
    suffix = "" if counters[agent_kind] == 1 else f"-{counters[agent_kind]}"
    agent_id = f"{agent_kind.replace('_', '-')}{suffix}"
    output = _output(agent_kind, goal_id, agent_id, shape, target)
    inputs = [
        {"from_goal": item, "artifact": upstream_artifacts[item]}
        for item in upstream
        if item in upstream_artifacts
    ]
    known_inputs = {(item["from_goal"], item["artifact"]) for item in inputs}
    for item in reusable_inputs or []:
        candidate = {
            "from_goal": str(item.get("from_goal", "")),
            "artifact": str(item.get("artifact", "")),
        }
        key = (candidate["from_goal"], candidate["artifact"])
        if all(candidate.values()) and key not in known_inputs:
            inputs.append(candidate)
            known_inputs.add(key)
    profile_config = scale_profile or _scale_profile(scale, None)
    return {
        "agent_id": agent_id,
        "display_name": name,
        "entity": entity,
        "task": task[:200],
        "depends_on": [] if previous_agent_id is None else [previous_agent_id],
        "inputs": inputs,
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
                source_item_limit=profile_config.source_item_limits.get(source_id),
                scale=scale,
            ),
            "assumptions_policy": "assume_and_declare",
        },
        "output": output,
        "extra_quota_credits": None,
        "origin": _origin(),
        "status": "queued",
    }


def _insert_rating_agents(
    agents: list[dict[str, Any]],
    *,
    goal_id: str,
    upstream: list[str],
    query: str,
    counters: Counter[str],
    scale: str,
    scale_profile: ResearchScaleProfile,
) -> list[dict[str, Any]]:
    """§RATE-1 货 2：每个采集章后面挂一个只评它的评级章（采集即评级）。

    评级章 `depends_on` 只连它那一个采集章——调度器一轮把所有 ready 的 agent
    全起（`scheduler._agent_ready`），所以某个采集章一 done 它的评级章立刻起跑，
    其余采集章照常在跑，评级墙钟基本被采集期吃掉。
    首个汇总章改为等齐**全部评级章**：评级是写作的前置条件，不是事后贴的标签。
    """
    collector_ids = [
        str(agent["agent_id"]) for agent in agents
        if agent["capability"]["profile"] == "web-collector"
    ]
    if not collector_ids:
        return agents
    result: list[dict[str, Any]] = []
    rating_ids: list[str] = []
    for agent in agents:
        result.append(agent)
        if agent["capability"]["profile"] != "web-collector":
            continue
        rating = _rating_agent(
            agent, goal_id=goal_id, upstream=upstream, query=query,
            counters=counters, scale=scale, scale_profile=scale_profile,
        )
        rating_ids.append(str(rating["agent_id"]))
        result.append(rating)
    for agent in result:
        if str(agent["agent_id"]) in rating_ids:
            continue
        if set(agent["depends_on"]) == set(collector_ids):
            agent["depends_on"] = list(rating_ids)
    return result


RATING_AGENT_KIND = "reliability_audit"


def _rating_agent(
    collector: Mapping[str, Any],
    *,
    goal_id: str,
    upstream: list[str],
    query: str,
    counters: Counter[str],
    scale: str,
    scale_profile: ResearchScaleProfile,
) -> dict[str, Any]:
    """§RATE-1 货 2：给一个采集章配一个只评它的评级章。

    产物路径/验证器沿用 `_output` 的 reliability_audit 五件，不新增校验器名。
    """
    counters[RATING_AGENT_KIND] += 1
    count = counters[RATING_AGENT_KIND]
    agent_id = "reliability-audit" + ("" if count == 1 else f"-{count}")
    output = _output(RATING_AGENT_KIND, goal_id, agent_id, "array", None)
    source_path = str(collector["output"]["path"])
    # §RATE-2 货 1：读的是**物化行文件**（该采集章真正入库的全部证据），
    # 不是采集产物（那里只有模型顺手写下的一小撮）。
    rows_path = rating_rows_path(source_path)
    # §RATE-3 货 4：文案只此一处（model.rating_task_text），章规格 / 重放脚本共用。
    task = rating_task_text(rows_path)
    return {
        "agent_id": agent_id,
        "display_name": "可靠度审计",
        "entity": None,
        "task": task[:200],
        "depends_on": [str(collector["agent_id"])],
        "inputs": [],
        "engine": pick_engine(RATING_AGENT_KIND, None).engine,
        "model": None,
        "capability": _capability("readonly-analyst", goal_id, upstream),
        "prompt": {
            "preamble_ref": "common/v1",
            "body": _agent_prompt(
                query, task, output, RATING_AGENT_KIND, scale=scale,
            ),
            "assumptions_policy": "assume_and_declare",
        },
        "output": output,
        "extra_quota_credits": None,
        "origin": _origin(),
        "status": "queued",
    }


def _upstream_collection_inventory(
    expansions: Mapping[str, Mapping[str, Any]],
    stop_before: int,
) -> list[dict[str, str]]:
    """从已落盘 goal 段确定性还原其采集源与最终 output.path。"""

    counters: Counter[str] = Counter()
    inventory: list[dict[str, str]] = []
    for index in range(1, stop_before):
        goal_id = f"goal-{index}"
        expansion = expansions.get(goal_id)
        if not isinstance(expansion, Mapping):
            continue
        raw_agents = expansion.get("agents", [])
        if not isinstance(raw_agents, list):
            continue
        try:
            deliverable = _deliverable(expansion.get("deliverable"), goal_id)
        except ValueError:
            deliverable = None
        for agent_index, raw in enumerate(raw_agents):
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("display_name") or raw.get("name") or "").strip()
            try:
                agent_kind, _ = _classify(name, "")
            except ValueError:
                continue
            counters[agent_kind] += 1
            suffix = "" if counters[agent_kind] == 1 else f"-{counters[agent_kind]}"
            agent_id = f"{agent_kind.replace('_', '-')}{suffix}"
            source_spec = _source_specs().get(_normalize_name(_role_name(name)))
            if source_spec is None:
                continue
            target = (
                deliverable
                if agent_index == len(raw_agents) - 1 and deliverable is not None
                else None
            )
            raw_output = raw.get("output")
            shape = (
                str(raw_output.get("shape", ""))
                if isinstance(raw_output, Mapping)
                else ""
            )
            if shape not in _SHAPES:
                continue
            output = _output(agent_kind, goal_id, agent_id, shape, target)
            inventory.append({
                "goal_id": goal_id,
                "agent_id": agent_id,
                "source_id": source_spec.source_id,
                "entity": _entity_name(name),
                "output_path": str(output["path"]),
            })
    return inventory


async def _emit(store: Any, event: NormalizedEvent) -> None:
    sink = getattr(store, "on_plan_event", None)
    if sink is None:
        return
    result = sink(event)
    if inspect.isawaitable(result):
        await result


def _progress_event(research_id: str, text: str) -> NormalizedEvent:
    return NormalizedEvent(
        engine="Owli",
        thread_id=research_id,
        turn_id="plan-progress",
        item_kind=ItemKind.THINKING,
        text=text,
        is_error=False,
        raw={},
        outcome="plan_progress",
    )


async def _emit_repairs(store: Any, research_id: str, repairs: list[str]) -> None:
    """§PLAN-1 货 2：每条机械修正各发一条事件，工作板与探针都看得见。"""

    for note in repairs:
        event = _progress_event(research_id, f"机械修正：{note}")
        await _emit(store, dataclasses.replace(event, raw={"repair": note}))


def _allocation_event(
    research_id: str, collection_plan: Mapping[str, list[dict[str, str]]],
) -> NormalizedEvent:
    """§PLAN-1：采集分配表落盘后发一条可读事件，工作板与探针都能看到。"""

    summary = "；".join(
        f"{goal_id}=" + "、".join(f"{s['source_id']}·{s['entity']}" for s in slots)
        for goal_id, slots in collection_plan.items() if slots
    ) or "无"
    return NormalizedEvent(
        engine="Owli",
        thread_id=research_id,
        turn_id="plan-progress",
        item_kind=ItemKind.THINKING,
        text=f"采集分配表：{summary}",
        is_error=False,
        raw={"collection_plan": dict(collection_plan)},
        outcome="plan_progress",
    )


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
    scale: str = "standard",
    scale_config: ResearchScaleConfig | None = None,
    chapter_engine_config: ChapterEngineConfig | None = None,
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
    product_scale_config = scale_config or load_research_scale_config()
    product_scale_config.profile(scale)
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
    market_profile = ""
    market_profile_justification = ""
    subjects: list[str] = []
    subjects_justification = ""
    collection_plan: dict[str, list[dict[str, str]]] = {}
    for skeleton_attempt in range(1, config.plan_segment_retries + 1):
        try:
            skeleton = await workspace.generate(
                "skeleton",
                _skeleton_prompt(
                    normalized_query,
                    skeleton_errors,
                    scale=scale,
                    scale_config=product_scale_config,
                ),
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
            scaffolds = _skeleton_scaffolds(
                skeleton,
                scale=scale,
                scale_config=product_scale_config,
            )
            market_profile, market_profile_justification = (
                _skeleton_market_profile(skeleton)
            )
            subjects, subjects_justification = _skeleton_subjects(skeleton)
            collection_plan = collection_plan_dict(allocate_collections(
                subjects, market_profile, scaffolds,
                product_scale_config.profile(scale),
            ))
            (workspace.root / "allocation.json").write_text(
                json.dumps(collection_plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            await _emit(
                store,
                _progress_event(
                    research_id, f"规划骨架落盘：{len(scaffolds)} 个 goal"
                ),
            )
            await _emit(store, _allocation_event(research_id, collection_plan))
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
            _goal_prompt(
                normalized_query,
                goal_id,
                scaffold,
                errors,
                upstream_collections=_upstream_collection_inventory(
                    expansions, index
                ),
                market_profile=market_profile,
                subjects=subjects,
                scale=scale,
                scale_config=product_scale_config,
                collection_slots=collection_plan.get(goal_id, []),
            ),
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
        await _emit(
            store, _progress_event(research_id, f"规划段 {goal_id} 落盘")
        )

    try:
        for index in range(1, len(scaffolds) + 1):
            await generate_goal(index, [])
    except PlanSegmentError as exc:
        raise PlanGenerationError(str(exc)) from exc

    max_chapters = product_scale_config.profile(scale).max_chapters_per_goal
    errors: list[str] = []
    plan: Plan | None = None
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
            repairs: list[str] = []
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
                scale=scale,
                scale_config=product_scale_config,
                market_profile=market_profile,
                market_profile_justification=market_profile_justification,
                subjects=subjects,
                subjects_justification=subjects_justification,
                repairs=repairs,
            )
            repairs.extend(normalize_plan(plan))
            await _emit_repairs(store, research_id, repairs)
            errors = lint(
                plan, max_chapters_per_goal=max_chapters,
                collection_plan=collection_plan,
            )["errors"]
        except PlanSegmentError as exc:
            raise PlanGenerationError(str(exc)) from exc
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors = _structure_errors(exc)
        if not errors:
            break
        if attempt < config.plan_segment_retries:
            await _emit(store, _retry_event(research_id, attempt + 1, errors))
            duplicate_goals = (
                duplicate_collection_goal_ids(plan)
                if plan is not None
                else set()
            )
            affected = _affected_goal_indices(
                [item for item in errors if not item.startswith("[规则21]")],
                len(scaffolds),
            )
            affected.extend(
                index
                for index in range(1, len(scaffolds) + 1)
                if f"goal-{index}" in duplicate_goals and index not in affected
            )
            for index in affected or list(range(1, len(scaffolds) + 1)):
                try:
                    workspace.reset_attempts(f"goal-{index}")
                    await generate_goal(index, errors)
                except PlanSegmentError as exc:
                    raise PlanGenerationError(str(exc)) from exc

    if errors or plan is None:
        raise PlanGenerationError(
            f"计划段级 lint 连续 {config.plan_segment_retries} 次仍有 error，"
            "计划未保存：\n" + "\n".join(errors)
        )

    chapter_errors: list[str] = []
    only_chapters: set[str] | None = None
    chapter_config = chapter_engine_config or ChapterEngineConfig()
    for chapter_attempt in range(1, config.plan_chapter_lint_retries + 1):
        try:
            await generate_chapter_specs(
                plan,
                workspace,
                adapter,
                chapter_config,
                on_chapter=lambda goal_id, chapter_id: _emit(
                    store,
                    _progress_event(
                        research_id, f"章 {goal_id}/{chapter_id} 落盘"
                    ),
                ),
                only_chapters=only_chapters,
                lint_errors=chapter_errors,
            )
            await _emit_repairs(store, research_id, normalize_plan(plan))
            chapter_errors = lint(
                plan, max_chapters_per_goal=max_chapters,
                collection_plan=collection_plan,
            )["errors"]
        except PlanSegmentError as exc:
            raise PlanGenerationError(str(exc)) from exc
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            chapter_errors = _structure_errors(exc)
        if not chapter_errors:
            store.save_plan_snapshot(
                research_id, snapshot=plan.to_dict(), expected_rev=0
            )
            await _emit(
                store,
                _progress_event(
                    research_id,
                    f"计划通过 lint 并保存：{len(plan.goals)} 个 goal",
                ),
            )
            return plan
        if chapter_attempt < config.plan_chapter_lint_retries:
            await _emit(
                store,
                _retry_event(research_id, chapter_attempt + 1, chapter_errors),
            )
            only_chapters = _affected_chapter_refs(chapter_errors) or {
                f"{goal.goal_id}/ch-{index}"
                for goal in plan.goals
                for index, _ in enumerate(goal.agents, start=1)
            }

    raise PlanGenerationError(
        "reason=chapter_lint_not_converged\n"
        f"章级 lint 未收敛：连续 {config.plan_chapter_lint_retries} 次仍有 error，"
        "计划未保存。最后一轮错误原文：\n" + "\n".join(chapter_errors),
        reason="chapter_lint_not_converged",
    )


__all__ = ["PlanGenerationError", "generate_plan"]
