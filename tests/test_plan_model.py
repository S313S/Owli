from __future__ import annotations

from dataclasses import fields

import pytest

from tests.plan_factory import make_plan_dict


def test_三层字段名与字段表逐字一致() -> None:
    from app.plan.model import Agent, Goal, Plan

    assert [field.name for field in fields(Plan)] == [
        "research_id", "plan_rev", "title", "research_question", "use_case",
        "market_profile", "market_profile_justification", "subjects",
        "subjects_justification", "entities", "scale", "status",
        "approved_at", "decision_balance", "expert_panel", "goals",
        "change_log", "baseline", "baseline_source", "created_at", "updated_at",
    ]
    assert [field.name for field in fields(Goal)] == [
        "goal_id", "title", "objective", "depends_on", "deliverable",
        "acceptance", "intervention", "retry_policy", "on_upstream_failure",
        "agents", "status",
    ]
    assert [field.name for field in fields(Agent)] == [
        "agent_id", "display_name", "entity", "task", "depends_on", "inputs", "engine",
        "model", "capability", "prompt", "output", "chapter", "extra_quota_credits",
        "origin", "status",
    ]


def test_JSON_往返无损且_baseline_不随当前树修改() -> None:
    from app.plan.model import Plan

    source = make_plan_dict()
    plan = Plan.from_dict(source)
    assert Plan.from_json(plan.to_json()).to_dict() == source

    plan.goals[0].title = "用户修改后的标题"
    assert plan.baseline["goals"][0]["title"] == "阶段 1 证据产物"
    with pytest.raises(TypeError, match="baseline 已冻结"):
        plan.baseline["title"] = "不可写"


def test_retry_policy_缺省时注入系统默认值() -> None:
    from app.plan.model import Plan

    source = make_plan_dict()
    del source["goals"][0]["retry_policy"]
    policy = Plan.from_dict(source).goals[0].retry_policy

    assert policy == {
        "max_attempts_per_round": 10,
        "ask_engine_switch_at": 5,
        "max_rounds": 2,
        "goal_deadline_hours": 12,
        "on_exhausted": "fail_goal",
    }


def test_goal_id_新增只取历史最大号之后且保留空洞() -> None:
    from app.plan.model import Plan

    source = make_plan_dict()
    source["goals"].pop(1)
    assert Plan.from_dict(source).next_goal_id() == "goal-4"


def test_新增_agent_可用_node_user_标记且非法_agent_id_拒绝() -> None:
    from app.plan.model import Agent

    data = make_plan_dict()["goals"][0]["agents"][0]
    data["origin"] = {"_node": "user", "engine": "user"}
    assert Agent.from_dict(data).origin["_node"] == "user"

    data["agent_id"] = "Invalid Agent"
    with pytest.raises(ValueError, match="kebab-case"):
        Agent.from_dict(data)


def test_baseline_source_仅接受三态() -> None:
    from app.plan.model import Plan

    source = make_plan_dict()
    source["baseline_source"] = "unknown"
    with pytest.raises(ValueError, match="baseline_source"):
        Plan.from_dict(source)


def test_scale_缺省兼容旧快照且只接受产品档位() -> None:
    from app.plan.model import Plan

    source = make_plan_dict()
    del source["scale"]
    assert Plan.from_dict(source).scale == "standard"

    source = make_plan_dict()
    source["scale"] = "tiny"
    with pytest.raises(ValueError, match="scale"):
        Plan.from_dict(source)


def test_card_封闭枚举和_card_update_事件形状() -> None:
    from app.plan.cards import Card, CardActionType, CardType

    card = Card(
        card_id="card-1", card_type=CardType.QUESTION,
        research_id="r-1", goal_id="goal-1", agent_id=None,
        title="请选择本次对比重点", body="答案会影响 goal-1。",
        target={"display_name": "决策天平", "type_icon": "question"},
        actions=[CardActionType.CHOICE_2], blocking="research", deadline=None,
        status="pending", result=None, created_at="2026-08-19T00:00:00Z",
        resolved_at=None,
    )
    event = card.to_event()
    assert event == {"type": "card_update", "data": {"card": card.to_dict()}}

    with pytest.raises(ValueError):
        Card(**{**card.to_dict(), "card_type": "UNKNOWN"})
    with pytest.raises(ValueError):
        Card(**{**card.to_dict(), "actions": ["CONFIRM"]})


def test_实体卡往返无损且按语域取检索名() -> None:
    """§ENT-1 货 2：实体卡进 plan JSON 往返无损；names 按语域分流。"""
    from app.plan.model import Plan

    source = make_plan_dict()
    source["entities"] = [{
        "id": "豆包",
        "canonical": "豆包",
        "names": {"zh": "豆包", "en": "Doubao", "aliases": ["豆包大模型", "Doubao AI"]},
        "official_handles": {"xhs": "doubao_official"},
        "same_product": True,
        "note": "字节跳动的对话式 AI 助手，国内叫豆包、海外叫 Doubao。",
    }]
    plan = Plan.from_dict(source)
    assert Plan.from_json(plan.to_json()).to_dict() == source
    entity = plan.entity_by_id("豆包")
    assert entity is not None
    assert entity.search_names("zh") == ["豆包", "豆包大模型"]
    assert entity.search_names("en") == ["Doubao", "Doubao AI"]
    assert plan.entity_by_id("不存在") is None


def test_同名不同产品必须写清差异且不跨语域借名() -> None:
    """§ENT-1 货 2：same_product=false 时 note 是硬要求；抖音不会去搜 TikTok。"""
    from app.plan.model import Entity

    douyin = Entity.from_dict({
        "id": "抖音",
        "canonical": "抖音",
        "names": {"zh": "抖音", "en": "Douyin", "aliases": []},
        "official_handles": {},
        "same_product": False,
        "note": "抖音与 TikTok 是字节面向国内与海外的两个独立产品，内容生态不互通。",
    })
    assert douyin.search_names("en") == ["Douyin"]
    with pytest.raises(ValueError, match="note 必须写清"):
        Entity.from_dict({
            "id": "抖音", "canonical": "抖音", "names": {},
            "official_handles": {}, "same_product": False, "note": "  ",
        })


def test_实体卡字段表与_id_唯一性都锁死() -> None:
    """§ENT-1 货 2：字段表外的键、重复 id 一律拒绝。"""
    from app.plan.model import Entity, Plan

    with pytest.raises(ValueError, match="含字段表之外的字段"):
        Entity.from_dict({"id": "豆包", "market": "cn"})
    source = make_plan_dict()
    source["entities"] = [
        {"id": "豆包", "canonical": "豆包"},
        {"id": "豆包", "canonical": "Doubao"},
    ]
    with pytest.raises(ValueError, match=r"entities\[\]\.id 重复"):
        Plan.from_dict(source)
