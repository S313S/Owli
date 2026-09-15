"""§RPT-4 沙盒（端口 8979）。

抄 `scripts/rpt3_serve_factory.py`，只换库名与端口：服 `var/rpt4-serve.db`（backup API 取自
../Owli-rpt3/var/rpt3-serve.db，库内绝对路径已改写到本树）+ 本树 `var/runs`（拷贝，不直指别的树）。
用途：客户视角修正后补实体归属、重出「国内大家对豆包的看法」咨询体正式稿。

起法（在 worktree 根目录）：
    nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.rpt4_serve_factory:app --factory --port 8979 > var/serve-8979.log 2>&1 &
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "rpt4-serve.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
