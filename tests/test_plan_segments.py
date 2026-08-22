from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_最长重叠去重并覆盖_json_token_中间断流():
    from app.plan.segments import merge_continuation

    assert merge_continuation("abcXYZ", "XYZdef") == "abcXYZdef"
    assert merge_continuation("abc", "def") == "abcdef"
    assert merge_continuation('{"title":"竞', '竞品"}') == '{"title":"竞品"}'
    assert merge_continuation('{"title":"竞', '品"}') == '{"title":"竞品"}'
    assert merge_continuation("完整前缀", "完整前缀加后缀") == "完整前缀加后缀"


def test_同一轮增量_chunk_不得按重叠去重(tmp_path):
    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentWorkspace

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            del request
            await on_text('{"x":"')
            await on_text('"}')
            return PlanningSegmentResult(text='{"x":""}', completed=True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-chunks",
        ResilienceConfig(3, 60, 900),
    )

    value = asyncio.run(workspace.generate("goal-1", "生成 JSON", Adapter()))

    assert value == {"x": ""}


def test_partial_即时落盘_重试前清理并续写为正式段(tmp_path):
    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentWorkspace

    calls = []

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            partial = workspace.partial_path("skeleton")
            calls.append((request.continuation, partial.exists()))
            if len(calls) == 1:
                await on_text('{"goals":[{"title":"竞')
                assert partial.read_text(encoding="utf-8").endswith("竞")
                return PlanningSegmentResult(
                    text='{"goals":[{"title":"竞',
                    completed=False,
                    transport_interrupted=True,
                    error="stream disconnected",
                )
            await on_text('竞品"}]}')
            return PlanningSegmentResult(text='竞品"}]}', completed=True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-1",
        ResilienceConfig(2, 60, 900),
        retry_sleep=lambda seconds: asyncio.sleep(0),
    )
    value = asyncio.run(workspace.generate(
        "skeleton",
        "生成 goal 骨架",
        Adapter(),
    ))

    assert value == {"goals": [{"title": "竞品"}]}
    assert calls == [("", False), ('{"goals":[{"title":"竞', False)]
    assert not workspace.partial_path("skeleton").exists()
    assert workspace.formal_path("skeleton").is_file()


def test_规划短流接受完整_json_围栏() -> None:
    from app.plan.segments import _json_payload

    assert _json_payload('```json\n{"ok": true}\n```') == '{"ok": true}'


def test_段级重试次数由配置覆盖(tmp_path):

    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentError, PlanSegmentWorkspace

    class Adapter:
        calls = 0

        async def run_planning_segment(self, request, on_text=None):
            del request, on_text
            self.calls += 1
            return PlanningSegmentResult(
                text="{", completed=False, transport_interrupted=True
            )

    adapter = Adapter()
    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-2",
        ResilienceConfig(4, 60, 900),
        retry_sleep=lambda seconds: asyncio.sleep(0),
    )

    with pytest.raises(PlanSegmentError, match="连续 4 次"):
        asyncio.run(workspace.generate("goal-1", "扩展 goal", adapter))

    assert adapter.calls == 4
    assert workspace.partial_path("goal-1").is_file()


def test_同一段跨语义重跑也共享配置预算(tmp_path):
    import pytest

    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentError, PlanSegmentWorkspace

    class Adapter:
        calls = 0

        async def run_planning_segment(self, request, on_text=None):
            del request
            self.calls += 1
            await on_text("{}")
            return PlanningSegmentResult(text="{}", completed=True)

    adapter = Adapter()
    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-budget",
        ResilienceConfig(2, 60, 900),
    )

    assert asyncio.run(workspace.generate("goal-1", "第一次", adapter)) == {}
    assert asyncio.run(workspace.generate("goal-1", "第二次", adapter)) == {}
    with pytest.raises(PlanSegmentError, match="总尝试预算 2 次已耗尽"):
        asyncio.run(workspace.generate("goal-1", "第三次", adapter))
    assert adapter.calls == 2


def test_完整但非法_json_重试必须清空前缀而非误续写(tmp_path):
    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentWorkspace

    continuations = []

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            continuations.append(request.continuation)
            text = "{invalid" if len(continuations) == 1 else '{"ok":true}'
            await on_text(text)
            return PlanningSegmentResult(text=text, completed=True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-invalid-json",
        ResilienceConfig(2, 60, 900),
    )
    value = asyncio.run(workspace.generate("goal-1", "生成合法 JSON", Adapter()))

    assert value == {"ok": True}
    assert continuations == ["", ""]


def test_规划限流按配置退避且不把_429_当断流续写(tmp_path):
    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentWorkspace

    continuations = []
    delays = []

    async def retry_sleep(seconds):
        delays.append(seconds)

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            continuations.append(request.continuation)
            if len(continuations) == 1:
                return PlanningSegmentResult(
                    text="429",
                    completed=False,
                    cause="rate_limit",
                    error="API 429",
                )
            await on_text('{"ok":true}')
            return PlanningSegmentResult(text='{"ok":true}', completed=True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-plan-rate",
        ResilienceConfig(3, 60, 900),
        retry_sleep=retry_sleep,
    )

    assert asyncio.run(workspace.generate("goal-1", "生成", Adapter())) == {
        "ok": True
    }
    assert continuations == ["", ""]
    assert delays == [60]


def test_规划短流路由固定_claude_且忽略执行期覆盖():
    from app.adapters.contracts import PlanningSegmentRequest, PlanningSegmentResult
    from app.adapters.routing import RoutedAdapter

    class Claude:
        calls = 0

        async def generate_plan_segment(self, request, on_text=None):
            del request, on_text
            self.calls += 1
            return PlanningSegmentResult(text="{}", completed=True)

    class Codex:
        async def generate_plan_segment(self, request, on_text=None):
            del request, on_text
            raise AssertionError("规划短流不得进入 Codex")

    claude = Claude()
    adapter = RoutedAdapter(
        adapters={"claude": claude, "codex": Codex()},
    )
    adapter._route_overrides["r-3"] = "codex"

    result = asyncio.run(adapter.run_planning_segment(
        PlanningSegmentRequest("r-3", "skeleton", "生成骨架")
    ))

    assert result.completed is True
    assert claude.calls == 1


def test_通用运行入口的规划任务也固定_claude_且忽略所有覆盖(tmp_path):
    from types import SimpleNamespace

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.routing import RoutedAdapter

    calls = []

    class Claude:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            calls.append("claude")
            return SimpleNamespace(succeeded=True)

    class Codex:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            raise AssertionError("规划任务不得进入 Codex")

    adapter = RoutedAdapter(
        adapters={"claude": Claude(), "codex": Codex()},
    )
    adapter._route_overrides["r-plan-fixed"] = "codex"
    adapter.request_alternate("r-plan-fixed")
    task = EngineTask(
        body="规划", output_path=tmp_path / "plan.json", output_format="json",
        research_id="r-plan-fixed", goal_id="goal-1", agent_id="planner",
        agent_kind="planning", validators=["file_exists"],
        capability=Capability(), user_override="codex",
    )

    result = asyncio.run(adapter.run(task, object()))

    assert result.succeeded is True
    assert calls == ["claude"]


def test_claude_规划续写走_user_continuation_且不注入_assistant_role():
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import PlanningSegmentRequest

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, text):
            self.content = [TextBlock(text)]

    class ResultMessage:
        is_error = False
        api_error_status = None

    class Options:
        def __init__(self, **values):
            self.values = values

    class Client:
        prompt_items = []
        options = None

        def __init__(self, options):
            Client.options = options

        async def connect(self, prompt):
            Client.prompt_items = [item async for item in prompt]

        async def receive_response(self):
            yield AssistantMessage('品"}')
            yield ResultMessage()

        async def disconnect(self):
            pass

    class Sdk:
        pass

    Sdk.ClaudeAgentOptions = Options
    Sdk.ClaudeSDKClient = Client
    Sdk.AssistantMessage = AssistantMessage
    Sdk.ResultMessage = ResultMessage
    Sdk.TextBlock = TextBlock

    result = asyncio.run(ClaudeAdapter(sdk=Sdk).generate_plan_segment(
        PlanningSegmentRequest(
            "r-4",
            "goal-1",
            "只输出 JSON",
            continuation='{"title":"竞',
        )
    ))

    item = Client.prompt_items[0]
    assert item["message"]["role"] == "user"
    assert '{"title":"竞' in item["message"]["content"]
    assert Client.options.values["tools"] == []
    assert Client.options.values["include_partial_messages"] is True
    assert result.text == '品"}' and result.completed is True


def test_claude_规划短流从_stream_event_即时保留断流前缀():
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import PlanningSegmentRequest

    class StreamEvent:
        def __init__(self, text):
            self.event = {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text},
            }

    class Options:
        def __init__(self, **values):
            self.values = values

    class Client:
        def __init__(self, options):
            self.options = options

        async def connect(self, prompt):
            async for _ in prompt:
                pass

        async def receive_response(self):
            yield StreamEvent('{"title":"竞')
            yield StreamEvent("品")
            raise RuntimeError("Stream idle timeout")

        async def disconnect(self):
            pass

    class Sdk:
        pass

    Sdk.ClaudeAgentOptions = Options
    Sdk.ClaudeSDKClient = Client
    Sdk.StreamEvent = StreamEvent
    Sdk.AssistantMessage = type("AssistantMessage", (), {})
    Sdk.ResultMessage = type("ResultMessage", (), {})
    Sdk.TextBlock = type("TextBlock", (), {})
    captured = []

    result = asyncio.run(ClaudeAdapter(sdk=Sdk).generate_plan_segment(
        PlanningSegmentRequest("r-stream", "goal-1", "只输出 JSON"),
        on_text=captured.append,
    ))

    assert captured == ['{"title":"竞', "品"]
    assert result.text == '{"title":"竞品'
    assert result.transport_interrupted is True
    assert result.cause == "transport"


def test_非法json回灌自带出错位置原文与引号指引(tmp_path):
    """6b 实跑取证（2026-08-21 r-d7857eb04e56）：字符串内嵌未转义英文引号，
    回灌只有行列号，模型连拒三轮无从自纠。"""
    from app.adapters.contracts import PlanningSegmentResult
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentWorkspace

    prompts = []
    bad = '{"acceptance":["文档包含"飞书核心定位"章节"]}'

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            prompts.append(request.prompt)
            text = bad if len(prompts) == 1 else '{"ok":true}'
            await on_text(text)
            return PlanningSegmentResult(text=text, completed=True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-quote-json",
        ResilienceConfig(2, 60, 900),
    )
    value = asyncio.run(workspace.generate("goal-1", "生成合法 JSON", Adapter()))

    assert value == {"ok": True}
    retry_prompt = prompts[1]
    assert "出错位置附近原文" in retry_prompt
    assert "飞书核心定位" in retry_prompt  # 出错文本片段真的被带回
    assert "中文引号「」" in retry_prompt
