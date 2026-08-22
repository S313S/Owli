from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


NOW = "2026-08-19T03:00:00+00:00"
RESEARCH_ID = "r-01JXPLAN0000000000000000"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def _agent(name: str, task: str, **extra) -> dict:
    shape = "array" if any(
        token in name for token in ("数据抓取", "MediaCrawler", "浏览器自动化", "审计")
    ) else "object"
    return {"name": name, "task": task, "output": {"shape": shape}, **extra}


def _goal(number: int, agents: list[dict], *, acceptance=None) -> dict:
    return {
        "title": f"阶段{number}",
        "objective": f"形成阶段{number}可独立验收的证据产物。",
        "depends_on": [] if number == 1 else [f"goal-{number - 1}"],
        "deliverable": {
            "format": "json" if number < 3 else "markdown",
            "shape": "array" if number < 3 else "object",
            "path": f"stage-{number}.json" if number < 3 else "report.md",
            "description": "可供下游复核的结构化产物。",
        },
        "acceptance": acceptance or ["文件存在且至少包含 1 条带链接记录"],
        "agents": agents,
    }


def _valid_skeleton() -> dict:
    return {
        "market_profile": "global_product",
        "market_profile_justification": "产品面向全球市场。",
        "subjects": ["飞书"],
        "subjects_justification": "研究主体为飞书。",
        "goals": [
            _goal(1, [_agent("HN 数据抓取·飞书", "通过 API 抓取 Hacker News 证据")]),
            _goal(2, [_agent("可靠度审计", "审核证据可靠度并做交叉验证")]),
            _goal(3, [_agent("报告撰写", "撰写带角标的 Markdown 报告")]),
        ]
    }


def _add_coverage_collector(skeleton: dict) -> None:
    """为只测非采集职能的整计划用例保留 subjects 的结构化采集覆盖。"""

    skeleton["goals"][1]["agents"].insert(
        0,
        _agent("HN 数据抓取·飞书", "采集研究主体证据"),
    )


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.runs_root = root / "runs"
        self.events = []
        self.saved = []

    def get_drafting_report(self, query: str):
        return {
            "id": RESEARCH_ID,
            "research_question": query,
            "created_at": NOW,
            "extra": {"plan_generated_at": NOW},
        }

    def save_plan_snapshot(self, report_id, *, snapshot, expected_rev):
        self.saved.append((report_id, deepcopy(snapshot), expected_rev))

    async def on_plan_event(self, event) -> None:
        self.events.append(event)


class FakeEngine:
    def __init__(self, skeletons: list[dict]) -> None:
        self.skeletons = [deepcopy(item) for item in skeletons]
        self.tasks = []
        self.chapter_tasks = []
        self._current = self.skeletons[0]
        self._goal_calls: dict[int, int] = {}
        self._chapter_outputs: list[str] = []

    @staticmethod
    def _prompt_json(body: str, label: str):
        tail = body.partition(label)[2]
        return json.JSONDecoder().raw_decode(tail)[0]

    async def run(self, task, ctx, on_event=None):
        del ctx, on_event
        if "-ch-" in task.output_path.stem:
            self.chapter_tasks.append(task)
        else:
            self.tasks.append(task)
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        if task.output_path.name == "skeleton.json":
            payload = {
                "market_profile": self._current["market_profile"],
                "market_profile_justification": self._current[
                    "market_profile_justification"
                ],
                "subjects": self._current["subjects"],
                "subjects_justification": self._current["subjects_justification"],
                "goals": [
                    {
                        "title": goal["title"],
                        "objective": goal["objective"],
                        "depends_on": goal["depends_on"],
                    }
                    for goal in self._current["goals"]
                ]
            }
        elif "-ch-" not in task.output_path.stem:
            number = int(task.output_path.stem.removeprefix("goal-"))
            call = self._goal_calls.get(number, 0)
            self._goal_calls[number] = call + 1
            source = self.skeletons[min(call, len(self.skeletons) - 1)]
            goal = source["goals"][number - 1]
            payload = {
                "deliverable": goal["deliverable"],
                "acceptance": goal["acceptance"],
                "agents": goal["agents"],
            }
        else:
            goal_text, chapter_text = task.output_path.stem.split("-ch-", 1)
            goal_number = int(goal_text.removeprefix("goal-"))
            chapter_number = int(chapter_text)
            raw_agent = self._current["goals"][goal_number - 1]["agents"][chapter_number - 1]
            name = str(raw_agent["name"]).casefold()
            if any(token in name for token in ("数据抓取", "mediacrawler", "浏览器自动化")):
                chapter_type = "collection"
            elif "代码执行" in name:
                chapter_type = "code_execution"
            elif "excel" in name:
                chapter_type = "excel_generation"
            elif "数据清洗" in name:
                chapter_type = "data_cleaning"
            elif "交叉验证" in name:
                chapter_type = "cross_validation"
            elif "报告" in name:
                chapter_type = "report"
            elif "摘要" in name:
                chapter_type = "summary"
            elif "标签" in name:
                chapter_type = "tagging"
            else:
                chapter_type = "audit"
            output_path = self._prompt_json(task.body, "系统声明 output.path=")
            declared_inputs = self._prompt_json(task.body, "系统声明 inputs=")
            inputs = list(declared_inputs)
            if chapter_type in {"comparison", "cross_validation"}:
                inputs = list(dict.fromkeys([*inputs, *self._chapter_outputs]))
            payload = {
                "chapter_type": chapter_type,
                "opening": {
                    "inputs": [{"path": path} for path in inputs],
                    "task": str(raw_agent["task"]),
                    "acceptance": ["产物按声明路径落盘"],
                },
                "closing": {
                    "output": {"path": output_path},
                    "entities": ["飞书"] if chapter_type == "collection" else [],
                    "expected_count": 1,
                    "notes": {},
                },
            }
            if chapter_type == "collection":
                self._chapter_outputs.append(output_path)
        task.output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(succeeded=True)


class ForbiddenEngine:
    async def run(self, task, ctx, on_event=None):
        del task, ctx, on_event
        raise AssertionError("规划任务不得路由到 Codex")


def _generate(tmp_path: Path, skeletons: list[dict]):
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    engine = FakeEngine(skeletons)
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )
    plan = asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))
    return plan, store, engine


def test_acceptance_写成分号长串时确定性归一为数组(tmp_path) -> None:
    # r-825ec6b5228a 实锤：生成器把整组验收写成「；」分隔长串，直接被
    # 「至少需要 1 条」打回三次导致规划不可用。归一后 lint 照常逐条把关。
    skeleton = _valid_skeleton()
    skeleton["goals"][0]["acceptance"] = (
        "文件为有效 JSON；每条命中含 objectID 且 permalink 非空；命中为 0 时保留空集说明"
    )

    plan, _, _ = _generate(tmp_path, [skeleton])
    goal = plan.to_dict()["goals"][0]
    assert goal["acceptance"] == [
        "文件为有效 JSON",
        "每条命中含 objectID 且 permalink 非空",
        "命中为 0 时保留空集说明",
    ]


def test_骨架由规划路由生成且系统补齐固定字段(tmp_path) -> None:
    from app.plan.lint import lint
    from app.plan.model import DEFAULT_RETRY_POLICY

    skeleton = _valid_skeleton()
    skeleton["estimated_cost"] = 12
    skeleton["goals"][0]["estimated_minutes"] = 20
    skeleton["goals"][0]["agents"][0].update(
        engine="claude", planned_steps=9, capability={"profile": "custom"}
    )

    plan, store, engine = _generate(tmp_path, [skeleton])

    assert len(engine.tasks) == 4
    assert engine.tasks[0].agent_kind == "planning"
    assert plan.status == "awaiting_review"
    assert plan.baseline_source == "generated"
    assert plan.expert_panel is None and plan.change_log == []
    assert lint(plan)["errors"] == []
    assert any(
        error.startswith("[规则12]")
        for error in lint(plan, for_approval=True)["errors"]
    )
    assert store.saved[0][0::2] == (RESEARCH_ID, 0)
    segment_root = store.runs_root / RESEARCH_ID / "plan-segments"
    assert sorted(path.name for path in segment_root.iterdir()) == [
        "assembled.json",
        "goal-1-ch-1.json",
        "goal-1.json",
        "goal-2-ch-1.json",
        "goal-2.json",
        "goal-3-ch-1.json",
        "goal-3.json",
        "skeleton.json",
    ]
    assert not list(segment_root.glob("*.partial"))
    raw = plan.to_dict()
    assert "estimated_cost" not in raw
    assert "estimated_minutes" not in raw["goals"][0]
    assert "planned_steps" not in raw["goals"][0]["agents"][0]
    assert [goal["goal_id"] for goal in raw["goals"]] == [
        "goal-1", "goal-2", "goal-3"
    ]
    for goal in raw["goals"]:
        assert goal["retry_policy"] == DEFAULT_RETRY_POLICY
        assert goal["status"] == "pending"
        assert goal["intervention"]["on_complete"] is True
        for agent in goal["agents"]:
            assert agent["prompt"]["preamble_ref"] == "common/v1"
            assert agent["prompt"]["assumptions_policy"] == "assume_and_declare"
            assert set(agent["origin"].values()) == {"generated"}
    report_writer = raw["goals"][-1]["agents"][-1]
    assert {
        "citation_marks_resolvable",
        "no_orphan_citation",
    } <= set(report_writer["output"]["validators"])


@pytest.mark.parametrize(
    ("name", "task", "engine", "profile"),
    [
        ("规划", "拆分 goal", "claude", "readonly-analyst"),
        ("计划仲裁", "仲裁计划", "claude", "readonly-analyst"),
        ("可靠度审计", "审核证据可靠度", "claude", "readonly-analyst"),
        ("交叉验证", "交叉验证断言", "claude", "readonly-analyst"),
        ("一致性检查", "检查证据一致性", "claude", "readonly-analyst"),
        ("报告撰写", "撰写报告", "claude", "report-writer"),
        ("摘要", "生成摘要", "claude", "report-writer"),
        ("标签", "生成标签", "claude", "report-writer"),
        ("API 数据抓取", "通过 API 抓取数据", "codex", "web-collector"),
        ("MediaCrawler", "运行 MediaCrawler 采集", "codex", "sandboxed-runner"),
        ("浏览器自动化", "自动化浏览器采集", "codex", "sandboxed-runner"),
        ("代码执行", "执行代码", "codex", "sandboxed-runner"),
        ("Excel 生成", "生成 Excel", "codex", "sandboxed-runner"),
        ("数据清洗", "清洗数据", "codex", "sandboxed-runner"),
    ],
)
def test_路由表逐项与四预设档映射(name, task, engine, profile, tmp_path) -> None:
    skeleton = _valid_skeleton()
    agent = _agent(name, task)
    agent["output"]["shape"] = skeleton["goals"][0]["deliverable"]["shape"]
    skeleton["goals"][0]["agents"] = [agent]
    _add_coverage_collector(skeleton)

    plan, _, _ = _generate(tmp_path, [skeleton])
    generated = plan.goals[0].agents[0]

    assert generated.engine == engine
    assert generated.capability["profile"] == profile


def test_角色只按封闭名称分类_任务中的_API_不得误派报告_agent(tmp_path) -> None:
    skeleton = _valid_skeleton()
    skeleton["goals"][0]["agents"] = [
        _agent("报告撰写", "撰写 API 竞品报告", output={"shape": "array"})
    ]
    _add_coverage_collector(skeleton)

    plan, _, _ = _generate(tmp_path, [skeleton])

    agent = plan.goals[0].agents[0]
    assert agent.engine == "claude"
    assert agent.capability["profile"] == "report-writer"


def test_每个_goal_最终_agent_产出_deliverable_且下游_inputs_逐字引用(tmp_path) -> None:
    plan, _, _ = _generate(tmp_path, [_valid_skeleton()])

    for goal in plan.goals:
        assert goal.agents[-1].output["path"] == goal.deliverable["path"]
        assert goal.agents[-1].output["format"] == goal.deliverable["format"]
        for index, agent in enumerate(goal.agents):
            expected = [] if index == 0 else [goal.agents[index - 1].agent_id]
            assert agent.depends_on == expected
    for goal in plan.goals[1:]:
        direct_inputs = [
            {
                "from_goal": upstream,
                "artifact": next(
                    item.deliverable["path"]
                    for item in plan.goals if item.goal_id == upstream
                ),
            }
            for upstream in goal.depends_on
        ]
        assert all(item in goal.agents[0].inputs for item in direct_inputs)
        assert len({
            (item["from_goal"], item["artifact"])
            for item in goal.agents[0].inputs
        }) == len(goal.agents[0].inputs)


def test_每个落盘_agent_都具有当前_goal_写权限(tmp_path) -> None:
    plan, _, _ = _generate(tmp_path, [_valid_skeleton()])

    for goal in plan.goals:
        writer = goal.agents[-1]
        assert "fs.write" in writer.capability["tools"]
        assert f"goals/{goal.goal_id}/**" in writer.capability["fs"]["write"]


def test_X_采集角色由系统派生_source_x_工具与来源槽位(tmp_path) -> None:
    skeleton = _valid_skeleton()
    skeleton["goals"][0]["agents"] = [
        _agent("X 数据抓取·飞书", "通过 recent search 采集 X 证据")
    ]

    plan, _, _ = _generate(tmp_path, [skeleton])

    capability = plan.goals[0].agents[0].capability
    assert capability["profile"] == "web-collector"
    assert capability["tools"] == ["source.x", "fs.write", "db.write"]
    assert capability["sources"] == ["x"]


def test_deliverable_格式改变时不沿用不兼容_validator(tmp_path) -> None:
    skeleton = _valid_skeleton()
    skeleton["goals"][0]["agents"] = [
        _agent("报告撰写", "输出 JSON 摘要", output={"shape": "array"})
    ]
    _add_coverage_collector(skeleton)

    plan, _, _ = _generate(tmp_path, [skeleton])

    output = plan.goals[0].agents[-1].output
    assert output["format"] == "json"
    assert output["validators"] == ["file_exists"]


def test_lint_error_原文回灌并在第三次通过(tmp_path) -> None:
    from app.adapters.events import NormalizedEvent

    invalid = _valid_skeleton()
    invalid["goals"][0]["acceptance"] = ["结果质量良好"]
    plan, store, engine = _generate(tmp_path, [invalid, invalid, _valid_skeleton()])

    assert plan.status == "awaiting_review"
    assert len(engine.tasks) == 6
    assert "[规则4]" in engine.tasks[4].body
    assert "结果质量良好" in engine.tasks[4].body
    assert "[规则4]" in engine.tasks[5].body
    retries = [event for event in store.events if event.outcome == "retrying"]
    assert len(retries) == 2
    assert all(isinstance(event, NormalizedEvent) for event in store.events)


def test_lint回灌前明确重置受影响goal段预算(monkeypatch, tmp_path) -> None:
    from app.plan.segments import PlanSegmentWorkspace

    invalid = _valid_skeleton()
    invalid["goals"][0]["acceptance"] = ["结果质量良好"]
    calls = []
    original = PlanSegmentWorkspace.reset_attempts

    def spy(self, name):
        calls.append(name)
        return original(self, name)

    monkeypatch.setattr(PlanSegmentWorkspace, "reset_attempts", spy)
    _generate(tmp_path, [invalid, _valid_skeleton()])

    assert "goal-1" in calls


def test_受影响_goal_只从_lint_消息头提取_引文不误伤(tmp_path) -> None:
    from app.plan.generate import _affected_goal_indices

    del tmp_path
    errors = [
        "[规则18] goal-3/report-writer 引文指向 goal-2 矩阵条目",
        "[结构] goal-1.deliverable.shape 非法",
    ]
    assert _affected_goal_indices(errors, 3) == [3, 1]


def test_结构异常只把每行起始_goal_提升为消息头() -> None:
    from app.plan.generate import _structure_errors

    errors = _structure_errors(ValueError(
        "goal-3.inputs 引用了路径 goals/goal-2/result.json"
    ))
    assert len(errors) == 1
    assert errors[0].startswith("[结构] goal-3.")
    assert "goal-2/result.json" in errors[0]


def test_段级_lint_失败时先回灌而不生成章(tmp_path) -> None:
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    invalid = _valid_skeleton()
    invalid["goals"][0]["acceptance"] = ["结果质量良好"]

    class OrderedEngine(FakeEngine):
        def __init__(self, skeletons):
            super().__init__(skeletons)
            self.order = []

        async def run(self, task, ctx, on_event=None):
            self.order.append(task.output_path.stem)
            return await super().run(task, ctx, on_event)

    engine = OrderedEngine([invalid, _valid_skeleton()])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )

    asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    assert engine.order.index("goal-1", 2) < next(
        index for index, name in enumerate(engine.order) if "-ch-" in name
    )


def test_lint_连续三次失败则不保存计划(tmp_path) -> None:
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import PlanGenerationError, generate_plan

    invalid = _valid_skeleton()
    invalid["goals"][0]["acceptance"] = ["结果质量良好"]
    engine = FakeEngine([invalid])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )

    with pytest.raises(PlanGenerationError, match="连续 3 次") as captured:
        asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    assert len(engine.tasks) == 6
    assert "[规则4]" in str(captured.value)
    assert store.saved == []


def test_规划双腿判定失败也带原文重试且共用三次上限(tmp_path) -> None:
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    class FlakyEngine(FakeEngine):
        async def run(self, task, ctx, on_event=None):
            result = await super().run(task, ctx, on_event)
            if len(self.tasks) == 1:
                return SimpleNamespace(
                    succeeded=False,
                    engine_error=None,
                    conclusion_error="owli-result.summary 必须是 200 字以内字符串",
                )
            return result

    engine = FlakyEngine([_valid_skeleton()])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )

    plan = asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    assert plan.status == "awaiting_review"
    assert len(engine.tasks) == 5
    assert "owli-result.summary 必须是 200 字以内字符串" in engine.tasks[1].body
    retries = [event for event in store.events if event.outcome == "retrying"]
    assert len(retries) == 1


def test_每轮起跑前清除残留骨架避免重试覆盖死锁(tmp_path) -> None:
    # 真实样本 r-41651a233827：首轮骨架落盘后 lint 拒收，重试轮因规划
    # 工具集无 Read 而无法覆盖残留文件，三轮全 blocked。修复后每轮
    # adapter.run 起跑时产物路径必须是空的。
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    invalid = _valid_skeleton()
    invalid["goals"][0]["acceptance"] = ["结果质量良好"]

    class RecordingEngine(FakeEngine):
        def __init__(self, skeletons: list[dict]) -> None:
            super().__init__(skeletons)
            self.existed_at_run: list[bool] = []

        async def run(self, task, ctx, on_event=None):
            partial = Path(f"{task.output_path}.partial")
            self.existed_at_run.append(partial.exists())
            return await super().run(task, ctx, on_event)

    engine = RecordingEngine([invalid, _valid_skeleton()])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )

    plan = asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    assert plan.status == "awaiting_review"
    assert engine.existed_at_run and not any(engine.existed_at_run)


def test_规划产物校验失败的原文与_offenders_回灌(tmp_path) -> None:
    from app.adapters import validation
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    class ValidationFlakyEngine(FakeEngine):
        async def run(self, task, ctx, on_event=None):
            result = await super().run(task, ctx, on_event)
            if len(self.tasks) == 1:
                failure = validation.Result(
                    validation.Verdict.FAIL,
                    "file_exists",
                    "产物文件不存在",
                    ["plan-skeleton.json"],
                )
                return SimpleNamespace(
                    succeeded=False,
                    engine_error=None,
                    conclusion_error=None,
                    validation=validation.ValidationReport(
                        validation.Verdict.FAIL, [failure]
                    ),
                )
            return result

    engine = ValidationFlakyEngine([_valid_skeleton()])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )

    asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    assert "产物文件不存在" in engine.tasks[1].body
    assert "plan-skeleton.json" in engine.tasks[1].body


def test_非采集_agent_prompt_明确禁止新抓取(tmp_path) -> None:
    plan, _, _ = _generate(tmp_path, [_valid_skeleton()])

    collector = plan.goals[0].agents[0]
    auditor = plan.goals[1].agents[0]
    assert "不发起新抓取" not in collector.prompt["body"]
    assert "不发起新抓取" in auditor.prompt["body"]


def test_decision_balance_选项式引用合法且_baseline_深拷贝独立(tmp_path) -> None:
    plan, _, _ = _generate(tmp_path, [_valid_skeleton()])
    questions = plan.decision_balance
    node_ids = {
        *(goal.goal_id for goal in plan.goals),
        *(agent.agent_id for goal in plan.goals for agent in goal.agents),
    }

    assert 1 <= len(questions) <= 5
    for question in questions:
        assert question["input_type"] in {"single", "multi", "choice_2"}
        assert 2 <= len(question["options"]) <= 4
        assert question["answer"] is None
        assert question["affects"]
        assert set(question["affects"]) <= node_ids

    baseline = plan.to_dict()["baseline"]
    before = deepcopy(baseline)
    plan.goals[0].title = "用户改过的标题"
    plan.goals[0].agents[0].task = "用户改过的任务"
    assert plan.to_dict()["baseline"] == before


def test_规划_prompt_固定四段与_HN_可复现参数(tmp_path) -> None:
    _, _, engine = _generate(tmp_path, [_valid_skeleton()])
    body = engine.tasks[1].body

    assert all(label in body for label in ("目标：", "方法要点：", "产物结构：", "边界与降级："))
    assert "created_at_i>" in body
    assert "7776000" in body
    assert "points>50" in body
    assert "3–7" in engine.tasks[0].body


def test_store_只返回同需求且尚未保存计划的最新报告(tmp_path) -> None:
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database)
    for report_id, created_at, snapshot in (
        ("r-old", "2026-08-19T01:00:00Z", None),
        ("r-done", "2026-08-19T02:00:00Z", {"plan_rev": 1}),
        (RESEARCH_ID, NOW, None),
    ):
        store.create_report(
            id=report_id,
            title="飞书竞品优缺点",
            research_question="飞书竞品优缺点",
            created_at=created_at,
            plan_snapshot=snapshot,
        )

    report = store.get_drafting_report("飞书竞品优缺点")

    assert report is not None and report["id"] == RESEARCH_ID


def test_真实_store_保留_normalized_plan_event(tmp_path) -> None:
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.store.dao import Store

    store = Store(tmp_path / "owli.db")
    event = NormalizedEvent(
        engine="Owli",
        thread_id=RESEARCH_ID,
        turn_id="plan-attempt-2",
        item_kind=ItemKind.ERROR,
        text="[rule4]",
        is_error=True,
        raw={"errors": ["[rule4]"]},
        outcome="retrying",
    )

    store.on_plan_event(event)

    assert store.plan_events == (event,)


def test_未知职能报错自带双闭集且goal定位可用于段级重试() -> None:
    """6b 实跑取证（2026-08-21）：模型自造 hn_competitor_scope_collector，
    报错不带合法值清单，回灌三轮无法自纠。"""
    from app.plan.generate import _build_agent, _classify
    from collections import Counter

    with pytest.raises(ValueError) as exc_info:
        _classify("hn_competitor_scope_collector", "采集 HN 竞品讨论")
    message = str(exc_info.value)
    assert "未知 agent 职能名称：hn_competitor_scope_collector" in message
    assert "计划仲裁" in message and "报告撰写" in message  # 职能闭集
    assert "网页搜索数据抓取" in message  # 注册表 collector_name 闭集

    with pytest.raises(ValueError) as goal_exc:
            _build_agent(
                {"name": "hn_competitor_scope_collector", "task": "采集",
                 "output": {"shape": "array"}},
            "goal-3",
            [],
            "查询",
            Counter(),
            previous_agent_id=None,
            upstream_artifacts={},
            target=None,
        )
    assert str(goal_exc.value).startswith("goal-3 ")  # 段级重试可定位涉事段


def test_goal段提示词自带非采集职能闭集() -> None:
    from app.plan.generate import _goal_prompt

    prompt = _goal_prompt("飞书竞品优缺点", "goal-2", {"title": "t"}, [])
    assert "职能闭集" in prompt
    assert "计划仲裁" in prompt and "一致性检查" in prompt
    assert "不得自造名称" in prompt


def test_display_name别名可作采集agent名称且源id解析正确() -> None:
    """6b 实跑取证（2026-08-21 r-e55ddfe36e51）：提示词列出
    display_name（collector_name），模型写 display_name 被拒。"""
    from collections import Counter

    from app.plan.generate import _build_agent, _classify
    from app.sources.registry import planning_catalog

    for spec in planning_catalog():
        assert _classify(spec.display_name, "采集") == (
            "data_collection", "web-collector",
        )
        agent = _build_agent(
            {"name": spec.display_name, "task": "采集",
             "output": {"shape": "array"}},
            "goal-1",
            [],
            "查询",
            Counter(),
            previous_agent_id=None,
            upstream_artifacts={},
            target=None,
        )
        assert spec.source_id in agent["capability"]["sources"]


def test_采集角色允许结构化实体后缀并仍按前缀派生信息源() -> None:
    from collections import Counter

    from app.plan.generate import _build_agent

    agent = _build_agent(
        {"name": "网页搜索数据抓取·豆包", "task": "采集豆包资料",
         "output": {"shape": "array"}},
        "goal-1",
        [],
        "豆包竞品",
        Counter(),
        previous_agent_id=None,
        upstream_artifacts={},
    )
    assert agent["display_name"] == "网页搜索数据抓取·豆包"
    assert agent["capability"]["sources"] == ["web_search"]


def test_同_goal_竞品采集章并行且首个汇总章等待全部采集() -> None:
    from app.plan.generate import _build_plan

    source = _valid_skeleton()
    source["goals"][0]["agents"] = [
        _agent("网页搜索数据抓取·豆包", "采集豆包"),
        _agent("网页搜索数据抓取·讯飞", "采集讯飞"),
        _agent("数据清洗", "汇总两份采集结果"),
    ]
    plan = _build_plan(
        source, query="豆包竞品", research_id=RESEARCH_ID, timestamp=NOW,
        scale="fast",
    )
    agents = plan.goals[0].agents
    assert agents[0].depends_on == []
    assert agents[1].depends_on == []
    assert agents[2].depends_on == [agents[0].agent_id, agents[1].agent_id]


def test_build_plan保留模型填写的owner_shape供规则17判冲突() -> None:
    from app.plan.generate import _build_plan
    from app.plan.lint import lint

    source = _valid_skeleton()
    source["goals"][2]["agents"][0]["output"]["shape"] = "array"
    plan = _build_plan(
        source, query="竞品分析", research_id=RESEARCH_ID, timestamp=NOW,
    )

    owner = plan.goals[2].agents[0]
    assert owner.output["path"] == plan.goals[2].deliverable["path"]
    assert owner.output["shape"] == "array"
    assert any(error.startswith("[规则17] goal-3/") for error in lint(plan)["errors"])


def test_agent_output段契约拒绝shape以外字段() -> None:
    from collections import Counter
    from app.plan.generate import _build_agent

    with pytest.raises(ValueError, match="仅含 shape"):
        _build_agent(
            {
                "name": "报告撰写", "task": "写报告",
                "output": {"shape": "object", "path": "越权路径.md"},
            },
            "goal-3", [], "竞品分析", Counter(),
            previous_agent_id=None, upstream_artifacts={}, target=None,
        )


def test_build_plan一次聚合全部goal结构错误() -> None:
    """6b 实跑取证（2026-08-21 r-e55ddfe36e51）：逐个抛错打地鼠，
    goal-1/2/4 各占一轮吃光 3 次段级预算。"""
    from app.plan.generate import _affected_goal_indices, _build_plan, _structure_errors

    raw = {
        "goals": [
            {
                "title": f"阶段{n}",
                "objective": "形成独立产物。",
                "depends_on": [],
                    "deliverable": {
                        "format": "json",
                        "shape": "object",
                    "path": f"stage-{n}.json",
                    "description": "结构化产物",
                },
                "acceptance": ["条目可判定"],
                    "agents": [{"name": name, "task": "执行",
                                "output": {"shape": "object"}}],
            }
            for n, name in ((1, "自造名甲"), (2, "hn 数据抓取"), (3, "自造名乙"))
        ]
    }
    with pytest.raises(ValueError) as exc_info:
        _build_plan(raw, query="查询", research_id="r-agg", timestamp="2026-08-21T00:00:00+00:00")
    message = str(exc_info.value)
    assert "goal-1" in message and "自造名甲" in message
    assert "goal-3" in message and "自造名乙" in message
    assert "goal-2" not in message
    assert _affected_goal_indices(_structure_errors(exc_info.value), 3) == [1, 3]


def test_同义变体归一化接受且格式报错带闭集() -> None:
    """6b 实跑取证（2026-08-21 r-bbc15dc5c4e8）：format 写 xlsx 被拒且
    报错不给合法值；agent 名写 product-hunt（连字符）被拒。"""
    from app.plan.generate import _classify, _deliverable

    value = _deliverable(
        {"format": "xlsx", "shape": "object", "path": "对比表.xlsx",
         "description": "竞品对比表"},
        "goal-3",
    )
    assert value["format"] == "excel"

    with pytest.raises(ValueError) as exc_info:
        _deliverable(
            {"format": "docx", "path": "x.docx", "description": "说明"}, "goal-3"
        )
    message = str(exc_info.value)
    assert "只能取" in message and "excel" in message and "markdown" in message

    for variant in ("product-hunt 数据抓取", "Product_Hunt 数据抓取"):
        assert _classify(variant, "采集") == ("data_collection", "web-collector")
