import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
SELFCHECK_PATH = ROOT / "app" / "adapters" / "selfcheck.py"


class SchemaSelfCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.database_path = self.temp_path / "owli.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_空库初始化后自检通过(self) -> None:
        self.assertTrue(SELFCHECK_PATH.is_file(), "app/adapters/selfcheck.py 尚未创建")
        from app.adapters.selfcheck import initialize_and_check

        result = initialize_and_check(self.database_path, SCHEMA_PATH)

        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(
            result["tables"],
            ["evidence", "ext_key_registry", "feedback", "report_tags", "reports"],
        )
        self.assertEqual(result["virtual_tables"], ["recall_fts"])

    def test_schema_列名变化时指出双向差异并拒绝启动(self) -> None:
        self.assertTrue(SELFCHECK_PATH.is_file(), "app/adapters/selfcheck.py 尚未创建")
        from app.adapters.selfcheck import SchemaCheckError, initialize_and_check

        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        changed_schema = self.temp_path / "changed-schema.sql"
        changed_schema.write_text(
            SCHEMA_PATH.read_text(encoding="utf-8").replace(
                "research_question  TEXT", "research_query     TEXT", 1
            ),
            encoding="utf-8",
        )

        with self.assertRaises(SchemaCheckError) as raised:
            initialize_and_check(self.database_path, changed_schema)

        message = str(raised.exception)
        self.assertIn("reports", message)
        self.assertIn("缺少列 research_query", message)
        self.assertIn("多出列 research_question", message)

    def test_缺表时指出表名并拒绝启动(self) -> None:
        self.assertTrue(SELFCHECK_PATH.is_file(), "app/adapters/selfcheck.py 尚未创建")
        from app.adapters.selfcheck import SchemaCheckError, initialize_and_check

        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.execute("DROP TABLE feedback")

        with self.assertRaises(SchemaCheckError) as raised:
            initialize_and_check(self.database_path, SCHEMA_PATH)

        self.assertIn("缺少表 feedback", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
