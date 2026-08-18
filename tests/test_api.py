import asyncio
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "app" / "api" / "main.py"
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class HealthApiTest(unittest.TestCase):
    def test_health_统一包封并带_schema_自检结果(self) -> None:
        self.assertTrue(MAIN_PATH.is_file(), "app/api/main.py 尚未创建")
        from app.api.main import create_app

        with tempfile.TemporaryDirectory() as temp_dir:
            engine_checks = {
                "claude": {"status": "available", "version": "0.1.60"},
                "codex": {"status": "unavailable", "status_label": "引擎不可用"},
            }
            app = create_app(
                Path(temp_dir) / "owli.db",
                SCHEMA_PATH,
                engine_probe=lambda: engine_checks,
            )

            async def request_health() -> dict:
                async with app.router.lifespan_context(app):
                    route = next(
                        route
                        for route in app.routes
                        if getattr(route, "path", None) == "/api/health"
                    )
                    return await route.endpoint()

            response = asyncio.run(request_health())

        self.assertEqual(set(response), {"ok", "data", "error"})
        self.assertTrue(response["ok"])
        self.assertIsNone(response["error"])
        self.assertTrue(response["data"]["schema"]["ok"])
        self.assertEqual(response["data"]["schema"]["journal_mode"], "wal")
        self.assertEqual(response["data"]["engines"], engine_checks)


if __name__ == "__main__":
    unittest.main()
