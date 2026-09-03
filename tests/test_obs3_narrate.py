"""§OBS-3 货 1–3：进程栏翻译层（原始事件 → 人话行）。

判据 1 的硬丢弃规则与判据 3 的两引擎覆盖都锁在这里。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.observability.narrate import (
    MAX_TEXT,
    narrate_lines,
    narrate_record,
    unmatched_kinds,
)


def _record(event: object, seq: int = 1, engine: str = "Claude") -> dict:
    return {"ts": 1_700_000_000.0, "seq": seq, "engine": engine, "event": event}


def test_系统初始化块与限额心跳不进进程栏() -> None:
    init = _record({"subtype": "init", "data": {"type": "system", "tools": ["Read"]}})
    quota = _record({"rate_limit_info": {"status": "allowed"}, "session_id": "s"})
    assert narrate_record(init) == []
    assert narrate_record(quota) == []


def test_思考块只取正文签名串一个字都不出() -> None:
    event = {"content": [{"thinking": "先看采集卡再动手", "signature": "EsoqCqgB" * 500}]}
    (line,) = narrate_record(_record(event))
    assert line.stage == "思考"
    assert line.text == "先看采集卡再动手"
    assert "Esoq" not in line.text


def test_工具调用只出名字与参数摘要不倒请求体() -> None:
    event = {"content": [{
        "id": "toolu_1", "name": "Read",
        "input": {"file_path": "/tmp/runs/r-1/goals/goal-1/采集卡.md", "content": "x" * 5000},
    }]}
    (line,) = narrate_record(_record(event))
    assert line.text == "调用 Read（采集卡.md）"
    assert "xxxx" not in line.text


def test_工具返回能数条数就出条数() -> None:
    event = {"content": [{
        "tool_use_id": "toolu_1", "is_error": None,
        "content": json.dumps({"rows": [{"a": 1}, {"a": 2}, {"a": 3}]}),
    }]}
    (line,) = narrate_record(_record(event))
    assert line.text == "取到 3 条" and line.count == 3


def test_写文件出文件名与字数() -> None:
    event = {
        "content": [{"tool_use_id": "t", "is_error": None, "content": "File created"}],
        "tool_use_result": {"type": "create", "filePath": "/x/goals/goal-1/sec-1.md",
                            "content": "正文" * 100},
    }
    (line,) = narrate_record(_record(event))
    assert line.stage == "写入产物" and line.text == "写好 sec-1.md，约 200 字"


def test_收尾事件出模型自己写的摘要不出token明细() -> None:
    event = {"subtype": "success", "duration_ms": 197_243, "is_error": False,
             "usage": {"input_tokens": 3, "cache_read_input_tokens": 60_943},
             "structured_output": {"status": "done", "summary": "写完 sec-1 第 1/3 片"}}
    (line,) = narrate_record(_record(event))
    assert line.stage == "本节完成"
    assert line.text == "写完 sec-1 第 1/3 片（耗时 197s）"
    assert "60" not in line.text and "token" not in line.text


def test_正文里塞了JSON信封只抽人话不倒串() -> None:
    envelope = json.dumps({"markdown": "## 结论\n- 用户反馈稳定性不足", "claims": [1, 2]})
    (line,) = narrate_record(_record({"content": [{"text": envelope}]}))
    assert line.text.startswith("## 结论")
    assert "claims" not in line.text


# ---- Codex 侧：无真机 transcript 样本，按 tests 里既有真机 JSONL 形状逐形态锁 ----


def _codex(event: object, seq: int = 1) -> dict:
    return {"ts": 1_700_000_000.0, "seq": seq, "engine": "Codex", "event": event}


def test_codex_起手事件不出行() -> None:
    assert narrate_record(_codex({"type": "thread.started", "thread_id": "t-1"})) == []
    assert narrate_record(_codex({"type": "turn.started", "turn_id": "u-1"})) == []
    assert narrate_record(_codex({"type": "item.started", "item": {"type": "reasoning"}})) == []


def test_codex_各item形态都译得出() -> None:
    cases = {
        "agent_message": ({"type": "agent_message", "text": "已写完这一片"}, "说明", "已写完这一片"),
        "reasoning": ({"type": "reasoning", "text": "先核证据池"}, "思考", "先核证据池"),
        "web_search": ({"type": "web_search", "query": "WorkBuddy 口碑"}, "调用工具",
                       "调用 网页搜索（WorkBuddy 口碑）"),
        "mcp_tool_call": ({"type": "mcp_tool_call", "tool": "collect_xhs",
                           "arguments": {"keyword": "WorkBuddy", "limit": 20}}, "调用工具",
                          "调用 collect_xhs（WorkBuddy · 20）"),
        "file_change": ({"type": "file_change", "changes": [{"path": "/x/sec-2.md"}]},
                        "写入产物", "写好 sec-2.md"),
    }
    for name, (item, stage, text) in cases.items():
        (line,) = narrate_record(_codex({"type": "item.completed", "item": item}))
        assert (line.stage, line.text) == (stage, text), name


def test_codex_命令执行与失败() -> None:
    ok = {"type": "command_execution", "command": "ls runs/", "exit_code": 0}
    (line,) = narrate_record(_codex({"type": "item.completed", "item": ok}))
    assert line.stage == "调用工具" and line.text == "执行命令（ls runs/）"
    bad = {"type": "command_execution", "command": "cat x", "exit_code": 1,
           "aggregated_output": "No such file"}
    (line,) = narrate_record(_codex({"type": "item.completed", "item": bad}))
    assert line.stage == "失败" and "No such file" in line.text


def test_codex_收尾与整轮失败不出usage() -> None:
    (line,) = narrate_record(_codex({"type": "turn.completed",
                                     "usage": {"input_tokens": 12, "output_tokens": 4}}))
    assert line.stage == "本节完成" and "12" not in line.text
    (line,) = narrate_record(_codex({"type": "turn.failed",
                                     "error": {"message": "传输中断"}}))
    assert line.stage == "失败" and "传输中断" in line.text


def test_同一工具连调折成一行并汇总条数() -> None:
    records = []
    for index in range(3):
        records.append(_codex({"type": "item.completed", "item": {
            "type": "mcp_tool_call", "tool": "collect_xhs",
            "arguments": {"keyword": "WorkBuddy"}}}, seq=index * 2 + 1))
        records.append(_record({"content": [{
            "tool_use_id": f"t{index}", "is_error": None,
            "content": json.dumps({"rows": [{"a": 1}] * 16})}]}, seq=index * 2 + 2))
    (line,) = narrate_lines(records)
    assert line.text == "调用 collect_xhs 3 次，取到 48 条"


def test_同一句话被说三遍只留最后一条() -> None:
    fail = "API Error: The socket connection was closed unexpectedly"
    records = [
        _record({"content": [{"text": fail}]}, seq=1),
        _record({"subtype": "error", "result": fail}, seq=2),
    ]
    lines = narrate_lines(records)
    assert len(lines) == 1 and lines[0].stage == "失败"


# ---- 真机底料回归：判据 1（硬丢弃）与判据 3（覆盖率）----

FIXTURES = Path(__file__).parent / "fixtures" / "obs3"
#: 允许「一行都不出」的类别：系统初始化、限额心跳、以及 SDK 只回签名正文为空的消息。
DROPPABLE = {"init", "rate_limit_info,session_id,uuid", "content,error,message_id"}
#: 三份真机样本：小（4 条）、长（53 条）、带围栏信封（本包重放当场落的 34 条）。
REAL_SAMPLES = (
    "claude-small.transcript.jsonl",
    "claude-long.transcript.jsonl",
    "claude-fenced.transcript.jsonl",
)


def _load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def test_真机底料译出的每一行都干净() -> None:
    for name in REAL_SAMPLES:
        lines = narrate_lines(_load(name))
        assert lines, name
        for line in lines:
            assert len(line.text) <= MAX_TEXT + 1, (name, line.text)
            assert "signature" not in line.text
            assert "Esoq" not in line.text  # 签名串的真机前缀
            assert '"type": "system"' not in line.text
            assert not line.text.lstrip().startswith(("{", "[", "```"))
            assert line.stage and line.text


def test_真机底料未译事件只落在应丢类别() -> None:
    """判据 3：分母是「本该出行的事件」，白名单外一条未译都不许有。"""

    for name in REAL_SAMPLES:
        tally = unmatched_kinds(_load(name))
        assert set(tally) <= DROPPABLE, (name, tally)


def test_真机底料出得来写盘与收尾两类关键行() -> None:
    lines = narrate_lines(_load("claude-long.transcript.jsonl"))
    stages = {line.stage for line in lines}
    assert {"调用工具", "写入产物", "本节完成"} <= stages
    written = [line.text for line in lines if line.stage == "写入产物"]
    assert any("约" in text and "字" in text for text in written)


def test_围栏包着的JSON信封也不许漏进进程栏() -> None:
    """真机现场打红：模型把信封写成 ```json owli-result\\n{...}```，首字符是反引号。"""

    envelope = ('```json owli-result\n'
                '{"status": "done", "output_path": "goals/goal-3/sec-2.part.2.md",'
                ' "summary": "已按分片规范落盘 ch-4/sec-2 第 2/4 片"}\n```')
    (line,) = narrate_record(_record({"content": [{"text": envelope}]}))
    assert line.text == "已按分片规范落盘 ch-4/sec-2 第 2/4 片"
    assert "output_path" not in line.text and "```" not in line.text


def test_钩子回灌的英文提示标成系统提醒() -> None:
    hook = "Stop hook feedback: You MUST call the StructuredOutput tool to complete this request."
    (line,) = narrate_record(_record({"content": [{"text": hook}]}))
    assert line.text.startswith("系统提醒：You MUST call")


def test_接口重试与系统提示都出行() -> None:
    retry = {"subtype": "api_retry", "data": {"type": "system", "subtype": "api_retry",
                                              "attempt": 1, "max_retries": 20}}
    (line,) = narrate_record(_record(retry))
    assert (line.stage, line.text) == ("重试", "接口重试第 1/20 次")
    note = {"subtype": "notification", "data": {"type": "system", "key": "stop-hook-error",
                                                "text": "Stop hook error occurred"}}
    (line,) = narrate_record(_record(note))
    assert (line.stage, line.text) == ("提示", "Stop hook error occurred")
