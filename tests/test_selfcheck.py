import sqlite3
import subprocess
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
        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(
            result["tables"],
            [
                "chapter_progress", "evidence", "ext_key_registry", "feedback", "report_tags", "reports",
                "source_usage", "source_usage_billed_resource",
            ],
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


class EngineSelfCheckTest(unittest.TestCase):
    def test_codex_版本与只读沙箱干跑均确认才可用(self) -> None:
        from app.adapters.selfcheck import probe_codex_cli

        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(command, 0, "codex-cli 0.146.0\n", "")
            return subprocess.CompletedProcess(
                command, 0, "sandbox: read-only [workdir, /tmp]\n自检完成\n", ""
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = probe_codex_cli(
                executable="fake-codex",
                codex_home=Path(temp_dir) / "codex-home",
                runner=runner,
            )

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["version"], "codex-cli 0.146.0")
        self.assertEqual(result["sandbox"], "read-only")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0][calls[1][0].index("-s") + 1], "read-only")

    def test_codex_不存在或沙箱回显不符均为引擎不可用(self) -> None:
        from app.adapters.selfcheck import probe_codex_cli

        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])

        def mismatch(command, **kwargs):
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(command, 0, "codex-cli 1.0.0\n", "")
            return subprocess.CompletedProcess(command, 0, "sandbox: workspace-write\n", "")

        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            missing_result = probe_codex_cli(
                executable="missing", codex_home=codex_home, runner=missing
            )
            mismatch_result = probe_codex_cli(
                executable="fake", codex_home=codex_home, runner=mismatch
            )

        self.assertEqual(missing_result["status"], "unavailable")
        self.assertEqual(missing_result["status_label"], "引擎不可用")
        self.assertEqual(mismatch_result["status"], "unavailable")
        self.assertIn("沙箱", mismatch_result["detail"])

    def test_claude_sdk_版本探测结果进入双引擎集合(self) -> None:
        from app.adapters.selfcheck import probe_claude_sdk, probe_engines

        claude = probe_claude_sdk(version_reader=lambda name: "0.1.60")
        engines = probe_engines(
            claude_probe=lambda: claude,
            codex_probe=lambda: {
                "status": "unavailable",
                "status_label": "引擎不可用",
                "version": None,
                "sandbox": None,
                "detail": "测试",
            },
        )

        self.assertEqual(claude["status"], "available")
        self.assertEqual(claude["version"], "0.1.60")
        self.assertEqual(set(engines), {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
