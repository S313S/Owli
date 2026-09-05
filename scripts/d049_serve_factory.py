"""§D-049 货 4 真机的服务入口（端口 8974，本 worktree 的新代码）。

起法（在 worktree 根目录）：
    OWLI_AUTO_CONFIRM=1 nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.d049_serve_factory:app --factory --port 8974 > var/serve-8974.log 2>&1 &
沙盒怎么来（别 cp 活库，8956 正拿着它当运行时）：
    sqlite3 ../Owli-mvp/var/mvp-8956.db ".backup $(pwd)/var/d049-sandbox.db"
    cp -R ../Owli-mvp/runs/r-3e04f808dffd var/runs/

与 D-046 那份的唯一差别是库文件名与端口。全用绝对路径指向本 worktree 的沙盒；
`../Owli-mvp` 下的库与 runs 零写入——8956 正拿着它们当运行时，连 SQLite 的
-wal/-shm 侧文件都不许碰。

本包要验的正是「补评之后不跑 scripts/rate4_sync_rating_artifacts.py 也不会被
旧产物还原」，所以这套沙盒起跑前**不许**再跑那个同步脚本。
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "d049-sandbox.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
