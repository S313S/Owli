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
import re
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
STAGE_RETRY = "重试"


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


_PART_FILE = re.compile(r"^sec-\d+\.part\.(\d+)\.md$")
_SECTION_FILE = re.compile(r"^sec-\d+\.md$")
_CHAPTER_FILE = re.compile(r"^ch-\d+\.md$")


def _write_task(path: Any) -> str:
    """写文件 → 人话任务描述。产物名只用来判断「在写什么」，不出现在行里。"""

    name = Path(str(path or "")).name
    part = _PART_FILE.match(name)
    if part:
        return f"写第 {part.group(1)} 片初稿"
    if _SECTION_FILE.match(name):
        return "写这一节初稿"
    if _CHAPTER_FILE.match(name):
        return "写这一章正文"
    if ".rows." in name or name.endswith(".rows.json"):
        return "整理采集结果"
    return "落盘结构化产物" if name.endswith(".json") else "写文档"


#: 工具名 → 任务描述。认不出的一律不出行（宁可少一行，也不让工具名露到进程栏）。
_TOOL_TASKS = {
    "structuredoutput": "按规范整理本节结论",
    "edit": "修改已写的内容",
    "notebookedit": "修改已写的内容",
    "glob": "查找文件",
    "grep": "检索内容",
    "bash": "执行命令",
}


def _tool_task(name: str, payload: Any) -> str | None:
    """工具调用 → 一句任务描述；认不出返回 None。"""

    lowered = str(name or "").lower().rsplit("__", 1)[-1]
    if not lowered:
        return None
    if lowered == "write":
        target = payload.get("file_path") if isinstance(payload, Mapping) else None
        return _write_task(target)
    if lowered == "read":
        target = str(payload.get("file_path") or "") if isinstance(payload, Mapping) else ""
        return "读取任务卡" if ("task" in target or "采集卡" in target) else "读取上游产物"
    if lowered in _TOOL_TASKS:
        return _TOOL_TASKS[lowered]
    if any(token in lowered for token in ("comment", "评论")):
        return "拉取评论"
    if any(token in lowered for token in ("collect", "crawl", "fetch", "采集")):
        return "采集数据"
    if any(token in lowered for token in ("search", "query", "检索")):
        return "检索资料"
    return None


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


_FENCE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)


def _unfence(text: str) -> str:
    """剥掉 markdown 代码围栏。

    真机里模型常把 JSON 信封写成 ```json owli-result\n{...}```——首字符是反引号，
    只看首字符的 `_looks_like_json` 认不出来，整串就漏进进程栏了（判据 1 现场打红）。
    """

    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def _plain_say(text: str) -> str:
    """模型正文：像 JSON 信封就抽里头的 markdown/summary，抽不出只留一句话。

    判据 1 要求进程栏零裸 JSON——正文里塞了整份信封的情形真机第一份底料就有。
    """

    raw = str(text or "").strip()
    # 「一句人话 + 一个 ```json owli-result 信封」是真机最常见的形态（评级章 20 批
    # 里 22 行都是），`_unfence` 只认整串就是围栏的情形，认不出这种「前面有正文」的，
    # 于是整份信封跟着人话一起漏进进程栏。有正文就只留正文，围栏留给日志栏。
    head, fence, _ = raw.partition("```")
    if fence and head.strip():
        return _clip(" ".join(head.split()))
    flat = _unfence(raw)
    if not flat:
        return ""
    if flat.startswith("Stop hook feedback:"):
        return ""  # 系统钩子回灌，不是模型在说话，也不是失败——不出行
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
    if isinstance(name, str) and name:  # 工具调用：只出任务描述（货 11）
        task = _tool_task(name, block.get("input"))
        return ProgressLine(ts, seq, STAGE_TOOL, task, "tool", tool=task, count=1) if task else None
    if block.get("tool_use_id"):
        return _claude_tool_result(block, ts, seq)
    return None


def _claude_tool_result(
    block: Mapping[str, Any], ts: float, seq: int
) -> ProgressLine | None:
    """工具返回：**只有失败才出行**（原文照给）；成功只在数得出条数时报条数。

    货 11 用户口径：成功路径不出英文 token——「Structured output provided
    successfully」这种套话对人零信息，日志栏里有。
    """

    payload = block.get("content")
    if block.get("is_error"):
        return ProgressLine(ts, seq, STAGE_FAIL, _clip(f"工具报错：{payload}"), "error")
    rows = _count_rows(payload)
    if rows is not None:
        return ProgressLine(ts, seq, STAGE_RESULT, f"取到 {rows} 条", "result", count=rows)
    return None


def _claude_write(result: Mapping[str, Any], ts: float, seq: int) -> ProgressLine | None:
    """`tool_use_result` 里的写文件：出「写好 <文件名>，约 N 字」。"""

    path = result.get("filePath") or result.get("file_path")
    if not path:
        return None
    body = result.get("content")
    words = len(" ".join(str(body).split())) if isinstance(body, str) else 0
    task = _write_task(path).replace("写", "", 1)
    tail = f"，约 {words} 字" if words else ""
    return ProgressLine(ts, seq, STAGE_WRITE, f"{task}已落盘{tail}", "write")


def _narrate_claude(event: Mapping[str, Any], ts: float, seq: int) -> list[ProgressLine]:
    """Claude（SDK 消息）一条原始事件 → 0..n 行。"""

    if event.get("subtype") == "init" or "rate_limit_info" in event:
        return []  # 系统初始化块与限额心跳：机器看的，不进进程栏
    system = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    if event.get("subtype") == "api_retry":  # 接口在重试，人最想知道的一类
        attempt, total = system.get("attempt"), system.get("max_retries")
        detail = f"第 {attempt}/{total} 次" if attempt and total else ""
        return [ProgressLine(ts, seq, STAGE_RETRY, f"接口重试{detail}", "retry")]
    if event.get("subtype") == "notification":
        return []  # SDK 内部提示（stop-hook 之类），不是任务进展也不是真失败
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
        task = _write_task(target).replace("写", "", 1)
        return ProgressLine(ts, seq, STAGE_WRITE, f"{task}已落盘", "write")
    if "web_search" in kind:
        query = _clip(str(item.get("query") or ""), MAX_ARG)
        text = "检索资料" + (f"（{query}）" if query else "")
        return ProgressLine(ts, seq, STAGE_TOOL, text, "tool", tool="检索资料", count=1)
    if "command_execution" in kind or "exec" in kind or "shell" in kind:
        name = _clip(str(item.get("command") or ""), MAX_ARG)
        if item.get("exit_code"):  # 只有失败才把命令与报错原文放出来
            detail = _clip(f"命令失败（{name}）：{item.get('aggregated_output') or ''}")
            return ProgressLine(ts, seq, STAGE_FAIL, detail, "error")
        return ProgressLine(ts, seq, STAGE_TOOL, "执行命令", "tool", tool="执行命令", count=1)

    if "mcp_tool_call" in kind or "tool" in kind:
        name = str(item.get("tool") or item.get("name") or "")
        task = _tool_task(name, item.get("arguments") or item.get("input"))
        return ProgressLine(ts, seq, STAGE_TOOL, task, "tool", tool=task, count=1) if task else None
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
    text = head.text if len(calls) == 1 else f"{head.text} {len(calls)} 次"
    return [ProgressLine(head.ts, head.seq, STAGE_TOOL, _clip(text + tail), "tool",
                         tool=head.tool, count=total)]


def _dedupe(lines: list[ProgressLine]) -> list[ProgressLine]:
    """同一句话被引擎说三遍（正文 + error + 收尾）只留最后一条。

    真机形态：一次 API 断连会同时出「说明 API Error…」「失败 引擎报错…」
    「失败 这一节没跑成：API Error…」，人读三遍等于没读。
    """

    sayish = {"say", "error", "done"}
    out: list[ProgressLine] = []
    for line in lines:
        head = line.text[:24]
        while (out and head and line.kind in sayish and out[-1].kind in sayish
               and (head in out[-1].text or out[-1].text[:40] in line.text)):
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


def short(text: str, limit: int = 24) -> str:
    """给角标用的短句：压平 + 截断（`_clip` 的公开出口）。"""

    return _clip(text, limit)
