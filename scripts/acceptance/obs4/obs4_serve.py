#!/usr/bin/env python3
"""§OBS-4 验收起服务：拿夜跑库的**副本**起一份只读服务，看运行面板。

夜跑库只读（提货单硬规矩），所以先 `.backup` 出副本再指过去；runs 目录只读不写。
用法：python3 scripts/acceptance/obs4/obs4_serve.py --db <副本> --runs <夜跑 runs> --port 8960
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
    parser.add_argument("--port", type=int, default=8960)
    parser.add_argument("--dist", default=str(ROOT / "web" / "dist"))
    args = parser.parse_args()
    app = create_app(
        database_path=Path(args.db).resolve(),
        frontend_dist=Path(args.dist).resolve(),
        runs_root=Path(args.runs).resolve(),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
