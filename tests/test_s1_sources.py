from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


class ImmediateGate:
    def wait(self) -> None:
        return None


def _cn_collection_plan(source_id: str) -> dict:
    from tests.plan_factory import make_agent, make_plan_dict

    plan = make_plan_dict()
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "产品主要面向中国大陆用户。"
    agent = make_agent("data-collection", "goal-1")
    agent["entity"] = "通义听悟"
    agent["capability"].update({
        "profile": "web-collector",
        "tools": [f"source.{source_id}", "fs.write", "db.write"],
        "sources": [source_id],
        "network": "sources_only",
    })
    agent["output"] = {
        "format": "json",
        "shape": "array",
        "path": "goals/goal-1/data-collection.json",
        "validators": [
            "file_exists",
            "json_array_min_items:1",
            "each_item_has:permalink,fetched_at",
        ],
    }
    agent["chapter"] = {
        "chapter_id": "ch-1",
        "chapter_type": "collection",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {
            "inputs": [],
            "task": agent["task"],
            "acceptance": ["文件存在且通过 validators"],
        },
        "closing": {
            "output": {"path": agent["output"]["path"]},
            "entities": ["通义听悟"],
            "expected_count": 1,
            "notes": {},
        },
    }
    plan["goals"][0]["agents"] = [agent]
    return plan


def test_规划与批准闸门共用三源市场归属() -> None:
    from app.plan.generate import _MARKET_SOURCES
    from app.plan.lint import _SOURCE_MARKET_PROFILES

    assert _MARKET_SOURCES == _SOURCE_MARKET_PROFILES
    assert {
        source_id: {
            profile
            for profile, sources in _MARKET_SOURCES.items()
            if source_id in sources
        }
        for source_id in ("xhs", "douyin", "reddit")
    } == {
        "xhs": {"cn_product"},
        "douyin": {"cn_product"},
        "reddit": {"global_product"},
    }


def test_国内产品规划候选集包含小红书与抖音() -> None:
    from app.plan.generate import _MARKET_SOURCES

    assert {"xhs", "douyin"} <= _MARKET_SOURCES["cn_product"]


def test_三源提示词采集条数使用真实_limit_参数() -> None:
    from app.plan.generate import _SOURCE_LIMIT_PARAMETERS

    assert {
        source_id: _SOURCE_LIMIT_PARAMETERS[source_id]
        for source_id in ("xhs", "douyin", "reddit")
    } == {"xhs": "limit", "douyin": "limit", "reddit": "limit"}


def test_国内产品小红书采集章能通过_lint() -> None:
    from app.plan.lint import lint

    assert lint(_cn_collection_plan("xhs"))["errors"] == []


def test_国内产品_reddit_采集章仍被_lint_拦截() -> None:
    from app.plan.lint import lint

    errors = lint(_cn_collection_plan("reddit"))["errors"]
    assert len(errors) == 1
    assert errors[0].startswith("[规则23]")
    assert "source_id=reddit" in errors[0]
    assert "cn_product" in errors[0]


def _xhs_note(index: int) -> dict:
    return {
        "model_type": "note",
        "note": {
            "id": f"note-{index}",
            "title": f"标题 {index}",
            "desc": f"正文摘要 {index}",
            "type": "normal",
            "xsec_token": f"signed-{index}=",
            "liked_count": index,
            "comments_count": index + 1,
            "collected_count": index + 2,
            "user": {
                "nickname": f"作者 {index}",
                "userid": f"user-{index}",
                "red_id": f"red-{index}",
                "red_official_verified": False,
            },
            "corner_tag_info": [
                {"type": "publish_time", "text": "3 day(s) ago"}
            ],
        },
    }


def test_三源声明可被自动注册() -> None:
    from app.sources.registry import discover_sources

    sources = discover_sources()
    assert sources["xhs"].tool_name == "source.xhs"
    assert sources["douyin"].tool_name == "source.douyin"
    assert sources["reddit"].tool_name == "source.reddit"


def test_两档墙钟通过运行配置自检且包含三源限额() -> None:
    from app.adapters.selfcheck import validate_runtime_config
    from app.config import load_research_scale_config

    config = load_research_scale_config()

    assert config.fast.chapter_wall_clock_seconds == 330
    assert config.standard.chapter_wall_clock_seconds == 1800
    validate_runtime_config(config)
    for profile in (config.fast, config.standard):
        assert {"xhs", "douyin", "reddit"} <= profile.source_item_limits.keys()


def test_小红书原生过滤_双会话翻页_签名链接与相对时间不落库() -> None:
    from app.sources import xhs

    calls = []

    def http_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        page = int(parse_qs(urlparse(url).query)["page"][0])
        payload = {
            "code": 200,
            "data": {
                "code": 200,
                "success": True,
                "data": {"items": [_xhs_note(page)]},
                "search_id": "search-1",
                "search_session_id": "session-1",
                "next_page": page + 1 if page == 1 else None,
            },
        }
        return xhs.HttpResponse(200, payload)

    result = xhs.search(
        "AI 会议",
        "7d",
        limit=2,
        sort_type="comment_descending",
        note_type="普通笔记",
        token="runtime-secret",
        http_get=http_get,
        rate_gate=ImmediateGate(),
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert len(result) == 2
    first_query = parse_qs(urlparse(calls[0][0]).query)
    second_query = parse_qs(urlparse(calls[1][0]).query)
    assert first_query["sort_type"] == ["comment_descending"]
    assert first_query["note_type"] == ["普通笔记"]
    assert first_query["time_filter"] == ["一周内"]
    assert second_query["search_id"] == ["search-1"]
    assert second_query["search_session_id"] == ["session-1"]
    assert result[0]["published_at"] is None
    assert "3 day" not in json.dumps(result, ensure_ascii=False)
    assert result[0]["permalink"].startswith(
        "https://www.xiaohongshu.com/explore/note-1?xsec_token=signed-1%3D"
    )
    assert result[0]["score_crossref"] is None
    assert "交叉?:缺断言血缘簇" in result[0]["rating_notes"]


def test_小红书可强制走tool_unavailable且不发真实请求() -> None:
    from app.sources import xhs

    events = []
    result = xhs.search(
        "AI",
        "7d",
        force_unavailable=True,
        on_event=events.append,
        http_get=lambda *_: pytest.fail("强制不可用时不得请求 TikHub"),
    )

    assert result == []
    assert events == [{
        "type": "source_unavailable",
        "data": {
            "source": "xhs",
            "reason": "tool_unavailable",
            "closed_reason": "tikhub_forced_unavailable",
            "provider": "tikhub",
            "fallback_available": False,
            "forced": True,
            "task_continues": True,
        },
    }]


def _douyin_video(index: int, comments: int) -> dict:
    return {
        "aweme_info": {
            "aweme_id": f"video-{index}",
            "desc": f"视频文案 {index}",
            "create_time": 1787799195,
            "author": {
                "uid": f"uid-{index}",
                "sec_uid": f"sec-{index}",
                "nickname": f"作者 {index}",
                "is_verified": False,
            },
            "statistics": {
                "digg_count": index + 10,
                "comment_count": comments,
                "share_count": 2,
                "collect_count": 3,
            },
        }
    }


def test_抖音搜索并全取评论正文_完整度实际到2() -> None:
    from app.sources import douyin

    calls = []

    def http_request(method, url, headers, body, timeout):
        calls.append((method, url, headers, body, timeout))
        if urlparse(url).path == douyin._SEARCH_PATH:
            return douyin.HttpResponse(200, {
                "code": 200,
                "data": {
                    "items": [_douyin_video(1, 1), _douyin_video(2, 8)],
                    "pagination": {"has_more": 0},
                },
            })
        query = parse_qs(urlparse(url).query)
        assert query["count"] == ["20"]
        return douyin.HttpResponse(200, {
            "code": 200,
            "data": {
                "comments": [{"cid": "c-1", "text": "真实评论正文"}],
                "cursor": 20,
                "has_more": 0,
                "total": 1,
            },
        })

    result = douyin.search(
        "AI 助手",
        "30d",
        limit=2,
        comment_video_limit=1,
        token="runtime-secret",
        http_request=http_request,
        rate_gate=ImmediateGate(),
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert len(result) == 2
    assert calls[0][0] == "POST"
    search_body = json.loads(calls[0][3])
    assert search_body == {
        "keyword": "AI 助手",
        "offset": 0,
        "page": 1,
        "search_id": "",
        "backtrace": "",
    }
    complete = next(item for item in result if item["platform_item_id"] == "video-1")
    assert complete["score_completeness"] == 2
    assert "完整2:评论区全取1条" in complete["rating_notes"]
    assert "真实评论正文" in complete["content_excerpt"]
    assert complete["extra"]["comments_complete"] is True
    assert all(item["score_crossref"] is None for item in result)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/douyin/app/v3/fetch_multi_video_high_quality_play_url",
        "/api/v1/douyin/app/v3/fetch_multi_video_v2",
        "/api/v1/douyin/app/v3/fetch_multi_video_statistics",
    ],
)
def test_抖音高价接口被异常拦截(path) -> None:
    from app.sources.douyin import _assert_allowed_path

    with pytest.raises(ValueError, match="禁止调用高价"):
        _assert_allowed_path(path)


def _sse_result(result: dict) -> bytes:
    payload = {"jsonrpc": "2.0", "id": 1, "result": result}
    return f"event: message\ndata: {json.dumps(payload)}\n\n".encode()


def _mcp_tool_result(value: dict) -> bytes:
    return _sse_result({
        "content": [{"type": "text", "text": json.dumps(value)}]
    })


def _reddit_item(index: int) -> dict:
    return {
        "redditId": f"t3_post{index}",
        "subreddit": "productivity",
        "title": f"Reddit 标题 {index}",
        "body": f"正文 {index}",
        "permalink": f"/r/productivity/comments/post{index}/title/",
        "score": index,
        "numComments": index + 1,
        "upvote_ratio": 0.9,
        "redditCreatedAt": "2026-08-20T00:00:00.000Z",
        "author": f"author{index}",
        "status": "normal",
        "removedByCategory": None,
    }


def test_reddit先免费Dataset_不足才花一次live_read() -> None:
    from app.sources import reddit

    tool_names = []

    def http_request(method, url, headers, body, timeout):
        payload = json.loads(body) if body else {}
        if payload.get("method") == "initialize":
            return reddit.HttpResponse(
                200, {"Mcp-Session-Id": "session-1"},
                _sse_result({"protocolVersion": "2025-03-26"}),
            )
        if payload.get("method") == "notifications/initialized":
            return reddit.HttpResponse(202, {}, b"")
        name = payload["params"]["name"]
        tool_names.append(name)
        value = (
            {"items": [_reddit_item(1)]}
            if name == "search_dataset"
            else {"items": [_reddit_item(2)]}
        )
        return reddit.HttpResponse(200, {}, _mcp_tool_result(value))

    events = []
    result = reddit.search(
        "AI meeting",
        "30d",
        limit=2,
        prowlo_token="prowlo-runtime",
        apify_token="apify-runtime",
        http_request=http_request,
        on_event=events.append,
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert tool_names == ["search_dataset", "social_search"]
    assert len(result) == 2
    assert all(item["norm_method"] == "none" for item in result)
    assert all(item["normalized_score"] is None for item in result)
    assert all(item["score_crossref"] is None for item in result)
    usage = next(event for event in events if event["type"] == "source_usage_reconciled")
    assert usage["data"]["calls"] == {
        "dataset_search": 1,
        "dataset_get_record": 0,
        "live_read": 1,
    }
    assert "prowlo-runtime" not in json.dumps(events, ensure_ascii=False)


def test_reddit主路径失败后走Apify异步三步且显式relevance() -> None:
    from app.sources import reddit

    calls = []
    statuses = iter(["RUNNING", "SUCCEEDED"])

    def http_request(method, url, headers, body, timeout):
        calls.append((method, url, body))
        if "api.prowlo.com" in url:
            return reddit.HttpResponse(503, {}, b"{}")
        path = urlparse(url).path
        if path.endswith("/runs"):
            actor_input = json.loads(body)
            assert actor_input["sort"] == "relevance"
            assert actor_input["skipComments"] is True
            assert "maxComments" not in actor_input
            return reddit.HttpResponse(
                201, {}, json.dumps({"data": {"id": "run-1"}}).encode()
            )
        if "/actor-runs/run-1" in path:
            status = next(statuses)
            data = {"status": status}
            if status == "SUCCEEDED":
                data["defaultDatasetId"] = "dataset-1"
            return reddit.HttpResponse(200, {}, json.dumps({"data": data}).encode())
        if "/datasets/dataset-1/items" in path:
            return reddit.HttpResponse(200, {}, json.dumps([_reddit_item(3)]).encode())
        pytest.fail(f"未预期请求：{url}")

    ticks = iter([0.0, 1.0, 2.0, 3.0])
    result = reddit.search(
        "AI meeting",
        "30d",
        limit=1,
        prowlo_token="prowlo-runtime",
        apify_token="apify-runtime",
        http_request=http_request,
        sleeper=lambda _: None,
        monotonic=lambda: next(ticks),
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    apify_paths = [urlparse(url).path for _, url, _ in calls if "apify.com" in url]
    assert apify_paths == [
        "/v2/acts/trudax~reddit-scraper-lite/runs",
        "/v2/actor-runs/run-1",
        "/v2/actor-runs/run-1",
        "/v2/datasets/dataset-1/items",
    ]
    assert len(result) == 1
    assert result[0]["extra"]["provider"] == "apify"
    assert result[0]["norm_method"] == "none"


def test_reddit丢弃Apify无标题条目并发出事件() -> None:
    from app.sources import reddit

    events = []

    def http_request(method, url, headers, body, timeout):
        if "api.prowlo.com" in url:
            return reddit.HttpResponse(503, {}, b"{}")
        path = urlparse(url).path
        if path.endswith("/runs"):
            return reddit.HttpResponse(
                201, {}, json.dumps({"data": {"id": "run-untitled"}}).encode()
            )
        if "/actor-runs/run-untitled" in path:
            data = {"status": "SUCCEEDED", "defaultDatasetId": "dataset-untitled"}
            return reddit.HttpResponse(200, {}, json.dumps({"data": data}).encode())
        if "/datasets/dataset-untitled/items" in path:
            untitled = {
                **_reddit_item(4),
                "title": None,
                "permalink": "/r/productivity/comments/post4/title/comment/p18mv0a",
            }
            return reddit.HttpResponse(
                200, {}, json.dumps([untitled, _reddit_item(5)]).encode()
            )
        pytest.fail(f"未预期请求：{url}")

    result = reddit.search(
        "AI meeting",
        "30d",
        limit=2,
        prowlo_token="prowlo-runtime",
        apify_token="apify-runtime",
        http_request=http_request,
        sleeper=lambda _: None,
        monotonic=lambda: 0.0,
        on_event=events.append,
        now=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert [item["title"] for item in result] == ["Reddit 标题 5"]
    dropped = next(event for event in events if event["type"] == "source_items_dropped")
    assert dropped["data"] == {
        "source": "reddit",
        "provider": "apify",
        "reason": "missing_title",
        "dropped": 1,
        "task_continues": True,
    }


def test_三源各五条可经固定DAO原子入evidence表(tmp_path) -> None:
    from app.reliability.scoring import normalize_evidence_metrics
    from app.sources.douyin import _to_evidence as douyin_evidence
    from app.sources.reddit import _to_evidence as reddit_evidence
    from app.sources.xhs import _to_evidence as xhs_evidence
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli.db"
    schema = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
    initialize_database_if_empty(database, schema)
    store = Store(database)
    store.create_report(
        id="r-s1",
        title="S-1 DAO 验证",
        research_question="三源能否原子入库",
        created_at="2026-08-27T00:00:00Z",
    )
    fetched_at = "2026-08-27T00:00:00+00:00"
    items = []
    for index in range(5):
        items.append(xhs_evidence(
            _xhs_note(index)["note"], query="q", fetched_at=fetched_at,
            time_filter="一周内",
        ))
        items.append(douyin_evidence(
            _douyin_video(index, 1)["aweme_info"], query="q",
            fetched_at=fetched_at, comments=[{"text": f"评论 {index}"}],
            comments_complete=True,
        ))
        items.append(reddit_evidence(
            _reddit_item(index), query="q", fetched_at=fetched_at,
            provider="prowlo",
        ))

    rows = []
    for index, item in enumerate(items):
        [normalized] = normalize_evidence_metrics(
            [item], computed_at=fetched_at, report_id="r-s1", goal_id="goal-1",
            queries=["q"],
        )
        rows.append({
            **normalized,
            "id": f"ev-s1-{index}",
            "report_id": "r-s1",
            "goal_id": "goal-1",
        })
    store.add_evidence_batch(rows)

    stored = store.list_evidence("r-s1")
    assert len(stored) == 15
    assert {platform: sum(row["platform"] == platform for row in stored)
            for platform in ("xhs", "douyin", "reddit")} == {
        "xhs": 5, "douyin": 5, "reddit": 5,
    }
    assert all(row["score_crossref"] is None for row in stored)
