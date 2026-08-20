from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _node(index: int, votes: int) -> dict:
    return {
        "id": str(index),
        "name": f"产品 {index}",
        "tagline": f"一句话 {index}",
        "votesCount": votes,
        "commentsCount": index,
        "createdAt": "2026-08-19T00:00:00Z",
        "url": f"https://www.producthunt.com/posts/product-{index}",
        "topics": {"edges": [{"node": {"name": "AI"}}]},
    }


def _response(nodes: list[dict], *, next_cursor: str | None = None, status: int = 200):
    from app.sources.product_hunt import GraphQLResponse

    return GraphQLResponse(
        status=status,
        headers={
            "x-rate-limit-limit": "6250",
            "x-rate-limit-remaining": "6150",
            "x-rate-limit-reset": "900",
        },
        payload={
            "data": {
                "posts": {
                    "edges": [{"node": node} for node in nodes],
                    "pageInfo": {
                        "hasNextPage": next_cursor is not None,
                        "endCursor": next_cursor,
                    },
                }
            }
        },
    )


def _routing_payloads(log_root: Path) -> list[dict]:
    paths = list((log_root / "routing").glob("source.product_hunt-*.jsonl"))
    assert len(paths) == 1
    return [json.loads(line) for line in paths[0].read_text().splitlines()]


def test_公共发布器可显式落_continue且回调只是附加出口(tmp_path) -> None:
    from app.adapters.ratelimit import (
        RouteDecision,
        RouteState,
        publish_route_decision,
    )

    received = []
    decision = RouteDecision(
        RouteState.CONTINUE,
        "Product Hunt 已恢复",
        {"source": "product_hunt", "kind": "recovered"},
    )

    publish_route_decision(
        decision,
        engine="source.product_hunt",
        on_event=received.append,
        log_root=tmp_path,
        log_clock=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
        publish_continue=True,
    )

    [payload] = _routing_payloads(tmp_path)
    assert payload["route_state"] == "CONTINUE"
    assert payload["raw"]["kind"] == "recovered"
    assert len(received) == 1
    assert received[0].raw == payload["raw"]


def test_graphql固定posted_after加votes并按游标分页(tmp_path) -> None:
    from app.sources import product_hunt as ph

    calls = []
    responses = [
        _response([_node(1, 30), _node(2, 20)], next_cursor="cursor-2"),
        _response([_node(3, 10)]),
    ]

    def fake_post(token, query, variables):
        calls.append((token, query, variables))
        return responses.pop(0)

    with (
        patch.object(ph, "_load_token", return_value="私密-token"),
        patch.object(ph, "_post_graphql", side_effect=fake_post),
        patch.object(
            ph,
            "_utc_now",
            return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    ):
        result = ph.search("", "7d", limit=3, page_size=2, log_root=tmp_path)

    assert [item["raw_metrics"]["votesCount"] for item in result] == [30, 20, 10]
    assert len(calls) == 2
    assert calls[0][0] == "私密-token"
    assert "postedAfter: $postedAfter" in calls[0][1]
    assert "order: VOTES" in calls[0][1]
    assert "after: $after" in calls[0][1]
    assert "pageInfo" in calls[0][1]
    assert calls[0][2] == {
        "first": 2,
        "after": None,
        "postedAfter": "2026-08-13T00:00:00+00:00",
    }
    assert calls[1][2]["after"] == "cursor-2"


def test_空窗口返回空数组且无回调仍默认落结构化说明(tmp_path) -> None:
    from app.sources import product_hunt as ph

    with (
        patch.object(ph, "_load_token", return_value="私密-token"),
        patch.object(ph, "_post_graphql", return_value=_response([])),
        patch.object(
            ph,
            "_utc_now",
            return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    ):
        result = ph.search(
            "AI",
            "7d",
            log_root=tmp_path,
            log_clock=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
        )

    assert result == []
    [payload] = _routing_payloads(tmp_path)
    assert payload["route_state"] == "CONTINUE"
    assert payload["raw"] == {
        "source": "product_hunt",
        "kind": "empty_window",
        "query": "AI",
        "window": "7d",
    }


def test_http_429发布backoff_退避后恢复并默认落盘(tmp_path) -> None:
    from app.sources import product_hunt as ph

    limited = ph.GraphQLResponse(
        status=429,
        headers={
            "x-rate-limit-limit": "6250",
            "x-rate-limit-remaining": "0",
            "x-rate-limit-reset": "2",
        },
        payload={"errors": [{"message": "rate limited"}]},
    )
    events = []
    budget = ph.CreditBudget()
    with (
        patch.object(ph, "_load_token", return_value="绝不能进日志"),
        patch.object(ph, "_BUDGET", budget),
        patch.object(ph, "_post_graphql", side_effect=[limited, _response([_node(1, 8)])]),
        patch.object(ph.time, "sleep") as sleep,
        patch.object(
            ph,
            "_utc_now",
            return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    ):
        result = ph.search(
            "",
            "7d",
            limit=1,
            on_event=events.append,
            log_root=tmp_path,
            log_clock=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
        )

    assert len(result) == 1
    sleep.assert_called_once_with(2.0)
    assert [event.route_state for event in events] == ["BACKOFF", "CONTINUE"]
    payloads = _routing_payloads(tmp_path)
    assert [item["route_state"] for item in payloads] == ["BACKOFF", "CONTINUE"]
    assert [item["raw"]["kind"] for item in payloads] == ["http_429", "recovered"]
    assert "绝不能进日志" not in json.dumps(payloads, ensure_ascii=False)


def test_本地额度耗尽先backoff_窗口重置后恢复(tmp_path) -> None:
    from app.sources import product_hunt as ph

    current = [100.0]

    def sleeper(seconds: float) -> None:
        current[0] += seconds

    budget = ph.CreditBudget(clock=lambda: current[0], sleeper=sleeper)
    budget.remaining = 0
    budget.reset_at = 103.0
    events = []
    with (
        patch.object(ph, "_load_token", return_value="私密-token"),
        patch.object(ph, "_BUDGET", budget),
        patch.object(ph, "_post_graphql", return_value=_response([_node(1, 8)])),
        patch.object(
            ph,
            "_utc_now",
            return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    ):
        ph.search("", "7d", limit=1, on_event=events.append, log_root=tmp_path)

    assert current[0] == 103.0
    assert [event.route_state for event in events] == ["BACKOFF", "CONTINUE"]
    assert events[0].raw["kind"] == "budget_exhausted"


def test_前20条按votes_count平台内归一化并补齐五维(tmp_path) -> None:
    from app.sources import product_hunt as ph

    nodes = [_node(index, 21 - index) for index in range(1, 21)]
    with (
        patch.object(ph, "_load_token", return_value="私密-token"),
        patch.object(ph, "_post_graphql", return_value=_response(nodes)),
        patch.object(ph, "_BUDGET", ph.CreditBudget()),
        patch.object(
            ph,
            "_utc_now",
            return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    ):
        result = ph.search("", "7d", limit=20, log_root=tmp_path)

    assert result[0]["normalized_score"] == 1.0
    assert result[-1]["normalized_score"] == 0.0
    assert all(item["norm_method"] == "percentile_in_batch" for item in result)
    assert all(item["norm_context"]["metric"] == "votes_count" for item in result)
    assert result[0]["raw_metrics"] == {
        "votesCount": 20,
        "commentsCount": 1,
        "votes_count": 20,
        "comments_count": 1,
    }
    assert [result[0][key] for key in (
        "score_authority",
        "score_freshness",
        "score_crossref",
        "score_completeness",
        "score_independence",
    )] == [2, 2, 0, 1, 1]
    assert result[0]["rated_by"] == "baseline:product_hunt@v1"
    assert "权威2:" in result[0]["rating_notes"]


def test_证据经store批量通道原子入库(tmp_path) -> None:
    from app.sources import product_hunt as ph
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli.db"
    schema = ROOT / "app" / "store" / "schema.sql"
    initialize_database_if_empty(database, schema)
    store = Store(database)
    store.create_report(
        id="r-ph",
        title="Product Hunt",
        research_question="近 7 天新品",
        created_at="2026-08-20T00:00:00Z",
    )
    nodes = [_node(index, 21 - index) for index in range(1, 21)]
    with (
        patch.object(ph, "_load_token", return_value="私密-token"),
        patch.object(ph, "_post_graphql", return_value=_response(nodes)),
        patch.object(ph, "_BUDGET", ph.CreditBudget()),
        patch.object(
            ph,
            "_utc_now",
            return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
    ):
        result = ph.search(
            "",
            "7d",
            limit=20,
            store=store,
            report_id="r-ph",
            goal_id="goal-1",
            log_root=tmp_path / "logs",
        )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT count(*), min(score_total), max(score_total) FROM evidence"
        ).fetchone()
        raw, context = connection.execute(
            "SELECT raw_metrics, norm_context FROM evidence WHERE platform_item_id='1'"
        ).fetchone()
    assert row == (20, 6, 6)
    assert json.loads(raw)["votesCount"] == 20
    assert json.loads(context)["metric"] == "votes_count"
    assert len(result) == 20


def test_SOURCE_SPEC分散声明product_hunt工具映射() -> None:
    from app.sources import product_hunt as ph

    assert ph.SOURCE_SPEC.source_id == "product_hunt"
    assert ph.SOURCE_SPEC.tool_name == "source.product_hunt"
    assert ph.SOURCE_SPEC.entrypoint is ph.search


def test_凭证只读指定env文件且忽略进程环境(tmp_path, monkeypatch) -> None:
    from app.sources import product_hunt as ph

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 本地凭证\nPRODUCT_HUNT_TOKEN='文件-token'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRODUCT_HUNT_TOKEN", "进程环境-token")
    with patch.object(ph, "_ENV_PATH", env_file):
        assert ph._load_token() == "文件-token"


def test_凭证缺失报错不回显任何候选token(tmp_path, monkeypatch) -> None:
    import pytest

    from app.sources import product_hunt as ph

    monkeypatch.setenv("PRODUCT_HUNT_TOKEN", "绝不能回显-token")
    missing = tmp_path / "missing.env"
    with (
        patch.object(ph, "_ENV_PATH", missing),
        pytest.raises(RuntimeError) as captured,
    ):
        ph._load_token()

    assert "绝不能回显-token" not in str(captured.value)
    assert "PRODUCT_HUNT_TOKEN" in str(captured.value)


def test_非Product_Hunt官方链接拒绝成为permalink() -> None:
    import pytest

    from app.sources import product_hunt as ph

    node = {**_node(1, 10), "url": "https://example.com/posts/fake"}
    with pytest.raises(RuntimeError, match="官方 url"):
        ph._to_evidence(node, "", "2026-08-20T00:00:00+00:00")
