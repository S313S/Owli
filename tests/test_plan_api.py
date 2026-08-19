from __future__ import annotations

import asyncio
import copy
import sqlite3
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


@asynccontextmanager
async def api_client(tmp_path: Path, *, unanswered: bool = False) -> AsyncIterator[tuple[Any, httpx.AsyncClient, str, Path]]:
    from app.api.main import create_app

    database = tmp_path / "owli.db"
    application = create_app(
        database,
        SCHEMA_PATH,
        enable_test_routes=True,
        engine_probe=lambda: {},
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            loaded = await client.post(
                "/api/test/fixtures/m2-d",
                json={"unanswered": unanswered},
                headers={"X-Request-ID": f"fixture-{int(unanswered)}"},
            )
            assert loaded.status_code == 200, loaded.text
            research_id = loaded.json()["data"]["research_id"]
            yield application, client, research_id, database


async def get_plan(client: httpx.AsyncClient, research_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/researches/{research_id}/plan")
    assert response.status_code == 200, response.text
    return response.json()["data"]


@async_test
async def test_PUT_旧_plan_rev_返回409且给出重新加载提示(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (_, client, research_id, _):
        stale = await get_plan(client, research_id)
        first = copy.deepcopy(stale)
        first["goals"][0]["agents"][0]["task"] = "先保存的一次修改。"
        saved = await client.put(f"/api/researches/{research_id}/plan", json=first)
        assert saved.status_code == 200, saved.text

        stale["goals"][0]["agents"][0]["task"] = "来自旧版本的覆盖。"
        conflict = await client.put(f"/api/researches/{research_id}/plan", json=stale)

    assert conflict.status_code == 409
    body = conflict.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "plan_revision_conflict"
    assert "重新加载" in body["error"]["message"]


@async_test
async def test_追问未答完拒绝批准_答完后冻结并返回时间与新版本(tmp_path: Path) -> None:
    async with api_client(tmp_path, unanswered=True) as (_, client, research_id, _):
        rejected = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "approve-before-answer"},
        )
        assert rejected.status_code == 422
        assert "还有 1 个追问未回答" in rejected.json()["error"]["message"]

        plan = await get_plan(client, research_id)
        plan["decision_balance"][0]["answer"] = "产品路线"
        plan["decision_balance"][0]["answered_at"] = "2026-08-19T04:00:00Z"
        answered = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert answered.status_code == 200, answered.text
        answer_rev = answered.json()["data"]["plan_rev"]

        approved = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "approve-after-answer"},
        )

    assert approved.status_code == 200, approved.text
    data = approved.json()["data"]
    assert data["status"] == "approved"
    assert data["approved_at"]
    assert data["plan_rev"] == answer_rev + 1


@async_test
async def test_不可编辑字段_retry_policy_与_preamble_ref_均拒改并定位(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (_, client, research_id, _):
        plan = await get_plan(client, research_id)
        plan["goals"][0]["retry_policy"]["max_rounds"] = 9
        rejected_policy = await client.put(f"/api/researches/{research_id}/plan", json=plan)

        plan = await get_plan(client, research_id)
        plan["goals"][0]["agents"][0]["prompt"]["preamble_ref"] = "common/v2"
        rejected_prompt = await client.put(f"/api/researches/{research_id}/plan", json=plan)

    assert rejected_policy.status_code == 422
    assert "retry_policy" in rejected_policy.json()["error"]["details"][0]["field"]
    assert rejected_prompt.status_code == 422
    assert "prompt.preamble_ref" in rejected_prompt.json()["error"]["details"][0]["field"]


@async_test
async def test_lint_error_拒绝保存并回传字段定位_warning_则放行(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (_, client, research_id, _):
        plan = await get_plan(client, research_id)
        plan["goals"][0]["acceptance"] = ["结果质量良好"]
        rejected = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert rejected.status_code == 422
        assert "goal-1.acceptance" in "；".join(rejected.json()["error"]["details"])

        plan = await get_plan(client, research_id)
        plan["goals"][0]["agents"][0]["output"]["validators"] = ["section_exists:结论"]
        allowed = await client.put(f"/api/researches/{research_id}/plan", json=plan)

    assert allowed.status_code == 200, allowed.text
    assert any("section_exists" in item for item in allowed.json()["data"]["lint"]["warnings"])


@async_test
async def test_reset_agent_与_plan_均回_baseline_标记_reset_且保留日志(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (_, client, research_id, _):
        plan = await get_plan(client, research_id)
        baseline_task = plan["baseline"]["goals"][0]["agents"][0]["task"]
        plan["goals"][0]["agents"][0]["task"] = "用户改过的任务。"
        changed = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert changed.status_code == 200, changed.text
        assert changed.json()["data"]["goals"][0]["agents"][0]["origin"]["task"] == "user"

        reset_agent = await client.post(
            f"/api/researches/{research_id}/plan/reset-agent",
            json={"agent_id": "agent-1"},
            headers={"X-Request-ID": "reset-agent-1"},
        )
        assert reset_agent.status_code == 200, reset_agent.text
        agent = reset_agent.json()["data"]["goals"][0]["agents"][0]
        assert agent["task"] == baseline_task
        assert agent["origin"]["task"] == "reset"
        assert reset_agent.json()["data"]["change_log"][-1]["after"] == "baseline"

        current = await get_plan(client, research_id)
        current["title"] = "用户改过的标题"
        current["goals"].pop()
        changed_tree = await client.put(f"/api/researches/{research_id}/plan", json=current)
        assert changed_tree.status_code == 200, changed_tree.text
        before_reset_count = len(changed_tree.json()["data"]["change_log"])

        reset_plan = await client.post(
            f"/api/researches/{research_id}/plan/reset",
            json={"scope": "plan"},
            headers={"X-Request-ID": "reset-whole-plan"},
        )

    assert reset_plan.status_code == 200, reset_plan.text
    restored = reset_plan.json()["data"]
    assert restored["title"] == restored["baseline"]["title"]
    assert len(restored["goals"]) == len(restored["baseline"]["goals"])
    assert len(restored["change_log"]) == before_reset_count + 1
    assert restored["goals"][0]["agents"][0]["origin"]["task"] == "reset"


@async_test
async def test_respond_同一客户端请求ID_重发不重复生效且只发布一次已答事件(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (application, client, research_id, _):
        injected = await client.post(
            f"/api/test/researches/{research_id}/cards",
            json={
                "card_id": "card-intervene-1",
                "card_type": "INTERVENE",
                "research_id": research_id,
                "goal_id": "goal-1",
                "agent_id": None,
                "title": "请确认是否继续下一阶段",
                "body": "阶段产物已通过校验。",
                "target": {},
                "actions": [{"type": "CHOICE_2", "id": "continue", "label": "继续"}],
                "blocking": "goal",
                "deadline": None,
                "status": "pending",
                "result": None,
                "created_at": "2026-08-19T04:00:00Z",
                "resolved_at": None,
            },
            headers={"X-Request-ID": "inject-card-1"},
        )
        assert injected.status_code == 200, injected.text

        request = {"action": "continue", "payload": {"choice": "continue"}}
        first = await client.post(
            "/api/cards/card-intervene-1/respond",
            json=request,
            headers={"X-Request-ID": "respond-card-1"},
        )
        second = await client.post(
            "/api/cards/card-intervene-1/respond",
            json=request,
            headers={"X-Request-ID": "respond-card-1"},
        )
        replay = await application.state.event_buffer.replay_after(research_id, None)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    answered = [
        event for event in replay.events
        if event.payload.get("type") == "card_update"
        and event.payload["data"]["card"]["card_id"] == "card-intervene-1"
        and event.payload["data"]["card"]["status"] == "answered"
    ]
    assert len(answered) == 1


@async_test
async def test_批准前只记_change_log_批准后编辑双写_feedback(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (_, client, research_id, database):
        plan = await get_plan(client, research_id)
        plan["goals"][0]["agents"][0]["task"] = "批准前的用户任务。"
        before_approve = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert before_approve.status_code == 200, before_approve.text
        assert before_approve.json()["data"]["change_log"][-1]["phase"] == "plan_review"
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0

        approved = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "approve-for-runtime-edit"},
        )
        assert approved.status_code == 200, approved.text

        current = await get_plan(client, research_id)
        current["goals"][0]["agents"][0]["task"] = "批准后的干预任务。"
        after_approve = await client.put(f"/api/researches/{research_id}/plan", json=current)
        assert after_approve.status_code == 200, after_approve.text

        runtime_change = after_approve.json()["data"]["change_log"][-1]
        with sqlite3.connect(database) as connection:
            feedback = connection.execute(
                "SELECT kind, target, applied FROM feedback WHERE report_id = ?",
                (research_id,),
            ).fetchall()

    assert runtime_change["phase"] == "runtime_intervention"
    assert runtime_change["feedback_id"].startswith("fb-")
    assert feedback == [("goal_change", "agent-1/goals[0].agents[0].task", 1)]
