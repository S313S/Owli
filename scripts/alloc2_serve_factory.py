"""§ALLOC-2 重采留服（端口 8976）。

抄 `scripts/alloc2_serve_factory.py`，只换库名与端口：服 `var/alloc2-serve.db`
+ `var/runs`。用途是用归位后的分配表（主角卡进主角 goal、竞品卡进竞品 goal）重新出一份「国内大家对豆包的
看法」的 fast 研究，重量三表看 goal-1 撰写章节池单平台占比。

起法（在 worktree 根目录）：
    nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.alloc2_serve_factory:app --factory --port 8976 > var/serve-8976.log 2>&1 &
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "alloc2-serve.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
