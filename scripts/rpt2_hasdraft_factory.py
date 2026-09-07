"""§RPT-2 判据 4 的服务入口（端口 8977，本 worktree 的新代码）。

起法（在 worktree 根目录）：
    OWLI_AUTO_CONFIRM=1 nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.rpt2_serve_factory:app --factory --port 8977 > var/serve-8977.log 2>&1 &

调度 09-06 拍：判据 4 的端口由 8969 改 8977——8969 跑的是 RPT-1 的冻结代码，
在那儿 grep 只能证明旧码的行为，验的是错对象。

库是 `../Owli-rpt1` 那份的 `.backup` 副本（mode=ro + sqlite backup API 拷的），
runs 是底料产物的整份拷贝，全用绝对路径指向本 worktree。
`../Owli-rpt1` 是别人的活跑树，它的库与 var/runs 全程零读写。
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "rpt2-8978.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "hasdraft" / "runs",
    )
