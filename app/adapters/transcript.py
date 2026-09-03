"""逐节 transcript 落盘（§OBS-2 货 1）。

引擎适配器读到的每条**原始事件**，在归一化之前原样追加到
`<run>/goals/<goal>/<chapter>.transcript.jsonl`，供事后逐行复盘与前端运行面板读取。

约定：
- 一章一个文件。章内的节（`<chapter>/sec-N.md`）与片（`sec-N.part.M.md`）都写同一份，
  按 `seq` 单调递增；`agent`/`output` 两列区分是谁写的。
- **写盘失败只记 warning，绝不中断节执行**（判据 5）。
- 上限 `OWLI_TRANSCRIPT_MAX_MB`（默认 50）：超过就保尾弃头。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from app.adapters import validation as artifact_validation

logger = logging.getLogger(__name__)

TRANSCRIPT_SUFFIX = ".transcript.jsonl"
_DEFAULT_MAX_MB = 50.0
#: 每写多少行核一次文件体积（核一次要 stat，不必每行都做）。
_SIZE_CHECK_EVERY = 200
#: 续 seq 时从文件尾读多少字节找最后一行。
_TAIL_PROBE_BYTES = 65536


def max_bytes() -> int:
    """`OWLI_TRANSCRIPT_MAX_MB` 上限，读不出数就用默认 50 MB。"""

    raw = os.environ.get("OWLI_TRANSCRIPT_MAX_MB", "")
    try:
        limit = float(raw) if raw.strip() else _DEFAULT_MAX_MB
    except ValueError:
        limit = _DEFAULT_MAX_MB
    if limit <= 0:
        limit = _DEFAULT_MAX_MB
    return int(limit * 1024 * 1024)


def _goal_root(task: Any) -> Path | None:
    try:
        research_root = artifact_validation.runs_root_of(task) / task.research_id
        return (research_root / "goals" / task.goal_id).resolve(strict=False)
    except Exception:  # pragma: no cover - task 形态异常时不落盘即可
        return None


def chapter_key(task: Any) -> str | None:
    """章标识：产物落在 goal 目录下的第一段路径去掉后缀。

    `goals/goal-1/ch-3.md` → `ch-3`；`goals/goal-1/ch-3/sec-2.part.1.md` → `ch-3`。
    """

    goal_root = _goal_root(task)
    output = getattr(task, "output_path", None)
    if goal_root is None or output is None:
        return None
    try:
        relative = Path(output).resolve(strict=False).relative_to(goal_root)
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    head = parts[0]
    return Path(head).stem if len(parts) == 1 else head


def transcript_path(task: Any) -> Path | None:
    """本任务该写哪份 transcript；算不出来就返回 None（不落盘，不报错）。"""

    goal_root = _goal_root(task)
    chapter = chapter_key(task)
    if goal_root is None or not chapter:
        return None
    return goal_root / f"{chapter}{TRANSCRIPT_SUFFIX}"


def _serialize(raw: Any) -> Any:
    """原样序列化；对象走 asdict，失败落 repr（方案 §2.4 风险 2）。"""

    if isinstance(raw, (str, int, float, bool)) or raw is None:
        return raw
    if isinstance(raw, (dict, list)):
        return raw
    if is_dataclass(raw) and not isinstance(raw, type):
        try:
            return asdict(raw)
        except Exception:
            pass
    for attribute in ("model_dump", "to_dict"):
        method = getattr(raw, attribute, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return repr(raw)


def _last_seq(path: Path) -> int:
    """续写已存在的 transcript（章级重试/多节共享一份）时接着上一条 seq 走。"""

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - _TAIL_PROBE_BYTES))
            tail = handle.read()
    except OSError:
        return 0
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        try:
            return int(json.loads(line.decode("utf-8", errors="replace"))["seq"])
        except Exception:
            continue
    return 0


def _trim_head(path: Path, limit: int) -> None:
    """超上限就保尾弃头：留最后 limit/2 字节，从下一个整行开始。"""

    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - limit // 2))
            kept = handle.read()
        newline = kept.find(b"\n")
        kept = kept[newline + 1:] if newline >= 0 else kept
        marker = json.dumps(
            {"ts": time.time(), "seq": -1, "event": "…transcript 超上限，已弃头保尾"},
            ensure_ascii=False,
        ).encode("utf-8")
        path.write_bytes(marker + b"\n" + kept)
    except OSError as exc:
        logger.warning("transcript 截断失败（不影响节执行）：%s", exc)


class TranscriptWriter:
    """一个引擎任务一只写手；`append` 幂等地把原始事件追加成一行 JSON。

    构造与写入都不抛异常：算不出路径就静默不写，写失败只记一次 warning。
    """

    def __init__(self, task: Any, *, engine: str) -> None:
        self.engine = engine
        self.path = transcript_path(task)
        self.agent_id = str(getattr(task, "agent_id", "") or "")
        output = getattr(task, "output_path", None)
        self.output_name = Path(output).name if output else ""
        self._seq = 0
        self._written = 0
        self._warned = False
        self._limit = max_bytes()
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._seq = _last_seq(self.path)
        except OSError as exc:
            logger.warning("transcript 目录不可写（不影响节执行）：%s", exc)
            self.path = None

    @property
    def last_seq(self) -> int:
        return self._seq

    def append(self, raw: Any) -> None:
        if self.path is None:
            return
        self._seq += 1
        record = {
            "ts": time.time(),
            "seq": self._seq,
            "engine": self.engine,
            "agent": self.agent_id,
            "output": self.output_name,
            "event": _serialize(raw),
        }
        try:
            line = json.dumps(record, ensure_ascii=False, default=repr)
        except Exception:
            line = json.dumps(
                {**record, "event": repr(raw)}, ensure_ascii=False, default=repr
            )
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception as exc:  # 磁盘满/权限/路径消失都不许中断节
            if not self._warned:
                self._warned = True
                logger.warning("transcript 写入失败（不影响节执行）：%s", exc)
            return
        self._written += 1
        if self._written % _SIZE_CHECK_EVERY == 0:
            try:
                oversized = self.path.stat().st_size > self._limit
            except OSError:
                oversized = False
            if oversized:
                _trim_head(self.path, self._limit)


#: 一次最多回多少行，防止 `tail=999999` 把 50 MB 全端出去。
MAX_READ_LINES = 2000
_READ_CHUNK = 262144


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """从文件尾往回读，最多取 max_lines 个完整行（顺序仍是正序）。"""

    chunks: list[bytes] = []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        newlines = 0
        while position > 0 and newlines <= max_lines:
            step = min(_READ_CHUNK, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            newlines += block.count(b"\n")
            chunks.append(block)
    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:] if max_lines >= 0 else lines


def read_transcript(
    path: Path | None, *, tail: int = 200, after_seq: int | None = None
) -> dict[str, Any]:
    """读 transcript：尾 tail 行，或 seq > after_seq 的增量。

    文件不存在返回空数组（**不 404**）——节还没开跑是常态，不是错。
    """

    empty: dict[str, Any] = {"lines": [], "last_seq": 0, "size_bytes": 0}
    if path is None or not path.is_file():
        return empty
    limit = max(1, min(int(tail or 0) or 1, MAX_READ_LINES))
    try:
        size = path.stat().st_size
        raw_lines = _tail_lines(path, limit if after_seq is None else MAX_READ_LINES)
    except OSError as exc:
        logger.warning("transcript 读取失败：%s", exc)
        return empty
    records: list[dict[str, Any]] = []
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    last_seq = 0
    for record in records:
        try:
            last_seq = max(last_seq, int(record.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    if after_seq is not None:
        records = [
            record for record in records
            if isinstance(record.get("seq"), int) and record["seq"] > after_seq
        ]
    return {
        "lines": records[-limit:],
        "last_seq": last_seq,
        "size_bytes": size,
    }
