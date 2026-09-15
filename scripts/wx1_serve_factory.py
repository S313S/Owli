"""§WX-1 standard 重采留服（端口 8980）。

抄 `../Owli-alloc2/scripts/alloc2_serve_factory.py`，只换库名与端口：库 `var/wx1-serve.db`（新建空库）
+ `var/runs`。用途：用含公众号预采集池的 standard 档重采「国内大家对豆包的看法（对比 DeepSeek、Kimi、
文心一言、通义千问）」。

起法（在 worktree 根目录；⛔ 剥掉 OWLI_AUTO_CONFIRM——设了计划一出就自动起跑）：
    env -u OWLI_AUTO_CONFIRM nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.wx1_serve_factory:app --factory --port 8980 > var/serve-8980.log 2>&1 < /dev/null &
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "wx1-serve.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
