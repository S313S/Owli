#!/usr/bin/env python3
"""M3-b 网页搜索真实验收：结构化断言优先，不以退出码代替证据。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sources import web_search  # noqa: E402
from app.store.dao import Store  # noqa: E402
from app.store.schema import initialize_database_if_empty  # noqa: E402


SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
SCORE_FIELDS = (
    "score_authority",
    "score_freshness",
    "score_crossref",
    "score_completeness",
    "score_independence",
)


def _iso_with_timezone(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _absolute_permalink(value: Any) -> bool:
    parsed = urlsplit(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _transport(force_429: bool):
    if not force_429:
        return web_search._http_post
    injected = False

    def post(url, headers, payload, timeout):
        nonlocal injected
        if url == "https://api.exa.ai/search" and not injected:
            injected = True
            raise web_search.ProviderRequestError("exa", 429)
        return web_search._http_post(url, headers, payload, timeout)

    return post


def _check(name: str, passed: bool, detail: str) -> tuple[str, bool, str]:
    label = "PASS" if passed else "FAIL"
    print(f"{name}: {label}｜{detail}")
    return name, passed, detail


def run(query: str, *, force_429: bool) -> bool:
    events = []
    with tempfile.TemporaryDirectory(prefix="owli-m3-web-search-") as temp_dir:
        temp = Path(temp_dir)
        database = temp / "owli.db"
        initialize_database_if_empty(database, SCHEMA_PATH)
        store = Store(database)
        report_id = f"t-m3-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        store.create_report(
            id=report_id,
            title="M3-b 网页搜索真实验收",
            research_question=query,
            created_at=now,
        )
        evidence = web_search.collect_and_store(
            query,
            "3650d",
            report_id=report_id,
            goal_id="goal-1",
            agent_name="m3-web-search-acceptance",
            store=store,
            on_event=events.append,
            log_root=temp / "logs",
            http_post=_transport(force_429),
        )

        print(f"查询={query}")
        print(f"证据数={len(evidence)}")
        for index, item in enumerate(evidence, 1):
            scores = "/".join(str(item[field]) for field in SCORE_FIELDS)
            print(
                f"{index:02d}. provider={item['extra']['provider']}｜"
                f"fetched_at={item['fetched_at']}｜五维={scores}｜"
                f"热度={item['normalized_score']}｜{item['permalink']}"
            )

        serialized = json.dumps(evidence, ensure_ascii=False)
        lead_answers = [
            event.raw.get("answer")
            for event in events
            if event.outcome == "lead" and isinstance(event.raw, dict)
        ]
        checks = [
            _check("证据数量", len(evidence) >= 5, f"实际 {len(evidence)}，要求 ≥5"),
            _check(
                "permalink",
                bool(evidence) and all(_absolute_permalink(item["permalink"]) for item in evidence),
                "全部为绝对 HTTP(S) URL",
            ),
            _check(
                "fetched_at",
                bool(evidence) and all(_iso_with_timezone(item["fetched_at"]) for item in evidence),
                "全部为带时区 ISO 8601",
            ),
            _check(
                "五维分",
                bool(evidence) and all(
                    all(
                        isinstance(item[field], int)
                        and not isinstance(item[field], bool)
                        and 0 <= item[field] <= 2
                        for field in SCORE_FIELDS
                    )
                    for item in evidence
                ),
                "每条均有五个 0–2 整数分",
            ),
            _check(
                "热度维",
                bool(evidence) and all(
                    item["normalized_score"] is None
                    and item["norm_method"] == "none"
                    and item["norm_context"]["reason"] == "no_metric_available"
                    for item in evidence
                ),
                "normalized_score=NULL，none/no_metric_available",
            ),
            _check(
                "Tavily answer 隔离",
                all(answer not in serialized for answer in lead_answers if answer),
                f"线索事件 {len(lead_answers)} 条，证据正文零命中",
            ),
        ]
        if force_429:
            failovers = [event for event in events if event.route_state == "FAILOVER"]
            routing_logs = list((temp / "logs" / "routing").glob("*.jsonl"))
            checks.extend([
                _check(
                    "429 自动降级",
                    bool(failovers) and failovers[0].failover_target == "tavily",
                    failovers[0].text if failovers else "未观察到降级事件",
                ),
                _check(
                    "降级事件落盘",
                    bool(routing_logs),
                    str(routing_logs[0]) if routing_logs else "无 routing JSONL",
                ),
            ])
        passed = all(item[1] for item in checks)
        print("结构化验收: " + ("PASS" if passed else "FAIL"))
        return passed


def main() -> int:
    parser = argparse.ArgumentParser(description="M3-b 网页搜索真实验收")
    parser.add_argument(
        "--query",
        default="飞书 竞品 协作工具 定价",
        help="自然语言网页搜索查询",
    )
    args = parser.parse_args()
    passed = run(
        args.query,
        force_429=os.getenv("OWLI_FORCE_EXA_429") == "1",
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
