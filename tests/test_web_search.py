from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest


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
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "payload": dict(payload),
            "timeout": timeout,
        })
        return self.responses.pop(0)


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
