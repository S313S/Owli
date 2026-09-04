#!/usr/bin/env python3
"""§D-041 货 4 起服务：空 target 库 + 8959 底料副本，跑一次「墙钟到点」重放。

库路径一律 `.resolve()`（D-039 §九.4：MCP 源子进程 cwd 不同，相对路径会
`unable to open database file`）。
用法：python3 scripts/acceptance/d041/d041_serve.py --db var/d041-target.db \
        --runs var/runs --port 8963
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.api.main import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--port", type=int, default=8963)
    parser.add_argument("--dist", default=str(ROOT / "web" / "dist"))
    args = parser.parse_args()
    runs = Path(args.runs).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    app = create_app(
        database_path=Path(args.db).resolve(),
        frontend_dist=Path(args.dist).resolve(),
        runs_root=runs,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
