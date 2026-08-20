from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def _exa_key() -> str:
    return str(uuid.UUID(int=1))


def _tavily_key() -> str:
    return "tvly-" + "unit-test-placeholder"


def _env_file(tmp_path: Path, *, exa: str | None, tavily: str | None) -> Path:
    lines = []
    if exa is not None:
        lines.append(f"EXA_API_KEY={exa}")
    if tavily is not None:
        lines.append(f"TAVILY_API_KEY={tavily}")
    path = tmp_path / "provider.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FakeHttp:
    def __init__(self, *responses: dict | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "payload": dict(payload),
            "timeout": timeout,
        })
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_只从指定_env_读取且_Exa_请求与证据映射固定(tmp_path, monkeypatch) -> None:
    from app.sources import web_search

    monkeypatch.setenv("EXA_API_KEY", "进程环境里的值不得使用")
    env_path = _env_file(tmp_path, exa=_exa_key(), tavily=None)
    http = FakeHttp({
        "results": [{
            "id": "exa-item-1",
            "title": "飞书定价与套餐",
            "url": "https://example.com/feishu-pricing",
            "text": "正文摘录" * 500,
            "author": "Example Author",
            "publishedDate": "2026-08-01T00:00:00Z",
        }]
    })

    result = web_search.search(
        "飞书 竞品 协作工具 定价",
        "90d",
        env_path=env_path,
        http_post=http,
        clock=lambda: "2026-08-20T00:00:00+00:00",
    )

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == "https://api.exa.ai/search"
    assert call["headers"]["x-api-key"] == _exa_key()
    assert call["payload"]["query"] == "飞书 竞品 协作工具 定价"
    assert call["payload"]["type"] == "neural"
    assert call["payload"]["numResults"] == 10
    assert call["payload"]["contents"] == {
        "text": {"maxCharacters": 1200}
    }
    evidence = result[0]
    assert evidence["platform"] == "web_search"
    assert evidence["permalink"] == "https://example.com/feishu-pricing"
    assert evidence["platform_item_id"] == "exa-item-1"
    assert evidence["title"] == "飞书定价与套餐"
    assert len(evidence["content_excerpt"]) <= 1200
    assert evidence["published_at"] == "2026-08-01T00:00:00+00:00"
    assert evidence["fetched_at"] == "2026-08-20T00:00:00+00:00"
    assert evidence["raw_metrics"] == {}
    assert evidence["extra"]["provider"] == "exa"
    datetime.fromisoformat(evidence["fetched_at"])


def test_Exa_正常空结果返回空数组且不降级(tmp_path) -> None:
    from app.sources import web_search

    env_path = _env_file(tmp_path, exa=_exa_key(), tavily=_tavily_key())
    http = FakeHttp({"results": []}, {"results": [{"url": "https://backup"}]})
    events = []

    result = web_search.search(
        "没有命中的查询",
        "90d",
        env_path=env_path,
        http_post=http,
        on_event=events.append,
        clock=lambda: "2026-08-20T00:00:00+00:00",
    )

    assert result == []
    assert len(http.calls) == 1
    assert len(events) == 1
    assert events[0].outcome == "empty"
    assert events[0].raw == {
        "source_id": "web_search",
        "provider": "exa",
        "outcome": "empty",
        "count": 0,
    }


@pytest.mark.parametrize(
    ("exa", "tavily", "variable"),
    [
        ("not-a-uuid", None, "EXA_API_KEY"),
        (None, "wrong-prefix", "TAVILY_API_KEY"),
    ],
)
def test_凭证格式错误拒起且报错不泄露原值(
    tmp_path, exa, tavily, variable
) -> None:
    from app.sources import web_search

    env_path = _env_file(tmp_path, exa=exa, tavily=tavily)
    secret = exa or tavily

    with pytest.raises(web_search.CredentialError) as captured:
        web_search.search("飞书", "90d", env_path=env_path, http_post=FakeHttp())

    assert variable in str(captured.value)
    assert secret not in str(captured.value)


def test_模块自声明_web_search_工具映射() -> None:
    from app.sources import web_search

    assert web_search.SOURCE_SPEC.source_id == "web_search"
    assert web_search.SOURCE_SPEC.tool_name == "source.web_search"
    assert web_search.SOURCE_SPEC.entrypoint is web_search.search
    assert json.dumps(web_search.SOURCE_SPEC.source_id) == '"web_search"'


@pytest.mark.parametrize("primary_failure", [429, 500, None])
def test_Exa_报错或超额才降级_Tavily_并落事件(
    tmp_path, primary_failure
) -> None:
    from app.sources import web_search

    env_path = _env_file(tmp_path, exa=_exa_key(), tavily=_tavily_key())
    failure = (
        web_search.ProviderRequestError("exa", primary_failure)
        if primary_failure is not None
        else RuntimeError("transport unavailable")
    )
    answer = "这是模型生成的综述，不是原文证据"
    http = FakeHttp(
        failure,
        {
            "answer": answer,
            "results": [{
                "title": "飞书与协作工具定价比较",
                "url": "https://example.org/pricing-review",
                "content": "搜索片段",
                "raw_content": "落地页原文" * 500,
                "published_date": "2026-08-18T00:00:00Z",
            }],
        },
    )
    events = []

    result = web_search.search(
        "飞书 竞品 协作工具 定价",
        "90d",
        env_path=env_path,
        http_post=http,
        on_event=events.append,
        log_root=tmp_path,
        clock=lambda: "2026-08-20T00:00:00+00:00",
    )

    assert [call["url"] for call in http.calls] == [
        "https://api.exa.ai/search",
        "https://api.tavily.com/search",
    ]
    tavily_call = http.calls[1]
    assert tavily_call["headers"]["Authorization"] == f"Bearer {_tavily_key()}"
    assert tavily_call["payload"]["search_depth"] == "advanced"
    assert tavily_call["payload"]["include_answer"] is True
    assert tavily_call["payload"]["include_raw_content"] == "text"
    assert tavily_call["payload"]["max_results"] == 10
    assert len(result) == 1
    evidence = result[0]
    assert evidence["extra"] == {
        "provider": "tavily",
        "freshness_degraded_source": "fetched_at",
    }
    assert evidence["published_at"] == evidence["fetched_at"]
    assert answer not in json.dumps(evidence, ensure_ascii=False)
    assert events[0].route_state == "FAILOVER"
    assert events[0].failover_target == "tavily"
    assert events[0].raw["provider"] == "exa"
    assert events[1].outcome == "lead"
    assert events[1].raw["answer"] == answer
    routing_logs = list((tmp_path / "routing").glob("*.jsonl"))
    assert len(routing_logs) == 1
    log_text = routing_logs[0].read_text(encoding="utf-8")
    assert events[0].text in log_text
    assert _exa_key() not in log_text
    assert _tavily_key() not in log_text


def test_缺少_Exa_key_直接以结构化原因降级_Tavily(tmp_path) -> None:
    from app.sources import web_search

    env_path = _env_file(tmp_path, exa=None, tavily=_tavily_key())
    http = FakeHttp({"answer": None, "results": []})
    events = []

    result = web_search.search(
        "飞书",
        "30d",
        env_path=env_path,
        http_post=http,
        on_event=events.append,
        log_root=tmp_path,
        clock=lambda: "2026-08-20T00:00:00+00:00",
    )

    assert result == []
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == "https://api.tavily.com/search"
    assert events[0].route_state == "FAILOVER"
    assert "EXA_API_KEY 缺失" in events[0].text
    assert events[1].outcome == "empty"


def test_Tavily_证据经_M3a_打分归一化后走_Store_入库且_answer_隔离(
    tmp_path,
) -> None:
    from app.sources import web_search
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(
        id="r-web",
        title="网页搜索验收",
        research_question="飞书竞品如何定价？",
        created_at="2026-08-20T00:00:00+00:00",
    )
    env_path = _env_file(tmp_path, exa=_exa_key(), tavily=_tavily_key())
    answer = "绝不能进入 evidence 的 Tavily 模型综述"
    http = FakeHttp(
        web_search.ProviderRequestError("exa", 429),
        {
            "answer": answer,
            "results": [{
                "title": "协作工具定价实测",
                "url": "https://example.net/collaboration-pricing",
                "content": "搜索片段",
                "raw_content": "具名落地页正文",
                "published_date": None,
            }],
        },
    )

    result = web_search.collect_and_store(
        "飞书 竞品 协作工具 定价",
        "90d",
        report_id="r-web",
        goal_id="goal-3",
        agent_name="official-doc-collector",
        store=store,
        env_path=env_path,
        http_post=http,
        log_root=tmp_path / "logs",
        clock=lambda: "2026-08-20T00:00:00+00:00",
        id_factory=lambda: "ev-web-1",
    )

    assert len(result) == 1
    evidence = result[0]
    assert evidence["id"] == "ev-web-1"
    assert evidence["normalized_score"] is None
    assert evidence["norm_method"] == "none"
    assert evidence["norm_context"]["reason"] == "no_metric_available"
    assert evidence["norm_context"]["degraded"] == {
        "provider": "tavily",
        "field": "published_at",
        "source": "fetched_at",
    }
    assert [
        evidence[field]
        for field in (
            "score_authority",
            "score_freshness",
            "score_crossref",
            "score_completeness",
            "score_independence",
        )
    ] == [1, 1, 1, 1, 1]
    assert evidence["rated_by"] == "rule:reliability@v1"
    assert "时效1:抓取时刻兜底" in evidence["rating_notes"]

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT raw_metrics, normalized_score, norm_method, norm_context, "
            "rating_notes, rated_by, content_excerpt, extra "
            "FROM evidence WHERE id = ?",
            ("ev-web-1",),
        ).fetchone()
    assert row is not None
    persisted = "\n".join("" if value is None else str(value) for value in row)
    assert answer not in persisted
    assert row[0] == "{}"
    assert row[1] is None
    assert row[2] == "none"
    assert json.loads(row[3])["reason"] == "no_metric_available"
