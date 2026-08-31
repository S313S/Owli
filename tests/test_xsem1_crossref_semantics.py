"""§XSEM-1 交叉验证维语义：空作者名、四维先验等级、反证行对称簇。"""

from __future__ import annotations

import pytest

from app.reliability.crossref import build_claim_clusters, independence_checks


def _ev(evidence_id: str, permalink: str, **overrides):
    item = {
        "id": evidence_id,
        "platform": "web_search",
        "permalink": permalink,
        "title": f"{evidence_id} 独立标题内容",
        "published_at": "2026-06-01T00:00:00Z",
    }
    item.update(overrides)
    return item


# --- 条 4：作者名缺失不再等价于「同一作者主体」 ---------------------------------

@pytest.mark.parametrize(
    "left_author, right_author, expected",
    [
        (None, None, True),          # 两边都不知道作者：不表态，按通过计
        ("柜体漫谈", None, True),      # 一边有名一边无名：同上
        ("Jon Bitner", "柜体漫谈", True),   # 两边具名且不等：独立
        ("柜体漫谈", "柜体漫谈", False),     # 两边具名且相等：同一主体
        ("柜体漫谈", " 柜体漫谈 ", False),   # 规范化后相等
    ],
)
def test_author_subject_treats_missing_name_as_unknown(left_author, right_author, expected):
    left = _ev("ev-1", "https://alpha-site.com/p1", author_name=left_author)
    right = _ev("ev-2", "https://beta-site.org/p2", author_name=right_author)
    checks = independence_checks(left, right, "c-01")
    assert checks["author_subject"] is expected


def test_missing_author_same_domain_still_one_cluster():
    """机构主体项仍然守着同域名：放开作者项不会让同一站点凑出多簇。"""
    items = [
        _ev("ev-1", "https://alpha-site.com/p1", author_name=None),
        _ev("ev-2", "https://alpha-site.com/p2", author_name=None),
    ]
    for item in items:
        item["firsthand_by_claim"] = {"c-01": True}
    result = build_claim_clusters(items, "c-01")
    assert result["k"] == 1
    assert result["verdict"] == "SINGLE"


def test_missing_author_cross_domain_forms_second_cluster():
    """跨注册域名、都无作者名 → §2.1 要求的「天然分属不同簇」。"""
    items = [
        _ev("ev-1", "https://alpha-site.com/p1", author_name=None,
            title="扫地机吸力实测记录"),
        _ev("ev-2", "https://beta-site.org/p2", author_name=None,
            title="洗衣机能耗横向对比"),
    ]
    for item in items:
        item["firsthand_by_claim"] = {"c-01": True}
    result = build_claim_clusters(items, "c-01")
    assert result["k"] == 2


# --- 条 3：等级回退从「整套平台基线」换成「四维实值 + 基线交叉分」 -------------------

from app.reliability.crossref import _grade  # noqa: E402


def _rated(platform: str, authority: int, freshness: int, completeness: int,
           independence: int) -> dict:
    return {
        "id": "ev-x", "platform": platform,
        "score_authority": authority, "score_freshness": freshness,
        "score_completeness": completeness, "score_independence": independence,
    }


def test_四维实值齐全时走四维先验等级():
    """web_search 基线总分 5 = C；四维打实后可以到 B/A，天花板被拆掉。"""
    assert _grade(_rated("web_search", 2, 2, 2, 2)) == "A"   # 8 + 基线交叉 1 = 9
    assert _grade(_rated("web_search", 1, 2, 2, 1)) == "B"   # 6 + 1 = 7
    assert _grade(_rated("web_search", 1, 1, 1, 1)) == "C"   # 4 + 1 = 5，与基线同


def test_四维先验也会下探不只上探():
    """X 平台基线 6 = B；讨论串取不全 + 权威未达 P75 的真实读数是 5 = C。"""
    assert _grade({"id": "ev-x", "platform": "x"}) == "B"    # 四维缺 → 整套基线 6
    assert _grade(_rated("x", 0, 2, 1, 2)) == "C"            # 5 + 基线交叉 0 = 5


@pytest.mark.parametrize("missing", [
    "score_authority", "score_freshness", "score_completeness", "score_independence",
])
def test_四维任缺一维仍回落整套平台基线(missing):
    item = _rated("hacker_news", 1, 1, 2, 2)
    item.pop(missing)
    assert _grade(item) == "B"          # hacker_news 基线总分 7 = B
    item[missing] = None
    assert _grade(item) == "B"


def test_五维齐全时优先走总分不走先验():
    item = _rated("web_search", 2, 2, 2, 2)
    item["score_crossref"] = 0
    assert _grade(item) == "A"          # 8 分，走第②级
    assert _grade({**item, "grade": "D"}) == "D"   # 第①级仍最优先


# --- 条 3 的不动点：四维实值在本轮才写上，也必须一次调用内收敛 --------------------

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

from app.reliability.backfill import backfill_report  # noqa: E402
from app.reliability.claims import register_claims  # noqa: E402
from tests.test_c1_claims import (  # noqa: E402
    NeverAdapter, add_evidence, answer_firsthand, make_store, raw_claim, ref,
)


def test_条3_连跑四遍逐字段零差异(tmp_path: Path) -> None:
    """D-013 的尺子照搬并加到四遍：第一遍才评上分的路径也不许漂。"""

    store = make_store(tmp_path)
    evidence = (
        ("ev-hn1", "hacker_news", "https://news.ycombinator.com/item?id=901",
         "甲", "2026-08-01T00:00:00+00:00"),
        ("ev-ws1", "web_search", "https://openai.com/index/one",
         "乙", "2026-08-10T00:00:00+00:00"),
        ("ev-ws2", "web_search", "https://anthropic.com/news/two",
         "丙", "2026-08-20T00:00:00+00:00"),
    )
    for evidence_id, platform, permalink, author, published_at in evidence:
        add_evidence(
            store, "r-c1", evidence_id, platform=platform, permalink=permalink,
            author=author, published_at=published_at,
        )
    register_claims(store, "r-c1", [raw_claim("c-01", [
        ref(permalink, firsthand=True) for _, _, permalink, _, _ in evidence
    ])], source="chapter")

    def snapshot() -> dict:
        rows = {row["id"]: row for row in store.list_evidence("r-c1")}
        return {
            "evidence": {
                evidence_id: {
                    key: row[key] for key in (
                        "score_authority", "score_freshness", "score_crossref",
                        "score_completeness", "score_independence",
                        "score_total", "grade", "rating_notes", "rated_by",
                    )
                } | {"extra": row["extra"]}
                for evidence_id, row in rows.items()
            },
            "claims": store.get_report("r-c1")["extra"]["claims"],
        }

    rounds = []
    for _ in range(4):
        asyncio.run(backfill_report(
            store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs",
        ))
        rounds.append(snapshot())
    assert rounds[1] == rounds[0]
    assert rounds[2] == rounds[0]
    assert rounds[3] == rounds[0]


# --- 条 2：反证行按 ¬c 的支撑面结算（B-1 对称簇） ---------------------------------

def _side(evidence_id: str, permalink: str, *, against: bool, **overrides):
    item = _ev(evidence_id, permalink, **overrides)
    item["firsthand_by_claim"] = {"c-01": True}
    if against:
        item["stance_by_claim"] = {"c-01": "contradicts"}
    return item


def _三对三夹具(**overrides) -> list[dict]:
    """支撑三条、反证三条，各自跨平台跨域名跨作者，两面都能成 ≥2 簇。"""

    rows = [
        _side("ev-s1", "https://news.ycombinator.com/item?id=701", against=False,
              platform="hacker_news", author_name="甲",
              title="自测内存占用记录", published_at="2026-06-01T00:00:00Z"),
        _side("ev-s2", "https://openai-notes.com/a", against=False,
              author_name="乙", title="第三方横向评测",
              published_at="2026-03-05T00:00:00Z"),
        _side("ev-s3", "https://ithome-mirror.net/b", against=False,
              author_name="丙", title="长期使用体感汇总",
              published_at="2026-01-09T00:00:00Z"),
        _side("ev-c1", "https://news.ycombinator.com/item?id=702", against=True,
              platform="hacker_news", author_name="丁",
              title="同版本复测未复现", published_at="2026-06-02T00:00:00Z"),
        _side("ev-c2", "https://labs-review.org/c", against=True,
              author_name="戊", title="实验室台架数据",
              published_at="2026-02-11T00:00:00Z"),
        _side("ev-c3", "https://user-forum.net/d", against=True,
              author_name="己", title="社区大样本投票",
              published_at="2026-04-17T00:00:00Z"),
    ]
    for row in rows:
        row.update(overrides.get(row["id"], {}))
    return rows


def test_条2_反证行首次拿到交叉结论与簇数():
    result = build_claim_clusters(_三对三夹具(), "c-01")
    for evidence_id in ("ev-c1", "ev-c2", "ev-c3"):
        patch = result["evidence_extra"][evidence_id]
        assert patch["crossref_verdict"] in {"PASS", "WEAK", "SINGLE", "CONFLICT"}
        assert patch["crossref_n_clusters"] >= 1
        assert "c-01" in patch["claim_ids"]
    # 反证面的簇数是它自己那面的，不是支撑面的。
    assert result["evidence_extra"]["ev-c1"]["crossref_n_clusters"] == 3
    assert result["k"] == 3


def test_条2_两面互指且未说明分歧时双方都判CONFLICT():
    result = build_claim_clusters(_三对三夹具(), "c-01")
    assert result["verdict"] == "CONFLICT"
    assert result["score_crossref"] == 0
    for evidence_id in ("ev-s1", "ev-c1"):
        patch = result["evidence_extra"][evidence_id]
        assert patch["crossref_verdict"] == "CONFLICT"
    # 各自的 conflicts 指向对面，不指向自己那面。
    assert set(result["evidence_extra"]["ev-s1"]["crossref_conflicts"]) == {
        "ev-c1", "ev-c2", "ev-c3",
    }
    assert set(result["evidence_extra"]["ev-c1"]["crossref_conflicts"]) == {
        "ev-s1", "ev-s2", "ev-s3",
    }


def test_条2_正文说明分歧后两面同时降为WEAK():
    result = build_claim_clusters(_三对三夹具(), "c-01", conflict_explained=True)
    assert result["verdict"] == "WEAK"
    for evidence_id in ("ev-s1", "ev-s2", "ev-c1", "ev-c2"):
        assert result["evidence_extra"][evidence_id]["crossref_verdict"] == "WEAK"


def test_条2_只有一条反证时反证面判孤证():
    rows = [row for row in _三对三夹具() if row["id"] not in {"ev-c2", "ev-c3"}]
    result = build_claim_clusters(rows, "c-01")
    assert result["evidence_extra"]["ev-c1"]["crossref_n_clusters"] == 1
    # 支撑面等级 ≥B（HN 基线 7 = B），故反证面先落 CONFLICT 分支而不是 SINGLE。
    assert result["evidence_extra"]["ev-c1"]["crossref_verdict"] == "CONFLICT"
    rows_explained = [dict(row) for row in rows]
    explained = build_claim_clusters(rows_explained, "c-01", conflict_explained=True)
    assert explained["evidence_extra"]["ev-c1"]["crossref_verdict"] == "WEAK"


def test_条2_无反证时支撑面读数与改前逐字段相同():
    """存量语料反证行为 0，本条必须对支撑面零漂移。"""

    rows = [row for row in _三对三夹具() if row["id"].startswith("ev-s")]
    result = build_claim_clusters(rows, "c-01")
    # 顶层取主簇（ev-s1，HN）的视角：它看到的其他簇是 {C, C} → WEAK。这是 C-1
    # 记档的既有设计（每条只看「除自己簇之外」的佐证等级），不是本包引入的。
    assert (result["k"], result["verdict"], result["score_crossref"]) == (3, "WEAK", 1)
    assert result["evidence_extra"]["ev-s2"]["crossref_verdict"] == "PASS"
    assert result["clusters"] == ["cl-01", "cl-02", "cl-03"]
    for evidence_id in ("ev-s1", "ev-s2", "ev-s3"):
        patch = result["evidence_extra"][evidence_id]
        assert patch["crossref_conflicts"] == []
        assert patch["crossref_n_clusters"] == 3


def test_条2_反证行经补评拿到五维与grade(tmp_path: Path) -> None:
    """C-1 那条「反证行按既有语义拿不到交叉分」的挂账，验到库行上。"""

    store = make_store(tmp_path)
    urls = {
        "s1": "https://news.ycombinator.com/item?id=801",
        "s2": "https://openai-notes.com/x",
        "c1": "https://labs-review.org/y",
        "c2": "https://user-forum.net/z",
    }
    for key, platform, author, published in (
        ("s1", "hacker_news", "甲", "2026-08-01T00:00:00+00:00"),
        ("s2", "web_search", "乙", "2026-08-05T00:00:00+00:00"),
        ("c1", "web_search", "丙", "2026-08-09T00:00:00+00:00"),
        ("c2", "web_search", "丁", "2026-08-13T00:00:00+00:00"),
    ):
        add_evidence(
            store, "r-c1", f"ev-{key}", platform=platform, permalink=urls[key],
            author=author, published_at=published,
        )
    register_claims(store, "r-c1", [raw_claim("c-01", [
        ref(urls["s1"], firsthand=True), ref(urls["s2"], firsthand=True),
        ref(urls["c1"], firsthand=True, stance="contradicts"),
        ref(urls["c2"], firsthand=True, stance="contradicts"),
    ])], source="chapter")

    asyncio.run(backfill_report(
        store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs",
    ))

    rows = {row["id"]: row for row in store.list_evidence("r-c1")}
    for evidence_id in ("ev-c1", "ev-c2"):
        row = rows[evidence_id]
        assert row["extra"]["crossref_verdict"] is not None
        assert row["score_crossref"] is not None
        assert row["score_total"] is not None
        assert row["grade"] is not None          # 改前这三列对反证行恒 NULL
        assert row["extra"]["crossref_n_clusters"] == 2


# --- 条 1：一手性审计确认（§3.2 第 5 项交回 reliability-auditor） -------------------

import json as _json  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class _FirsthandAuditor:
    """按 (断言, 证据) 对给定判定的审计桩；非审计任务一律拒绝。"""

    def __init__(self, verdicts: dict[str, bool], reason: str = "自测数据",
                 *, also_label: bool = False) -> None:
        self.verdicts = verdicts
        self.reason = reason
        self.also_label = also_label
        self.audit_calls = 0
        self.label_calls = 0

    async def run(self, task, ctx=None, on_event=None):
        del ctx, on_event
        marker = "输入 (断言, 证据) 对："
        if marker not in task.body:
            if not self.also_label:
                raise AssertionError("本夹具只应触发一手性审计")
            self.label_calls += 1
            items, _ = _json.JSONDecoder().raw_decode(
                task.body.split("输入证据：", 1)[1]
            )
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(_json.dumps([{
                "id": item["id"], "authority_kind": "named_secondary",
                "content_kind": "industry_view",
                "interest_relation": "arms_length", "missing_dimensions": {},
            } for item in items], ensure_ascii=False), encoding="utf-8")
            return SimpleNamespace(succeeded=True)
        self.audit_calls += 1
        pairs, _ = _json.JSONDecoder().raw_decode(task.body.split(marker, 1)[1])
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(_json.dumps([{
            "claim_id": pair["claim_id"], "evidence_id": pair["evidence_id"],
            "firsthand": self.verdicts[pair["evidence_id"]],
            "reason": self.reason,
        } for pair in pairs], ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(succeeded=True)


def _三源一断言(tmp_path: Path):
    store = make_store(tmp_path)
    urls = {
        "a": "https://news.ycombinator.com/item?id=601",
        "b": "https://openai-notes.com/one",
        "c": "https://labs-review.org/two",
    }
    for key, platform, author, published in (
        ("a", "hacker_news", "甲", "2026-08-01T00:00:00+00:00"),
        ("b", "web_search", "乙", "2026-08-06T00:00:00+00:00"),
        ("c", "web_search", "丙", "2026-08-12T00:00:00+00:00"),
    ):
        add_evidence(
            store, "r-c1", f"ev-{key}", platform=platform, permalink=urls[key],
            author=author, published_at=published,
        )
    # 撰写方三条全声明一手——改前这就是终局，没人核。
    register_claims(store, "r-c1", [raw_claim("c-01", [
        ref(url, firsthand=True) for url in urls.values()
    ])], source="chapter")
    return store, urls


def test_条1_审计判否会把闸门关上_k与verdict跟着降(tmp_path: Path) -> None:
    """一手性是 §3.2 五项里唯一的闸门：审计一收紧，k 就掉下来。"""

    store, _ = _三源一断言(tmp_path)
    auditor = _FirsthandAuditor(
        {"ev-a": True, "ev-b": False, "ev-c": False}, reason="仅复述官方公告",
    )
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
    ))

    claim = store.get_report("r-c1")["extra"]["claims"][0]
    assert auditor.audit_calls == 1
    assert claim["firsthand_source"] == "audited"
    assert claim["firsthand"] == ["ev-a"]          # 撰写方声明的三条被核成一条
    assert claim["firsthand_audit"]["ev-b"] == {
        "firsthand": False, "reason": "仅复述官方公告",
    }
    # §3.2「至少一方」：ev-a 仍是一手，所以 (a,b)(a,c) 两对照过，只有 (b,c) 并簇。
    assert (claim["k"], claim["verdict"]) == (2, "PASS")


def test_条1_审计全判否则交叉维整个归零(tmp_path: Path) -> None:
    """用户 09-01 拍板接受的口径变更：闸门守起来后交叉维可能整体下降甚至归零。"""

    store, _ = _三源一断言(tmp_path)
    auditor = _FirsthandAuditor(
        {"ev-a": False, "ev-b": False, "ev-c": False}, reason="全篇转述发布稿",
    )
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
    ))

    claim = store.get_report("r-c1")["extra"]["claims"][0]
    assert claim["firsthand"] == []
    assert (claim["k"], claim["verdict"]) == (1, "SINGLE")
    rows = {row["id"]: row for row in store.list_evidence("r-c1")}
    assert all(row["score_crossref"] == 0 for row in rows.values())


def test_条1_审计判是则闸门放行_读数与声明一致(tmp_path: Path) -> None:
    store, _ = _三源一断言(tmp_path)
    auditor = _FirsthandAuditor({"ev-a": True, "ev-b": True, "ev-c": True})
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
    ))

    claim = store.get_report("r-c1")["extra"]["claims"][0]
    assert claim["firsthand"] == ["ev-a", "ev-b", "ev-c"]
    assert claim["k"] == 3
    assert all(
        entry["reason"] for entry in claim["firsthand_audit"].values()
    )


def test_条1_引擎整批失败则保留撰写方声明不覆盖(tmp_path: Path) -> None:
    """失败不等于「全否」——全否会把交叉维直接打到零分，那是拿失败当结论。"""

    class _FailingAuditor:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, task, ctx=None, on_event=None):
            del ctx, on_event
            if "输入 (断言, 证据) 对：" in task.body:
                self.calls += 1
                return SimpleNamespace(succeeded=False)
            return await NeverAdapter().run(task)

    store, _ = _三源一断言(tmp_path)
    events: list[dict] = []

    async def on_event(payload):
        events.append(payload)

    auditor = _FailingAuditor()
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
        on_event=on_event,
    ))

    claim = store.get_report("r-c1")["extra"]["claims"][0]
    assert auditor.calls >= 1
    assert claim["firsthand_source"] == "declared_by_writer"   # 没升级
    assert claim["firsthand"] == ["ev-a", "ev-b", "ev-c"]      # 声明原样留着
    assert "firsthand_audit" not in claim
    assert claim["k"] == 3                                     # 读数与改前相同
    progress = [
        event["data"] for event in events
        if event["type"] == "firsthand_audit_progress"
    ]
    assert progress and progress[-1]["failed_total"] == 3
    assert progress[-1]["audited_total"] == 0


def test_条1_幂等_连跑两遍零差异且第二遍不再烧引擎(tmp_path: Path) -> None:
    store, _ = _三源一断言(tmp_path)
    auditor = _FirsthandAuditor({"ev-a": True, "ev-b": False, "ev-c": True})

    def snapshot() -> dict:
        rows = {row["id"]: row for row in store.list_evidence("r-c1")}
        return {
            "evidence": {
                evidence_id: {
                    key: row[key] for key in (
                        "score_crossref", "score_total", "grade", "rating_notes",
                    )
                } | {"extra": row["extra"]}
                for evidence_id, row in rows.items()
            },
            "claims": store.get_report("r-c1")["extra"]["claims"],
        }

    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
    ))
    first, calls_after_first = snapshot(), auditor.audit_calls
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
    ))

    assert snapshot() == first
    assert calls_after_first == 1
    assert auditor.audit_calls == 1, "已审计且逐条留痕的断言不得重烧引擎"


def test_条1_force时重跑审计(tmp_path: Path) -> None:
    store, _ = _三源一断言(tmp_path)
    auditor = _FirsthandAuditor(
        {"ev-a": True, "ev-b": True, "ev-c": True}, also_label=True,
    )
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs",
    ))
    assert (auditor.audit_calls, auditor.label_calls) == (1, 0)
    asyncio.run(backfill_report(
        store, "r-c1", adapter=auditor, runs_root=tmp_path / "runs", force=True,
    ))
    assert auditor.audit_calls == 2
