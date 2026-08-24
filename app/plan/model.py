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
        "subjects_justification", "scale", "status",
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
        result["baseline"] = _thaw(self.baseline)
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def next_goal_id(self) -> str:
        """新增编号只向后增长，删除造成的空洞永不复用。"""
        numbers = [int(goal.goal_id.removeprefix("goal-")) for goal in self.goals]
        return f"goal-{max(numbers, default=0) + 1}"
