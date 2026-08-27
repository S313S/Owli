#!/usr/bin/env python3
"""把显式断言文件按 permalink 联接并双向登记到 Store。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.reliability.claims import (  # noqa: E402
    ClaimsRegistrationError,
    register_claims,
)
from app.store.dao import Store  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把章节或存量提取的 claims 双向登记到 Owli Store"
    )
    parser.add_argument("database", type=Path, help="Owli SQLite 数据库路径")
    parser.add_argument("--report-id", required=True, help="目标报告 ID")
    parser.add_argument("--claims", type=Path, required=True, help="claims JSON 文件")
    parser.add_argument(
        "--source",
        choices=("chapter", "backfill"),
        required=True,
        help="断言来源标记",
    )
    return parser


def _claims_payload(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("claims"), list):
        return list(value["claims"])
    raise TypeError("claims 文件必须是数组，或含 claims 数组的 object")


def main() -> int:
    args = _parser().parse_args()
    try:
        database = args.database.resolve()
        claims_path = args.claims.resolve()
        if not database.is_file():
            raise FileNotFoundError(f"数据库不存在：{database}")
        raw = json.loads(claims_path.read_text(encoding="utf-8"))
        store = Store(database)
        claims = register_claims(
            store,
            args.report_id,
            _claims_payload(raw),
            source=args.source,
        )
        attached = len({
            evidence_id
            for claim in claims
            for evidence_id in claim["evidence_ids"]
        })
        payload = {
            "ok": True,
            "report_id": args.report_id,
            "claims": len(claims),
            "evidence_attached": attached,
            "claims_source": args.source,
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        if isinstance(exc, ClaimsRegistrationError):
            payload["error"]["offenders"] = exc.offenders
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
