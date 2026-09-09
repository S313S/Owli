"""§POOL-1 读稿留服（端口 8969，用户读正式稿与工作稿的地方）。

抄 `scripts/d051_serve_factory.py`，只换库名与端口。上一棒的 `shard1_serve.py`
建在临时目录里，2026-09-09 早终端重启后随之消失——落进版本库就不会再丢。

起法（在 worktree 根目录）：
    nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.pool1_serve_factory:app --factory --port 8969 > var/serve-8969.log 2>&1 &

服的是 `var/shard1-serve.db` + `var/runs`——**跑稿脚本指的就是这一份**（`98c20c0`
堵的那道库接缝），所以页面上看到的与出稿用的是同一批数据。
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "shard1-serve.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
