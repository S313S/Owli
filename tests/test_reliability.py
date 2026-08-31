from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def _evidence(**overrides):
    item = {
        "id": "ev-01",
        "report_id": "r-01",
        "goal_id": "goal-1",
        "platform": "hacker_news",
        "source_type": "post",
        "permalink": "https://news.ycombinator.com/item?id=1",
        "title": "Independent measurement",
        "author_name": "alice",
        "author_meta": {"affiliation": "independent-lab"},
        "published_at": "2026-02-02T00:00:00Z",
        "fetched_at": "2026-08-01T00:00:00Z",
        "fetch_method": "official_api",
        "raw_metrics": {"points": 100, "num_comments": 10},
        "normalized_score": 0.8,
        "has_body": True,
        "permalink_reachable": True,
        "comments_fetched": 10,
        "comments_total": 10,
        "firsthand_by_claim": {"c-01": True},
        "extra": {
            "claim_ids": ["c-01"],
            "authority_kind": "community_high_signal",
            "content_kind": "user_opinion",
            "interest_relation": "arms_length",
            "crossref_verdict": "PASS",
        },
    }
    for key, value in overrides.items():
        if key == "extra":
            item["extra"] = {**item["extra"], **value}
        else:
            item[key] = value
    return item


@pytest.mark.parametrize(
    ("content_kind", "age_days", "expected"),
    [
        ("product_launch", 90, 2),
        ("product_launch", 91, 1),
        ("product_launch", 365, 1),
        ("product_launch", 366, 0),
        ("market_data", 30, 2),
        ("market_data", 31, 1),
        ("market_data", 181, 0),
        ("user_opinion", 180, 2),
        ("user_opinion", 181, 1),
        ("user_opinion", 731, 0),
        ("industry_view", 365, 2),
        ("industry_view", 366, 1),
        ("industry_view", 1096, 0),
    ],
)
def test_时效性阈值边界可人工复算(content_kind, age_days, expected) -> None:
    from datetime import datetime, timedelta, timezone

    from app.reliability.scoring import score_evidence

    fetched = datetime(2026, 8, 1, tzinfo=timezone.utc)
    published = fetched - timedelta(days=age_days)
    result = score_evidence(
        _evidence(
            published_at=published.isoformat(),
            fetched_at=fetched.isoformat(),
            extra={"content_kind": content_kind},
        )
    )

    assert result["score_freshness"] == expected


def test_五维闭集映射_官方无关性封顶_等级与理由格式() -> None:
    from app.reliability.scoring import (
        PLATFORM_BASELINES,
        rating_notes_problem,
        score_evidence,
    )

    assert PLATFORM_BASELINES["hacker_news"] == {
        "score_authority": 1,
        "score_freshness": 1,
        "score_crossref": 1,
        "score_completeness": 2,
        "score_independence": 2,
    }
    result = score_evidence(
        _evidence(
            extra={
                "authority_kind": "first_party_official",
                "content_kind": "reference",
                "interest_relation": "arms_length",
                "crossref_verdict": "SINGLE",
            },
            reference_status="current",
        )
    )

    assert [
        result["score_authority"],
        result["score_freshness"],
        result["score_crossref"],
        result["score_completeness"],
        result["score_independence"],
    ] == [2, 2, 0, 2, 1]
    assert result["score_total"] == 7
    assert result["grade"] == "B"
    assert rating_notes_problem(result["rating_notes"], result) is None


def test_未来时间戳降为零并写唯一异常尾注() -> None:
    from app.reliability.scoring import score_evidence

    result = score_evidence(
        _evidence(
            published_at="2026-08-02T00:00:00Z",
            fetched_at="2026-08-01T00:00:00Z",
        )
    )
    assert result["score_freshness"] == 0
    assert result["rating_notes"].endswith(" ⚠️时间戳异常")


def test_时间戳异常与反证限制合并进唯一尾注() -> None:
    from app.reliability.scoring import score_evidence

    result = score_evidence(
        _evidence(
            published_at="2026-08-02T00:00:00Z",
            fetched_at="2026-08-01T00:00:00Z",
            extra={"crossref_verdict": "CONFLICT"},
        )
    )
    assert result["rating_notes"].endswith(" ⚠️时间戳异常；存在反证")
    assert result["rating_notes"].count("⚠️") == 1


def test_缺发布时间不能被高分平台基线抬高() -> None:
    from app.reliability.scoring import score_evidence

    result = score_evidence(
        _evidence(
            platform="product_hunt",
            published_at=None,
            extra={"content_kind": "product_launch"},
        )
    )
    assert result["score_freshness"] == 0


def test_抓取时刻代替发布时间时_时效封顶一分并留痕() -> None:
    from app.reliability.scoring import score_evidence

    degraded = score_evidence(
        _evidence(
            platform="web_search",
            published_at="2026-08-01T00:00:00Z",
            fetched_at="2026-08-01T00:00:00Z",
            extra={
                "content_kind": "market_data",
                "freshness_degraded_source": "fetched_at",
            },
        )
    )
    ordinary = score_evidence(
        _evidence(
            platform="web_search",
            published_at="2026-08-01T00:00:00Z",
            fetched_at="2026-08-01T00:00:00Z",
            extra={"content_kind": "market_data"},
        )
    )

    assert degraded["score_freshness"] == 1
    assert "时效1:抓取时刻兜底" in degraded["rating_notes"]
    assert ordinary["score_freshness"] == 2


def test_内容类型与发布时间同缺时取保守下界而非平台基线() -> None:
    from app.reliability.scoring import score_evidence

    result = score_evidence(
        _evidence(
            platform="product_hunt",
            published_at=None,
            extra={"content_kind": None},
        )
    )
    assert result["score_freshness"] == 0


def test_社区高信号必须达到_P75_或有可查历史() -> None:
    from app.reliability.scoring import score_evidence

    low_heat = score_evidence(_evidence(normalized_score=0.2))
    known_author = score_evidence(
        _evidence(normalized_score=0.2, author_history_verified=True)
    )
    assert low_heat["score_authority"] == 0
    assert known_author["score_authority"] == 1


def test_X_常规只取单帖正文完整度封顶一分() -> None:
    from app.reliability.scoring import score_evidence

    ordinary = score_evidence(
        _evidence(platform="x", comments_fetched=None, comments_total=None)
    )
    complete_thread = score_evidence(
        _evidence(platform="x", conversation_complete=True)
    )
    assert ordinary["score_completeness"] == 1
    assert complete_thread["score_completeness"] == 2


def test_D级证据不能独撑普通结论() -> None:
    from app.reliability.scoring import claim_support_is_valid, grade_for_total

    assert [grade_for_total(value) for value in (0, 3, 4, 5, 6, 7, 8, 10)] == [
        "D", "D", "C", "C", "B", "B", "A", "A"
    ]
    assert claim_support_is_valid(["D"]) is False
    assert claim_support_is_valid(["D"], downgraded_to_lead=True) is True
    assert claim_support_is_valid(["D", "C"]) is True


def _pair():
    left = _evidence(
        id="ev-left",
        permalink="https://one.example/a",
        title="Measured latency in desktop client",
        author_name="alice",
        author_meta={"affiliation": "lab-one"},
        published_at="2026-01-01T00:00:00Z",
        explicit_origin_url="https://origin-one.example/report",
    )
    right = _evidence(
        id="ev-right",
        permalink="https://two.example/b",
        title="Independent memory test on desktop",
        author_name="bob",
        author_meta={"affiliation": "lab-two"},
        published_at="2026-02-01T00:00:00Z",
        explicit_origin_url="https://origin-two.example/report",
    )
    return left, right


def test_独立性五项排除法逐项可阻断() -> None:
    from app.reliability.crossref import independence_checks

    left, right = _pair()
    assert all(independence_checks(left, right, "c-01").values())

    cases = {
        "citation_lineage": {"explicit_origin_url": left["explicit_origin_url"], "firsthand_by_claim": {}},
        "author_subject": {"author_name": "ALICE"},
        "institution_subject": {"author_meta": {"affiliation": "lab-one"}},
        "repost_batch": {
            "published_at": "2026-01-02T00:00:00Z",
            "title": "Measured latency in desktop client",
        },
        "firsthand": {"firsthand_by_claim": {}},
    }
    for failed_check, updates in cases.items():
        candidate = deepcopy(right)
        candidate.update(updates)
        if failed_check == "firsthand":
            source = deepcopy(left)
            source["firsthand_by_claim"] = {}
        else:
            source = left
        checks = independence_checks(source, candidate, "c-01")
        assert checks[failed_check] is False, (failed_check, checks)


def test_作者别名表两端使用同一规范化规则() -> None:
    from app.reliability.crossref import independence_checks

    left, right = _pair()
    left["author_name"] = "Alice Smith"
    right["author_name"] = "a_smith"
    checks = independence_checks(
        left, right, "c-01",
        author_aliases={"Alice Smith": "person-1", "a_smith": "person-1"},
    )
    assert checks["author_subject"] is False


def test_机构主体按_eTLD加一_且复用已落盘_origin_key() -> None:
    from app.reliability.crossref import independence_checks, origin_key

    left, right = _pair()
    left.update(
        permalink="https://support.microsoft.com/a",
        author_meta={},
        extra={**left["extra"], "origin_key": "press.example/release"},
    )
    right.update(
        permalink="https://www.microsoft.com/b",
        author_meta={},
        extra={**right["extra"], "origin_key": "press.example/release"},
    )
    checks = independence_checks(left, right, "c-01")
    assert checks["institution_subject"] is False
    assert checks["citation_lineage"] is False
    assert origin_key(left, "c-01") == "press.example/release"


def test_机构主体按公共后缀表识别私有后缀注册主体() -> None:
    from app.reliability.crossref import independence_checks

    left, right = _pair()
    left.update(permalink="https://alpha.blogspot.com/a", author_meta={})
    right.update(permalink="https://beta.blogspot.com/b", author_meta={})
    assert independence_checks(left, right, "c-01")["institution_subject"] is True


def test_机构名称与其注册域名不会被误判为独立主体() -> None:
    from app.reliability.crossref import independence_checks

    left, right = _pair()
    left.update(permalink="https://news.example/a", author_meta={"affiliation": "Microsoft"})
    right.update(permalink="https://support.microsoft.com/b", author_meta={})
    assert independence_checks(left, right, "c-01")["institution_subject"] is False


def test_HN_不同作者顶层一手评论不因平台域名相同而合簇() -> None:
    from app.reliability.crossref import build_claim_clusters

    comments = [
        _evidence(
            id=f"ev-{number}", source_type="comment", author_meta={},
            author_name=author, permalink=f"https://news.ycombinator.com/item?id={number}",
            parent_permalink="https://news.ycombinator.com/item?id=100",
            is_top_level_comment=True, story_id="100",
            firsthand_by_claim={"c-01": True},
            title=f"firsthand measurement {number}",
            published_at=f"2026-0{number}-01T00:00:00Z",
            grade="B",
        )
        for number, author in ((1, "alice"), (2, "bob"))
    ]
    assert build_claim_clusters(comments, "c-01")["k"] == 2


def test_HN_顶层一手评论与其层级回复强制并入同簇() -> None:
    from app.reliability.crossref import build_claim_clusters

    top = _evidence(
        id="ev-top", source_type="comment", author_meta={}, author_name="alice",
        permalink="https://news.ycombinator.com/item?id=101",
        parent_permalink="https://news.ycombinator.com/item?id=100",
        is_top_level_comment=True, story_id="100",
        firsthand_by_claim={"c-01": True}, grade="B",
    )
    reply = _evidence(
        id="ev-reply", source_type="comment", author_meta={}, author_name="bob",
        permalink="https://news.ycombinator.com/item?id=102",
        parent_permalink=top["permalink"], story_id="100",
        firsthand_by_claim={"c-01": False}, grade="B",
        title="unrelated follow-up wording", published_at="2026-07-01T00:00:00Z",
    )
    assert build_claim_clusters([top, reply], "c-01")["k"] == 1


def test_小红书中文模板文案在四十八小时内按二元组识别为同批次() -> None:
    from app.reliability.crossref import independence_checks

    left, right = _pair()
    left.update(
        platform="xhs", title="飞书协作效率提升", published_at="2026-08-01T00:00:00Z",
    )
    right.update(
        platform="xhs", title="飞书协作效率大幅提升", published_at="2026-08-02T00:00:00Z",
    )
    assert independence_checks(left, right, "c-01")["repost_batch"] is False


def test_同一证据的次断言可使用不同_origin_且不复用主断言值() -> None:
    from app.reliability.crossref import origin_key

    item = _evidence(
        extra={
            "claim_ids": ["c-03"],
            "origin_key": "windowslatest.com/story",
            "crossref_secondary": {
                "c-06": {"origin_key": "microsoft.com/release", "verdict": "SINGLE"}
            },
        },
        explicit_origin_by_claim={"c-07": "https://lab.example/test"},
    )
    assert origin_key(item, "c-03") == "windowslatest.com/story"
    assert origin_key(item, "c-06") == "microsoft.com/release"
    assert origin_key(item, "c-07") == "lab.example/test"


def test_断言簇构建_同源转载只投一票_并输出受控_extra() -> None:
    from app.reliability.crossref import build_claim_clusters

    first = _evidence(
        id="ev-01", explicit_origin_url="https://vendor.example/release",
        firsthand_by_claim={},
    )
    repost = _evidence(
        id="ev-02",
        permalink="https://media.example/repost",
        author_name="bob",
        explicit_origin_url="https://vendor.example/release?utm_source=x",
        firsthand_by_claim={},
    )
    independent = _evidence(
        id="ev-03",
        permalink="https://lab.example/test",
        title="Third-party memory benchmark",
        author_name="carol",
        author_meta={"affiliation": "lab"},
        published_at="2026-05-01T00:00:00Z",
        explicit_origin_url="https://lab.example/test",
        score_authority=1,
        score_freshness=1,
        score_crossref=0,
        score_completeness=2,
        score_independence=2,
    )
    result = build_claim_clusters([first, repost, independent], "c-01")

    assert result["k"] == 2
    assert result["verdict"] == "PASS"
    assert result["score_crossref"] == 2
    assert result["evidence_extra"]["ev-01"]["crossref_cluster"] == result["evidence_extra"]["ev-02"]["crossref_cluster"]
    assert result["evidence_extra"]["ev-03"]["crossref_n_clusters"] == 2
    assert set(result["evidence_extra"]["ev-03"]) == {
        "claim_ids", "origin_key", "crossref_cluster", "crossref_n_clusters",
        "crossref_peers", "crossref_conflicts", "crossref_verdict",
    }


def test_B级未解释反证为_CONFLICT_解释后为_WEAK() -> None:
    from app.reliability.crossref import build_claim_clusters

    support = _evidence(id="ev-support")
    conflict = _evidence(
        id="ev-conflict",
        permalink="https://official.example/claim",
        title="Official performance statement",
        author_name="official",
        author_meta={"affiliation": "vendor"},
        published_at="2026-07-01T00:00:00Z",
        stance_by_claim={"c-01": "contradicts"},
        score_authority=2,
        score_freshness=2,
        score_crossref=0,
        score_completeness=2,
        score_independence=1,
    )
    assert build_claim_clusters([support, conflict], "c-01")["verdict"] == "CONFLICT"
    explained = build_claim_clusters(
        [support, conflict], "c-01", conflict_explained=True
    )
    assert explained["verdict"] == "WEAK"
    assert explained["score_crossref"] == 1


def test_B加D两簇_主证据不能把本簇当作其他强源() -> None:
    from app.reliability.crossref import build_claim_clusters

    strong = _evidence(
        id="ev-b", grade="B", title="Primary benchmark",
        published_at="2026-01-01T00:00:00Z",
    )
    weak = _evidence(
        id="ev-d", grade="D", permalink="https://weak.example/post",
        author_name="weak-author", author_meta={"affiliation": "weak-org"},
        title="Independent weak anecdote", published_at="2026-06-01T00:00:00Z",
    )
    result = build_claim_clusters([strong, weak], "c-01")
    assert result["verdict"] == "WEAK"
    assert result["evidence_extra"]["ev-b"]["crossref_verdict"] == "WEAK"
    assert result["evidence_extra"]["ev-d"]["crossref_verdict"] == "PASS"


def test_同线程只取等级最高两簇而非输入前两条() -> None:
    from app.reliability.crossref import build_claim_clusters

    thread_items = [
        _evidence(
            id=f"ev-thread-{number}", grade=grade,
            permalink=f"https://news.ycombinator.com/item?id={number}",
            author_name=f"author-{number}",
            author_meta={"affiliation": f"org-{number}"},
            story_id="story-1", title=f"firsthand result {number}",
            published_at=f"2026-0{number}-01T00:00:00Z",
        )
        for number, grade in ((1, "D"), (2, "D"), (3, "B"))
    ]
    external = _evidence(
        id="ev-external", grade="B", platform="web_search",
        permalink="https://lab.example/benchmark", author_name="lab",
        author_meta={"affiliation": "external-lab"},
        title="external independent benchmark", published_at="2026-06-01T00:00:00Z",
    )
    result = build_claim_clusters([*thread_items, external], "c-01")
    assert result["k"] == 3
    assert result["evidence_extra"]["ev-external"]["crossref_verdict"] == "PASS"


def test_多断言不覆盖主断言且反证按对称簇登记() -> None:
    from app.reliability.crossref import build_claim_clusters

    primary = _evidence(
        id="ev-primary",
        extra={
            "claim_ids": ["c-06"],
            "origin_key": "primary.example/c-06",
            "crossref_cluster": "cl-old",
            "crossref_n_clusters": 1,
            "crossref_peers": [],
            "crossref_conflicts": [],
            "crossref_verdict": "SINGLE",
        },
        stance_by_claim={"c-03": "contradicts"},
    )
    support = _evidence(
        id="ev-support", grade="B", permalink="https://support.example/a",
        author_name="support", author_meta={"affiliation": "support-org"},
        title="supporting benchmark", published_at="2026-06-01T00:00:00Z",
        extra={"claim_ids": ["c-03"]},
    )
    result = build_claim_clusters([primary, support], "c-03")
    primary_patch = result["evidence_extra"]["ev-primary"]
    # §XSEM-1 条 2（B-1）：反证行改按 ¬c 的支撑面结算，所以它要登记 c-03——
    # 改前它在这里直接早退、永远拿不到 crossref_verdict。主断言仍是 c-06，
    # 因此 c-03 的结论落在 crossref_secondary，主键位的 SINGLE 一个字不动。
    assert primary_patch["claim_ids"] == ["c-06", "c-03"]
    assert primary_patch["crossref_verdict"] == "SINGLE"
    assert primary_patch["crossref_secondary"]["c-03"]["verdict"] == "CONFLICT"

    secondary = _evidence(
        id="ev-secondary", grade="B", permalink="https://secondary.example/a",
        author_name="secondary", author_meta={"affiliation": "secondary-org"},
        title="another supporting benchmark", published_at="2026-04-01T00:00:00Z",
        extra={
            "claim_ids": ["c-01"],
            "crossref_cluster": "cl-primary",
            "crossref_n_clusters": 1,
            "crossref_peers": [],
            "crossref_conflicts": [],
            "crossref_verdict": "SINGLE",
        },
        firsthand_by_claim={"c-03": True},
    )
    result = build_claim_clusters([secondary, support], "c-03")
    patch = result["evidence_extra"]["ev-secondary"]
    assert patch["claim_ids"] == ["c-01", "c-03"]
    assert patch["crossref_verdict"] == "SINGLE"
    assert patch["crossref_secondary"]["c-03"]["verdict"] == "PASS"


def test_归一化四件套按平台分组且不改原始指标() -> None:
    from app.reliability.scoring import normalize_evidence_metrics

    items = [
        {"id": f"hn-{value}", "platform": "hacker_news", "raw_metrics": {"points": value}}
        for value in range(1, 21)
    ]
    items.append({"id": "reddit-1", "platform": "reddit", "raw_metrics": {}})
    normalized = normalize_evidence_metrics(
        items,
        computed_at="2026-08-19T00:00:00Z",
        report_id="r-01",
        goal_id="goal-1",
        queries=["飞书竞品"],
        filters="points>0",
    )

    assert normalized[0]["raw_metrics"] == {"points": 1}
    assert normalized[0]["normalized_score"] == 0.0
    assert normalized[19]["normalized_score"] == 1.0
    assert normalized[19]["norm_method"] == "percentile_in_batch"
    assert normalized[19]["norm_context"]["platform"] == "hacker_news"
    assert normalized[19]["norm_context"]["n"] == 20
    assert normalized[20]["norm_method"] == "none"
    assert normalized[20]["normalized_score"] is None
    assert normalized[20]["norm_context"]["reason"] == "no_metric_available"


def test_X_样本不足_none_仍标注偏置且_scope_属于闭集() -> None:
    from app.reliability.scoring import normalize_evidence_metrics

    [item] = normalize_evidence_metrics(
        [{"id": "x-1", "platform": "x", "raw_metrics": {"like_count": 2}}],
        computed_at="2026-08-20T00:00:00Z",
        report_id="r-01",
        goal_id="goal-1",
    )
    assert item["norm_method"] == "none"
    assert item["norm_context"]["scope"] in {"batch", "window"}
    assert item["norm_context"]["sampling"] == "post_filtered_local"


def test_窗口百分位自动纳入当前值且始终在零到一() -> None:
    from app.reliability.scoring import normalize_evidence_metrics

    [item] = normalize_evidence_metrics(
        [{"id": "hn-max", "platform": "hacker_news", "raw_metrics": {"points": 100}}],
        computed_at="2026-08-20T00:00:00Z", report_id="r-01", goal_id="goal-1",
        window_values={"hacker_news": list(range(50))},
    )
    assert item["norm_method"] == "percentile_in_window"
    assert item["normalized_score"] == 1.0
    assert item["norm_context"]["n"] == 51


def test_显式_none_可关闭归一化且显式百分位遵守样本门槛() -> None:
    from app.reliability.scoring import normalize_evidence_metrics

    [item] = normalize_evidence_metrics(
        [{"id": "hn-1", "platform": "hacker_news", "raw_metrics": {"points": 1}}],
        computed_at="2026-08-20T00:00:00Z", report_id="r-01", goal_id="goal-1",
        method="none",
    )
    assert item["norm_method"] == "none"
    with pytest.raises(ValueError, match="n>=20"):
        normalize_evidence_metrics(
            [{"id": "hn-1", "platform": "hacker_news", "raw_metrics": {"points": 1}}],
            computed_at="2026-08-20T00:00:00Z", report_id="r-01", goal_id="goal-1",
            method="percentile_in_batch",
        )


def test_evidence_批量写入原子落五维与受控_extra(tmp_path) -> None:
    from app.reliability.scoring import score_evidence
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database_path)
    store.create_report(
        id="r-01",
        title="可靠度测试",
        research_question="可靠度契约",
        created_at="2026-08-19T00:00:00Z",
    )
    source = _evidence()
    scored = score_evidence(source)
    row = {
        key: value
        for key, value in source.items()
        if key not in {"has_body", "permalink_reachable", "comments_fetched", "comments_total", "firsthand_by_claim"}
    }
    row.update(scored)
    row["rated_by"] = "agent:reliability-auditor@claude"
    row["norm_method"] = "none"
    row["normalized_score"] = None
    row["norm_context"] = {
        "scope": "batch", "platform": "hacker_news", "metric": "points", "n": 0,
        "formula": "none", "stats": {}, "computed_at": "2026-08-19T00:00:00Z",
        "reason": "insufficient_sample",
    }
    row["extra"] = {
        **row["extra"],
        "origin_key": "news.ycombinator.com/item?id=1",
        "crossref_cluster": "cl-01",
        "crossref_n_clusters": 2,
        "crossref_peers": [],
        "crossref_conflicts": [],
    }

    store.add_evidence_batch([row])

    with sqlite3.connect(database_path) as connection:
        saved = connection.execute(
            "SELECT score_authority, score_freshness, score_crossref, "
            "score_completeness, score_independence, rating_notes, extra "
            "FROM evidence WHERE id='ev-01'"
        ).fetchone()
    assert saved[:5] == (1, 2, 2, 2, 2)
    assert saved[5] == scored["rating_notes"]
    assert '"crossref_cluster":"cl-01"' in saved[6]


def test_evidence_批写任一条_rating_notes_非法则整批回滚(tmp_path) -> None:
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database_path)
    store.create_report(
        id="r-01", title="回滚", research_question="回滚", created_at="2026-08-19T00:00:00Z"
    )
    base = {
        "report_id": "r-01", "platform": "hacker_news", "permalink": "https://example.com/1",
        "fetched_at": "2026-08-19T00:00:00Z", "score_authority": 1,
        "score_freshness": 1, "score_crossref": 1, "score_completeness": 1,
        "score_independence": 1, "rated_by": "agent:reliability-auditor@claude",
        "extra": {},
    }
    good = {**base, "id": "ev-good", "rating_notes": "权威1:具名作者 · 时效1:历史内容 · 交叉1:弱源佐证 · 完整1:评论未全取 · 无关1:利益已披露"}
    bad = {**base, "id": "ev-bad", "permalink": "https://example.com/2", "rating_notes": "格式错误"}

    with pytest.raises(ValueError, match="rating_notes"):
        store.add_evidence_batch([good, bad])
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_evidence_批写第二条唯一键冲突时真实事务整批回滚(tmp_path) -> None:
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database_path)
    store.create_report(
        id="r-01", title="回滚", research_question="唯一键冲突", created_at="2026-08-20T00:00:00Z"
    )
    base = {
        "id": "ev-duplicate", "report_id": "r-01", "platform": "web_search",
        "permalink": "https://example.com/a", "fetched_at": "2026-08-20T00:00:00Z",
    }
    with pytest.raises(sqlite3.IntegrityError):
        store.add_evidence_batch([base, {**base, "permalink": "https://example.com/b"}])
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_evidence_有五维分时必须同时提供合法理由(tmp_path) -> None:
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(database_path)
    store.create_report(
        id="r-01", title="理由", research_question="理由必填", created_at="2026-08-20T00:00:00Z"
    )
    with pytest.raises(ValueError, match="rating_notes"):
        store.add_evidence(
            id="ev-01", report_id="r-01", platform="web_search",
            permalink="https://example.com/a", fetched_at="2026-08-20T00:00:00Z",
            score_authority=1, score_freshness=1, score_crossref=1,
            score_completeness=1, score_independence=1,
        )
