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


if __name__ == "__main__":
    unittest.main()
