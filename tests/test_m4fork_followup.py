from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class BackfillEngine:
    def __init__(self, *, missing_independence_for: str | None = None) -> None:
        self.missing_independence_for = missing_independence_for
        self.calls = 0
        self.goal_batches: list[set[str]] = []
        self.engine_preferences: list[str | None] = []

    async def run(self, task, ctx, on_event=None):
        assert ctx.agent_id == task.agent_id
        del ctx, on_event
        self.calls += 1
        assert str(task.output_path) in task.body
        assert task.runs_root == task.output_path.parents[4]
        inputs, _ = json.JSONDecoder().raw_decode(
            task.body.split("输入证据：", 1)[1]
        )
        assert task.output_path.parent.is_dir()
        self.engine_preferences.append(task.user_override)
        self.goal_batches.append({item["goal_id"] for item in inputs})
        output = []
        for item in inputs:
            missing = {}
            relation = "arms_length"
            if item["id"] == self.missing_independence_for:
                relation = None
                missing["score_independence"] = "缺正文披露信息"
            output.append({
                "id": item["id"],
                "authority_kind": "named_secondary",
                "content_kind": "industry_view",
                "interest_relation": relation,
                "missing_dimensions": missing,
            })
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(
            json.dumps(output, ensure_ascii=False), encoding="utf-8"
        )
        return SimpleNamespace(succeeded=True)


def _database(tmp_path: Path):
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return database_path, Store(database_path)


def _evidence(report_id: str, suffix: str, **changes) -> dict:
    item = {
        "id": f"ev-{suffix}",
        "report_id": report_id,
        "goal_id": "goal-1",
        "agent_name": "collector",
        "engine": "codex",
        "platform": "web_search",
        "source_type": "article",
        "permalink": f"https://example.com/{suffix}",
        "title": f"来源 {suffix}",
        "content_excerpt": "这是一段足以复核结论的正文摘要。",
        "author_name": "具名作者",
        "published_at": "2026-08-01T00:00:00Z",
        "fetched_at": "2026-08-26T00:00:00Z",
        "raw_metrics": {},
        "extra": {
            "claim_ids": ["c-1"],
            "crossref_n_clusters": 1,
            "crossref_verdict": "SINGLE",
        },
    }
    item.update(changes)
    return item


def test_rating_notes_允许单维诚实缺失且报告渲染问号() -> None:
    from app.reliability.scoring import rating_notes_problem
    from app.report.markdown import render_source_list

    item = _evidence("r-partial", "partial") | {
        "citation_no": 1,
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": 0,
        "score_completeness": 2,
        "score_independence": None,
        "rating_notes": (
            "权威1:具名二手来源 · 时效2:距采集25天 · 交叉0:该断言仅一簇 · "
            "完整2:正文作者时间齐全 · 无关?:缺正文披露信息"
        ),
    }

    assert rating_notes_problem(item["rating_notes"], item) is None
    rendered = render_source_list([item])
    assert "五维=权威1/时效2/交叉0/完整2/无关?" in rendered
    assert "无关?:缺正文披露信息" in rendered


def test_外部闭集判定只接收最小公开信号() -> None:
    from app.reliability.backfill import _engine_input, _scoring_view

    compact = _engine_input(_evidence("r-private", "private") | {
        "content_excerpt": "不应外发的证据摘要",
        "author_name": "不应外发的作者名",
        "raw_metrics": {"private_metric": 42},
        "extra": {
            "facts": ["不应外发的事实原文"],
            "evidence": "不应外发的扩展正文",
            "entity": "公开研究主体",
            "dimension": "公开研究维度",
        },
    })

    serialized = json.dumps(compact, ensure_ascii=False)
    assert "不应外发" not in serialized
    assert "private_metric" not in serialized
    assert compact["author_signals"] == {
        "present": True,
        "verified": False,
        "affiliation_present": False,
    }
    assert compact["signals"] == {
        "entity": "公开研究主体",
        "dimension": "公开研究维度",
    }
    unknown_reachability = _scoring_view(
        _evidence("r-private", "unknown-reachability"),
        {
            "authority_kind": "named_secondary",
            "content_kind": "industry_view",
            "interest_relation": "arms_length",
        },
    )
    assert unknown_reachability["permalink_reachable"] is None


def test_引擎不能把本地可计算维度宣告为缺失() -> None:
    from app.reliability.backfill import _label_errors

    inputs = [{"id": "ev-1"}]
    value = [{
        "id": "ev-1",
        "authority_kind": "named_secondary",
        "content_kind": "industry_view",
        "interest_relation": "arms_length",
        "missing_dimensions": {"score_completeness": "未看到正文"},
    }]

    errors = _label_errors(value, inputs)
    assert any("本地计算维度" in error for error in errors)

    value[0]["missing_dimensions"] = {"score_freshness": "缺少发布时间"}
    errors = _label_errors(value, inputs)
    assert any("标签已判定" in error for error in errors)


def test_补评提示词包含无作者与无可见利益的保守闭集口径(tmp_path: Path) -> None:
    from app.reliability.backfill import _prompt

    text = _prompt([], output_path=tmp_path / "batch.json")

    assert "作者信号不存在=anonymous_or_unverifiable" in text
    assert "无可见利益关系=arms_length" in text
    assert "仍无法落入任一闭集" in text


def test_短暂传输告警后仅在产物与结论双腿齐全时恢复(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-recovered"
    store.create_report(
        id=report_id,
        title="传输恢复",
        research_question="双腿齐全能否恢复？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([_evidence(report_id, "recovered")])

    class RecoverableEngine(BackfillEngine):
        async def run(self, task, ctx, on_event=None):
            await super().run(task, ctx, on_event)
            conclusion_path = (
                task.output_path.parent
                / ".reliability-auditor-codex-last-message.json"
            )
            conclusion_path.write_text(json.dumps({
                "status": "done",
                "output_path": str(task.output_path),
                "summary": "产物已落盘",
                "assumptions": [],
                "unmet": [],
                "capability_denials": [],
                "reason": None,
            }, ensure_ascii=False), encoding="utf-8")
            return SimpleNamespace(
                succeeded=False,
                engine_error="stream disconnected before completion: tls handshake eof",
            )

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=RecoverableEngine(),
        runs_root=tmp_path / "runs",
        engine_preference="codex",
    ))

    assert result.rated == 1
    assert result.failed == 0
    assert store.list_evidence(report_id)[0]["score_authority"] == 1


def test_上轮结论不能为本轮传输失败背书(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-stale-conclusion"
    store.create_report(
        id=report_id,
        title="旧结论",
        research_question="能否复用上轮结论？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([_evidence(report_id, "stale")])
    output_path = (
        tmp_path / "runs" / report_id / "goals" / "goal-1"
        / "reliability-backfill" / "batch-001.json"
    )
    output_path.parent.mkdir(parents=True)
    conclusion_path = (
        output_path.parent / ".reliability-auditor-codex-last-message.json"
    )
    conclusion_path.write_text(json.dumps({
        "status": "done",
        "output_path": str(output_path),
        "summary": "上轮结论",
        "assumptions": [],
        "unmet": [],
        "capability_denials": [],
        "reason": None,
    }, ensure_ascii=False), encoding="utf-8")

    class OutputOnlyFailure(BackfillEngine):
        async def run(self, task, ctx, on_event=None):
            await super().run(task, ctx, on_event)
            return SimpleNamespace(
                succeeded=False,
                engine_error="stream disconnected before completion: tls handshake eof",
            )

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=OutputOnlyFailure(),
        runs_root=tmp_path / "runs",
        engine_preference="codex",
    ))

    assert result.failed == 1
    assert store.list_evidence(report_id)[0]["score_authority"] is None


def test_路径身份越界在建目录与删产物前被拒绝(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-safe-path"
    store.create_report(
        id=report_id,
        title="路径边界",
        research_question="goal_id 能否越界？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([
        _evidence(report_id, "escape", goal_id="../../../../escape")
    ])
    engine = BackfillEngine()

    try:
        asyncio.run(backfill_report(
            store,
            report_id,
            adapter=engine,
            runs_root=tmp_path / "runs",
        ))
    except ValueError as exc:
        assert "goal_id" in str(exc)
    else:
        raise AssertionError("越界 goal_id 应在投递前被拒绝")

    assert engine.calls == 0
    assert not (tmp_path / "escape").exists()


def test_补评研究目录为外部软链时拒绝写入(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-symlink-escape"
    store.create_report(
        id=report_id,
        title="软链边界",
        research_question="研究目录软链能否越界？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([_evidence(report_id, "symlink")])
    runs_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    runs_root.mkdir()
    outside.mkdir()
    (runs_root / report_id).symlink_to(outside, target_is_directory=True)
    engine = BackfillEngine()

    try:
        asyncio.run(backfill_report(
            store,
            report_id,
            adapter=engine,
            runs_root=runs_root,
        ))
    except ValueError as exc:
        assert "路径越界" in str(exc)
    else:
        raise AssertionError("外部软链研究目录应在投递前被拒绝")

    assert engine.calls == 0
    assert list(outside.iterdir()) == []


def test_未指定引擎时_rated_by_记录权威默认路由(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-unrouted-name"
    store.create_report(
        id=report_id,
        title="引擎溯源",
        research_question="未指定引擎时如何记录？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([_evidence(report_id, "unrouted")])

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=BackfillEngine(),
        runs_root=tmp_path / "runs",
    ))

    assert result.failed == 0
    assert (
        store.list_evidence(report_id)[0]["rated_by"]
        == "agent:reliability-auditor@claude"
    )


def test_缺断言血缘簇时交叉维度保持_null(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-missing-crossref"
    store.create_report(
        id=report_id,
        title="交叉缺失",
        research_question="无断言簇如何处理？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([
        _evidence(report_id, "missing-crossref", extra={})
    ])

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=BackfillEngine(),
        runs_root=tmp_path / "runs",
        engine_preference="codex",
    ))

    [row] = store.list_evidence(report_id)
    assert result.failed == 0
    assert row["score_crossref"] is None
    assert "交叉?:缺断言血缘簇" in row["rating_notes"]


def test_已落库闭集标签可由本地补算_不重投外部引擎(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-reuse-labels"
    store.create_report(
        id=report_id,
        title="复用闭集",
        research_question="已落库标签是否可复用？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([_evidence(
        report_id,
        "reuse-labels",
        extra={
            "authority_kind": "named_secondary",
            "content_kind": "industry_view",
            "interest_relation": "arms_length",
        },
    )])

    class MustNotRun:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            raise AssertionError("已落库闭集标签不应重投外部引擎")

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=MustNotRun(),
        runs_root=tmp_path / "runs",
        engine_preference="codex",
    ))

    assert result.rated == 1
    assert result.failed == 0
    [row] = store.list_evidence(report_id)
    assert row["score_crossref"] is None
    assert row["rated_by"] == "rule:reliability-backfill@v1"


def test_本地补算保留既有诚实缺失与原始评级来源(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-reuse-partial"
    store.create_report(
        id=report_id,
        title="复用诚实缺失",
        research_question="既有缺失理由与来源是否保留？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([_evidence(
        report_id,
        "reuse-partial",
        extra={
            "authority_kind": "named_secondary",
            "interest_relation": "arms_length",
        },
        score_authority=1,
        score_freshness=None,
        score_crossref=None,
        score_completeness=2,
        score_independence=2,
        score_total=None,
        grade=None,
        rating_notes=(
            "权威1:具名二手来源 · 时效?:内容类型不可判 · 交叉?:缺断言血缘簇 · "
            "完整2:正文作者时间齐全 · 无关2:无可见利益关系"
        ),
        rated_by="baseline:curated-fixture",
    )])

    class MustNotRun:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            raise AssertionError("既有诚实缺失不应重投外部引擎")

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=MustNotRun(),
        runs_root=tmp_path / "runs",
        engine_preference="codex",
    ))

    assert result.failed == 0
    [row] = store.list_evidence(report_id)
    assert row["score_freshness"] is None
    assert "时效?:内容类型不可判" in row["rating_notes"]
    assert row["rated_by"] == "baseline:curated-fixture"


def test_混合批次只外评未分类证据_不覆盖既有诚实缺失(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-mixed-reuse"
    store.create_report(
        id=report_id,
        title="混合批次",
        research_question="待外评项能否污染同批诚实缺失？",
        created_at="2026-08-26T00:00:00Z",
    )
    store.upsert_evidence_batch([
        _evidence(
            report_id,
            "locked-partial",
            extra={
                "authority_kind": "named_secondary",
                "interest_relation": "arms_length",
            },
            score_authority=1,
            score_freshness=None,
            score_crossref=None,
            score_completeness=2,
            score_independence=2,
            score_total=None,
            grade=None,
            rating_notes=(
                "权威1:具名二手来源 · 时效?:内容类型不可判 · 交叉?:缺断言血缘簇 · "
                "完整2:正文作者时间齐全 · 无关2:无可见利益关系"
            ),
            rated_by="baseline:curated-fixture",
        ),
        _evidence(report_id, "needs-engine", extra={}),
    ])
    engine = BackfillEngine()

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=engine,
        runs_root=tmp_path / "runs",
        batch_size=50,
        engine_preference="codex",
    ))

    assert result.rated == 2
    assert result.failed == 0
    assert engine.calls == 1
    [locked, classified] = store.list_evidence(report_id)
    assert locked["id"] == "ev-locked-partial"
    assert locked["score_freshness"] is None
    assert "时效?:内容类型不可判" in locked["rating_notes"]
    assert locked["rated_by"] == "baseline:curated-fixture"
    assert classified["id"] == "ev-needs-engine"
    assert classified["score_freshness"] == 2
    assert classified["rated_by"] == "agent:reliability-auditor@codex"


def test_库后补评保留诚实缺失_角标双向可查且两遍幂等(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    database_path, store = _database(tmp_path)
    report_id = "r-backfill"
    report_path = tmp_path / "runs" / report_id / "goals" / "goal-3" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        "title": "补评报告",
        "sections": [{
            "section_id": "sec-1",
            "title": "结论",
            "markdown": (
                "## 结论\n\n- 数据库补评后可以复核来源 [S01]\n"
                "- 信息不足的维度保持空值 [S02]\n\n"
                "## 信息源\n\n- [S01] [来源 a](https://example.com/a)\n"
                "- [S02] [来源 b](https://example.com/b)"
            ),
        }],
    }, ensure_ascii=False), encoding="utf-8")
    store.create_report(
        id=report_id,
        title="补评报告",
        research_question="补评是否幂等？",
        created_at="2026-08-26T00:00:00Z",
        completed_at="2026-08-26T01:00:00Z",
        status="completed",
        summary_line="全部 goal 已完成",
        report_path=str(report_path),
    )
    store.upsert_evidence_batch([
        _evidence(report_id, "a"),
        _evidence(report_id, "b", goal_id="goal-2"),
    ])
    engine = BackfillEngine(missing_independence_for="ev-b")

    first = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=engine,
        runs_root=tmp_path / "runs",
        batch_size=50,
        engine_preference="codex",
    ))
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        first_rows = [dict(row) for row in connection.execute(
            "SELECT id, score_authority, score_freshness, score_crossref, "
            "score_completeness, score_independence, rating_notes, rated_by, "
            "citation_no FROM evidence WHERE report_id = ? ORDER BY id",
            (report_id,),
        )]
    second = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=engine,
        runs_root=tmp_path / "runs",
        batch_size=50,
        force=True,
        engine_preference="codex",
    ))
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        second_rows = [dict(row) for row in connection.execute(
            "SELECT id, score_authority, score_freshness, score_crossref, "
            "score_completeness, score_independence, rating_notes, rated_by, "
            "citation_no FROM evidence WHERE report_id = ? ORDER BY id",
            (report_id,),
        )]

    assert first.before_rows == first.after_rows == 2
    assert second.before_rows == second.after_rows == 2
    assert first_rows == second_rows
    assert all(len(goal_ids) == 1 for goal_ids in engine.goal_batches)
    assert set(engine.engine_preferences) == {"codex"}
    assert all(first_rows[0][field] is not None for field in (
        "score_authority", "score_freshness", "score_crossref",
        "score_completeness", "score_independence",
    ))
    assert first_rows[0]["citation_no"] == 1
    assert first_rows[1]["citation_no"] == 2
    assert first_rows[1]["score_independence"] is None
    assert first_rows[0]["rated_by"].endswith("@codex")
    assert "无关?:缺正文披露信息" in first_rows[1]["rating_notes"]
    report = store.get_report(report_id)
    assert report["summary_line"] == "数据库补评后可以复核来源"
    with sqlite3.connect(database_path) as connection:
        indexed_summary = connection.execute(
            "SELECT summary_line FROM recall_fts WHERE report_id = ?", (report_id,)
        ).fetchone()[0]
    assert indexed_summary == report["summary_line"]
    rendered = json.loads(report_path.read_text(encoding="utf-8"))
    source_markdown = rendered["sections"][0]["markdown"]
    assert "[S01]" in source_markdown
    assert "[S02]" in source_markdown
    assert "五维=权威1/时效2/交叉0/完整2/无关2" in source_markdown
    assert "五维=权威1/时效2/交叉0/完整2/无关?" in source_markdown
    from app.adapters.validation import _source_inventory_offenders
    assert _source_inventory_offenders(source_markdown) == ([], [])


def test_角标改写前拒绝正文与来源不双向(tmp_path: Path) -> None:
    del tmp_path
    from app.reliability.backfill import (
        _assert_body_citations_resolvable,
        _assert_citation_bijection,
    )

    markdown = (
        "## 结论\n\n- 正文引用 [S01]\n\n"
        "## 信息源\n\n- [S02] [来源](https://example.com/a)"
    )
    try:
        _assert_citation_bijection(markdown)
    except ValueError as exc:
        assert "角标不双向" in str(exc)
    else:
        raise AssertionError("孤立或无法解析角标应被拒绝")

    removable_orphan = (
        "## 结论\n\n- 正文引用 [S01]\n\n"
        "## 信息源\n\n- [S01] [已引用](https://example.com/a)\n"
        "- [S02] [未引用](https://example.com/b)"
    )
    assert _assert_body_citations_resolvable(removable_orphan) == {1}


def test_补评引擎失败不拿平台基线冒充实评(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report

    _, store = _database(tmp_path)
    report_id = "r-engine-failed"
    report_path = tmp_path / "runs" / report_id / "goals" / "goal-3" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        "sections": [{
            "markdown": (
                "## 结论\n\n- 引擎失败时保持待补评 [S01]\n\n"
                "## 信息源\n\n- [S01] [来源](https://example.com/failed)"
            )
        }]
    }, ensure_ascii=False), encoding="utf-8")
    store.create_report(
        id=report_id,
        title="失败保真",
        research_question="引擎失败怎么办？",
        created_at="2026-08-26T00:00:00Z",
        report_path=str(report_path),
    )
    store.upsert_evidence_batch([_evidence(report_id, "failed")])

    class FailedEngine:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            return SimpleNamespace(succeeded=False)

    result = asyncio.run(backfill_report(
        store,
        report_id,
        adapter=FailedEngine(),
        runs_root=tmp_path / "runs",
    ))
    [row] = store.list_evidence(report_id)
    assert result.failed == 1
    assert all(row[field] is None for field in (
        "score_authority", "score_freshness", "score_crossref",
        "score_completeness", "score_independence",
    ))
    assert row["rated_by"] is None
    assert row["citation_no"] is None


def test_补评脚本数据库走位置参数且研究可重复指定() -> None:
    script = ROOT / "scripts" / "backfill-evidence-ratings.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "database" in completed.stdout
    assert "--report-id" in completed.stdout
