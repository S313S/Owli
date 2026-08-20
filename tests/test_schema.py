import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class SchemaSqlTest(unittest.TestCase):
    def test_schema_建立五张业务表_两张运行态表和召回虚拟表(self) -> None:
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
            },
        )
        self.assertIn("recall_fts", tables)
        self.assertEqual(journal_mode, "wal")


if __name__ == "__main__":
    unittest.main()
