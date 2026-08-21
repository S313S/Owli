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


def test_m0_固定三卡已由计划驱动运行时替换():
    from app.orchestrator.mini import MiniOrchestrator
    from app.orchestrator.runtime import RuntimeCoordinator

    assert MiniOrchestrator is RuntimeCoordinator


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
        clock=lambda: 0.0,
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
    adapter = RoutedAdapter(clock=lambda: 0.0, adapters={
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


def test_执行期第三次传输故障让路并在真实探活后复位(tmp_path):
    import inspect
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig

    calls: list[str] = []
    observed: list[NormalizedEvent] = []
    probe_waiting = asyncio.Event()
    release_probe = asyncio.Event()

    async def controlled_sleep(seconds: float) -> None:
        assert seconds == 300
        probe_waiting.set()
        await release_probe.wait()

    class FakeAdapter:
        def __init__(self, name: str, *, transport: bool = False) -> None:
            self.name = name
            self.transport = transport
            self.probes = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            calls.append(self.name)
            if self.transport:
                event = NormalizedEvent(
                    engine=self.name,
                    thread_id="t",
                    turn_id="u",
                    item_kind=ItemKind.ERROR,
                    text="stream disconnected",
                    is_error=True,
                    raw={},
                    route_state="BACKOFF",
                    suspend_new_tasks=True,
                    cause="transport",
                )
                result = on_event(event)
                if inspect.isawaitable(result):
                    await result
                return SimpleNamespace(succeeded=False)
            return SimpleNamespace(succeeded=True)

        async def probe(self) -> bool:
            self.probes += 1
            return True

    claude = FakeAdapter("claude", transport=True)
    codex = FakeAdapter("codex")
    task = EngineTask(
        body="执行期任务",
        output_path=tmp_path / "result.md",
        output_format="markdown",
        research_id="r-trip",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(),
    )
    adapter = RoutedAdapter(
        clock=lambda: 0.0,
        adapters={"claude": claude, "codex": codex},
        resilience_config=ResilienceConfig(3, 3, 60, 900, 300),
        probe_sleep=controlled_sleep,
        backoff_sleep=lambda seconds: asyncio.sleep(0),
    )

    def observe(event):
        observed.append(event)
        if event.outcome == "PROBE_OK":
            raise RuntimeError("模拟健康事件展示回调失败")

    async def scenario() -> None:
        for _ in range(3):
            await adapter.run(task, object(), on_event=observe)
        assert calls == ["claude", "claude", "claude"]
        assert adapter.route_override == "codex"
        assert codex.probes == 1
        await adapter.run(task, object(), on_event=observe)
        assert calls[-1] == "codex"

        await probe_waiting.wait()
        release_probe.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert claude.probes == 1
        assert adapter.route_override is None

    asyncio.run(scenario())

    assert [event.outcome for event in observed if event.outcome] == [
        "ENGINE_DOWN", "PROBE_OK", "RESET",
    ]


def test_规划期和限流事件都不触发传输断路(tmp_path):
    import inspect
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig

    class FakeAdapter:
        def __init__(self, cause: str) -> None:
            self.cause = cause
            self.calls = 0
            self.probes = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            self.calls += 1
            event = NormalizedEvent(
                engine="claude", thread_id="t", turn_id="u",
                item_kind=ItemKind.ERROR, text=self.cause, is_error=True, raw={},
                route_state="BACKOFF", suspend_new_tasks=True, cause=self.cause,
            )
            result = on_event(event)
            if inspect.isawaitable(result):
                await result
            return SimpleNamespace(succeeded=False)

        async def probe(self) -> bool:
            self.probes += 1
            return True

    async def exercise(kind: str, cause: str) -> tuple[int, int, str | None]:
        async def no_wait(seconds: float) -> None:
            del seconds

        claude = FakeAdapter(cause)
        codex = FakeAdapter(cause)
        adapter = RoutedAdapter(
            clock=lambda: 0.0,
            adapters={"claude": claude, "codex": codex},
            resilience_config=ResilienceConfig(2, 3, 60, 900, 300),
            backoff_sleep=no_wait,
        )
        task = EngineTask(
            body="测试", output_path=tmp_path / f"{kind}-{cause}.json",
            output_format="json", research_id=f"r-{kind}-{cause}",
            goal_id="goal-1", agent_id="agent-1", agent_kind=kind,
            validators=["file_exists"], capability=Capability(),
        )
        for _ in range(4):
            await adapter.run(task, object())
        return claude.calls, codex.probes, adapter.route_override

    planning = asyncio.run(exercise("planning", "transport"))
    limited = asyncio.run(exercise("report", "rate_limit"))

    assert planning == (4, 0, None)
    assert limited == (4, 0, None)


def test_传输事件后本轮成功不得累计断路故障(tmp_path):
    import inspect
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig

    class Engine:
        probes = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            event = NormalizedEvent(
                engine="claude", thread_id="t", turn_id="u",
                item_kind=ItemKind.ERROR, text="瞬时断连后 SDK 已恢复",
                is_error=True, raw={}, route_state="BACKOFF",
                suspend_new_tasks=True, cause="transport",
            )
            emitted = on_event(event)
            if inspect.isawaitable(emitted):
                await emitted
            return SimpleNamespace(succeeded=True)

        async def probe(self):
            self.probes += 1
            return True

    async def no_wait(seconds):
        del seconds

    claude = Engine()
    codex = Engine()
    adapter = RoutedAdapter(
        clock=lambda: 0.0,
        adapters={"claude": claude, "codex": codex},
        resilience_config=ResilienceConfig(2, 3, 60, 900, 300),
        backoff_sleep=no_wait,
    )
    task = EngineTask(
        body="执行", output_path=tmp_path / "result.md", output_format="markdown",
        research_id="r-recovered", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"], capability=Capability(),
    )

    async def scenario():
        for _ in range(4):
            await adapter.run(task, object())

    asyncio.run(scenario())

    assert codex.probes == 0
    assert adapter.route_override is None


def test_并发传输故障只触发一次升级探测(tmp_path):
    import inspect
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig

    candidate_waiting = asyncio.Event()
    release_candidate = asyncio.Event()
    recovery_wait = asyncio.Event()
    observed = []

    class Failing:
        async def run(self, task, ctx, on_event=None):
            del task, ctx
            event = NormalizedEvent(
                engine="claude", thread_id="t", turn_id="u",
                item_kind=ItemKind.ERROR, text="stream disconnected",
                is_error=True, raw={}, route_state="BACKOFF",
                suspend_new_tasks=True, cause="transport",
            )
            emitted = on_event(event)
            if inspect.isawaitable(emitted):
                await emitted
            return SimpleNamespace(succeeded=False)

        async def probe(self):
            return True

    class Healthy:
        probes = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            return SimpleNamespace(succeeded=True)

        async def probe(self):
            self.probes += 1
            candidate_waiting.set()
            await release_candidate.wait()
            return True

    async def no_wait(seconds):
        del seconds

    async def wait_recovery(seconds):
        del seconds
        await recovery_wait.wait()

    healthy = Healthy()
    adapter = RoutedAdapter(
        clock=lambda: 0.0,
        adapters={"claude": Failing(), "codex": healthy},
        resilience_config=ResilienceConfig(1, 3, 60, 900, 300),
        backoff_sleep=no_wait,
        probe_sleep=wait_recovery,
    )
    task = EngineTask(
        body="执行", output_path=tmp_path / "result.md", output_format="markdown",
        research_id="r-concurrent", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"], capability=Capability(),
    )

    async def scenario():
        runs = [
            asyncio.create_task(adapter.run(task, object(), on_event=observed.append))
            for _ in range(2)
        ]
        await candidate_waiting.wait()
        release_candidate.set()
        await asyncio.gather(*runs)
        assert healthy.probes == 1
        assert sum(event.outcome == "ENGINE_DOWN" for event in observed) == 1
        recovery_wait.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_执行期会话停滞会中断_发取证事件并累计一次传输故障(tmp_path):
    import inspect
    from types import SimpleNamespace

    import pytest

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter, SessionStallError
    from app.config import ResilienceConfig

    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    observed = []

    def retry_event():
        return NormalizedEvent(
            engine="Claude", thread_id="r-stall", turn_id="turn-1",
            item_kind=ItemKind.THINKING, text="[session] api_retry",
            is_error=False, raw={}, outcome="API_RETRY",
        )

    class Stalled:
        interrupts = 0
        calls = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            self.calls += 1
            for elapsed in (0, 600):
                clock.value = (self.calls - 1) * 1000 + elapsed
                emitted = on_event(retry_event())
                if inspect.isawaitable(emitted):
                    await emitted
            return SimpleNamespace(succeeded=True)

        async def interrupt(self):
            self.interrupts += 1

        async def probe(self):
            return True

    class Healthy:
        probes = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            return SimpleNamespace(succeeded=True)

        async def probe(self):
            self.probes += 1
            return True

    async def no_wait(seconds):
        del seconds

    recovery_hold = asyncio.Event()

    async def hold_recovery(seconds):
        del seconds
        await recovery_hold.wait()

    stalled = Stalled()
    healthy = Healthy()
    adapter = RoutedAdapter(
        clock=clock,
        adapters={"claude": stalled, "codex": healthy},
        resilience_config=ResilienceConfig(3, 3, 60, 900, 300, 600),
        backoff_sleep=no_wait,
        probe_sleep=hold_recovery,
    )
    task = EngineTask(
        body="执行", output_path=tmp_path / "result.md", output_format="markdown",
        research_id="r-stall", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"], capability=Capability(),
    )

    async def scenario():
        for _ in range(3):
            with pytest.raises(SessionStallError):
                await adapter.run(task, object(), on_event=observed.append)

    asyncio.run(scenario())

    stalls = [event for event in observed if event.outcome == "SESSION_STALL"]
    assert stalled.interrupts == 3
    assert len(stalls) == 3
    assert stalls[0].raw["elapsed_seconds"] == 600
    assert stalls[0].raw["api_retry_count"] == 2
    assert all(event.cause == "transport" for event in stalls)
    assert adapter.route_override == "codex"
    assert healthy.probes == 1


def test_会话活动复位且限流与规划任务不触发停滞(tmp_path):
    import inspect
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig

    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()

    def event(kind, *, outcome=None, cause=None):
        return NormalizedEvent(
            engine="Claude", thread_id="r", turn_id="turn",
            item_kind=kind, text=str(outcome or kind.value), is_error=False,
            raw={}, outcome=outcome, cause=cause,
        )

    class Engine:
        def __init__(self, mode):
            self.mode = mode
            self.interrupts = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            sequences = {
                "activity": [
                    (0, event(ItemKind.THINKING, outcome="API_RETRY")),
                    (590, event(ItemKind.TOOL_CALL)),
                    (610, event(ItemKind.THINKING, outcome="API_RETRY")),
                    (1190, event(ItemKind.THINKING, outcome="API_RETRY")),
                ],
                "rate_limit": [
                    (0, event(ItemKind.THINKING, outcome="API_RETRY", cause="rate_limit")),
                    (1200, event(ItemKind.THINKING, outcome="API_RETRY", cause="rate_limit")),
                ],
                "planning": [
                    (0, event(ItemKind.THINKING, outcome="API_RETRY")),
                    (1200, event(ItemKind.THINKING, outcome="API_RETRY")),
                ],
            }
            for current, item in sequences[self.mode]:
                clock.value = current
                emitted = on_event(item)
                if inspect.isawaitable(emitted):
                    await emitted
            return SimpleNamespace(succeeded=True)

        async def interrupt(self):
            self.interrupts += 1

    async def run_case(mode, kind):
        claude = Engine(mode)
        adapter = RoutedAdapter(
            clock=clock,
            adapters={"claude": claude, "codex": Engine(mode)},
            resilience_config=ResilienceConfig(3, 3, 60, 900, 300, 600),
        )
        task = EngineTask(
            body="测试", output_path=tmp_path / f"{mode}.json",
            output_format="json", research_id=f"r-{mode}", goal_id="goal-1",
            agent_id="agent-1", agent_kind=kind, validators=["file_exists"],
            capability=Capability(),
        )
        result = await adapter.run(task, object())
        return result.succeeded, claude.interrupts, adapter.route_override

    assert asyncio.run(run_case("activity", "report")) == (True, 0, None)
    assert asyncio.run(run_case("rate_limit", "report")) == (True, 0, None)
    assert asyncio.run(run_case("planning", "planning")) == (True, 0, None)


def test_会话停滞超时值配置覆盖到_routed_adapter(tmp_path):
    import inspect
    from types import SimpleNamespace

    import pytest

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter, SessionStallError
    from app.config import ResilienceConfig

    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()

    class Engine:
        interrupts = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            for current in (0, 119, 120):
                clock.value = current
                item = NormalizedEvent(
                    engine="Claude", thread_id="r", turn_id="t",
                    item_kind=ItemKind.THINKING, text="api_retry",
                    is_error=False, raw={}, outcome="API_RETRY",
                )
                emitted = on_event(item)
                if inspect.isawaitable(emitted):
                    await emitted
            return SimpleNamespace(succeeded=True)

        async def interrupt(self):
            self.interrupts += 1

        async def probe(self):
            return False

    async def no_wait(seconds):
        del seconds

    engine = Engine()
    adapter = RoutedAdapter(
        clock=clock,
        adapters={"claude": engine, "codex": Engine()},
        resilience_config=ResilienceConfig(3, 3, 60, 900, 300, 120),
        backoff_sleep=no_wait,
    )
    task = EngineTask(
        body="执行", output_path=tmp_path / "result.json", output_format="json",
        research_id="r-config-stall", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"], capability=Capability(),
    )

    with pytest.raises(SessionStallError):
        asyncio.run(adapter.run(task, object()))
    assert engine.interrupts == 1


def test_claude_probe_只认真实模型健康标记且不用工具():
    from app.adapters.claude import ClaudeAdapter

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, text):
            self.content = [TextBlock(text)]

    class ResultMessage:
        def __init__(self):
            self.result = ""
            self.is_error = False
            self.api_error_status = None

    class Client:
        last_options = None

        def __init__(self, options):
            Client.last_options = options

        async def connect(self, prompt):
            self.prompt = [item async for item in prompt]

        async def receive_response(self):
            yield AssistantMessage("OWLI_HEALTHY")
            yield ResultMessage()

        async def disconnect(self):
            pass

    class Options:
        def __init__(self, **values):
            self.values = values

    class Sdk:
        pass

    Sdk.ClaudeSDKClient = Client
    Sdk.ClaudeAgentOptions = Options
    Sdk.AssistantMessage = AssistantMessage
    Sdk.ResultMessage = ResultMessage
    Sdk.TextBlock = TextBlock

    healthy = asyncio.run(ClaudeAdapter(sdk=Sdk).probe())

    assert healthy is True
    assert Client.last_options.values["tools"] == []
    assert Client.last_options.values["allowed_tools"] == []
    assert Client.last_options.values["permission_mode"] == "dontAsk"


def test_codex_probe_按结构化输出而非退出码判健康(tmp_path):
    from app.adapters.codex import CodexAdapter

    executable = tmp_path / "fake-codex-probe"
    executable.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({'type': 'item.completed', 'item': {
    'type': 'agent_message', 'text': 'OWLI_HEALTHY'
}}))
raise SystemExit(7)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    healthy = asyncio.run(CodexAdapter(
        executable=str(executable),
        codex_home=tmp_path / "codex-home",
    ).probe())

    assert healthy is True


def test_限流退避由适配层等待且不触发断路(tmp_path):
    import inspect
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig

    calls = []
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def backoff_sleep(seconds):
        assert seconds == 60
        waiting.set()
        await release.wait()

    class Engine:
        async def run(self, task, ctx, on_event=None):
            del task, ctx
            calls.append("claude")
            if len(calls) == 1:
                event = NormalizedEvent(
                    engine="claude", thread_id="t", turn_id="u",
                    item_kind=ItemKind.ERROR, text="429", is_error=True,
                    raw={"api_error_status": 429}, route_state="BACKOFF",
                    suspend_new_tasks=True, cause="rate_limit",
                )
                result = on_event(event)
                if inspect.isawaitable(result):
                    await result
            return SimpleNamespace(succeeded=True)

    class Other:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            calls.append("codex")
            return SimpleNamespace(succeeded=True)

    task = EngineTask(
        body="执行", output_path=tmp_path / "result.md", output_format="markdown",
        research_id="r-backoff", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"], capability=Capability(),
    )
    adapter = RoutedAdapter(
        clock=lambda: 0.0,
        adapters={"claude": Engine(), "codex": Other()},
        resilience_config=ResilienceConfig(3, 3, 60, 900, 300),
        backoff_sleep=backoff_sleep,
    )

    async def scenario():
        await adapter.run(task, object())
        await waiting.wait()
        second = asyncio.create_task(adapter.run(task, object()))
        await asyncio.sleep(0)
        assert calls == ["claude"]
        release.set()
        await second

    asyncio.run(scenario())

    assert calls == ["claude", "claude"]
    assert adapter.route_override is None


def test_人工切换请求只在适配层按_agent_尝试数生效(tmp_path):
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.routing import RoutedAdapter

    calls = []

    class Engine:
        def __init__(self, name):
            self.name = name

        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            calls.append(self.name)
            return SimpleNamespace(succeeded=True)

    task = EngineTask(
        body="执行", output_path=tmp_path / "result.md", output_format="markdown",
        research_id="r-manual", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"], capability=Capability(),
    )
    adapter = RoutedAdapter(clock=lambda: 0.0, adapters={
        "claude": Engine("claude"), "codex": Engine("codex"),
    })
    adapter.request_alternate("r-manual", agent_id="agent-1", after_attempt=1)

    asyncio.run(adapter.run(task, object()))
    asyncio.run(adapter.run(task, object()))

    assert calls == ["claude", "codex"]


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


def test_claude_真实执行流异常会投影传输_backoff_事件(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import EngineTask

    class Client:
        def __init__(self, options):
            self.options = options

        async def connect(self, prompt):
            async for _ in prompt:
                pass

        async def receive_response(self):
            if False:
                yield None
            raise RuntimeError("Stream idle timeout")

        async def disconnect(self):
            pass

    class Options:
        def __init__(self, **values):
            self.values = values

    class Sdk:
        ClaudeSDKClient = Client
        ClaudeAgentOptions = Options
        ResultMessage = type("ResultMessage", (), {})
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
            "HookMatcher", (),
            {"__init__": lambda self, matcher=None, hooks=None: None},
        )

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-stream" / "goals" / "goal-1" / "result.md"
    task = EngineTask(
        body="执行", output_path=output_path, output_format="markdown",
        research_id="r-stream", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    observed = []

    result = asyncio.run(ClaudeAdapter(
        sdk=Sdk, log_root=tmp_path / "logs"
    ).run(task, _ctx(validation, output_path), on_event=observed.append))

    transport = [event for event in observed if event.cause == "transport"]
    assert result.succeeded is False
    assert len(transport) == 1
    assert transport[0].route_state == "BACKOFF"


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


def test_runtime_defensive_raw_copy_preserves_nested_rate_limit_sample():
    from app.orchestrator.runtime import RuntimeCoordinator

    raw = {"type": "rate_limit_event", "nested": {"kept": [1, 2, 3]}}
    runtime = object.__new__(RuntimeCoordinator)

    assert runtime._plain(raw) == raw
