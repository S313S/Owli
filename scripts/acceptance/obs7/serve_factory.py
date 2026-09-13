"""§OBS-7 验收留服（端口 8981）：服 alloc2 库的 backup 副本 `var/obs7-serve.db` + `var/runs`。

用途：单卡重放 r-50600e09f7dd 的 goal-1/ch-3（抖音采集卡），量三判据。
起法（worktree 根目录）：
    OWLI_AUTO_EXPORT=excel OWLI_UNATTENDED=1 nohup ../Owli/.venv/bin/python -m uvicorn \\
        scripts.acceptance.obs7.serve_factory:app --factory --port 8981 > var/serve-8981.log 2>&1 &
"""

from pathlib import Path

from app.api.main import create_app

ROOT = Path(__file__).resolve().parents[3]


def app():
    return create_app(
        database_path=ROOT / "var" / "obs7-serve.db",
        schema_path=ROOT / "app" / "store" / "schema.sql",
        frontend_dist=ROOT / "web" / "dist",
        runs_root=ROOT / "var" / "runs",
    )
