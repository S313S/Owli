import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeStore:
    def __init__(self, values=None, error: Exception | None = None):
        self.values = values or {}
        self.error = error

    def read_validation_path(self, path: str, report_id: str):
        if self.error:
            raise self.error
        return self.values.get((report_id, path))


@pytest.fixture
def validation_env(tmp_path, monkeypatch):
    from app.adapters import validation

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "artifact.json"
    output_path.parent.mkdir(parents=True)
    return validation, output_path


def make_ctx(validation, output_path: Path, *, output_format="json", store=None):
    cache = {}

    def read_text():
        if "text" not in cache:
            cache["text"] = output_path.read_text(encoding="utf-8")
        return cache["text"]

    def read_json():
        if "json" not in cache:
            cache["json"] = json.loads(read_text())
        return cache["json"]

    return validation.Ctx(
        output_path=output_path,
        output_format=output_format,
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        read_text=read_text,
        read_json=read_json,
        store=store,
        source_domains=frozenset(),
    )


def test_三态严格区分_agent_失败与校验器不可用(validation_env):
    validation, output_path = validation_env
    ctx = make_ctx(validation, output_path)

    missing = validation.validate(ctx, ["file_exists"])
    stub = validation.validate(ctx, ["xlsx_sheets_exact:摘要"])
    unknown = validation.validate(ctx, ["invented_validator"])

    assert missing.verdict is validation.Verdict.FAIL
    assert stub.verdict is validation.Verdict.FAIL  # 隐式 file_exists 先失败
    assert unknown.verdict is validation.Verdict.FAIL

    output_path.write_text("[]", encoding="utf-8")
    stub = validation.validate(ctx, ["xlsx_sheets_exact:摘要"])
    unknown = validation.validate(ctx, ["invented_validator"])
    assert stub.verdict is validation.Verdict.UNAVAILABLE
    assert unknown.verdict is validation.Verdict.UNAVAILABLE
    assert "尚未实现" in stub.results[-1].message
    assert "不在封闭注册表" in unknown.results[-1].message


def test_file_exists_检查非空与白名单并且失败时短路(validation_env):
    validation, output_path = validation_env
    output_path.write_text("", encoding="utf-8")
    ctx = make_ctx(validation, output_path)

    report = validation.validate(ctx, ["each_item_has:id", "file_exists"])

    assert report.verdict is validation.Verdict.FAIL
    assert len(report.results) == 1
    assert "空文件" in report.results[0].message

    outside = output_path.parents[3] / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    outside_report = validation.validate(make_ctx(validation, outside), ["file_exists"])
    assert outside_report.verdict is validation.Verdict.FAIL
    assert str(outside.resolve()) in outside_report.results[0].offenders


def test_json_数组数量与字段校验一次返回三条失败(validation_env):
    validation, output_path = validation_env
    output_path.write_text(
        json.dumps([{"id": "", "score": 0, "enabled": False}], ensure_ascii=False),
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path)

    report = validation.validate(
        ctx,
        [
            "json_array_min_items:3",
            "each_item_has:id",
            "each_item_has:extra.permalink",
        ],
    )

    assert report.verdict is validation.Verdict.FAIL
    assert len(report.failures) == 3
    assert len([result for result in report.results if result.verdict is validation.Verdict.FAIL]) == 3


def test_计划生成器默认评级校验器可真实执行(validation_env):
    validation, output_path = validation_env
    rated = {
        "score_authority": 2,
        "score_freshness": 1,
        "score_crossref": 2,
        "score_completeness": 1,
        "score_independence": 0,
        "rating_notes": "权威2:官方文档 · 时效1:历史页面 · 交叉2:多源一致 · 完整1:部分字段 · 无关0:厂商自述",
        "rated_by": "reliability-auditor",
    }
    output_path.write_text(json.dumps([rated], ensure_ascii=False), encoding="utf-8")
    rating = validation.validate(make_ctx(validation, output_path), ["no_item_missing_rating"])
    assert rating.verdict is validation.Verdict.PASS

    invalid = {**rated, "score_authority": True, "score_freshness": 7, "score_crossref": "2"}
    output_path.write_text(json.dumps([invalid], ensure_ascii=False), encoding="utf-8")
    rejected = validation.validate(make_ctx(validation, output_path), ["no_item_missing_rating"])
    assert rejected.verdict is validation.Verdict.FAIL
    assert set(rejected.failures[0].offenders) >= {
        "items[0].score_authority", "items[0].score_freshness", "items[0].score_crossref",
    }


def test_rating_notes_正则校验器已注册且逐条执行(validation_env):
    validation, output_path = validation_env
    valid = {
        "score_authority": 2,
        "score_freshness": 1,
        "score_crossref": 0,
        "score_completeness": 2,
        "score_independence": 1,
        "rating_notes": "权威2:官方文档 · 时效1:历史页面 · 交叉0:孤证 · 完整2:全文可读 · 无关1:厂商自述",
    }
    output_path.write_text(json.dumps([valid, valid], ensure_ascii=False), encoding="utf-8")
    passed = validation.validate(
        make_ctx(validation, output_path),
        ["rating_notes_matches_regex", "rating_notes_scores_match_columns"],
    )
    assert passed.verdict is validation.Verdict.PASS

    invalid = {**valid, "rating_notes": "权威2:可能是官方 · 时效1:历史页面 · 交叉0:孤证 · 完整2:全文可读 · 无关1:厂商自述"}
    output_path.write_text(json.dumps([invalid], ensure_ascii=False), encoding="utf-8")
    failed = validation.validate(make_ctx(validation, output_path), ["rating_notes_matches_regex"])
    assert failed.verdict is validation.Verdict.FAIL
    assert failed.failures[0].offenders == ["items[0].rating_notes"]


def test_each_item_has_把容器和空串判空但保留零与_false(validation_env):
    validation, output_path = validation_env
    output_path.write_text(
        json.dumps(
            [
                {"zero": 0, "flag": False, "none": None},
                {"zero": 0, "flag": False, "none": []},
            ]
        ),
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path)

    valid = validation.validate(ctx, ["each_item_has:zero,flag"])
    invalid = validation.validate(ctx, ["each_item_has:none"])

    assert valid.verdict is validation.Verdict.PASS
    assert invalid.verdict is validation.Verdict.FAIL
    assert len(invalid.results[-1].offenders) == 2


def test_json_顶层不是数组与损坏_json_属于_agent_失败(validation_env):
    validation, output_path = validation_env
    output_path.write_text('{"id": 1}', encoding="utf-8")
    ctx = make_ctx(validation, output_path)
    assert validation.validate(ctx, ["json_array_min_items:1"]).verdict is validation.Verdict.FAIL

    output_path.write_text("{", encoding="utf-8")
    broken_ctx = make_ctx(validation, output_path)
    assert validation.validate(
        broken_ctx, ["json_array_min_items:1"]
    ).verdict is validation.Verdict.FAIL


def test_sections_exist_含单数别名并要求正文非空(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 执行摘要\n有内容\n\n## 空章节\n\n## 信息源清单\n[S01] https://example.com\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    plural = validation.validate(ctx, ["sections_exist:执行摘要,空章节,缺失章节"])
    alias = validation.validate(ctx, ["section_exists:执行摘要"])

    assert plural.verdict is validation.Verdict.FAIL
    assert set(plural.results[-1].offenders) == {"空章节", "缺失章节"}
    assert alias.verdict is validation.Verdict.PASS
    assert alias.results[-1].detail["alias_of"] == "sections_exist"


def test_sections_exist_父章节只有子标题正文也算非空(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n\n## Slack\n- 结论 [S01]\n\n# 信息源\n- [S01] https://example.com\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(ctx, ["sections_exist:结论,信息源"])

    assert report.verdict is validation.Verdict.PASS


def test_信息源标题与内联链接风格的双向校验通过(validation_env):
    # r-7497a1d65adb 实锤：章节叫「信息源」（与 sections_exist 同口径）且正文
    # 内联 [链接](url) [Sxx] 风格时，不得落入「行内 URL=清单行」启发式误判。
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n- 结论甲 [HN 讨论](https://news.ycombinator.com/item?id=1) [S01]\n"
        "- 结论乙 [HN 讨论](https://news.ycombinator.com/item?id=2) [S02]\n\n"
        "## 信息源\n- [S01] [标题一](https://news.ycombinator.com/item?id=1)\n"
        "- [S02] [标题二](https://news.ycombinator.com/item?id=2)\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )

    assert report.verdict is validation.Verdict.PASS


def test_角标双向校验分别报告编造与孤立(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n结论甲 [S01]，结论乙 [S03]。\n\n"
        "## 信息源清单\n- [S01] https://example.com/1\n- [S02] https://example.com/2\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )

    assert report.verdict is validation.Verdict.FAIL
    assert len(report.failures) == 2
    assert report.failures[0].offenders == ["[S03]"]
    assert report.failures[1].offenders == ["[S02]"]


def test_角标双向校验不允许两个空集合冒充通过(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n只有无引用结论。\n\n# 信息源\n暂无。\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )

    assert report.verdict is validation.Verdict.FAIL
    assert len(report.failures) == 2
    assert "正文未找到任何" in report.failures[0].message
    assert "信息源清单未找到任何" in report.failures[1].message


def test_每条列表结论都必须带角标(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n- 有证据的结论 [S01]\n- 没有证据的结论\n\n"
        "# 信息源\n- [S01] https://example.com/1\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )

    assert report.verdict is validation.Verdict.FAIL
    assert report.failures[0].offenders == ["没有证据的结论"]


def test_结论必须使用可逐条校验的_Markdown_列表(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n第一段有证据 [S01]。\n\n第二段没有证据。\n\n"
        "# 信息源\n- [S01] https://example.com/1\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )

    assert report.verdict is validation.Verdict.FAIL
    assert "Markdown 列表" in report.failures[0].message


def test_结论容器列表项可以由带角标的子项支撑(validation_env):
    validation, output_path = validation_env
    output_path = output_path.with_suffix(".md")
    output_path.write_text(
        "# 结论\n- 缺点：\n  - 子结论 [S01]\n\n"
        "# 信息源\n- [S01] https://example.com/1\n",
        encoding="utf-8",
    )
    ctx = make_ctx(validation, output_path, output_format="markdown")

    report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )

    assert report.verdict is validation.Verdict.PASS


def test_db_row_exists_走固定读接口并区分空值与读库失败(validation_env):
    validation, output_path = validation_env
    output_path.write_text("[]", encoding="utf-8")

    found_ctx = make_ctx(
        validation,
        output_path,
        store=FakeStore({("research-1", "reports.extra.claims"): ["c-1"]}),
    )
    empty_ctx = make_ctx(
        validation,
        output_path,
        store=FakeStore({("research-1", "reports.extra.claims"): []}),
    )
    broken_ctx = make_ctx(
        validation, output_path, store=FakeStore(error=OSError("数据库离线"))
    )

    assert validation.validate(
        found_ctx, ["db_row_exists:reports.extra.claims"]
    ).verdict is validation.Verdict.PASS
    assert validation.validate(
        empty_ctx, ["db_row_exists:reports.extra.claims"]
    ).verdict is validation.Verdict.FAIL
    assert validation.validate(
        broken_ctx, ["db_row_exists:reports.extra.claims"]
    ).verdict is validation.Verdict.UNAVAILABLE


def test_store_提供封闭的校验读取接口(tmp_path):
    import sqlite3
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    schema_path = ROOT / "app" / "store" / "schema.sql"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
    store = Store(database_path)
    store.create_report(
        id="research-1",
        title="测试",
        research_question="验证固定读接口",
        created_at="2026-08-18T12:00:00+08:00",
        extra={"claims": [{"id": "c-1"}]},
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO report_tags (report_id, tag, created_at) VALUES (?, ?, ?)",
            ("research-1", "竞品", "2026-08-18T12:00:00+08:00"),
        )

    assert store.read_validation_path(
        "reports.extra.claims", "research-1"
    ) == [{"id": "c-1"}]
    assert store.read_validation_path("report_tags", "research-1") == ["竞品"]
    with pytest.raises(ValueError, match="不支持"):
        store.read_validation_path("evidence; DROP TABLE reports", "research-1")


def test_注册表是含别名的_29_个封闭名字(validation_env):
    """28 + sectioned_document_valid（M4-a，validator-registry §2.2b）。"""
    validation, _ = validation_env
    assert len(validation.REGISTRY) == 29
    assert "section_exists" in validation.REGISTRY
    assert "xlsx_sheets_exact" in validation.REGISTRY
    assert "sectioned_document_valid" in validation.REGISTRY


def test_owli_result_解析最后一个结论块并校验字段(validation_env):
    from app.adapters.claude import OwliResultError, parse_owli_result

    text = """过程输出
```json owli-result
{"status":"partial","output_path":"runs/research-1/goals/goal-1/a.md","summary":"初稿","assumptions":[],"unmet":["缺一项"],"capability_denials":[],"reason":"empty_result"}
```
"""
    result = parse_owli_result(text)
    assert result.status == "partial"
    assert result.unmet == ["缺一项"]

    with pytest.raises(OwliResultError, match="未找到"):
        parse_owli_result("自然语言宣布完成")

    invalid = text.replace('"unmet":["缺一项"]', '"unmet":[]')
    with pytest.raises(OwliResultError, match="unmet"):
        parse_owli_result(invalid)


def test_conclusion_invalid只属于系统账本原因而非agent自报原因():
    from app.adapters.claude import OwliResultError, parse_owli_result

    schema = json.loads(
        (ROOT / "app/prompts/common/owli-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "conclusion_invalid" not in schema["properties"]["reason"]["enum"]

    agent_result = """```json owli-result
{"status":"partial","output_path":"runs/r/goals/goal-1/a.md","summary":"结论块异常","assumptions":[],"unmet":["结论块不合法"],"capability_denials":[],"reason":"conclusion_invalid"}
```"""
    with pytest.raises(OwliResultError, match="reason 不在缺失原因闭集"):
        parse_owli_result(agent_result)


def test_claude_选项强制隔离设置并用_disallowed_tools_收敛白名单(validation_env):
    from app.adapters.claude import ClaudeTask, build_claude_options

    class FakeOptions:
        def __init__(self, **values):
            self.values = values

    class FakeHookMatcher:
        def __init__(self, *, matcher=None, hooks=None):
            self.matcher = matcher
            self.hooks = hooks or []

    class FakeSdk:
        ClaudeAgentOptions = FakeOptions
        HookMatcher = FakeHookMatcher

    _, output_path = validation_env
    task = ClaudeTask(
        body="执行任务",
        output_path=output_path,
        output_format="json",
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        validators=["file_exists"],
        tools=frozenset({"Read", "Write"}),
    )
    options = build_claude_options(task, lambda *args: None, sdk=FakeSdk)

    assert options.values["setting_sources"] == []
    assert options.values["permission_mode"] == "dontAsk"
    assert options.values["tools"] == ["Read", "Write"]
    assert options.values["allowed_tools"] == ["Read", "Write"]
    assert "Bash" in options.values["disallowed_tools"]
    assert "Read" not in options.values["disallowed_tools"]
    assert "Write" not in options.values["disallowed_tools"]
    assert options.values["can_use_tool"] is not None
    assert len(options.values["hooks"]["PreToolUse"]) == 1


def test_can_use_tool_拒绝越界写入并指出路径(validation_env):
    import asyncio
    from app.adapters.claude import ClaudeTask, make_permission_callback

    class Allow:
        pass

    class Deny:
        def __init__(self, *, message):
            self.message = message

    class FakeSdk:
        PermissionResultAllow = Allow
        PermissionResultDeny = Deny

    _, output_path = validation_env
    task = ClaudeTask(
        body="执行任务",
        output_path=output_path,
        output_format="json",
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        validators=["file_exists"],
        tools=frozenset({"Write"}),
    )
    denials = []
    callback = make_permission_callback(task, denials, sdk=FakeSdk)
    denied = asyncio.run(
        callback("Write", {"file_path": str(output_path.parents[3] / "outside.txt")}, None)
    )
    allowed = asyncio.run(callback("Write", {"file_path": str(output_path)}, None))

    assert isinstance(denied, Deny)
    assert "越界" in denied.message
    assert str(output_path.parents[3] / "outside.txt") in denied.message
    assert denials == [str(output_path.parents[3] / "outside.txt")]
    assert isinstance(allowed, Allow)


def test_claude_结构化输出协议工具不占用业务capability(validation_env):
    import asyncio
    from app.adapters.claude import ClaudeTask, make_permission_callback

    class Allow:
        pass

    class Deny:
        def __init__(self, *, message):
            self.message = message

    class FakeSdk:
        PermissionResultAllow = Allow
        PermissionResultDeny = Deny

    _, output_path = validation_env
    task = ClaudeTask(
        body="执行任务",
        output_path=output_path,
        output_format="json",
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        validators=["file_exists"],
        tools=frozenset({"Write"}),
    )
    denials = []
    callback = make_permission_callback(task, denials, sdk=FakeSdk)

    decision = asyncio.run(callback("StructuredOutput", {"answer": "完成"}, None))

    assert isinstance(decision, Allow)
    assert denials == []


def test_PreToolUse_即使工具已预批准也强制复核写入路径(validation_env):
    import asyncio
    from app.adapters.claude import (
        ClaudeTask,
        build_claude_options,
        make_permission_callback,
    )

    class Allow:
        pass

    class Deny:
        def __init__(self, *, message):
            self.message = message

    class FakeOptions:
        def __init__(self, **values):
            self.values = values

    class FakeHookMatcher:
        def __init__(self, *, matcher=None, hooks=None):
            self.matcher = matcher
            self.hooks = hooks or []

    class FakeSdk:
        PermissionResultAllow = Allow
        PermissionResultDeny = Deny
        ClaudeAgentOptions = FakeOptions
        HookMatcher = FakeHookMatcher

    _, output_path = validation_env
    task = ClaudeTask(
        body="执行任务",
        output_path=output_path,
        output_format="json",
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        validators=["file_exists"],
        tools=frozenset({"Write"}),
    )
    denials = []
    callback = make_permission_callback(task, denials, sdk=FakeSdk)
    options = build_claude_options(task, callback, sdk=FakeSdk)
    hook = options.values["hooks"]["PreToolUse"][0].hooks[0]

    outside = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(output_path.parents[3] / "outside.txt")},
            },
            "tool-1",
            {"signal": None},
        )
    )
    inside = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(output_path)},
            },
            "tool-2",
            {"signal": None},
        )
    )

    assert outside["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "越界" in outside["hookSpecificOutput"]["permissionDecisionReason"]
    assert inside["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_common_prompt_总是在任务正文之前(validation_env):
    from app.adapters.claude import compose_prompt

    prompt = compose_prompt("只写这一份产物")
    assert prompt.startswith("你是 Owli 市场调研系统中的一个执行 agent。")
    assert prompt.endswith("（以下为本次任务的具体指令）\n只写这一份产物")


def test_claude_sdk_流式读取后以结论块加产物校验判成功(validation_env):
    import asyncio
    from types import SimpleNamespace
    from app.adapters.claude import ClaudeAdapter, ClaudeTask

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, result="", is_error=False):
            self.result = result
            self.is_error = is_error

    class FakeClient:
        instance = None

        def __init__(self, options):
            self.options = options
            self.prompt = ""
            self.interrupted = False
            FakeClient.instance = self

        async def connect(self, prompt_stream):
            async for message in prompt_stream:
                self.prompt += message["message"]["content"]

        async def receive_response(self):
            yield AssistantMessage([TextBlock(self.sdk_text)])
            yield ResultMessage(is_error=True)  # 引擎事件不能代替双保险判定。

        async def interrupt(self):
            self.interrupted = True

        async def disconnect(self):
            pass

    class FakeOptions:
        def __init__(self, **values):
            self.values = values

    FakeSdk = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ClaudeSDKClient=FakeClient,
        AssistantMessage=AssistantMessage,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        ToolUseBlock=type("ToolUseBlock", (), {}),
        UserMessage=type("UserMessage", (), {}),
        SystemMessage=type("SystemMessage", (), {}),
        PermissionResultAllow=type("Allow", (), {}),
        PermissionResultDeny=type("Deny", (), {"__init__": lambda self, **kw: None}),
        HookMatcher=type(
            "HookMatcher",
            (),
            {
                "__init__": lambda self, matcher=None, hooks=None: (
                    setattr(self, "matcher", matcher),
                    setattr(self, "hooks", hooks or []),
                )[-1],
            },
        ),
    )

    validation, output_path = validation_env
    output_path.write_text("[]", encoding="utf-8")
    FakeClient.sdk_text = f"""完成
```json owli-result
    {{"status":"done","output_path":"{output_path}","summary":"完成","assumptions":[],"unmet":[],"capability_denials":[],"reason":null}}
```"""
    task = ClaudeTask(
        body="写入产物",
        output_path=output_path,
        output_format="json",
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        validators=["file_exists"],
        tools=frozenset({"Read", "Write"}),
    )
    ctx = make_ctx(validation, output_path)
    adapter = ClaudeAdapter(sdk=FakeSdk)
    result = asyncio.run(adapter.run(task, ctx))

    assert result.succeeded
    assert result.validation.verdict is validation.Verdict.PASS
    assert result.conclusion.status == "done"
    assert result.events[-1].is_error is True
    assert FakeClient.instance.prompt.startswith("你是 Owli")

    outside = output_path.parents[3] / "outside.json"
    FakeClient.sdk_text = FakeClient.sdk_text.replace(str(output_path), str(outside))
    outside_result = asyncio.run(ClaudeAdapter(sdk=FakeSdk).run(task, ctx))
    assert outside_result.validation.verdict is validation.Verdict.FAIL
    assert str(outside) in outside_result.validation.failures[-1].offenders
