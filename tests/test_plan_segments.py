from __future__ import annotations

import asyncio
from pathlib import Path


def test_最长重叠去重并覆盖_json_token_中间断流():
    from app.plan.segments import merge_continuation

    assert merge_continuation("abcXYZ", "XYZdef") == "abcXYZdef"
    assert merge_continuation("abc", "def") == "abcdef"
    assert merge_continuation('{"title":"竞', '竞品"}') == '{"title":"竞品"}'
    assert merge_continuation('{"title":"竞', '品"}') == '{"title":"竞品"}'
    assert merge_continuation("完整前缀", "完整前缀加后缀") == "完整前缀加后缀"


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
        ResilienceConfig(3, 2, 60, 900, 300),
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


def test_段级重试次数由配置覆盖(tmp_path):
    import pytest

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
        ResilienceConfig(3, 4, 60, 900, 300),
    )

    with pytest.raises(PlanSegmentError, match="连续 4 次"):
        asyncio.run(workspace.generate("goal-1", "扩展 goal", adapter))

    assert adapter.calls == 4
    assert workspace.partial_path("goal-1").is_file()


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
    adapter = RoutedAdapter(adapters={"claude": claude, "codex": Codex()})
    adapter._route_overrides["r-3"] = "codex"

    result = asyncio.run(adapter.run_planning_segment(
        PlanningSegmentRequest("r-3", "skeleton", "生成骨架")
    ))

    assert result.completed is True
    assert claude.calls == 1


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
    assert result.text == '品"}' and result.completed is True
