"""§X-1 货 4：起跑前探活接口——假适配器三态（有数据 / 空 / 抛错）+ 超时 + 缺凭证 + 路由信封。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from app.sources_probe import configured_sources, probe_sources


def _env(tmp_path: Path, *keys: str) -> Path:
    env = tmp_path / ".env"
    env.write_text("".join(f"{key}=secret-{key}\n" for key in keys), encoding="utf-8")
    return env


def _spec(fn, *, window=True):
    return SimpleNamespace(entrypoint=fn, window=object() if window else None)


def test_三态与超时与缺凭证各自落到failure字段(tmp_path: Path) -> None:
    calls: dict[str, tuple] = {}

    def ok(query, window, **kw):
        calls["xhs"] = (query, window, kw); return [{"id": 1}, {"id": 2}]

    def empty(query, **kw):
        calls["douyin"] = (query, kw); return []

    def boom(query, window, **kw):
        raise RuntimeError("TikHub 401")

    def slow(query, **kw):
        time.sleep(0.5); return [{"id": 1}]

    registry = {"xhs": _spec(ok), "douyin": _spec(empty, window=False),
                "web_search": _spec(boom), "hacker_news": _spec(slow, window=False),
                "reddit": _spec(ok)}
    env = _env(tmp_path, "TIKHUB_API_KEY", "EXA_API_KEY")
    result = asyncio.run(probe_sources(
        ["xhs", "douyin", "web_search", "hacker_news", "reddit"],
        registry=registry, env_path=env, timeout_seconds=0.1,
    ))
    assert result["xhs"]["ok"] is True and result["xhs"]["items"] == 2
    assert calls["xhs"][1] == "30d" and calls["xhs"][2] == {"limit": 2}
    assert calls["douyin"][1] == {"limit": 1, "comment_video_limit": 1}
    assert result["douyin"] == {"ok": False, "items": 0, "elapsed_s": result["douyin"]["elapsed_s"], "failure": "empty"}
    assert result["web_search"]["ok"] is False and "RuntimeError: TikHub 401" == result["web_search"]["failure"]
    assert result["hacker_news"]["failure"].startswith("timeout>")
    assert result["reddit"]["failure"] == "missing_credentials"
    assert all(item["elapsed_s"] >= 0 for item in result.values())


def test_缺省探活集只含配了凭证或无需凭证的源(tmp_path: Path) -> None:
    registry = {"xhs": _spec(lambda *a, **k: []), "reddit": _spec(lambda *a, **k: []),
                "hacker_news": _spec(lambda *a, **k: [], window=False)}
    assert configured_sources(env_path=_env(tmp_path, "TIKHUB_API_KEY"), registry=registry) == [
        "hacker_news", "xhs",
    ]
    assert configured_sources(env_path=tmp_path / "absent.env", registry=registry) == ["hacker_news"]


def test_路由信封与未知源400(tmp_path: Path, monkeypatch) -> None:
    import sqlite3

    from app.api.main import create_app
    import app.sources_probe as probe_module

    schema = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(schema.read_text(encoding="utf-8"))
    application = create_app(database, schema, runs_root=tmp_path / "runs",
                             engine_probe=lambda: {})

    async def fake_probe(sources):
        if sources and "nope" in sources:
            raise KeyError("未注册的信息源：nope")
        return {"xhs": {"ok": True, "items": 2, "elapsed_s": 0.1, "failure": None},
                "douyin": {"ok": False, "items": 0, "elapsed_s": 0.2, "failure": "empty"}}

    monkeypatch.setattr(probe_module, "probe_sources", fake_probe)
    route = next(r for r in application.routes if getattr(r, "path", None) == "/api/sources/probe")

    body = asyncio.run(route.endpoint(sources="xhs,douyin"))
    assert body["ok"] is True and body["data"]["all_ok"] is False
    assert set(body["data"]["sources"]) == {"xhs", "douyin"}
    assert set(body["data"]["sources"]["xhs"]) == {"ok", "items", "elapsed_s", "failure"}

    bad = asyncio.run(route.endpoint(sources="nope"))
    assert bad.status_code == 400
