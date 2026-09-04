"""§OBS-4：节日志的**读侧**补全——找齐分批 transcript、给失败章补一行终态。

两件事，都只读、都不碰适配器：

1. `merge_transcripts()`：RATE-3 把评级章切成 `<章>.part.<N>.json` 平铺在 goal 根下，
   `transcript.chapter_key()` 于是把 `.part.<N>` 当成章名的一部分，一章写出 N 份
   transcript，面板按章名找不到（货 1 读数：夜跑库 17 个 agent 里 4 个评级章全空）。
   这里按文件名把批次找齐、按批次号拼成一条时间线（批次严格串行，真机验证过不重叠）。
2. `terminal_lines()`：章的死因（`reason` / `engine_error`）只在 `chapter_progress` 库行里，
   transcript 里一个字都没有。给终态是 missing/deferred 的章补一行，日志栏出原文、
   进程栏出人话 + 原文（用户口径「该有的问题，至少也应该写在日志上」）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.adapters.transcript import TRANSCRIPT_SUFFIX, read_transcript
from app.observability.narrate import STAGE_FAIL, ProgressLine

#: 一份 part transcript 的 seq 独立从 1 起，合并时按批次号错开到各自的号段。
#: 单批上限 `read_transcript` 的 MAX_READ_LINES=2000，一百万的号段远远够用。
PART_STRIDE = 1_000_000
#: 补出来的终态行放在所有真实行之上，且**不计入 `last_seq`**——它每次响应都重出，
#: 靠前端按 seq 去重，这样章还在跑时后续真实行的号段不会被它占掉。
TERMINAL_SEQ_BASE = 2_000_000_000
#: 一次最多回多少行（与 `read_transcript` 的单文件上限同数量级）。
MAX_LINES = 2000

_PART_RE = re.compile(r"^(?P<stem>.+)\.part\.(?P<no>\d+)$")


def part_files(direct: Path) -> list[Path]:
    """`<章>.transcript.jsonl` → 同章的批次文件 `<章>.part.<N>.transcript.jsonl`（按 N 排序）。"""

    stem = direct.name[: -len(TRANSCRIPT_SUFFIX)] if direct.name.endswith(
        TRANSCRIPT_SUFFIX
    ) else direct.stem
    found: list[tuple[int, Path]] = []
    try:
        entries = list(direct.parent.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(TRANSCRIPT_SUFFIX) or not entry.is_file():
            continue
        matched = _PART_RE.match(entry.name[: -len(TRANSCRIPT_SUFFIX)])
        if matched and matched.group("stem") == stem:
            found.append((int(matched.group("no")), entry))
    return [path for _, path in sorted(found)]


def read_section(
    direct: Path | None, *, tail: int, after_seq: int | None
) -> dict[str, Any]:
    """读一章的原始流：直连文件在就照旧读，不在就把批次文件拼起来。

    单文件那一支**与 §OBS-2 口径逐字节相同**（同一个 `read_transcript` 调用）；
    只有「直连文件不存在而批次文件存在」时才走合并支，历史行为不受影响。
    """

    if direct is None:
        return read_transcript(None, tail=tail, after_seq=after_seq)
    if direct.is_file():
        return read_transcript(direct, tail=tail, after_seq=after_seq)
    parts = part_files(direct)
    if not parts:
        return read_transcript(direct, tail=tail, after_seq=after_seq)
    return merge_transcripts(parts, tail=tail, after_seq=after_seq)


def merge_transcripts(
    parts: list[Path], *, tail: int, after_seq: int | None
) -> dict[str, Any]:
    """把同章的批次文件拼成一条时间线；每批的 seq 按批次号错到自己的号段。

    从最后一批往前读，凑够 `tail` 行就停——尾部优先，与单文件口径一致。
    """

    want = max(1, min(int(tail), MAX_LINES))
    collected: list[list[dict[str, Any]]] = []
    size_bytes = 0
    last_seq = 0
    for index, path in enumerate(reversed(parts), start=1):
        part_no = len(parts) - index + 1
        raw = read_transcript(path, tail=want, after_seq=None)
        size_bytes += int(raw.get("size_bytes") or 0)
        lines = []
        for line in raw.get("lines") or []:
            item = dict(line)
            item["seq"] = part_no * PART_STRIDE + int(item.get("seq") or 0)
            item["part"] = part_no
            last_seq = max(last_seq, int(item["seq"]))
            lines.append(item)
        collected.append(lines)
        want -= len(lines)
        if want <= 0:
            break
    merged: list[dict[str, Any]] = []
    for lines in reversed(collected):
        merged.extend(lines)
    if after_seq is not None:
        merged = [line for line in merged if int(line.get("seq") or 0) > after_seq]
    return {"lines": merged, "last_seq": last_seq, "size_bytes": size_bytes}


#: 库里的 `reason` 枚举翻成人话（进程栏用；日志栏出原文）。
REASON_TEXT = {
    "timeout": "超时",
    "empty_result": "没取到内容",
    "tool_unavailable": "工具不可用",
    "quota_exhausted": "额度用尽",
    "retry_exhausted": "重试用尽",
    "conclusion_invalid": "结论不合规",
}
STATUS_TEXT = {"missing": "本节失败", "deferred": "本节改期重跑"}
#: 只有终态是这两种的章才补行；done / running 不补，健康章的两栏一字不动。
TERMINAL_STATUS = ("missing", "deferred")


def terminal_rows(
    chapters: Iterable[Mapping[str, Any]], *, goal_id: str, chapter_id: str
) -> list[dict[str, Any]]:
    """本章自己 + 章下各节里，终态是 missing/deferred 的库行（按章节号排）。

    面板一个标签页 = 一个 agent = 一章，章下的节（`ch-5/sec-1`）没有自己的标签页，
    所以节的死因也要落到本章这一栏里，否则三个被断连吃掉的撰写节永远无人可见。
    """

    picked = []
    for row in chapters or []:
        if str(row.get("goal_id") or "") != goal_id:
            continue
        key = str(row.get("chapter_id") or "")
        if key != chapter_id and not key.startswith(f"{chapter_id}/"):
            continue
        if str(row.get("status") or "") in TERMINAL_STATUS:
            picked.append(dict(row))
    return sorted(picked, key=lambda row: str(row.get("chapter_id") or ""))


def _stamp(row: Mapping[str, Any]) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(row.get("updated_at") or "")).timestamp()
    except ValueError:
        return 0.0


def _error_text(row: Mapping[str, Any]) -> str:
    """死因原文：引擎报错优先，其次结论校验报错；两个都没有就空。"""

    return str(row.get("engine_error") or row.get("conclusion_error") or "").strip()


def terminal_records(rows: list[dict[str, Any]], *, engine: str = "") -> list[dict]:
    """日志栏用：一条终态 = 一行结构化 record，`event` 里带库行原文。"""

    records = []
    for index, row in enumerate(rows):
        records.append({
            "ts": _stamp(row),
            "seq": TERMINAL_SEQ_BASE + index,
            "engine": str(row.get("engine") or engine or ""),
            "agent": str(row.get("chapter_id") or ""),
            "output": str(row.get("actual_output_path") or ""),
            "event": {
                "owli_terminal": True,
                "chapter_id": row.get("chapter_id"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "attempts": row.get("attempts"),
                "engine_error": row.get("engine_error"),
                "conclusion_error": row.get("conclusion_error"),
            },
        })
    return records


def terminal_progress(rows: list[dict[str, Any]]) -> list[ProgressLine]:
    """进程栏用：一条终态 = 一行「失败 · 人话 + 原文」。原文是失败行的特权（OBS-3 货 11）。"""

    lines = []
    for index, row in enumerate(rows):
        status = STATUS_TEXT.get(str(row.get("status") or ""), "本节未完成")
        reason = REASON_TEXT.get(str(row.get("reason") or ""), "")
        head = f"{status}（{row.get('chapter_id')}）"
        text = f"{head}：{reason}" if reason else f"{head}：引擎没给出原因"
        detail = _error_text(row)
        if detail:
            text = f"{text} · 原文：{' '.join(detail.split())[:400]}"
        lines.append(ProgressLine(
            ts=_stamp(row), seq=TERMINAL_SEQ_BASE + index,
            stage=STAGE_FAIL, text=text, kind="error",
        ))
    return lines
