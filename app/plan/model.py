"""plan → goals → agents 三层计划树与无损 JSON 转换。"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_GOAL_ID_PATTERN = re.compile(r"^goal-([1-9][0-9]*)$")
_ORIGIN_VALUES = {"generated", "user", "reset"}

DEFAULT_RETRY_POLICY = {
    "max_attempts_per_round": 10,
    "ask_engine_switch_at": 5,
    "max_rounds": 2,
    "goal_deadline_hours": 12,
    "on_exhausted": "fail_goal",
}
OPTIONAL_RETRY_POLICY_FIELDS = frozenset({"chapter_deadline_seconds"})

# agent_id 前缀 → 职能；运行期与 plan_lint 共用（原 runtime._agent_kind 内联表）。
AGENT_KIND_PREFIXES = {
    "goal-planning": "goal_planning",
    "plan-arbitration": "plan_arbitration",
    "reliability-audit": "reliability_audit",
    "cross-validation": "cross_validation",
    "consistency-check": "consistency_check",
    "report-writing": "report_writing",
    "summary": "summary",
    "tagging": "tagging",
    "data-collection": "data_collection",
    "browser-automation": "browser_automation",
    "code-execution": "code_execution",
    "excel-generation": "excel_generation",
    "data-cleaning": "data_cleaning",
}
_PROFILE_FALLBACK_KINDS = {
    "web-collector": "data_collection",
    "report-writer": "report_writing",
    "sandboxed-runner": "code_execution",
    "readonly-analyst": "audit",
}

# 节化职能闭集（agents-spec §2.3.1）。与 orchestrator.sectioning.SECTIONED_KINDS
# 必须同口径——运行期那份零改动，由 tests/test_m4a_report_contract.py 锁一致。
SECTIONED_CHAPTER_KINDS = frozenset(
    {"cross_validation", "summary", "report", "report_writing"}
)


def agent_kind_of(agent_id: str, profile: Any = None) -> str:
    """按 agent_id 前缀归职能；前缀不命中时退回 capability.profile。"""
    for prefix, kind in AGENT_KIND_PREFIXES.items():
        if agent_id == prefix or agent_id.startswith(f"{prefix}-"):
            return kind
    return _PROFILE_FALLBACK_KINDS.get(str(profile or ""), "audit")


def rated_collector_id(
    *,
    output: Mapping[str, Any],
    depends_on: Any,
    deliverable_path: str,
    collector_ids: Any,
) -> str:
    """§RATE-1 货 2：这一章是不是「只评一个采集章的评级章」，是则返回那章 agent_id。

    不另立字段、只按计划结构判：产物走逐条评级验证器、只依赖一个同 goal 的采集章、
    且不是本 goal 的交付物章。评级章的产物契约由系统定死，与 goal 验收文本无关，
    也不占模型的章数预算——lint 的 19/24/28 三条都要按这个口径把它摘出去。
    """
    if str(output.get("path", "")) == str(deliverable_path or ""):
        return ""
    validators = [str(item) for item in output.get("validators", [])]
    if "no_item_missing_rating" not in validators:
        return ""
    upstream = [str(item) for item in (depends_on or [])]
    if len(upstream) != 1:
        return ""
    return upstream[0] if upstream[0] in set(collector_ids or ()) else ""


def rating_rows_path(collector_output_path: str) -> str:
    """§RATE-2 货 1：评级章要读的「这一章采到的库行」物化文件路径。

    从采集章产物路径派生（`x.json` → `x.rows.json`）：与采集产物同目录、文件名可
    区分。它不是任何 agent 的声明产物，通用投影 `load_evidence_payloads` 按声明
    产物路径读文件，因此不会把物化行再当证据产物投影一遍。
    """
    raw = str(collector_output_path or "").strip()
    if not raw:
        return ""
    base = raw[: -len(".json")] if raw.endswith(".json") else raw
    return f"{base}.rows.json"


#: §RATE-3：评级章一章内分批的批大小上限（行/批）。提货单按 RATE-1 实测 4.4 s/条估的
#: 50 行 ≈ 3.7 min；RATE-3 第 1 轮重放实测 goal-1（web_search 行，正文长）50 行的片
#: 三章全在 305 s 被本机代理掐流（goal-2 小红书行 50 行只要 127 s）——按提货单坑 2
#: 「被掐就降到 30」落成默认值；`OWLI_RATING_BATCH_ROWS` 仍可按环境调。
#: §M6-e 货 1（防掐流，用户拍板）：关账整跑前再压 20 → 15——RATE-3 第 3 轮 20 行片
#: 仍有片贴着 300 s 适配器超时线跑（`claude.py` 300 s 不动、片钟 330 s 不加时间），
#: 只能继续往下切片；字节封顶 32 KB 与并发均不动。
RATING_BATCH_ROWS = 15

#: §RATE-3 第 2 轮实测：行的「重量」按源差 6 倍——web_search 行带 1200 字正文
#: ≈ 3.3 KB/行，小红书行 ≈ 0.57 KB/行；同样 30 行/片，前者 ≥300 s 被引擎超时掐掉、
#: 后者 107 s 跑完。所以片不能只按行数切，还要按序列化字节数封顶。
RATING_BATCH_BYTES = 32_000


def rating_batch_sizes(
    rows: list[Any], *, batch_rows: int = RATING_BATCH_ROWS,
    batch_bytes: int = RATING_BATCH_BYTES,
) -> list[int]:
    """§RATE-3 货 1：顺序切片——行数到 batch_rows 或字节数到 batch_bytes 就封一片。

    单行超过字节预算时自成一片（不丢行）。返回每片行数表，与 `rating_batches` 同形。
    """
    max_rows = max(1, int(batch_rows))
    max_bytes = max(1, int(batch_bytes))
    sizes: list[int] = []
    count = 0
    used = 0
    for row in rows:
        weight = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if count and (count >= max_rows or used + weight > max_bytes):
            sizes.append(count)
            count, used = 0, 0
        count += 1
        used += weight
    if count:
        sizes.append(count)
    return sizes


def rating_batch_path(rows_path: str, index: int) -> str:
    """§RATE-3 货 1：物化行文件的第 index 片（`x.rows.json` → `x.rows.<index>.json`）。"""
    raw = str(rows_path or "").strip()
    if not raw or index < 1:
        return ""
    base = raw[: -len(".json")] if raw.endswith(".json") else raw
    return f"{base}.{int(index)}.json"


def rating_batch_output_path(output_path: str, index: int) -> str:
    """§RATE-3 货 2：评级章第 index 片的产物（`y.json` → `y.part.<index>.json`）。

    片产物不是任何 agent 的声明产物；系统按片序合并成声明路径那一个数组文件，
    投影层只读声明路径，不会把片产物再投影一遍。
    """
    raw = str(output_path or "").strip()
    if not raw or index < 1:
        return ""
    base = raw[: -len(".json")] if raw.endswith(".json") else raw
    return f"{base}.part.{int(index)}.json"


def rating_task_text(rows_path: str) -> str:
    """§RATE-3 货 4：评级章的任务文案（章规格 task 与 agent.task 共用这一句）。

    RATE-2 踩过：章规格改了、任务文案没改，模型照文案走就还是去读那 10 条。
    所以文案只此一处；「按批」也在这句里说清——系统把物化文件按 ≤50 行切片，
    每次会话只喂一批，条数与**本批**一一对应。
    """
    return (
        f"逐条评级 {rows_path} 里的每一条证据：该文件是这一采集章真正入库的全部"
        f"证据行，系统按 ≤{RATING_BATCH_ROWS} 行切成 .rows.<n>.json 分批喂入、"
        "每次会话只评这一批；对每条给出五维评分与 rating_notes，并原样回带它的 "
        "permalink（条数与本批一一对应，不新增不丢条）。"
    )


def rating_batches(row_count: int, batch_rows: int = RATING_BATCH_ROWS) -> list[int]:
    """§RATE-3 货 1：按批大小把 row_count 行切成每片行数表（135 → [50, 50, 35]）。"""
    size = max(1, int(batch_rows))
    total = max(0, int(row_count))
    sizes = [size] * (total // size)
    if total % size:
        sizes.append(total % size)
    return sizes


def _strict_fields(data: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{location} 含字段表之外的字段：{sorted(unknown)}")


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


class _FrozenDict(dict):
    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("baseline 已冻结，不允许修改")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked

    def __deepcopy__(self, memo: dict[int, Any]) -> "_FrozenDict":
        del memo
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return copy.deepcopy(value)


@dataclass
class Entity:
    """§ENT-1 货 2：研究对象的「实体卡」——同一产品在中外市场的叫法与身份。

    `id` 是稳定标识，取骨架 `subjects` 里的原名：采集卡的 `entity`、分配表的
    `entity`、规则 25 的覆盖判定全按这个字符串对齐，所以它一旦定下就不再改；
    用户在计划编辑页改的是 `canonical`（展示名）与 `names`（检索用名）。

    `same_product` 说的是**本实体的中外名字是不是同一个产品**：豆包 / Doubao
    是（true）；抖音 / TikTok 不是（false，字节面向国内与海外的两个独立产品，
    内容生态不互通）。false 时 `note` 必须写清差异——报告交叉章据此只并列、
    不做跨市场交叉，查询词组装也不会把对方的名字当别名去搜。
    """

    id: str
    canonical: str
    names: dict[str, Any]
    official_handles: dict[str, str]
    same_product: bool
    note: str

    _FIELDS_ORDER: ClassVar[tuple[str, ...]] = (
        "id", "canonical", "names", "official_handles", "same_product", "note",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("entity.id 必须是非空字符串")
        if not isinstance(self.canonical, str) or not self.canonical.strip():
            raise ValueError(f"entity {self.id}.canonical 必须是非空字符串")
        if not isinstance(self.names, dict):
            raise ValueError(f"entity {self.id}.names 必须是 object")
        unknown = set(self.names) - {"zh", "en", "aliases"}
        if unknown:
            raise ValueError(f"entity {self.id}.names 含未知字段：{sorted(unknown)}")
        for key in ("zh", "en"):
            value = self.names.get(key)
            if value is not None and not (isinstance(value, str) and value.strip()):
                raise ValueError(f"entity {self.id}.names.{key} 必须是非空字符串或 null")
        aliases = self.names.setdefault("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(item, str) and item.strip() for item in aliases
        ):
            raise ValueError(f"entity {self.id}.names.aliases 必须是非空字符串数组")
        if len(set(aliases)) != len(aliases):
            raise ValueError(f"entity {self.id}.names.aliases 不得重复")
        if not isinstance(self.official_handles, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.official_handles.items()
        ):
            raise ValueError(f"entity {self.id}.official_handles 必须是 str→str 映射")
        if not isinstance(self.same_product, bool):
            raise ValueError(f"entity {self.id}.same_product 必须是布尔值")
        if not self.same_product and not str(self.note or "").strip():
            raise ValueError(
                f"entity {self.id}.same_product=false 时 note 必须写清中外名字的差异"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Entity":
        _strict_fields(data, set(cls._FIELDS_ORDER), "entity")
        values = _copy(dict(data))
        values.setdefault("canonical", values.get("id", ""))
        values.setdefault("names", {})
        values.setdefault("official_handles", {})
        values.setdefault("same_product", True)
        values.setdefault("note", "")
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {name: _copy(getattr(self, name)) for name in self._FIELDS_ORDER}

    def search_names(self, locale: str) -> list[str]:
        """§ENT-1 货 4：按语域取检索用名——中文源取中文名与中文别名，海外源取英文。

        顺序即优先级：先主名后别名，调用方按「每实体每源 ≤2 个查询词」截断。
        `same_product=false` 的实体不跨语域借名（抖音不会去搜 TikTok）；这条在
        取名阶段就成立，因为对方的名字压根不在本实体的 names 里。
        """
        if locale not in {"zh", "en"}:
            raise ValueError(f"locale 只能取 zh 或 en：{locale!r}")
        picked: list[str] = []
        primary = self.names.get(locale)
        if isinstance(primary, str) and primary.strip():
            picked.append(primary.strip())
        for alias in self.names.get("aliases", []):
            if _name_locale(alias) == locale:
                picked.append(alias.strip())
        if not picked:
            picked.append(self.canonical.strip())
        seen: set[str] = set()
        return [name for name in picked if not (name in seen or seen.add(name))]


def _name_locale(name: str) -> str:
    """名字按字符判语域：含 CJK 字符即中文名，否则按英文名。"""
    return "zh" if any("一" <= ch <= "鿿" for ch in str(name)) else "en"


@dataclass
class Agent:
    agent_id: str
    display_name: str
    entity: str | None
    task: str
    depends_on: list[str]
    inputs: list[dict[str, Any]]
    engine: str
    model: str | None
    capability: dict[str, Any]
    prompt: dict[str, Any]
    output: dict[str, Any]
    chapter: dict[str, Any] | None
    extra_quota_credits: float | int | None
    origin: dict[str, str]
    status: str

    _FIELDS: ClassVar[set[str]] = {
        "agent_id", "display_name", "entity", "task", "depends_on", "inputs", "engine",
        "model", "capability", "prompt", "output", "chapter", "extra_quota_credits",
        "origin", "status",
    }

    def __post_init__(self) -> None:
        if not _AGENT_ID_PATTERN.fullmatch(self.agent_id):
            raise ValueError(f"agent_id 必须是 kebab-case：{self.agent_id}")
        if self.entity is not None and (
            not isinstance(self.entity, str) or not self.entity.strip()
        ):
            raise ValueError(f"agent {self.agent_id}.entity 必须是非空字符串或 null")
        for field_name, value in self.origin.items():
            if field_name == "_node":
                if value not in {"generated", "user"}:
                    raise ValueError("origin._node 只能是 generated 或 user")
            elif value not in _ORIGIN_VALUES:
                raise ValueError(
                    f"agent {self.agent_id}.origin.{field_name} 来源非法：{value}"
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Agent":
        _strict_fields(data, cls._FIELDS, "agent")
        values = dict(data)
        values.setdefault("entity", None)
        values.setdefault("inputs", [])
        values.setdefault("model", None)
        values.setdefault("chapter", None)
        values.setdefault("extra_quota_credits", None)
        return cls(**_copy(values))

    def to_dict(self) -> dict[str, Any]:
        return {name: _copy(getattr(self, name)) for name in self._FIELDS_ORDER}

    _FIELDS_ORDER: ClassVar[tuple[str, ...]] = (
        "agent_id", "display_name", "entity", "task", "depends_on", "inputs", "engine",
        "model", "capability", "prompt", "output", "chapter", "extra_quota_credits",
        "origin", "status",
    )


@dataclass
class Goal:
    goal_id: str
    title: str
    objective: str
    depends_on: list[str]
    deliverable: dict[str, Any]
    acceptance: list[str]
    intervention: dict[str, Any]
    retry_policy: dict[str, Any]
    on_upstream_failure: str
    agents: list[Agent]
    status: str

    _FIELDS_ORDER: ClassVar[tuple[str, ...]] = (
        "goal_id", "title", "objective", "depends_on", "deliverable",
        "acceptance", "intervention", "retry_policy", "on_upstream_failure",
        "agents", "status",
    )

    def __post_init__(self) -> None:
        if not _GOAL_ID_PATTERN.fullmatch(self.goal_id):
            raise ValueError(f"goal_id 必须符合 goal-<n>：{self.goal_id}")
        unknown_policy = set(self.retry_policy) - (
            set(DEFAULT_RETRY_POLICY) | OPTIONAL_RETRY_POLICY_FIELDS
        )
        if unknown_policy:
            raise ValueError(f"{self.goal_id}.retry_policy 含未知字段：{unknown_policy}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Goal":
        _strict_fields(data, set(cls._FIELDS_ORDER), "goal")
        values = _copy(dict(data))
        policy = _copy(DEFAULT_RETRY_POLICY)
        policy.update(values.get("retry_policy") or {})
        values["retry_policy"] = policy
        values["agents"] = [Agent.from_dict(item) for item in values["agents"]]
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        result = {name: _copy(getattr(self, name)) for name in self._FIELDS_ORDER}
        result["agents"] = [agent.to_dict() for agent in self.agents]
        return result


@dataclass
class Plan:
    research_id: str
    plan_rev: int
    title: str
    research_question: str
    use_case: str
    market_profile: str
    market_profile_justification: str
    subjects: list[str]
    subjects_justification: str
    entities: list[Entity]
    scale: str
    status: str
    approved_at: str | None
    decision_balance: list[dict[str, Any]]
    expert_panel: dict[str, Any] | None
    goals: list[Goal]
    change_log: list[dict[str, Any]]
    baseline: dict[str, Any] | None
    baseline_source: str
    created_at: str
    updated_at: str

    _FIELDS_ORDER: ClassVar[tuple[str, ...]] = (
        "research_id", "plan_rev", "title", "research_question", "use_case",
        "market_profile", "market_profile_justification", "subjects",
        "subjects_justification", "entities", "scale", "status",
        "approved_at", "decision_balance", "expert_panel", "goals",
        "change_log", "baseline", "baseline_source", "created_at", "updated_at",
    )

    def __post_init__(self) -> None:
        if self.plan_rev < 1:
            raise ValueError("plan_rev 必须从 1 开始")
        seen_agent_ids: set[str] = set()
        duplicate_agent_ids: set[str] = set()
        for goal in self.goals:
            for agent in goal.agents:
                if agent.agent_id in seen_agent_ids:
                    duplicate_agent_ids.add(agent.agent_id)
                seen_agent_ids.add(agent.agent_id)
        if duplicate_agent_ids:
            raise ValueError(
                f"agent_id 跨 goal 重复：{sorted(duplicate_agent_ids)}"
            )
        entity_ids = [entity.id for entity in self.entities]
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError(f"entities[].id 重复：{sorted(entity_ids)}")
        if self.scale not in {"fast", "standard"}:
            raise ValueError(f"scale 只能取 fast 或 standard：{self.scale!r}")
        if self.market_profile not in {"cn_product", "global_product"}:
            raise ValueError(
                "market_profile 只能取 cn_product 或 global_product："
                f"{self.market_profile!r}"
            )
        if not self.market_profile_justification.strip():
            raise ValueError("market_profile_justification 不能为空")
        if not isinstance(self.subjects, list) or not all(
            isinstance(item, str) and item.strip() for item in self.subjects
        ):
            raise ValueError("subjects 必须是非空字符串数组或兼容旧计划的空数组")
        if len(set(self.subjects)) != len(self.subjects):
            raise ValueError("subjects 不得含重复实体")
        if self.subjects and not self.subjects_justification.strip():
            raise ValueError("subjects_justification 不能为空")
        if not (
            self.baseline_source in {"generated", "expert_panel"}
            or re.fullmatch(r"reused:r-[A-Za-z0-9-]+", self.baseline_source)
        ):
            raise ValueError(f"baseline_source 不在三态范围内：{self.baseline_source}")
        if self.baseline is None:
            baseline = {
                "title": self.title,
                "use_case": self.use_case,
                "goals": [goal.to_dict() for goal in self.goals],
            }
        else:
            baseline = _copy(self.baseline)
        self.baseline = _freeze(baseline)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        _strict_fields(data, set(cls._FIELDS_ORDER), "plan")
        values = _copy(dict(data))
        values.setdefault("approved_at", None)
        values.setdefault("expert_panel", None)
        values.setdefault("baseline", None)
        values.setdefault("scale", "standard")
        values.setdefault("subjects", [])
        values.setdefault("subjects_justification", "历史计划未记录研究实体。")
        values.setdefault("entities", [])
        values["entities"] = [Entity.from_dict(item) for item in values["entities"]]
        values.setdefault("market_profile", "global_product")
        values.setdefault(
            "market_profile_justification", "历史计划未记录市场属性，按全球产品兼容。"
        )
        values["goals"] = [Goal.from_dict(item) for item in values["goals"]]
        return cls(**values)

    @classmethod
    def from_json(cls, value: str) -> "Plan":
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError("计划书 JSON 顶层必须是 object")
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        result = {name: _copy(getattr(self, name)) for name in self._FIELDS_ORDER}
        result["goals"] = [goal.to_dict() for goal in self.goals]
        result["entities"] = [entity.to_dict() for entity in self.entities]
        result["baseline"] = _thaw(self.baseline)
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def entity_by_id(self, entity_id: str) -> Entity | None:
        """按 id 取实体卡；没有实体卡（历史计划 entities=[]）时恒为 None。"""
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        return None

    def next_goal_id(self) -> str:
        """新增编号只向后增长，删除造成的空洞永不复用。"""
        numbers = [int(goal.goal_id.removeprefix("goal-")) for goal in self.goals]
        return f"goal-{max(numbers, default=0) + 1}"
