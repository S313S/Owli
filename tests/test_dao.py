import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DAO_PATH = ROOT / "app" / "store" / "dao.py"
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "owli.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reports_与_evidence_只能通过具名接口写入(self) -> None:
        self.assertTrue(DAO_PATH.is_file(), "app/store/dao.py 尚未创建")
        from app.store.dao import Store

        store = Store(self.database_path)
        store.create_report(
            id="r-01",
            title="飞书竞品优缺点",
            research_question="飞书与竞品相比有哪些优缺点？",
            created_at="2026-08-18T10:00:00+08:00",
            extra={"subject_domains": ["feishu.cn"]},
        )
        store.add_evidence(
            id="ev-01",
            report_id="r-01",
            platform="hacker_news",
            permalink="https://news.ycombinator.com/item?id=1",
            fetched_at="2026-08-18T10:01:00+08:00",
            raw_metrics={"points": 42},
            extra={"claim_ids": ["claim-1"]},
        )

        report = store.get_report("r-01")
        self.assertIsNotNone(report)
        self.assertEqual(report["extra"], {"subject_domains": ["feishu.cn"]})

        with sqlite3.connect(self.database_path) as connection:
            evidence = connection.execute(
                "SELECT raw_metrics, extra FROM evidence WHERE id = ?", ("ev-01",)
            ).fetchone()
            registry = connection.execute(
                "SELECT table_name, key, seen_count, report_count "
                "FROM ext_key_registry ORDER BY table_name, key"
            ).fetchall()

        self.assertEqual(evidence, ('{"points":42}', '{"claim_ids":["claim-1"]}'))
        self.assertEqual(
            registry,
            [
                ("evidence", "claim_ids", 1, 1),
                ("reports", "subject_domains", 1, 1),
            ],
        )

    def test_extra_必须是字典(self) -> None:
        self.assertTrue(DAO_PATH.is_file(), "app/store/dao.py 尚未创建")
        from app.store.dao import Store

        store = Store(self.database_path)
        with self.assertRaisesRegex(TypeError, "extra 必须是 dict"):
            store.create_report(
                id="r-02",
                title="错误数据",
                research_question="验证 extra",
                created_at="2026-08-18T10:00:00+08:00",
                extra=[],
            )

    def _create_report(self, store) -> None:
        store.create_report(
            id="r-upsert",
            title="证据幂等",
            research_question="同一 goal 重试是否重复写行？",
            created_at="2026-08-21T10:00:00+08:00",
        )

    def _evidence(self, **changes) -> dict:
        scores = {
            "score_authority": 1,
            "score_freshness": 2,
            "score_crossref": 0,
            "score_completeness": 1,
            "score_independence": 2,
        }
        item = {
            "id": "ev-round-1",
            "report_id": "r-upsert",
            "goal_id": "goal-5",
            "platform": "hacker_news",
            "platform_item_id": "item-42",
            "permalink": "https://news.ycombinator.com/item?id=42",
            "title": "第一轮",
            "fetched_at": "2026-08-21T02:00:00+00:00",
            "raw_metrics": {"points": 42},
            "normalized_score": None,
            "norm_method": "none",
            "norm_context": {
                "scope": "batch",
                "platform": "hacker_news",
                "metric": "points",
                "n": 1,
                "formula": "none",
                "stats": {},
                "computed_at": "2026-08-21T02:00:00+00:00",
                "reason": "insufficient_sample",
            },
            **scores,
            "rating_notes": (
                "权威1:平台基线 · 时效2:时间窗内 · 交叉0:单一来源 · "
                "完整1:摘要可追溯 · 无关2:无利益关系"
            ),
            "rated_by": "baseline:hacker_news@v1",
        }
        item.update(changes)
        return item

    def test_有平台原生id时两轮upsert行数不变且字段取最新(self) -> None:
        from app.store.dao import Store

        store = Store(self.database_path)
        self._create_report(store)
        first = self._evidence()
        second = self._evidence(
            id="ev-round-2",
            permalink="https://news.ycombinator.com/item?id=42&ref=latest",
            title="第二轮最新",
            fetched_at="2026-08-21T03:00:00+00:00",
            raw_metrics={"points": 88},
            score_authority=2,
            rating_notes=(
                "权威2:第二轮已核验 · 时效2:时间窗内 · 交叉0:单一来源 · "
                "完整1:摘要可追溯 · 无关2:无利益关系"
            ),
        )

        store.upsert_evidence_batch([first])
        store.upsert_evidence_batch([second])

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT id, title, permalink, raw_metrics, score_authority "
                "FROM evidence WHERE report_id = ?",
                ("r-upsert",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "ev-round-1")
        self.assertEqual(rows[0][1:], (
            "第二轮最新",
            "https://news.ycombinator.com/item?id=42&ref=latest",
            '{"points":88}',
            2,
        ))

    def test_无平台原生id时按归一化permalink幂等(self) -> None:
        from app.store.dao import Store

        store = Store(self.database_path)
        self._create_report(store)
        first = self._evidence(
            platform="web_search",
            platform_item_id=None,
            permalink="HTTPS://Example.COM:443/path/#old",
            norm_context={
                "scope": "batch", "platform": "web_search", "metric": None,
                "n": 1, "formula": "none", "stats": {},
                "computed_at": "2026-08-21T02:00:00+00:00",
                "reason": "no_metric_available",
            },
        )
        second = {
            **first,
            "id": "ev-round-2",
            "permalink": "https://example.com/path",
            "title": "归一化后更新",
        }

        store.upsert_evidence_batch([first])
        store.upsert_evidence_batch([second])

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT permalink, title FROM evidence WHERE report_id = ?",
                ("r-upsert",),
            ).fetchall()
        self.assertEqual(rows, [("https://example.com/path", "归一化后更新")])


if __name__ == "__main__":
    unittest.main()
