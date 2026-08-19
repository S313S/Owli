"""计划树的固定存储接口；所有 SQL 留在 app.store 层。"""

from __future__ import annotations

import secrets
import time
from typing import Any, Mapping

from app.plan.model import Plan
from app.store.dao import PlanSnapshotConflict, Store


class PlanRevisionConflict(RuntimeError):
    """语义对应 API 层 HTTP 409。"""


_CHANGE_FIELDS = {
    "change_id", "at", "phase", "scope", "target_id", "field", "before",
    "after", "summary", "reason", "actor", "artifact_discarded", "feedback_id",
}
_CHANGE_REQUIRED = {
    "change_id", "at", "phase", "scope", "target_id", "field", "before",
    "after", "summary", "actor",
}
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    encoded = []
    for _ in range(26):
        encoded.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(encoded))


def _raise_conflict(error: PlanSnapshotConflict) -> None:
    raise PlanRevisionConflict(str(error)) from error


def save_plan(
    store: Store, plan: Plan, *, expected_rev: int | None = None
) -> Plan:
    if expected_rev is None:
        expected_rev = plan.plan_rev - 1
    if plan.plan_rev != expected_rev + 1:
        raise ValueError(
            f"待保存 plan_rev 必须等于 expected_rev + 1："
            f"实际 {plan.plan_rev}，期望 {expected_rev + 1}"
        )
    try:
        store.save_plan_snapshot(
            plan.research_id, snapshot=plan.to_dict(), expected_rev=expected_rev
        )
    except PlanSnapshotConflict as error:
        _raise_conflict(error)
    return plan


def load_plan(store: Store, research_id: str) -> Plan | None:
    report = store.get_report(research_id)
    if report is None:
        raise KeyError(f"报告不存在：{research_id}")
    snapshot = report["plan_snapshot"]
    return None if snapshot is None else Plan.from_dict(snapshot)


def bump_rev(
    store: Store, plan: Plan, *, expected_rev: int | None = None
) -> Plan:
    if expected_rev is None:
        expected_rev = plan.plan_rev
    if plan.plan_rev != expected_rev:
        raise PlanRevisionConflict(
            f"计划版本冲突：{plan.research_id} 期望 rev={expected_rev}，"
            f"调用方计划为 rev={plan.plan_rev}"
        )
    updated = Plan.from_dict(plan.to_dict())
    updated.plan_rev = expected_rev + 1
    return save_plan(store, updated, expected_rev=expected_rev)


def _normalize_change(change: Mapping[str, Any], next_number: int) -> dict[str, Any]:
    unknown = set(change) - _CHANGE_FIELDS
    missing = _CHANGE_REQUIRED - set(change)
    if unknown:
        raise ValueError(f"change_log 含字段表之外的字段：{sorted(unknown)}")
    if missing:
        raise ValueError(f"change_log 缺少必填字段：{sorted(missing)}")
    result = dict(change)
    result.setdefault("reason", None)
    result.setdefault("artifact_discarded", None)
    result["feedback_id"] = None
    if result["change_id"] != f"chg-{next_number}":
        raise ValueError(f"change_id 必须单调递增为 chg-{next_number}")
    if result["phase"] not in {"plan_review", "runtime_intervention"}:
        raise ValueError("change_log.phase 只能是 plan_review 或 runtime_intervention")
    if result["scope"] not in {"plan", "goal", "agent"}:
        raise ValueError("change_log.scope 只能是 plan、goal 或 agent")
    return result


def _summary_sides(summary: str) -> tuple[str, str]:
    before, separator, after = summary.partition("→")
    if not separator or not before.strip() or not after.strip():
        raise ValueError("change_log.summary 必须是一行“原方式 → 调整后方式”")
    return before.strip(), after.strip()


def _feedback(plan: Plan, change: dict[str, Any]) -> dict[str, Any]:
    summary_before, summary_after = _summary_sides(str(change["summary"]))
    return {
        "id": change["feedback_id"],
        "report_id": plan.research_id,
        "evidence_id": None,
        "kind": "goal_change",
        "target": f"{change['target_id']}/{change['field']}",
        "before_value": {
            "value": change["before"],
            "summary_before": summary_before,
        },
        "after_value": {
            "value": change["after"],
            "summary_after": summary_after,
        },
        "reason": change["reason"],
        "actor": change["actor"],
        "created_at": change["at"],
        "applied": 1,
        "extra": {
            "change_id": change["change_id"],
            "phase": "runtime_intervention",
            "scope": change["scope"],
            "summary": change["summary"],
            "artifact_discarded": change["artifact_discarded"],
            "plan_rev": plan.plan_rev,
        },
    }


def append_change_log(
    store: Store,
    plan: Plan,
    change: Mapping[str, Any],
    *,
    expected_rev: int | None = None,
) -> Plan:
    """追加变更并升 rev；仅运行期双写 feedback，失败留 null。"""
    if expected_rev is None:
        expected_rev = plan.plan_rev
    if plan.plan_rev != expected_rev:
        raise PlanRevisionConflict(
            f"计划版本冲突：{plan.research_id} 期望 rev={expected_rev}，"
            f"调用方计划为 rev={plan.plan_rev}"
        )
    normalized = _normalize_change(change, len(plan.change_log) + 1)
    updated = Plan.from_dict(plan.to_dict())
    updated.plan_rev = expected_rev + 1
    updated.updated_at = str(normalized["at"])
    feedback = None
    if normalized["phase"] == "runtime_intervention":
        normalized["feedback_id"] = f"fb-{_ulid()}"
    updated.change_log.append(normalized)
    if normalized["phase"] == "runtime_intervention":
        feedback = _feedback(updated, normalized)
    try:
        feedback_id = store.save_plan_change(
            updated.research_id,
            snapshot=updated.to_dict(),
            expected_rev=expected_rev,
            feedback=feedback,
        )
    except PlanSnapshotConflict as error:
        _raise_conflict(error)
    if feedback is not None and feedback_id is None:
        updated.change_log[-1]["feedback_id"] = None
    return updated


def commit_changes(
    store: Store,
    plan: Plan,
    changes: list[Mapping[str, Any]],
    *,
    expected_rev: int | None = None,
) -> Plan:
    """一个 PUT 追加多条日志但只升一次 plan_rev。"""
    if expected_rev is None:
        expected_rev = plan.plan_rev
    if plan.plan_rev != expected_rev:
        raise PlanRevisionConflict(
            f"计划版本冲突：{plan.research_id} 期望 rev={expected_rev}，"
            f"调用方计划为 rev={plan.plan_rev}"
        )
    if not changes:
        return plan

    updated = Plan.from_dict(plan.to_dict())
    updated.plan_rev = expected_rev + 1
    feedbacks: list[dict[str, Any]] = []
    for offset, change in enumerate(changes, start=1):
        normalized = _normalize_change(change, len(plan.change_log) + offset)
        if normalized["phase"] == "runtime_intervention":
            normalized["feedback_id"] = f"fb-{_ulid()}"
        updated.change_log.append(normalized)
        if normalized["phase"] == "runtime_intervention":
            feedbacks.append(_feedback(updated, normalized))
    updated.updated_at = str(updated.change_log[-1]["at"])

    try:
        saved_ids = store.save_plan_changes(
            updated.research_id,
            snapshot=updated.to_dict(),
            expected_rev=expected_rev,
            feedbacks=feedbacks,
        )
    except PlanSnapshotConflict as error:
        _raise_conflict(error)
    saved = set(saved_ids)
    for change in updated.change_log:
        feedback_id = change.get("feedback_id")
        if feedback_id is not None and feedback_id not in saved:
            change["feedback_id"] = None
    return updated
