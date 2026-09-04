from __future__ import annotations

import uuid
from pathlib import Path


def _env_file(tmp_path: Path, *, serper: bool = True) -> Path:
    lines = [f"EXA_API_KEY={uuid.UUID(int=2)}"]
    if serper:
        lines.append("SERPER_API_KEY=unit-test-serper-key")
    path = tmp_path / ("with-serper.env" if serper else "without-serper.env")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FakeHttp:
    def __init__(self, *responses: dict | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _exa_result() -> dict:
    return {
        "results": [{
            "id": "exa-1",
            "title": "Exa 结果",
            "url": "http://example.com/a/?utm_source=exa#section",
            "text": "Exa 正文",
            "author": "作者",
            "publishedDate": "2026-09-01T00:00:00Z",
        }]
    }


def test_开关关闭时_serper_凭证不改变既有输出请求与事件(tmp_path, monkeypatch) -> None:
    from app.sources import web_search

    monkeypatch.delenv("OWLI_WEB_SEARCH_GOOGLE", raising=False)
    without = FakeHttp(_exa_result())
    with_key = FakeHttp(_exa_result())
    events_without, events_with = [], []

    expected = web_search.search(
        "OpenAI vs Claude Code", "30d", env_path=_env_file(tmp_path, serper=False),
        http_post=without, on_event=events_without.append,
        clock=lambda: "2026-09-04T00:00:00+00:00",
    )
    actual = web_search.search(
        "OpenAI vs Claude Code", "30d", env_path=_env_file(tmp_path, serper=True),
        http_post=with_key, on_event=events_with.append,
        clock=lambda: "2026-09-04T00:00:00+00:00",
    )

    assert actual == expected
    assert with_key.calls == without.calls
    assert events_with == events_without == []


def test_开关开启后只并入_organic_正文与片段降级并去重截断(tmp_path, monkeypatch) -> None:
    from app.sources import web_search

    monkeypatch.setenv("OWLI_WEB_SEARCH_GOOGLE", "1")
    google = {
        "organic": [
            {"title": "重复", "link": "https://example.com/a?fbclid=tracking"},
            {"title": "正文命中", "link": "https://www.example.org/body/", "snippet": "旧片段", "date": "2 days ago"},
            {"title": "片段降级", "link": "https://fallback.example.net/post", "snippet": "保底片段", "date": "not-a-date"},
        ],
        "topStories": [{"title": "不得进入", "link": "https://news.example/top"}],
        "peopleAlsoAsk": [{"question": "不得进入"}],
        "ads": [{"title": "不得进入", "link": "https://ads.example"}],
    }
    http = FakeHttp(_exa_result(), google)
    page_calls: list[str] = []

    def page_text(url: str) -> str | None:
        page_calls.append(url)
        return "抓到的页面正文" * 200 if "example.org" in url else None

    events = []
    result = web_search.search(
        "OpenAI vs Claude Code", "30d", max_results=3,
        env_path=_env_file(tmp_path), http_post=http,
        page_text_fetcher=page_text, on_event=events.append,
        clock=lambda: "2026-09-04T00:00:00+00:00",
    )

    assert [call["url"] for call in http.calls] == [
        "https://api.exa.ai/search", "https://google.serper.dev/search",
    ]
    google_call = http.calls[1]
    assert google_call["headers"]["X-API-KEY"] == "unit-test-serper-key"
    assert google_call["payload"]["q"] == "OpenAI vs Claude Code"
    assert google_call["payload"]["num"] == 3
    assert page_calls == ["https://www.example.org/body/", "https://fallback.example.net/post"]
    assert [item["extra"]["provider"] for item in result] == ["exa", "google", "google"]
    assert result[1]["content_excerpt"] == ("抓到的页面正文" * 200)[:1200]
    assert result[1]["author_name"] == "example.org"
    assert result[1]["published_at"] == "2026-09-02T00:00:00+00:00"
    assert result[2]["content_excerpt"] == "保底片段"
    assert result[2]["published_at"] is None
    assert all(item["grade"] != "D" for item in result if item["extra"]["provider"] == "google")
    assert not any("不得进入" in str(item) for item in result)
    fallback = next(event for event in events if event.raw.get("event") == "web_search_page_text_fallback")
    assert fallback.raw["permalink"] == "https://fallback.example.net/post"
    summary = next(event for event in events if event.raw.get("event") == "web_search_provider_result")
    assert summary.raw == {
        "event": "web_search_provider_result", "source_id": "web_search",
        "provider": "google", "hits": 3, "deduped": 1,
        "page_text_ok": 1, "page_text_fallback": 1,
    }


def test_google_失败只发供应商失败事件且保留_exa_结果(tmp_path, monkeypatch) -> None:
    from app.sources import web_search

    monkeypatch.setenv("OWLI_WEB_SEARCH_GOOGLE", "1")
    http = FakeHttp(_exa_result(), web_search.ProviderRequestError("google", 502))
    events = []

    result = web_search.search(
        "国内大家对豆包的看法", "30d", env_path=_env_file(tmp_path),
        http_post=http, on_event=events.append, log_root=tmp_path,
        clock=lambda: "2026-09-04T00:00:00+00:00",
    )

    assert [item["extra"]["provider"] for item in result] == ["exa"]
    failed = next(event for event in events if event.is_error)
    assert failed.raw["provider"] == "google"
    assert failed.raw["status_code"] == 502
    assert failed.scope == "source.web_search"

