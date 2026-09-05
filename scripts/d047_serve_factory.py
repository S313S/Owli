"""§D-047 货 3 真机的服务入口（端口 8970，本 worktree 的新代码）。

抄 `scripts/d046_serve_factory.py`，只换库名与端口。起法（在 worktree 根目录）：
    OWLI_AUTO_CONFIRM=1 nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.d047_serve_factory:app --factory --port 8970 > var/serve-8970.log 2>&1 &
沙盒怎么来（别 cp 活库，8956 正拿着它当运行时）：
    sqlite3 ../Owli-mvp/var/mvp-8956.db ".backup $(pwd)/var/d047-sandbox.db"
    cp -R ../Owli-mvp/runs/r-3e04f808dffd var/runs/

全用绝对路径指向本 worktree 的沙盒：库是夜跑库的 `.backup` 副本、runs 是
底料产物的整份拷贝。`../Owli-mvp` 下的库与 runs 零写入——8956 正拿着它们
当运行时，连 SQLite 的 -wal/-shm 侧文件都不许碰。
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def app():
    return create_app(
        database_path=ROOT / "var" / "d047-sandbox.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
