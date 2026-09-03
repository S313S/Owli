"""D-037：跨 goal / 跨章同位片的 claims id 相撞，登记整批拒绝。

夹具全确定性、不用引擎：直接构造多份「JSON 报告章产物」document，
走 claims_from_documents → prepare_claim_registration 这条生产路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.reliability.claims import (
    ClaimsRegistrationError,
    claims_from_documents,
    register_claims,
)

from tests.test_c1_claims import add_evidence, make_store, raw_claim, ref


URL_A = "https://example.com/d037-a"
URL_B = "https://example.com/d037-b"


def _document(claims: list[dict], chapter_id: str = "ch-3") -> dict:
    return {"title": "章", "chapter_id": chapter_id, "sections": [], "claims": claims}


def test_跨文档同位id加命名空间后登记通过(tmp_path: Path) -> None:
    """两份报告章各自从 c-010101 起编——真机 250 条只剩 84 唯一的最小复现。"""

    store = make_store(tmp_path, "r-d037")
    add_evidence(
        store, "r-d037", "ev-a", platform="web_search", permalink=URL_A, author="甲",
    )
    add_evidence(
        store, "r-d037", "ev-b", platform="web_search", permalink=URL_B, author="乙",
    )
    documents = [
        _document([raw_claim("c-010101", [ref(URL_A)], text="goal-1 第一片第一条")]),
        _document(
            [raw_claim("c-010101", [ref(URL_B)], text="goal-2 第一片第一条")],
            chapter_id="ch-4",
        ),
    ]

    claims = register_claims(
        store, "r-d037", claims_from_documents(documents), source="chapter",
    )

    assert [claim["id"] for claim in claims] == ["c-01010101", "c-02010101"]
    assert [claim["text"] for claim in claims] == [
        "goal-1 第一片第一条", "goal-2 第一片第一条",
    ]
    rows = {row["id"]: row["extra"]["claim_ids"] for row in store.list_evidence("r-d037")}
    assert rows == {"ev-a": ["c-01010101"], "ev-b": ["c-02010101"]}


def test_同一文档内重复仍整批拒绝(tmp_path: Path) -> None:
    """guard：命名空间是去撞不是去重，同一章里真重复照旧报错。"""

    store = make_store(tmp_path, "r-d037-dup")
    add_evidence(
        store, "r-d037-dup", "ev-a", platform="web_search",
        permalink=URL_A, author="甲",
    )
    documents = [_document([
        raw_claim("c-010101", [ref(URL_A)], text="第一条"),
        raw_claim("c-010101", [ref(URL_A)], text="同章重复"),
    ])]

    with pytest.raises(ClaimsRegistrationError) as excinfo:
        register_claims(
            store, "r-d037-dup", claims_from_documents(documents), source="chapter",
        )

    assert excinfo.value.offenders == ["claims[1].id 报告内重复：c-01010101"]


def test_id格式非法不被命名空间掩盖(tmp_path: Path) -> None:
    """guard：非法 id 原样透传，仍按格式违规报错，不会被前缀补成合法。"""

    store = make_store(tmp_path, "r-d037-bad")
    add_evidence(
        store, "r-d037-bad", "ev-a", platform="web_search",
        permalink=URL_A, author="甲",
    )
    documents = [_document([raw_claim("claim-1", [ref(URL_A)])])]

    collected = claims_from_documents(documents)
    assert collected[0]["id"] == "claim-1"
    with pytest.raises(ClaimsRegistrationError) as excinfo:
        register_claims(
            store, "r-d037-bad", collected, source="chapter",
        )
    assert excinfo.value.offenders == ["claims[0].id 不符合 c-\\d{2,}"]


def test_runtime_交叉章与报告章同位id收尾判pass(tmp_path: Path, monkeypatch) -> None:
    """真机形态：goal-2 交叉验证章 + goal-3 报告章各自从 c-010101 起编。"""

    from tests.test_m3h_finalize import _finalize, _plan

    plan = _plan(report_format="json", path="goals/goal-3/report.json")
    plan.goals[2].agents[0].output["shape"] = "object"
    cross = plan.goals[1].agents[0]
    cross.agent_id = "cross-validation"
    cross.capability["profile"] = "report-writer"
    cross.output = {
        "format": "json", "shape": "object",
        "path": "goals/goal-2/cross.json", "validators": ["file_exists"],
    }
    cross.chapter["chapter_type"] = "cross_validation"

    def write(path: str, url: str, text: str) -> None:
        artifact = tmp_path / "runs/r-ledger" / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({
            "title": "报告", "chapter_id": "ch-3",
            "sections": [{
                "section_id": "ch-3/sec-1", "goal_id": "goal-1", "title": "节",
                "markdown": "## 结论\n\n- 断言。\n\n## 信息源\n\n- 无。",
            }],
            "缺失清单": [],
            "claims": [raw_claim("c-010101", [ref(url)], text=text)],
        }, ensure_ascii=False), encoding="utf-8")

    write("goals/goal-2/cross.json", URL_A, "交叉章第一片第一条")
    write("goals/goal-3/report.json", URL_B, "报告章第一片第一条")

    def prepare(store):
        add_evidence(
            store, "r-ledger", "ev-a", platform="web_search",
            permalink=URL_A, author="甲",
        )
        add_evidence(
            store, "r-ledger", "ev-b", platform="web_search",
            permalink=URL_B, author="乙",
        )

    _, store, events = _finalize(tmp_path, plan, monkeypatch, prepare=prepare)

    validations = [e["data"] for e in events if e.get("type") == "report_validation"]
    assert validations[-1]["failures"] == []
    assert validations[-1]["verdict"] == "pass"
    claims = store.get_report("r-ledger")["extra"]["claims"]
    assert [claim["id"] for claim in claims] == ["c-01010101", "c-02010101"]


def test_闭集外的键被机械剥离且逐条记账(tmp_path: Path) -> None:
    """用户 09-03 拍板「甲」：只剥闭集外的键，剥了什么可查。"""

    store = make_store(tmp_path, "r-d037-strip")
    add_evidence(
        store, "r-d037-strip", "ev-a", platform="web_search",
        permalink=URL_A, author="甲",
    )
    claim = raw_claim("c-010101", [dict(ref(URL_A), fetched_at="2026-09-03T00:00:00Z")])
    claim["stance"] = "supports"
    stripped: list[dict] = []

    collected = claims_from_documents([_document([claim])], stripped=stripped)

    assert set(collected[0]) == {"id", "text", "evidence"}
    assert set(collected[0]["evidence"][0]) == {"permalink"}
    assert stripped == [{
        "location": "claims[0]", "origin": "文档 1 的第 1 条",
        "claim_id": "c-01010101",
        "removed": {"claim": ["stance"], "evidence": ["fetched_at"]},
    }]
    claims = register_claims(store, "r-d037-strip", collected, source="chapter")
    assert [c["id"] for c in claims] == ["c-01010101"]


def test_闭集本身不放宽_登记入口仍拒未知键(tmp_path: Path) -> None:
    """guard：剥离只发生在装配层；直接送进登记的未知键照旧整批拒绝。"""

    from app.reliability.claims import prepare_claim_registration

    claim = raw_claim("c-0101", [ref(URL_A)])
    claim["stance"] = "supports"
    with pytest.raises(ClaimsRegistrationError) as excinfo:
        prepare_claim_registration([], [claim], source="chapter")
    assert "claims[0] 含未知键 ['stance']" in excinfo.value.offenders


def test_片提示词讲死断言键闭集() -> None:
    """乙：双保险落在提示词里，别让写手一开始就写多。"""

    from app.orchestrator.sectioning import _shard_notice

    notice = _shard_notice(1, 4, 5, 1, "")
    assert "`id`／`text`／`evidence`／`conflict_note`" in notice
    assert "`permalink`／`stance`／`firsthand`／`origin_url`" in notice


def test_runtime_剥离闭集外键后收尾判pass且发记账事件(tmp_path: Path, monkeypatch) -> None:
    """判据落在事件上：剥了什么、剥了几处，收尾事件里查得到。"""

    from tests.test_m3h_finalize import _finalize, _plan

    plan = _plan(report_format="json", path="goals/goal-3/report.json")
    plan.goals[2].agents[0].output["shape"] = "object"
    claim = raw_claim("c-010101", [dict(ref(URL_A), fetched_at="2026-09-03T00:00:00Z")])
    claim["stance"] = "supports"
    artifact = tmp_path / "runs/r-ledger/goals/goal-3/report.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({
        "title": "报告", "chapter_id": "ch-3",
        "sections": [{
            "section_id": "ch-3/sec-1", "goal_id": "goal-1", "title": "节",
            "markdown": "## 结论\n\n- 断言。\n\n## 信息源\n\n- 无。",
        }],
        "缺失清单": [], "claims": [claim],
    }, ensure_ascii=False), encoding="utf-8")

    def prepare(store):
        add_evidence(
            store, "r-ledger", "ev-a", platform="web_search",
            permalink=URL_A, author="甲",
        )

    _, store, events = _finalize(tmp_path, plan, monkeypatch, prepare=prepare)

    validations = [e["data"] for e in events if e.get("type") == "report_validation"]
    assert validations[-1]["verdict"] == "pass"
    stripped = [e["data"] for e in events if e.get("type") == "claims_keys_stripped"]
    assert len(stripped) == 1 and stripped[0]["count"] == 1
    assert stripped[0]["entries"][0]["removed"] == {
        "claim": ["stance"], "evidence": ["fetched_at"],
    }
    assert store.get_report("r-ledger")["extra"]["claims"][0]["id"] == "c-01010101"
