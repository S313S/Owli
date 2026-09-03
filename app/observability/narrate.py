"""§OBS-3 翻译层：把 transcript 里的**原始事件**译成「时间 · 阶段 · 一句人话」。

只读。输入是 §OBS-2 落盘的一行 record（`{ts,seq,engine,agent,output,event}`），
输出 0..n 条 `ProgressLine`。**不碰适配器、不改归一化、不改写盘。**

硬丢弃规则（用户口径：签名串、JSON 请求体、系统初始化块一律不进进程栏）：

- `{"subtype": "init", ...}` 系统初始化块、`rate_limit_info` 心跳 —— 整条丢；
- `thinking` 块里的 `signature`（动辄上千字的 base64）—— 只取 `thinking` 正文；
- 工具调用的完整 `input` —— 只出工具名 + 一句参数摘要；
- `usage` / token 明细 —— 不出行。

认不出的事件返回空列表（**宁可少出行也不倒原文**），由 `unmatched_kinds` 统计覆盖率。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 一行进程文本的上限（判据 1：单行 ≤200 字）。
MAX_TEXT = 180
#: 参数摘要上限。
MAX_ARG = 40

STAGE_THINK = "思考"
STAGE_SAY = "说明"
STAGE_TOOL = "调用工具"
STAGE_RESULT = "工具返回"
STAGE_WRITE = "写入产物"
STAGE_DONE = "本节完成"
STAGE_FAIL = "失败"


@dataclass(frozen=True)
class ProgressLine:
    """进程栏的一行。`tool`/`count` 供聚合用，前端只渲染 stage 与 text。"""

    ts: float
    seq: int
    stage: str
    text: str
    kind: str
    tool: str = ""
    count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "seq": self.seq, "stage": self.stage,
            "text": self.text, "kind": self.kind,
        }


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    """压平成一行并截断；截断处加省略号，让人一眼看出后面还有。"""

    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _looks_like_json(text: str) -> bool:
    """判据 1：裸 JSON 请求体不许进进程栏。"""

    head = text.lstrip()[:2]
    return head.startswith("{") or head.startswith("[")


def _name_of(value: Any) -> str:
    """路径类参数只留文件名，省得一行全是绝对路径。"""

    text = str(value)
    return Path(text).name if "/" in text else text


#: 工具参数里值得给人看的字段（按优先级）。其余一律不出，避免倒 JSON。
_ARG_KEYS = (
    "query", "keyword", "keywords", "topic", "q",
    "file_path", "path", "notebook_path", "output_path",
    "command", "url", "platform", "source", "note_id", "post_id",
    "limit", "top_k", "per_post", "count",
)


def _arg_summary(payload: Any) -> str:
    """工具参数摘要：挑几个人看得懂的键，拼成一句，绝不整串倒 JSON。"""

    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    for key in _ARG_KEYS:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        parts.append(_name_of(value) if key.endswith("path") else str(value))
        if len(parts) >= 2:
            break
    return _clip(" · ".join(parts), MAX_ARG)


def _count_rows(payload: Any) -> int | None:
    """从工具返回体里数条数：JSON 数组或常见 rows/items/data 字段，数不出就 None。"""

    body: Any = payload
    if isinstance(body, str):
        if not _looks_like_json(body):
            return None
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if isinstance(body, list):
        return len(body)
    if isinstance(body, Mapping):
        for key in ("rows", "items", "results", "data", "evidence"):
            value = body.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def _plain_say(text: str) -> str:
    """模型正文：像 JSON 信封就抽里头的 markdown/summary，抽不出只留一句话。

    判据 1 要求进程栏零裸 JSON——正文里塞了整份信封的情形真机第一份底料就有。
    """

    flat = str(text or "").strip()
    if not flat:
        return ""
    if not _looks_like_json(flat):
        return _clip(flat)
    try:
        body = json.loads(flat)
    except ValueError:
        return "输出 JSON 信封（详见日志栏）"
    if isinstance(body, Mapping):
        for key in ("summary", "markdown", "text", "content"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(" ".join(value.split()))
    return "输出 JSON 信封（详见日志栏）"


def _claude_block(block: Mapping[str, Any], ts: float, seq: int) -> ProgressLine | None:
    """Claude 一条消息里的一个 content 块译一行。"""

    if block.get("thinking") is not None:  # signature 是签名串，只取正文
        body = _clip(block.get("thinking") or "")
        return ProgressLine(ts, seq, STAGE_THINK, body, "think") if body else None
    if isinstance(block.get("text"), str):
        body = _plain_say(block["text"])
        return ProgressLine(ts, seq, STAGE_SAY, body, "say") if body else None
    name = block.get("name")
    if isinstance(name, str) and name:  # 工具调用：只出名字 + 参数摘要
        summary = _arg_summary(block.get("input"))
        text = f"调用 {name}" + (f"（{summary}）" if summary else "")
        return ProgressLine(ts, seq, STAGE_TOOL, text, "tool", tool=name, count=1)
    if block.get("tool_use_id"):
        return _claude_tool_result(block, ts, seq)
    return None


def _claude_tool_result(block: Mapping[str, Any], ts: float, seq: int) -> ProgressLine:
    """工具返回：报错出原文，能数条数就数，数不出只说一句「详见日志栏」。"""

    payload = block.get("content")
    if block.get("is_error"):
        return ProgressLine(ts, seq, STAGE_FAIL, _clip(f"工具报错：{payload}"), "error")
    rows = _count_rows(payload)
    if rows is not None:
        return ProgressLine(ts, seq, STAGE_RESULT, f"取到 {rows} 条", "result", count=rows)
    text = str(payload or "")
    if not text.strip():
        return ProgressLine(ts, seq, STAGE_RESULT, "工具返回空", "result")
    if _looks_like_json(text):
        return ProgressLine(ts, seq, STAGE_RESULT, "工具返回结果（详见日志栏）", "result")
    return ProgressLine(ts, seq, STAGE_RESULT, _clip(text), "result")


def _claude_write(result: Mapping[str, Any], ts: float, seq: int) -> ProgressLine | None:
    """`tool_use_result` 里的写文件：出「写好 <文件名>，约 N 字」。"""

    path = result.get("filePath") or result.get("file_path")
    if not path:
        return None
    body = result.get("content")
    words = len(" ".join(str(body).split())) if isinstance(body, str) else 0
    tail = f"，约 {words} 字" if words else ""
    return ProgressLine(ts, seq, STAGE_WRITE, f"写好 {_name_of(path)}{tail}", "write")


def _narrate_claude(event: Mapping[str, Any], ts: float, seq: int) -> list[ProgressLine]:
    """Claude（SDK 消息）一条原始事件 → 0..n 行。"""

    if event.get("subtype") == "init" or "rate_limit_info" in event:
        return []  # 系统初始化块与限额心跳：机器看的，不进进程栏
    subtype = event.get("subtype")
    if subtype in ("success", "error", "error_max_turns", "error_during_execution"):
        return _claude_finish(event, ts, seq)
    lines: list[ProgressLine] = []
    content = event.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                line = _claude_block(block, ts, seq)
                if line is not None:
                    lines.append(line)
    result = event.get("tool_use_result")
    if isinstance(result, Mapping):
        write = _claude_write(result, ts, seq)
        if write is not None:
            lines = [line for line in lines if line.kind != "result"] + [write]
    error = event.get("error")
    if error and str(error).lower() not in ("unknown", "none", "null"):
        lines.append(ProgressLine(ts, seq, STAGE_FAIL, _clip(f"引擎报错：{error}"), "error"))
    return lines


def _claude_finish(event: Mapping[str, Any], ts: float, seq: int) -> list[ProgressLine]:
    """收尾事件：模型自己写的 `structured_output.summary` 就是最好的人话。"""

    output = event.get("structured_output")
    summary = ""
    if isinstance(output, Mapping):
        summary = _clip(str(output.get("summary") or ""))
    if event.get("is_error") or event.get("subtype") != "success":
        detail = summary or _clip(str(event.get("result") or event.get("subtype") or ""))
        return [ProgressLine(ts, seq, STAGE_FAIL, f"这一节没跑成：{detail}", "error")]
    seconds = event.get("duration_ms")
    tail = f"（耗时 {round(float(seconds) / 1000)}s）" if isinstance(seconds, (int, float)) else ""
    text = summary or "这一节跑完了"
    return [ProgressLine(ts, seq, STAGE_DONE, _clip(f"{text}{tail}"), "done")]


def _codex_item(item: Mapping[str, Any], ts: float, seq: int) -> ProgressLine | None:
    """Codex `item.completed` 里的一个 item 译一行（`item.type` 决定阶段）。"""

    kind = str(item.get("type") or "").lower()
    text = str(item.get("text") or item.get("message") or "")
    if "agent_message" in kind or "assistant" in kind or "output_text" in kind:
        return ProgressLine(ts, seq, STAGE_SAY, _clip(text), "say") if text.strip() else None
    if "reasoning" in kind or "thinking" in kind:
        return ProgressLine(ts, seq, STAGE_THINK, _clip(text), "think") if text.strip() else None
    if "file_change" in kind or "patch" in kind:
        changes = item.get("changes") or item.get("path") or item.get("files")
        first = changes[0] if isinstance(changes, list) and changes else changes
        target = first.get("path") if isinstance(first, Mapping) else first
        return ProgressLine(ts, seq, STAGE_WRITE, f"写好 {_name_of(target or '产物')}", "write")
    if "web_search" in kind:
        query = _clip(str(item.get("query") or ""), MAX_ARG)
        text = "调用 网页搜索" + (f"（{query}）" if query else "")
        return ProgressLine(ts, seq, STAGE_TOOL, text, "tool", tool="网页搜索", count=1)
    if "command_execution" in kind or "exec" in kind or "shell" in kind:
        name = _clip(str(item.get("command") or ""), MAX_ARG)
        label = f"执行命令（{name}）" if name else "执行命令"
        line = ProgressLine(ts, seq, STAGE_TOOL, label, "tool",
                            tool="命令", count=1)
        return line if not item.get("exit_code") else ProgressLine(
            ts, seq, STAGE_FAIL, _clip(f"命令失败（{name}）：{item.get('aggregated_output') or ''}"),
            "error")


def short(text: str, limit: int = 24) -> str:
    """给角标用的短句：压平 + 截断（`_clip` 的公开出口）。"""

    return _clip(text, limit)
    if "mcp_tool_call" in kind or "tool" in kind:
        name = str(item.get("tool") or item.get("name") or "工具")
        summary = _arg_summary(item.get("arguments") or item.get("input"))
        text = f"调用 {name}" + (f"（{summary}）" if summary else "")
        return ProgressLine(ts, seq, STAGE_TOOL, text, "tool", tool=name, count=1)
    if "error" in kind:
        return ProgressLine(ts, seq, STAGE_FAIL, _clip(f"出错：{text}"), "error")
    return None


def _narrate_codex(event: Mapping[str, Any], ts: float, seq: int) -> list[ProgressLine]:
    """Codex（JSONL）一条原始事件 → 0..1 行。"""

    event_type = str(event.get("type") or "")
    if event_type in ("thread.started", "turn.started", "item.started"):
        return []  # 起手事件：没信息量，起讫由别的行体现
    if event_type == "turn.completed":
        return [ProgressLine(ts, seq, STAGE_DONE, "这一轮跑完了", "done")]
    if event_type == "turn.failed" or "error" in event_type.lower():
        detail = event.get("error") or event.get("message") or event
        text = detail.get("message") if isinstance(detail, Mapping) else detail
        return [ProgressLine(ts, seq, STAGE_FAIL, _clip(f"这一轮没跑成：{text}"), "error")]
    item = event.get("item") or event.get("msg")
    if isinstance(item, Mapping):
        line = _codex_item(item, ts, seq)
        return [line] if line is not None else []
    return []


def _engine_of(record: Mapping[str, Any], event: Any) -> str:
    """认引擎：先信 transcript 记的 `engine`，认不出就按事件形状嗅。"""

    engine = str(record.get("engine") or "").lower()
    if engine.startswith("claude"):
        return "claude"
    if engine.startswith("codex"):
        return "codex"
    if isinstance(event, Mapping):
        head = str(event.get("type") or "")
        if head.startswith(("thread.", "turn.", "item.")):
            return "codex"
    return "claude"


def narrate_record(record: Mapping[str, Any]) -> list[ProgressLine]:
    """一条 transcript record → 0..n 条进程行；认不出返回空（不倒原文）。"""

    if not isinstance(record, Mapping):
        return []
    try:
        ts = float(record.get("ts") or 0.0)
        seq = int(record.get("seq") or 0)
    except (TypeError, ValueError):
        return []
    event = record.get("event")
    if isinstance(event, str):
        text = event.strip()
        if not text or _looks_like_json(text):
            return []
        return [ProgressLine(ts, seq, STAGE_SAY, _clip(text), "say")]
    if not isinstance(event, Mapping):
        return []
    if _engine_of(record, event) == "codex":
        return _narrate_codex(event, ts, seq)
    return _narrate_claude(event, ts, seq)


def _fold_tools(run: list[ProgressLine]) -> list[ProgressLine]:
    """一串同工具的调用+返回折成一行：「调用 X N 次，取到 M 条」。"""

    calls = [line for line in run if line.kind == "tool"]
    rows = [line.count for line in run if line.kind == "result" and line.count is not None]
    total = sum(rows) if rows else None
    if not calls:
        return run
    head = calls[0]
    if len(calls) == 1 and total is None:  # 只留有内容的返回，套话返回不占行
        kept = [line for line in run
                if line.kind != "result" or not line.text.startswith("工具返回")]
        return kept or [head]
    tail = f"，取到 {total} 条" if total is not None else ""
    text = head.text if len(calls) == 1 else f"调用 {head.tool} {len(calls)} 次"
    return [ProgressLine(head.ts, head.seq, STAGE_TOOL, _clip(text + tail), "tool",
                         tool=head.tool, count=total)]


def _dedupe(lines: list[ProgressLine]) -> list[ProgressLine]:
    """同一句话被引擎说三遍（正文 + error + 收尾）只留最后一条。

    真机形态：一次 API 断连会同时出「说明 API Error…」「失败 引擎报错…」
    「失败 这一节没跑成：API Error…」，人读三遍等于没读。
    """

    out: list[ProgressLine] = []
    for line in lines:
        head = line.text[:40]
        while out and head and (head in out[-1].text or out[-1].text[:40] in line.text):
            out.pop()
        out.append(line)
    return out


def narrate_lines(records: Iterable[Mapping[str, Any]]) -> list[ProgressLine]:
    """一批 transcript record → 进程行（含跨行折叠）。顺序按原始 seq。"""

    raw: list[ProgressLine] = []
    for record in records:
        raw.extend(narrate_record(record))
    out: list[ProgressLine] = []
    run: list[ProgressLine] = []

    def flush() -> None:
        nonlocal run
        if run:
            out.extend(_fold_tools(run))
            run = []

    for line in _dedupe(raw):
        if line.kind == "tool":
            if run and run[0].tool != line.tool:
                flush()
            run.append(line)
        elif line.kind == "result" and run:
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return out


def unmatched_kinds(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """没译出任何一行的事件按类别计数——覆盖率靠它量，别靠人眼扫。"""

    tally: dict[str, int] = {}
    for record in records:
        if narrate_record(record):
            continue
        event = record.get("event") if isinstance(record, Mapping) else None
        if isinstance(event, Mapping):
            item = event.get("item")
            key = str(event.get("type") or event.get("subtype") or "")
            if isinstance(item, Mapping) and item.get("type"):
                key = f"{key}:{item['type']}"
            key = key or ",".join(sorted(event)[:3])
        else:
            key = type(event).__name__
        tally[key] = tally.get(key, 0) + 1
    return tally
