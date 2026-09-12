"""§ALLOC-1 重采留服（端口 8975）。

抄 `scripts/pool1_serve_factory.py`，只换库名与端口：服 `var/alloc1-serve.db`
+ `var/runs`。用途是用新分配表（主角先占各主源）重新出一份「国内大家对豆包的
看法」的 fast 研究，看抖音/微博两位主角源能不能采到真讲豆包的语料。

起法（在 worktree 根目录）：
    nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.alloc1_serve_factory:app --factory --port 8975 > var/serve-8975.log 2>&1 &
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "alloc1-serve.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
