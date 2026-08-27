from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters import validation
from app.orchestrator.sectioning import _assemble
from app.reliability.backfill import backfill_report
from app.reliability.claims import (
    FIRSTHAND_SOURCES,
    ClaimsRegistrationError,
    register_claims,
)
from app.store.dao import Store


ROOT = Path(__file__).resolve().parents[1]


class NeverAdapter:
    async def run(self, *args, **kwargs):
        raise AssertionError("已有闭集标签时不应调用引擎")


def make_store(tmp_path: Path, report_id: str = "r-c1") -> Store:
    database = tmp_path / f"{report_id}.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            (ROOT / "app/store/schema.sql").read_text(encoding="utf-8")
        )
    store = Store(database)
    store.create_report(
        id=report_id,
        title="C1 夹具",
        research_question="验证断言到 grade",
        created_at="2026-08-27T10:00:00+08:00",
        completed_at="2026-08-27T11:00:00+08:00",
    )
    return store


def add_evidence(
    store: Store,
    report_id: str,
    evidence_id: str,
    *,
    platform: str,
    permalink: str,
    author: str | None,
    published_at: str | None = "2026-08-20T00:00:00+00:00",
    source_type: str = "comment",
    extra: dict | None = None,
) -> None:
    labels = {
        "authority_kind": (
            "community_high_signal" if author else "anonymous_or_unverifiable"
        ),
        "content_kind": "user_opinion",
        "interest_relation": "arms_length",
    }
    labels.update(extra or {})
    store.add_evidence(
        id=evidence_id,
        report_id=report_id,
        goal_id="goal-1",
        platform=platform,
        permalink=permalink,
        fetched_at="2026-08-27T00:00:00+00:00",
        published_at=published_at,
        source_type=source_type,
        fetch_method="official_api" if platform != "web_search" else "search_index",
        title=f"{evidence_id} 的独立标题",
        content_excerpt="可复核正文",
        author_name=author,
        extra=labels,
    )


def raw_claim(claim_id: str, evidence: list[dict], text: str = "可证否断言") -> dict:
    return {"id": claim_id, "text": text, "evidence": evidence}


def ref(url: str, **extra) -> dict:
    return {"permalink": url, **extra}


def test_断言登记双向可达且重复执行不追加(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    urls = ["https://example.com/a", "https://example.org/b"]
    add_evidence(store, "r-c1", "ev-a", platform="web_search", permalink=urls[0], author="甲")
    add_evidence(store, "r-c1", "ev-b", platform="web_search", permalink=urls[1], author="乙")
    claims = [raw_claim("c-01", [
        ref(urls[0], firsthand=True),
        ref(urls[1], stance="contradicts", origin_url="https://origin.example/x"),
    ])]

    first = register_claims(store, "r-c1", claims, source="chapter")
    second = register_claims(store, "r-c1", claims, source="chapter")

    assert first == second
    stored_claim = store.get_report("r-c1")["extra"]["claims"][0]
    assert stored_claim["evidence_ids"] == ["ev-a", "ev-b"]
    assert stored_claim["stance"] == {"ev-b": "contradicts"}
    assert stored_claim["firsthand"] == ["ev-a"]
    assert stored_claim["claims_source"] == "chapter"
    rows = {row["id"]: row for row in store.list_evidence("r-c1")}
    assert rows["ev-a"]["extra"]["claim_ids"] == ["c-01"]
    assert rows["ev-b"]["extra"]["claim_ids"] == ["c-01"]


def test_firsthand_声明来源区分撰写与存量回填(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    url = "https://example.com/firsthand"
    add_evidence(
        store, "r-c1", "ev-firsthand", platform="web_search",
        permalink=url, author="甲",
    )
    claims = [raw_claim("c-01", [ref(url, firsthand=True)])]

    register_claims(store, "r-c1", claims, source="chapter")
    writer = store.get_report("r-c1")["extra"]["claims"][0]
    register_claims(store, "r-c1", claims, source="backfill")
    backfill = store.get_report("r-c1")["extra"]["claims"][0]

    assert FIRSTHAND_SOURCES == {
        "declared_by_writer", "declared_by_backfill",
    }
    assert writer["firsthand_source"] in FIRSTHAND_SOURCES
    assert backfill["firsthand_source"] in FIRSTHAND_SOURCES
    assert writer["firsthand_source"] == "declared_by_writer"
    assert backfill["firsthand_source"] == "declared_by_backfill"
    assert writer["firsthand_source"] != backfill["firsthand_source"]


def test_悬空_permalink_不产生半份登记(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    add_evidence(
        store, "r-c1", "ev-a", platform="web_search",
        permalink="https://example.com/a", author="甲",
    )
    with pytest.raises(ClaimsRegistrationError, match="断言登记失败") as caught:
        register_claims(
            store,
            "r-c1",
            [raw_claim("c-01", [ref("https://missing.example/x")])],
            source="chapter",
        )
    assert "悬空 permalink" in caught.value.offenders[0]
    assert "claims" not in store.get_report("r-c1")["extra"]
    assert "claim_ids" not in store.list_evidence("r-c1")[0]["extra"]


def test_JSON_节显式_claims_聚合进父章信封(tmp_path: Path) -> None:
    section_root = tmp_path / "sections"
    section_root.mkdir()
    payload = {
        "markdown": "## 结论\n\n- 结论 [S01]\n\n## 信息源\n\n- [S01] https://example.com/a",
        "claims": [raw_claim("c-01", [ref("https://example.com/a")])],
    }
    (section_root / "sec-1.md").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    output = tmp_path / "report.json"
    agent = SimpleNamespace(
        agent_id="report-writing", chapter={"chapter_id": "ch-1"},
        output={"shape": "object"},
    )
    _assemble(
        plan=SimpleNamespace(title="夹具"), agent=agent, output_path=output,
        output_format="json", section_root=section_root,
        sections=[{
            "section_id": "ch-1/sec-1", "filename": "sec-1.md",
            "title": "范围", "goal_id": "goal-1",
        }],
        rows=[{
            "chapter_id": "ch-1/sec-1", "goal_id": "goal-1",
            "status": "done", "reason": None,
        }],
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["claims"] == payload["claims"]
    assert document["sections"][0]["markdown"].startswith("## 结论")


def test_backfill_接通_PASS_X禁推_HN线程上限与次断言(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    urls = {
        "h1": "https://news.ycombinator.com/item?id=101",
        "h2": "https://news.ycombinator.com/item?id=102",
        "h3": "https://news.ycombinator.com/item?id=103",
        "x1": "https://x.com/a/status/201",
        "x2": "https://x.com/b/status/202",
    }
    for key, author, published in (
        ("h1", "甲", "2026-08-01T00:00:00+00:00"),
        ("h2", "乙", "2026-08-10T00:00:00+00:00"),
        ("h3", "丙", "2026-08-20T00:00:00+00:00"),
    ):
        add_evidence(
            store, "r-c1", f"ev-{key}", platform="hacker_news",
            permalink=urls[key], author=author,
            published_at=published,
            extra={"story_id": 999},
        )
    for key, author, published in (
        ("x1", "丁", "2026-07-01T00:00:00+00:00"),
        ("x2", "戊", "2026-07-10T00:00:00+00:00"),
    ):
        add_evidence(
            store, "r-c1", f"ev-{key}", platform="x",
            permalink=urls[key], author=author,
            published_at=published,
            extra={"conversation_id": key},
        )
    claims = [
        raw_claim("c-01", [ref(urls["h1"], firsthand=True), ref(urls["h2"], firsthand=True)]),
        raw_claim("c-02", [
            ref(urls["h1"], firsthand=True),
            ref(urls["h2"], firsthand=True),
            ref(urls["h3"], firsthand=True),
        ]),
        raw_claim("c-03", [ref(urls["x1"], firsthand=True), ref(urls["x2"], firsthand=True)]),
        raw_claim("c-04", [ref(urls["h1"], firsthand=True), ref(urls["x1"], firsthand=True)]),
    ]
    register_claims(store, "r-c1", claims, source="chapter")

    result = asyncio.run(backfill_report(
        store,
        "r-c1",
        adapter=NeverAdapter(),
        runs_root=tmp_path / "runs",
    ))

    stored = {
        claim["id"]: claim
        for claim in store.get_report("r-c1")["extra"]["claims"]
    }
    assert (stored["c-01"]["k"], stored["c-01"]["verdict"]) == (2, "PASS")
    assert stored["c-02"]["k"] == 2  # extra.story_id 被提升后触发 HN 上限。
    assert (stored["c-03"]["k"], stored["c-03"]["verdict"]) == (1, "SINGLE")
    assert (stored["c-04"]["k"], stored["c-04"]["verdict"]) == (2, "PASS")
    rows = {row["id"]: row for row in store.list_evidence("r-c1")}
    assert rows["ev-h1"]["score_crossref"] == 2
    assert rows["ev-h1"]["grade"] is not None
    assert rows["ev-h1"]["extra"]["crossref_secondary"]["c-04"]["verdict"] == "PASS"
    assert result.weak_claims == []


def test_backfill_交叉维从第一遍起就是不动点(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    evidence = (
        (
            "ev-hn1", "hacker_news",
            "https://news.ycombinator.com/item?id=401", "甲",
            "2026-08-01T00:00:00+00:00",
        ),
        (
            "ev-ws1", "web_search", "https://openai.com/index/one", "乙",
            "2026-08-10T00:00:00+00:00",
        ),
        (
            "ev-ws2", "web_search", "https://anthropic.com/news/two", "丙",
            "2026-08-20T00:00:00+00:00",
        ),
    )
    for evidence_id, platform, permalink, author, published_at in evidence:
        add_evidence(
            store,
            "r-c1",
            evidence_id,
            platform=platform,
            permalink=permalink,
            author=author,
            published_at=published_at,
        )
    register_claims(
        store,
        "r-c1",
        [raw_claim("c-01", [
            ref(permalink, firsthand=True)
            for _, _, permalink, _, _ in evidence
        ])],
        source="chapter",
    )

    def snapshot() -> dict:
        rows = {row["id"]: row for row in store.list_evidence("r-c1")}
        return {
            "evidence": {
                evidence_id: {
                    "score_crossref": row["score_crossref"],
                    "crossref_verdict": row["extra"]["crossref_verdict"],
                    "rating_notes": row["rating_notes"],
                }
                for evidence_id, row in rows.items()
            },
            "claims": store.get_report("r-c1")["extra"]["claims"],
        }

    asyncio.run(backfill_report(
        store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs"
    ))
    first = snapshot()
    asyncio.run(backfill_report(
        store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs"
    ))

    assert snapshot() == first


def test_D级独撑进入_weak_claims_补一条C以上后解除(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    weak_url = "https://weak.example/snippet"
    strong_url = "https://news.ycombinator.com/item?id=301"
    add_evidence(
        store,
        "r-c1",
        "ev-weak",
        platform="web_search",
        permalink=weak_url,
        author=None,
        published_at=None,
        source_type="search_snippet",
    )
    register_claims(
        store, "r-c1", [raw_claim("c-01", [ref(weak_url)])], source="chapter"
    )
    first = asyncio.run(backfill_report(
        store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs"
    ))
    assert store.list_evidence("r-c1")[0]["grade"] == "D"
    assert first.weak_claims == ["c-01"]

    add_evidence(
        store,
        "r-c1",
        "ev-strong",
        platform="hacker_news",
        permalink=strong_url,
        author="强作者",
    )
    register_claims(
        store,
        "r-c1",
        [raw_claim("c-01", [ref(weak_url), ref(strong_url, firsthand=True)])],
        source="chapter",
    )
    second = asyncio.run(backfill_report(
        store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs"
    ))
    assert second.weak_claims == []
    assert any(
        row["grade"] in {"A", "B", "C"}
        for row in store.list_evidence("r-c1")
        if row["id"] == "ev-strong"
    )


class ClaimsReader:
    def __init__(self, claims):
        self.claims = claims

    def read_validation_path(self, path, report_id):
        assert path == "reports.extra.claims"
        assert report_id == "r-c1"
        return self.claims


def validator_ctx(tmp_path: Path, claims) -> validation.Ctx:
    output = tmp_path / "runs/r-c1/goals/goal-1/output.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("{}", encoding="utf-8")
    return validation.Ctx(
        output_path=output,
        output_format="json",
        research_id="r-c1",
        goal_id="goal-1",
        agent_id="report-writing",
        read_text=lambda: output.read_text(encoding="utf-8"),
        read_json=lambda: {},
        store=ClaimsReader(claims),
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )


def test_claims_backfilled_逐断言报告缺键且注册表总数不变(tmp_path: Path) -> None:
    failed = validation.validate(
        validator_ctx(tmp_path, [
            {"id": "c-01", "clusters": ["cl-01"], "k": 1},
            {"id": "c-02", "clusters": ["cl-01"], "k": 1, "verdict": "SINGLE"},
        ]),
        ["claims_backfilled:clusters,k,verdict"],
    )
    passed = validation.validate(
        validator_ctx(tmp_path, [
            {"id": "c-01", "clusters": ["cl-01"], "k": 1, "verdict": "SINGLE"},
        ]),
        ["claims_backfilled:clusters,k,verdict"],
    )
    assert failed.verdict is validation.Verdict.FAIL
    assert failed.failures[0].offenders == ["c-01 缺 verdict"]
    assert passed.verdict is validation.Verdict.PASS
    assert len(validation.REGISTRY) == 29


def test_runtime_在角标回填后登记全部JSON报告章(tmp_path: Path, monkeypatch) -> None:
    from tests.test_m3h_finalize import _finalize, _plan

    path = "goals/goal-3/report.json"
    plan = _plan(report_format="json", path=path)
    plan.goals[2].agents[0].output["shape"] = "object"
    url = "https://example.com/runtime"
    artifact = tmp_path / "runs/r-ledger" / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({
        "title": "报告",
        "chapter_id": "ch-3",
        "sections": [{
            "section_id": "ch-3/sec-1", "goal_id": "goal-1", "title": "节",
            "markdown": "## 结论\n\n- 运行期断言。\n\n## 信息源\n\n- 无。",
        }],
        "缺失清单": [],
        "claims": [raw_claim("c-01", [ref(url)])],
    }, ensure_ascii=False), encoding="utf-8")

    def prepare(store):
        add_evidence(
            store, "r-ledger", "ev-runtime", platform="web_search",
            permalink=url, author="作者",
        )

    _, store, events = _finalize(
        tmp_path, plan, monkeypatch, prepare=prepare,
    )
    claim = store.get_report("r-ledger")["extra"]["claims"][0]
    assert claim["claims_source"] == "chapter"
    assert claim["evidence_ids"] == ["ev-runtime"]
    assert store.list_evidence("r-ledger")[0]["extra"]["claim_ids"] == ["c-01"]
    validation_events = [
        event for event in events if event.get("type") == "report_validation"
    ]
    assert validation_events[-1]["data"]["verdict"] == "pass"


def test_runtime_悬空断言走报告失败事件而不崩收尾(tmp_path: Path, monkeypatch) -> None:
    from tests.test_m3h_finalize import _finalize, _plan

    path = "goals/goal-3/report.json"
    plan = _plan(report_format="json", path=path)
    plan.goals[2].agents[0].output["shape"] = "object"
    artifact = tmp_path / "runs/r-ledger" / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({
        "title": "报告", "chapter_id": "ch-3",
        "sections": [{
            "section_id": "ch-3/sec-1", "goal_id": "goal-1", "title": "节",
            "markdown": "## 结论\n\n- 悬空。\n\n## 信息源\n\n- 无。",
        }],
        "缺失清单": [],
        "claims": [raw_claim("c-01", [ref("https://missing.example/x")])],
    }, ensure_ascii=False), encoding="utf-8")

    _, store, events = _finalize(tmp_path, plan, monkeypatch)
    validations = [
        event["data"] for event in events if event.get("type") == "report_validation"
    ]
    assert validations[-1]["verdict"] == "fail"
    assert validations[-1]["failures"][-1]["validator"] == "claims_registration"
    assert "悬空 permalink" in validations[-1]["failures"][-1]["offenders"][0]
    assert store.get_report("r-ledger")["status"] == "completed"
