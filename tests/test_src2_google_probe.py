from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def _env(tmp_path: Path, *keys: str) -> Path:
    path = tmp_path / ".env"
    path.write_text("".join(f"{key}=secret\n" for key in keys), encoding="utf-8")
    return path


def _spec(fn):
    return SimpleNamespace(entrypoint=fn, window=object())


def test_开关关闭时_web_search_探活不注入_google_参数(tmp_path, monkeypatch) -> None:
    from app.sources_probe import probe_sources

    monkeypatch.delenv("OWLI_WEB_SEARCH_GOOGLE", raising=False)
    calls = []

    def search(query, window, **kwargs):
        calls.append(kwargs)
        return [{"id": 1}]

    result = asyncio.run(probe_sources(
        ["web_search"], registry={"web_search": _spec(search)},
        env_path=_env(tmp_path, "EXA_API_KEY"),
    ))

    assert result["web_search"]["ok"] is True
    assert "on_event" not in calls[0]
    assert "page_text_fetcher" not in calls[0]


def test_开关开启时_探活必须看到_google_供应商命中(tmp_path, monkeypatch) -> None:
    from app.sources_probe import probe_sources

    monkeypatch.setenv("OWLI_WEB_SEARCH_GOOGLE", "1")
    calls = []

    def search(query, window, **kwargs):
        calls.append(kwargs)
        kwargs["on_event"](SimpleNamespace(raw={
            "event": "web_search_provider_result", "provider": "google", "hits": 5,
        }))
        return [{"id": 1}]

    env_path = _env(tmp_path, "EXA_API_KEY", "SERPER_API_KEY")
    result = asyncio.run(probe_sources(
        ["web_search"], registry={"web_search": _spec(search)}, env_path=env_path,
    ))

    assert result["web_search"]["ok"] is True
    assert calls[0]["env_path"] == env_path
    assert calls[0]["page_text_fetcher"]("https://example.com") is None


def test_开关开启但缺_key_或无_google_事件时探活失败(tmp_path, monkeypatch) -> None:
    from app.sources_probe import probe_sources

    monkeypatch.setenv("OWLI_WEB_SEARCH_GOOGLE", "1")

    def search(query, window, **kwargs):
        return [{"id": 1}]

    missing_key = asyncio.run(probe_sources(
        ["web_search"], registry={"web_search": _spec(search)},
        env_path=_env(tmp_path, "EXA_API_KEY"),
    ))
    no_event = asyncio.run(probe_sources(
        ["web_search"], registry={"web_search": _spec(search)},
        env_path=_env(tmp_path, "EXA_API_KEY", "SERPER_API_KEY"),
    ))

    assert missing_key["web_search"]["failure"] == "missing_credentials"
    assert "google_probe_unavailable" in no_event["web_search"]["failure"]

