import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class SchemaSqlTest(unittest.TestCase):
    def test_schema_建立五张业务表_三张运行态表和召回虚拟表(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file(), "app/store/schema.sql 尚未创建")

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "owli.db"
            with sqlite3.connect(database_path) as connection:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        application_tables = {
            name
            for name in tables
            if not name.startswith("recall_fts") and not name.startswith("sqlite_")
        }
        self.assertEqual(
            application_tables,
            {
                "reports", "evidence", "feedback", "report_tags", "ext_key_registry",
                "source_usage", "source_usage_billed_resource",
                "chapter_progress", "events",
            },
        )
        self.assertIn("recall_fts", tables)
        self.assertEqual(journal_mode, "wal")

    def test_v6数据库迁移到_v10_且保留既有报告(self) -> None:
        from app.store.schema import initialize_database_if_empty

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "owli-v6.db"
            with sqlite3.connect(database_path) as connection:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                connection.execute("DROP TABLE IF EXISTS events")
                connection.execute("DROP INDEX IF EXISTS idx_evidence_native_identity")
                connection.execute("PRAGMA user_version = 6")
                connection.execute(
                    "INSERT INTO reports(id,title,research_question,created_at) "
                    "VALUES ('r-existing','既有报告','不得丢失','2026-08-25T00:00:00Z')"
                )

            initialize_database_if_empty(database_path, SCHEMA_PATH)

            with sqlite3.connect(database_path) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                report = connection.execute(
                    "SELECT title FROM reports WHERE id='r-existing'"
                ).fetchone()
                event_columns = {
                    row[1] for row in connection.execute("PRAGMA table_xinfo(events)")
                }
                chapter_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_xinfo(chapter_progress)")
                }

        self.assertEqual(version, 10)
        self.assertEqual(report, ("既有报告",))
        self.assertEqual(
            event_columns,
            {"research_id", "sequence", "type", "payload", "created_at"},
        )
        self.assertIn("extra", chapter_columns)

    def test_evidence_原生平台键有非空唯一索引(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            indexes = connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'evidence'"
            ).fetchall()

        definition = next(
            sql for name, sql in indexes
            if name == "idx_evidence_native_identity"
        )
        self.assertIn("UNIQUE", definition)
        self.assertIn("report_id, platform, platform_item_id", definition)
        self.assertIn("platform_item_id IS NOT NULL", definition)


if __name__ == "__main__":
    unittest.main()
