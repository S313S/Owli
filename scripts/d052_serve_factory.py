"""§D-052 货 0 造红用的服务入口（端口 8976，本 worktree 的代码）。

抄 `scripts/d051_serve_factory.py`，换库名与端口，另加两件**只在沙盒里生效**的事
（生产代码零改动，全部落在本文件内）：

1. **每片 prompt 落盘**：片任务的 body 在 `_run_section_shards` 里由
   `replace(section_task, body=…, agent_id="…-part-N")` 组出来，transcript 只落
   引擎输出不落提示词。这里包一层 `sectioning.replace`，凡是片任务就把整份 body
   写到 `var/prompts/<variant>/`，A/B 两轮的提示词因此可以逐字比。
2. **B 变体**：`OWLI_D052_VARIANT=B` 时，把 §D-051 货 6 那一段（禁运行时章节编号
   /shard/HTML 注释、「不要给编号」）从 `_shard_notice` 的输出里剪掉，等价于把
   95fdd0b 的 1758–1768 注释掉，不改生产文件。A 变体（默认）原样跑。

起法（在 worktree 根目录）：
    OWLI_AUTO_CONFIRM=1 OWLI_D052_VARIANT=A nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.d052_serve_factory:app --factory --port 8976 > var/serve-8976.log 2>&1 &
沙盒怎么来（别 cp 活库，8956 正拿着它当运行时）：
    sqlite3 ../Owli-mvp/var/mvp-8956.db ".backup $(pwd)/var/d052-sandbox.db"
    cp -R ../Owli-mvp/runs/r-3e04f808dffd var/runs/
`../Owli-mvp` 下的库与 runs 零写入，连 -wal/-shm 都不许碰。
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from app.api.main import create_app
from app.orchestrator import sectioning

ROOT = Path(__file__).resolve().parent.parent
VARIANT = os.environ.get("OWLI_D052_VARIANT", "A").strip().upper() or "A"
PROMPT_DIR = ROOT / "var" / "prompts" / VARIANT

# §D-051 货 6 那一段的首尾锚点（95fdd0b `_shard_notice` 1758–1768）。
_G6_HEAD = "**同样不要写 `goal-1/ch-1`"
_G6_TAIL = "不要给编号。\n"


def _strip_g6(notice: str) -> str:
    head = notice.find(_G6_HEAD)
    if head < 0:
        raise RuntimeError("B 变体剪不掉货 6 那一段：锚点没找到，代码变了先核对")
    tail = notice.find(_G6_TAIL, head)
    if tail < 0:
        raise RuntimeError("B 变体剪不掉货 6 那一段：结尾锚点没找到")
    return notice[:head] + notice[tail + len(_G6_TAIL):]


_orig_notice = sectioning._shard_notice
_orig_replace = sectioning.replace


def _notice(*args, **kwargs):
    notice = _orig_notice(*args, **kwargs)
    return _strip_g6(notice) if VARIANT == "B" else notice


def _replace(obj, **changes):
    task = _orig_replace(obj, **changes)
    agent_id = str(getattr(task, "agent_id", "") or "")
    if "-part-" in agent_id and "body" in changes:
        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = PROMPT_DIR / f"{stamp}-{agent_id}.txt"
        path.write_text(str(changes["body"]), encoding="utf-8")
    return task


sectioning._shard_notice = _notice
sectioning.replace = _replace


def app():
    return create_app(
        database_path=ROOT / "var" / "d052-sandbox.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
