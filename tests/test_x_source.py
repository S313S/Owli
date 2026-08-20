from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


def _config(**overrides):
    from app.sources.x import XSourceConfig

    values = dict(
        api_base_url="https://api.x.test/2",
        weekly_budget_usd=Decimal("20"),
        balance_usd=Decimal("30"),
        billing_cycle_cap_usd=Decimal("90"),
        billing_cycle_spent_usd=Decimal("10"),
        price_per_read_usd=Decimal("0.01"),
    )
    values.update(overrides)
    return XSourceConfig(**values)


def _usage_store(tmp_path):
    from app.store.schema import initialize_database_if_empty
    from app.store.usage import SourceUsageStore

    database = tmp_path / "owli.db"
    schema = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
    initialize_database_if_empty(database, schema)
    return SourceUsageStore(database)


def _payload():
    return {
        "data": [
            {
                "id": "1001",
                "text": "short text",
                "note_tweet": {"text": "full long-form text"},
                "created_at": "2026-08-20T01:00:00.000Z",
                "author_id": "author-1",
                "lang": "en",
                "public_metrics": {
                    "like_count": 21,
                    "retweet_count": 2,
                    "reply_count": 3,
                    "quote_count": 4,
                    "impression_count": 999,
                },
            },
            {
                "id": "1002",
                "text": "low engagement",
                "created_at": "2026-08-20T02:00:00.000Z",
                "author_id": "author-2",
                "lang": "en",
                "public_metrics": {
                    "like_count": 2,
                    "retweet_count": 1,
                    "reply_count": 0,
                    "quote_count": 0,
                },
            },
        ],
        "meta": {"result_count": 2},
    }


def test_SOURCE_SPEC_在源模块内声明_x_槽位() -> None:
    from app.sources.x import SOURCE_SPEC

    assert SOURCE_SPEC == {
        "source_id": "x",
        "tool": "source.x",
        "entrypoint": "app.sources.x:search",
    }


def test_recent_search_查询强制双降噪且不含_API_不支持的互动量操作符() -> None:
    from app.sources.x import build_recent_search_params

    params = build_recent_search_params(
        '"AI agent"',
        lang="en",
        window="7d",
        max_results=10,
        now=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )

    assert params["query"] == '"AI agent" -is:retweet -is:reply lang:en'
    assert "min_faves" not in params["query"]
    assert "min_retweets" not in params["query"]
    assert params["start_time"] == "2026-08-13T12:00:00Z"
    assert params["max_results"] == "10"
    assert params["sort_order"] == "relevancy"
    assert set(params["tweet.fields"].split(",")) == {
        "created_at", "public_metrics", "author_id", "lang", "note_tweet",
    }


@pytest.mark.parametrize("query", ["AI min_faves:20", "AI min_retweets:5"])
def test_recent_search_拒绝伪装成本地阈值的网页版操作符(query) -> None:
    from app.sources.x import build_recent_search_params

    with pytest.raises(ValueError, match="互动量过滤必须在本地"):
        build_recent_search_params(
            query,
            lang="en",
            window="7d",
            max_results=10,
            now=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        )


def test_字段映射保留四项指标_permalink_与过滤前后结构化计数() -> None:
    from app.sources.x import map_recent_search_response

    result = map_recent_search_response(
        _payload(),
        query='"AI agent"',
        fetched_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        min_likes=20,
        min_retweets=5,
    )

    assert result.conclusion["before_filter"] == 2
    assert result.conclusion["after_filter"] == 1
    assert result.conclusion["filtered_out"] == 1
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence["platform"] == "x"
    assert evidence["permalink"] == "https://x.com/i/status/1001"
    assert evidence["content_excerpt"] == "full long-form text"
    assert evidence["raw_metrics"] == {
        "like_count": 21,
        "retweet_count": 2,
        "reply_count": 3,
        "quote_count": 4,
    }
    assert evidence["author_meta"] == {"author_id": "author-1"}
    assert [evidence[field] for field in (
        "score_authority", "score_freshness", "score_crossref",
        "score_completeness", "score_independence",
    )] == [1, 2, 0, 1, 2]
    assert evidence["rated_by"] == "baseline:x@v1"


def test_token_只从指定的_owli_env_读取且不采纳进程环境(tmp_path, monkeypatch) -> None:
    from app.sources.x import load_bearer_token

    env_file = tmp_path / ".owli" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("X_BEARER_TOKEN=file-secret\n", encoding="utf-8")
    monkeypatch.setenv("X_BEARER_TOKEN", "process-secret")

    assert load_bearer_token(env_file) == "file-secret"
    assert os.environ["X_BEARER_TOKEN"] == "process-secret"


def test_search_使用_urlencode_与_Bearer_但结构化结果不泄漏_token(tmp_path) -> None:
    from app.sources.x import HttpResponse, XRecentSearch
    calls = []

    def http_get(url, *, headers, timeout):
        calls.append((url, headers, timeout))
        return HttpResponse(status=200, headers={}, payload=_payload())

    source = XRecentSearch(
        config=_config(),
        usage_store=_usage_store(tmp_path),
        token_loader=lambda: "runtime-secret",
        http_get=http_get,
        clock=lambda: datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )
    result = source.search(
        '"AI agent"', window="7d", lang="en", max_results=10,
        min_likes=20, min_retweets=5,
    )

    parsed = parse_qs(urlparse(calls[0][0]).query)
    assert parsed["query"] == ['"AI agent" -is:retweet -is:reply lang:en']
    assert calls[0][1]["Authorization"] == "Bearer runtime-secret"
    assert "runtime-secret" not in repr(result)
    assert result.conclusion["before_filter"] == 2
    assert result.conclusion["after_filter"] == 1


def test_第一道防线_本地预估超周预算发非阻塞提示且任务继续(tmp_path) -> None:
    from app.sources.x import HttpResponse, XRecentSearch

    events = []
    calls = []

    def http_get(url, *, headers, timeout):
        calls.append(url)
        return HttpResponse(status=200, headers={}, payload=_payload())

    source = XRecentSearch(
        config=_config(weekly_budget_usd=Decimal("0.02")),
        usage_store=_usage_store(tmp_path),
        token_loader=lambda: "secret",
        http_get=http_get,
        clock=lambda: datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        on_event=events.append,
    )

    result = source.search(
        "AI agent", window="7d", lang="en", max_results=10,
        min_likes=20, min_retweets=5,
    )

    assert len(calls) == 1
    assert result.conclusion["status"] == "completed"
    assert result.conclusion["soft_budget_warning"] is True
    card = next(event["data"]["card"] for event in events if event["type"] == "card_update")
    assert card["card_type"] == "EXTRA_QUOTA_CONFIRM"
    assert card["blocking"] == "none"
    assert card["target"]["gate"] == "owli_weekly_budget"
    assert card["target"]["estimated_reads"] == 10


@pytest.mark.parametrize(
    ("overrides", "expected_gate", "expected_reads"),
    [
        ({"balance_usd": Decimal("0.05")}, "platform_credits_balance", 5),
        (
            {
                "billing_cycle_cap_usd": Decimal("10.03"),
                "billing_cycle_spent_usd": Decimal("10"),
            },
            "platform_billing_cycle_cap",
            3,
        ),
    ],
)
def test_第二道防线_有效额度取_cap_剩余与余额的最小值(
    tmp_path, overrides, expected_gate, expected_reads
) -> None:
    from app.sources.x import budget_snapshot

    snapshot = budget_snapshot(
        _config(**overrides),
        _usage_store(tmp_path),
        now=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        estimated_reads=10,
    )

    assert snapshot["limiting_gate"] == expected_gate
    assert snapshot["effective_quota_reads"] == expected_reads
    assert snapshot["warning"] is True


def test_第三道防线_事后按实际返回_id_去重对账且_expansions_不计费(tmp_path) -> None:
    from app.sources.x import HttpResponse, XRecentSearch
    from app.store.usage import week_start_utc

    payload = _payload()
    payload["includes"] = {"users": [{"id": "author-1"}, {"id": "author-2"}]}
    events = []
    usage = _usage_store(tmp_path)
    now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    source = XRecentSearch(
        config=_config(),
        usage_store=usage,
        token_loader=lambda: "secret",
        http_get=lambda *args, **kwargs: HttpResponse(200, {}, payload),
        clock=lambda: now,
        on_event=events.append,
    )

    first = source.search(
        "AI", window="7d", lang="en", max_results=10,
        min_likes=0, min_retweets=0,
    )
    second = source.search(
        "AI", window="7d", lang="en", max_results=10,
        min_likes=0, min_retweets=0,
    )

    assert first.conclusion["actual_returned"] == 2
    assert first.conclusion["newly_billed"] == 2
    assert second.conclusion["newly_billed"] == 0
    assert usage.reads_since("x_api", week_start_utc(now)) == 2
    reconciled = [event for event in events if event["type"] == "source_usage_reconciled"]
    assert reconciled[-1]["data"]["expansions_billed_reads"] == 0
    assert reconciled[-1]["data"]["newly_billed"] == 0


def test_周预算已耗尽又撞平台硬闸时_硬闸胜出且事件写明闸门(tmp_path) -> None:
    from app.sources.x import HttpResponse, XRecentSearch

    events = []
    source = XRecentSearch(
        config=_config(weekly_budget_usd=Decimal("0")),
        usage_store=_usage_store(tmp_path),
        token_loader=lambda: "secret",
        http_get=lambda *args, **kwargs: HttpResponse(
            403,
            {},
            {"title": "UsageCapExceeded", "detail": "Billing cycle cap reached"},
        ),
        clock=lambda: datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        on_event=events.append,
    )

    result = source.search(
        "AI", window="7d", lang="en", max_results=10,
        min_likes=0, min_retweets=0,
    )

    assert result.conclusion["status"] == "unavailable"
    assert result.conclusion["soft_budget_warning"] is True
    assert result.conclusion["hard_gate"] == "platform_billing_cycle_cap"
    unavailable = [event for event in events if event["type"] == "source_unavailable"]
    assert unavailable[-1]["data"]["gate"] == "platform_billing_cycle_cap"
    assert unavailable[-1]["data"]["supersedes"] == "owli_weekly_budget"
