from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.plan_factory import make_plan_dict


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


@pytest.fixture
def plan_store(tmp_path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database)
    source = make_plan_dict()
    store.create_report(
        id=source["research_id"], title=source["title"],
        research_question=source["research_question"],
        created_at=source["created_at"], use_case=source["use_case"],
    )
    return store, database


def _change(phase: str) -> dict:
    return {
        "change_id": "chg-1",
        "at": "2026-08-19T02:00:00Z",
        "phase": phase,
        "scope": "goal",
        "target_id": "goal-1",
        "field": "goals[0].agents[0].engine",
        "before": "claude",
        "after": "codex",
        "summary": "引擎：Claude → Codex",
        "reason": "运行时额度预警",
        "actor": "user",
        "artifact_discarded": None,
        "feedback_id": None,
    }


def test_save_load_整棵树无损且同步_decision_balance(plan_store) -> None:
    from app.plan.model import Plan
    from app.plan.store import load_plan, save_plan

    store, database = plan_store
    source = make_plan_dict()
    saved = save_plan(store, Plan.from_dict(source), expected_rev=0)
    loaded = load_plan(store, source["research_id"])

    assert saved.to_dict() == source
    assert loaded is not None and loaded.to_dict() == source
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT json_valid(plan_snapshot), decision_balance FROM reports WHERE id = ?",
            (source["research_id"],),
        ).fetchone()
    assert row == (1, json.dumps(source["decision_balance"], ensure_ascii=False, separators=(",", ":")))


def test_bump_rev_单调递增且旧_rev_触发乐观锁冲突(plan_store) -> None:
    from app.plan.model import Plan
    from app.plan.store import PlanRevisionConflict, bump_rev, save_plan

    store, _ = plan_store
    plan = save_plan(store, Plan.from_dict(make_plan_dict()), expected_rev=0)
    updated = bump_rev(store, plan, expected_rev=1)
    assert updated.plan_rev == 2

    with pytest.raises(PlanRevisionConflict, match="期望 rev=1"):
        bump_rev(store, plan, expected_rev=1)


def test_runtime_intervention_双写_feedback_且映射_json_valid(plan_store) -> None:
    from app.plan.model import Plan
    from app.plan.store import append_change_log, save_plan

    store, database = plan_store
    plan = save_plan(store, Plan.from_dict(make_plan_dict()), expected_rev=0)
    updated = append_change_log(store, plan, _change("runtime_intervention"), expected_rev=1)

    assert updated.plan_rev == 2
    assert updated.change_log[-1]["feedback_id"].startswith("fb-")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT kind, target, before_value, after_value, applied, extra "
            "FROM feedback WHERE report_id = ?",
            (plan.research_id,),
        ).fetchone()
        valid = connection.execute(
            "SELECT json_valid(before_value), json_valid(after_value), json_valid(extra) FROM feedback"
        ).fetchone()
    assert row[:2] == ("goal_change", "goal-1/goals[0].agents[0].engine")
    assert json.loads(row[2]) == {"value": "claude", "summary_before": "引擎：Claude"}
    assert json.loads(row[3]) == {"value": "codex", "summary_after": "Codex"}
    assert row[4] == 1 and json.loads(row[5])["plan_rev"] == 2
    assert valid == (1, 1, 1)


def test_plan_review_只写_change_log_不写_feedback(plan_store) -> None:
    from app.plan.model import Plan
    from app.plan.store import append_change_log, save_plan

    store, database = plan_store
    plan = save_plan(store, Plan.from_dict(make_plan_dict()), expected_rev=0)
    updated = append_change_log(store, plan, _change("plan_review"), expected_rev=1)

    assert updated.change_log[-1]["feedback_id"] is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0


def test_feedback_写失败不阻塞计划变更并保留_null_供重试(plan_store, monkeypatch) -> None:
    from app.plan.model import Plan
    from app.plan.store import append_change_log, load_plan, save_plan

    store, _ = plan_store
    plan = save_plan(store, Plan.from_dict(make_plan_dict()), expected_rev=0)

    def fail_feedback(*args, **kwargs):
        raise sqlite3.OperationalError("故障注入")

    monkeypatch.setattr(store, "_insert_feedback", fail_feedback)
    updated = append_change_log(store, plan, _change("runtime_intervention"), expected_rev=1)
    loaded = load_plan(store, plan.research_id)

    assert updated.plan_rev == 2
    assert updated.change_log[-1]["feedback_id"] is None
    assert loaded is not None and loaded.change_log[-1]["feedback_id"] is None
