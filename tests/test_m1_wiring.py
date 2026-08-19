import asyncio
import json
import stat
from dataclasses import fields
from datetime import datetime, timezone


def _ctx(validation, output_path):
    return validation.Ctx(
        output_path=output_path,
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        read_text=lambda: output_path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(output_path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
    )


def test_r2_default_route_and_user_origin_marker():
    from app.adapters.routing import pick_engine

    claude_kinds = ("planning", "audit", "report")
    codex_kinds = ("code_execution", "data_collection")

    assert {pick_engine(kind, None).engine for kind in claude_kinds} == {"claude"}
    assert {pick_engine(kind, None).engine for kind in codex_kinds} == {"codex"}
    assert all(pick_engine(kind, None).origin == "system" for kind in claude_kinds)

    overridden = pick_engine("report", "codex")
    assert overridden.engine == "codex"
    assert overridden.origin == "user"


def test_m0_three_cards_use_routing_but_keep_all_claude_assignment():
    from app.orchestrator.mini import build_initial_state

    state = build_initial_state("r-1", "飞书竞品优缺点")
    agents = state["goals"][0]["agents"]

    assert [agent["agent_kind"] for agent in agents] == [
        "planning", "m0_hn_collection", "report_writing"
    ]
    assert {agent["engine"] for agent in agents} == {"Claude"}


def test_same_task_and_same_result_contract_are_shared_by_both_adapters(tmp_path):
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import ClaudeRunResult, ClaudeTask
    from app.adapters.codex import CodexRunResult, CodexTask
    from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult

    task = EngineTask(
        body="写一个文件",
        output_path=tmp_path / "result.md",
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )

    assert ClaudeRunResult is CodexRunResult is EngineRunResult
    assert not {"tools", "sandbox", "network"} & {
        item.name for item in fields(task)
    }
    assert {item.name for item in fields(OwliResult)} == {
        "status",
        "output_path",
        "summary",
        "assumptions",
        "unmet",
        "capability_denials",
    }


def test_routed_adapter_selects_without_orchestrator_engine_branch(tmp_path):
    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.routing import RoutedAdapter

    calls = []

    class FakeAdapter:
        def __init__(self, name):
            self.name = name

        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            calls.append(self.name)
            return self.name

    task = EngineTask(
        body="只验证路由",
        output_path=tmp_path / "result.md",
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(),
    )
    adapter = RoutedAdapter(
        adapters={"claude": FakeAdapter("claude"), "codex": FakeAdapter("codex")}
    )

    result = asyncio.run(adapter.run(task, object()))

    assert result == "claude"
    assert calls == ["claude"]


def test_routed_adapter_moves_only_future_tasks_to_codex_after_warning(tmp_path):
    import inspect

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter

    calls = []
    warning = NormalizedEvent(
        engine="Claude", thread_id="s-1", turn_id="t-1",
        item_kind=ItemKind.THINKING, text="后续新任务让路", is_error=False,
        raw={"rate_limit_info": {"status": "allowed_warning"}},
        route_state="WARN", failover_target="codex", no_fallback_left=True,
        scope="new_tasks", allow_current_task_to_finish=True,
    )

    class FakeAdapter:
        def __init__(self, name, event=None):
            self.name = name
            self.event = event

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            calls.append(self.name)
            if self.event is not None:
                returned = on_event(self.event)
                if inspect.isawaitable(returned):
                    await returned
            return self.name

    task = EngineTask(
        body="同一种新任务", output_path=tmp_path / "result.md",
        output_format="markdown", research_id="r-1", goal_id="goal-1",
        agent_id="agent-1", agent_kind="report", validators=["file_exists"],
        capability=Capability(),
    )
    adapter = RoutedAdapter(adapters={
        "claude": FakeAdapter("claude", warning),
        "codex": FakeAdapter("codex"),
    })
    observed = []

    first = asyncio.run(adapter.run(task, object(), on_event=observed.append))
    second = asyncio.run(adapter.run(task, object(), on_event=observed.append))

    assert first == "claude"
    assert second == "codex"
    assert calls == ["claude", "codex"]
    assert observed == [warning]
    assert adapter.future_engine == "codex"


def test_capability_is_mounted_on_claude_callback_and_codex_tier(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import build_claude_options, make_permission_callback
    from app.adapters.codex import build_codex_command
    from app.adapters.contracts import EngineTask

    class Allow:
        pass

    class Deny:
        def __init__(self, *, message):
            self.message = message

    class Options:
        def __init__(self, **values):
            self.values = values

    class FakeHookMatcher:
        def __init__(self, *, matcher=None, hooks=None):
            self.matcher = matcher
            self.hooks = hooks or []

    class FakeSdk:
        PermissionResultAllow = Allow
        PermissionResultDeny = Deny
        ClaudeAgentOptions = Options
        HookMatcher = FakeHookMatcher

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    goal_root = runs_root / "r-1" / "goals" / "goal-1"
    capability = Capability(
        tools=("fs.write",),
        fs=FileSystemScope(write=("goals/goal-1/allowed/**",)),
        network="none",
        shell="none",
    )
    task = EngineTask(
        body="写入受限目录",
        output_path=goal_root / "allowed" / "result.md",
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=capability,
    )
    denials = []
    callback = make_permission_callback(task, denials, sdk=FakeSdk)
    options = build_claude_options(task, callback, sdk=FakeSdk)

    accepted = asyncio.run(callback(
        "Write", {"file_path": str(task.output_path)}, None
    ))
    rejected = asyncio.run(callback(
        "Write", {"file_path": str(goal_root / "blocked.md")}, None
    ))
    command = build_codex_command(task)

    assert isinstance(accepted, Allow)
    assert isinstance(rejected, Deny)
    assert "capability 路径范围" in rejected.message
    assert "Write" not in options.values["disallowed_tools"]
    assert "Bash" in options.values["disallowed_tools"]
    assert command[command.index("-s") + 1] == "workspace-write"
    assert command[command.index("--add-dir") + 1] == str(
        goal_root / "allowed"
    )
    assert "sandbox_workspace_write.network_access=true" not in command


def test_codex_readonly_capability_rejects_task_that_requires_output_write(
    tmp_path, monkeypatch
):
    import pytest

    from app.adapters.capability import READONLY_ANALYST
    from app.adapters import validation
    from app.adapters.codex import build_codex_command
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    task = EngineTask(
        body="尝试写文件",
        output_path=runs_root / "r-1" / "goals" / "goal-1" / "result.md",
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=READONLY_ANALYST,
    )

    with pytest.raises(ValueError, match="capability.fs.write"):
        build_codex_command(task)


def test_codex_capability_cannot_mount_writable_root_outside_current_goal(
    tmp_path, monkeypatch
):
    import pytest

    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.codex import build_codex_command
    from app.adapters.contracts import EngineTask

    monkeypatch.setattr(validation, "RUNS_ROOT", tmp_path / "runs")
    task = EngineTask(
        body="不得写兄弟 goal",
        output_path=tmp_path / "runs" / "r-1" / "goals" / "goal-1" / "a.md",
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(
                write=("goals/goal-1/**", "goals/goal-2/**")
            ),
        ),
    )

    with pytest.raises(ValueError, match="当前 goal"):
        build_codex_command(task)

    blocked_inside_goal = EngineTask(
        body="不得靠 cwd 绕过路径谓词",
        output_path=(
            tmp_path / "runs" / "r-1" / "goals" / "goal-1" / "blocked" / "a.md"
        ),
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/allowed/**",)),
        ),
    )
    with pytest.raises(ValueError, match="capability.fs.write"):
        build_codex_command(blocked_inside_goal)


def test_rate_limit_sequence_becomes_persisted_normalized_events_with_raw(tmp_path):
    from app.adapters.events import NormalizedEvent
    from app.adapters.ratelimit import route

    raw_messages = [
        {
            "type": "rate_limit_event",
            "session_id": "s-0",
            "rate_limit_info": {
                "status": "allowed",
                "rate_limit_type": "five_hour",
                "utilization": 0.20,
            },
        },
        {
            "type": "rate_limit_event",
            "session_id": "s-1",
            "rate_limit_info": {
                "status": "allowed_warning",
                "rate_limit_type": "five_hour",
                "utilization": 0.85,
            },
        },
        {
            "type": "result",
            "uuid": "t-2",
            "subtype": "success",
            "api_error_status": 429,
            "is_error": False,
        },
        {
            "type": "result",
            "uuid": "t-3",
            "is_error": True,
            "subtype": "transport_error",
        },
    ]
    events = []
    decisions = []
    clock = lambda: datetime(2026, 8, 19, tzinfo=timezone.utc)

    for message in raw_messages:
        decisions.append(route(
            message, on_event=events.append, log_root=tmp_path, log_clock=clock
        ))

    assert [decision.state.value for decision in decisions] == [
        "CONTINUE", "WARN", "BACKOFF", "FAILOVER"
    ]
    assert all(isinstance(event, NormalizedEvent) for event in events)
    assert [event.route_state for event in events] == [
        "WARN", "BACKOFF", "FAILOVER"
    ]
    assert [event.raw for event in events] == raw_messages[1:]
    assert events[0].failover_target == "codex"
    assert events[0].scope == "new_tasks"
    assert events[0].allow_current_task_to_finish is True
    assert events[1].suspend_new_tasks is True
    assert events[2].scope == "new_tasks"
    assert events[2].allow_current_task_to_finish is True
    assert events[2].failover_target == "codex"
    assert events[2].no_fallback_left is True

    persisted = tmp_path / "routing" / "claude-2026-08-19.jsonl"
    payloads = [json.loads(line) for line in persisted.read_text().splitlines()]
    assert [item["route_state"] for item in payloads] == [
        "WARN", "BACKOFF", "FAILOVER"
    ]
    assert [item["raw"] for item in payloads] == raw_messages[1:]


def test_codex_rate_limit_is_backoff_with_no_fallback_marker(tmp_path):
    from app.adapters.ratelimit import route

    raw = {
        "type": "turn.failed",
        "error": {"message": "You've hit your usage limit"},
    }
    events = []

    decision = route(raw, engine="Codex", on_event=events.append, log_root=tmp_path)

    assert decision.state.value == "BACKOFF"
    assert decision.no_fallback_left is True
    assert events[0].route_state == "BACKOFF"
    assert events[0].raw is raw


def test_claude_adapter_feeds_native_messages_into_rate_route(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import EngineTask

    class FakeResultMessage:
        def __init__(self, result="", **values):
            self.result = result
            self.is_error = values.get("is_error", False)
            self.api_error_status = values.get("api_error_status")
            self.subtype = values.get("subtype", "success")
            self.session_id = "s-1"
            self.uuid = values.get("uuid")

    class RateMessage:
        session_id = "s-1"
        uuid = "rate-1"
        rate_limit_info = {
            "status": "allowed_warning",
            "rate_limit_type": "five_hour",
            "utilization": 0.85,
        }

    class FakeClient:
        messages = []

        def __init__(self, options):
            self.options = options

        async def connect(self, prompt):
            async for _ in prompt:
                pass

        async def receive_response(self):
            for message in self.messages:
                yield message

        async def disconnect(self):
            pass

    class FakeOptions:
        def __init__(self, **values):
            self.values = values

    class FakeSdk:
        ClaudeSDKClient = FakeClient
        ClaudeAgentOptions = FakeOptions
        ResultMessage = FakeResultMessage
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        TextBlock = type("TextBlock", (), {})
        ToolUseBlock = type("ToolUseBlock", (), {})
        PermissionResultAllow = type("Allow", (), {})
        PermissionResultDeny = type(
            "Deny", (), {"__init__": lambda self, **values: None}
        )
        HookMatcher = type(
            "HookMatcher",
            (),
            {"__init__": lambda self, matcher=None, hooks=None: None},
        )

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-1" / "goals" / "goal-1" / "result.md"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("产物", encoding="utf-8")
    conclusion = json.dumps({
        "status": "done", "output_path": str(output_path), "summary": "完成",
        "assumptions": [], "unmet": [], "capability_denials": [],
    }, ensure_ascii=False)
    FakeClient.messages = [
        RateMessage(),
        FakeResultMessage(api_error_status=429, uuid="backoff"),
        FakeResultMessage(is_error=True, subtype="transport_error", uuid="failover"),
        FakeResultMessage(f"```json owli-result\n{conclusion}\n```", uuid="done"),
    ]
    task = EngineTask(
        body="假引擎任务", output_path=output_path, output_format="markdown",
        research_id="r-1", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    ctx = _ctx(validation, output_path)

    result = asyncio.run(ClaudeAdapter(
        sdk=FakeSdk, log_root=tmp_path / "logs"
    ).run(task, ctx))

    route_events = [event for event in result.events if event.route_state]
    assert [event.route_state for event in route_events] == [
        "WARN", "BACKOFF", "FAILOVER"
    ]
    assert route_events[2].no_fallback_left is True


def test_codex_adapter_feeds_jsonl_into_rate_route(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.codex import CodexAdapter
    from app.adapters.contracts import EngineTask

    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
last_message = pathlib.Path(args[args.index('-o') + 1])
output_path = pathlib.Path(args[-1].split('ARTIFACT=', 1)[1].splitlines()[0])
last_message.parent.mkdir(parents=True, exist_ok=True)
last_message.write_text(json.dumps({
    'status': 'done',
    'output_path': str(output_path),
    'summary': '收到限流',
    'assumptions': [],
    'unmet': [],
    'capability_denials': [],
}, ensure_ascii=False), encoding='utf-8')
print(json.dumps({'type': 'thread.started', 'thread_id': 'codex-thread'}), flush=True)
print(json.dumps({'type': 'turn.started', 'turn_id': 'codex-turn'}), flush=True)
print(json.dumps({
    'type': 'turn.failed',
    'error': {'message': "You've hit your usage limit"},
}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-1" / "goals" / "goal-1" / "missing.md"
    task = EngineTask(
        body=f"假引擎任务\nARTIFACT={output_path}",
        output_path=output_path,
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )

    result = asyncio.run(CodexAdapter(
        executable=str(executable),
        codex_home=tmp_path / "codex-home",
        log_root=tmp_path / "logs",
    ).run(task, _ctx(validation, output_path)))

    route_events = [event for event in result.events if event.route_state]
    outcome_events = [event for event in result.events if event.outcome]
    assert result.validation.verdict is validation.Verdict.FAIL
    assert [event.route_state for event in route_events] == ["BACKOFF"]
    assert [event.outcome for event in outcome_events] == ["FAIL"]
    assert route_events[0].no_fallback_left is True
    assert route_events[0].thread_id == "codex-thread"
    assert route_events[0].turn_id == "codex-turn"
    assert route_events[0].raw == {
        "type": "turn.failed",
        "error": {"message": "You've hit your usage limit"},
    }


def test_codex_unavailable_event_is_distinct_from_task_fail(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.codex import CodexAdapter
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-1" / "goals" / "goal-1" / "result.md"
    task = EngineTask(
        body="探测不存在的引擎",
        output_path=output_path,
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    streamed = []

    result = asyncio.run(CodexAdapter(
        executable=str(tmp_path / "不存在-codex"),
        codex_home=tmp_path / "codex-home",
        log_root=tmp_path / "logs",
    ).run(task, _ctx(validation, output_path), on_event=streamed.append))

    assert result.validation.verdict is validation.Verdict.UNAVAILABLE
    assert [event.outcome for event in result.events if event.outcome] == [
        "UNAVAILABLE"
    ]
    assert [event.outcome for event in streamed if event.outcome] == [
        "UNAVAILABLE"
    ]


def test_codex_nonzero_status_cannot_override_artifact_and_conclusion(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.codex import CodexAdapter
    from app.adapters.contracts import EngineTask

    executable = tmp_path / "fake-codex-nonzero"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
prompt = args[-1]
output_path = pathlib.Path(prompt.split('ARTIFACT=', 1)[1].splitlines()[0])
last_message = pathlib.Path(args[args.index('-o') + 1])
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text('产物完整', encoding='utf-8')
last_message.write_text(json.dumps({
    'status': 'done', 'output_path': str(output_path), 'summary': '双腿通过',
    'assumptions': [], 'unmet': [], 'capability_denials': [],
}, ensure_ascii=False), encoding='utf-8')
print(json.dumps({'type': 'turn.completed'}), flush=True)
sys.exit(7)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-1" / "goals" / "goal-1" / "result.md"
    task = EngineTask(
        body=f"假引擎任务\nARTIFACT={output_path}",
        output_path=output_path, output_format="markdown", research_id="r-1",
        goal_id="goal-1", agent_id="agent-1", agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )

    result = asyncio.run(CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "codex-home"
    ).run(task, _ctx(validation, output_path)))

    assert result.succeeded is True
    assert result.validation.verdict is validation.Verdict.PASS
    assert result.engine_error is None


def test_claude_initialization_and_capability_failures_return_unified_results(
    tmp_path, monkeypatch
):
    from app.adapters import claude, validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineRunResult, EngineTask

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-1" / "goals" / "goal-1" / "result.md"
    capability_failure_task = EngineTask(
        body="Claude 不得执行 shell",
        output_path=output_path,
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("shell.exec", "fs.write"),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
            shell="workspace",
        ),
    )

    rejected = asyncio.run(
        claude.ClaudeAdapter(sdk=object()).run(
            capability_failure_task, _ctx(validation, output_path)
        )
    )

    assert isinstance(rejected, EngineRunResult)
    assert rejected.validation.verdict is validation.Verdict.FAIL
    assert rejected.validation.results[0].name == "claude_capability"
    assert rejected.engine_error is None

    available_task = EngineTask(
        body="探测 SDK",
        output_path=output_path,
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )

    def unavailable_sdk():
        raise RuntimeError("测试：SDK 缺失")

    monkeypatch.setattr(claude, "_load_sdk", unavailable_sdk)
    unavailable = asyncio.run(
        claude.ClaudeAdapter().run(available_task, _ctx(validation, output_path))
    )

    assert isinstance(unavailable, EngineRunResult)
    assert unavailable.validation.verdict is validation.Verdict.UNAVAILABLE
    assert unavailable.validation.results[0].name == "claude_sdk"
    assert unavailable.engine_error is not None


def test_codex_invalid_capability_is_task_fail_not_engine_unavailable(
    tmp_path, monkeypatch
):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.codex import CodexAdapter
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-1" / "goals" / "goal-1" / "blocked" / "result.md"
    task = EngineTask(
        body="不得靠工作目录绕过 capability",
        output_path=output_path,
        output_format="markdown",
        research_id="r-1",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/allowed/**",)),
        ),
    )

    result = asyncio.run(CodexAdapter(
        executable=str(tmp_path / "不存在-codex"),
        codex_home=tmp_path / "codex-home",
        log_root=tmp_path / "logs",
    ).run(task, _ctx(validation, output_path)))

    assert result.validation.verdict is validation.Verdict.FAIL
    assert result.validation.results[0].name == "codex_capability"
    assert result.engine_error is None
    assert [event.outcome for event in result.events if event.outcome] == ["FAIL"]


def test_mini_publishes_route_transition_to_sse_with_complete_raw(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.api.events import ResearchEventBuffer
    from app.orchestrator.mini import MiniOrchestrator, build_initial_state

    raw = {"type": "rate_limit_event", "nested": {"kept": [1, 2, 3]}}
    route_event = NormalizedEvent(
        engine="Claude", thread_id="s-1", turn_id="t-1",
        item_kind=ItemKind.THINKING, text="后续新任务让路", is_error=False,
        raw=raw, route_state="WARN", scope="new_tasks",
        allow_current_task_to_finish=True,
    )

    class FakeAdapter:
        async def run(self, task, ctx, on_event=None):
            await on_event(route_event)
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text("产物", encoding="utf-8")
            report = validation.validate(ctx, task.validators)
            conclusion = OwliResult(
                "done", str(task.output_path), "完成", [], [], []
            )
            return EngineRunResult(conclusion, None, report, [route_event], [])

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    buffer = ResearchEventBuffer()
    orchestrator = MiniOrchestrator(
        research_id="r-1", query="测试", store=object(), event_buffer=buffer,
        state=build_initial_state("r-1", "测试"), adapter=FakeAdapter(),
        runs_root=runs_root,
    )
    task = EngineTask(
        body="假任务", output_path=orchestrator.keywords_path,
        output_format="markdown", research_id="r-1", goal_id="goal-1",
        agent_id="keyword-extractor", agent_kind="planning",
        validators=["file_exists"], capability=Capability(),
    )

    asyncio.run(orchestrator._run_engine_task(task))
    replay = asyncio.run(buffer.replay_after("r-1", 0))
    payload = next(
        event.payload for event in replay.events
        if event.payload["type"] == "engine_route"
    )

    assert payload["raw"] == raw
    assert payload["data"]["state"] == "WARN"
    assert payload["data"]["scope"] == "new_tasks"
    assert payload["data"]["allow_current_task_to_finish"] is True
