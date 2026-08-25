import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVENTS_PATH = ROOT / "app" / "api" / "events.py"
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


class EventBufferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.assertTrue(EVENTS_PATH.is_file(), "app/api/events.py 尚未创建")
        from app.api.events import ResearchEventBuffer

        self.clock = MutableClock()
        self.buffer = ResearchEventBuffer(max_events=3, max_age_seconds=3600, clock=self.clock)

    async def test_事件序号单调递增且_sse_id_一致(self) -> None:
        first = await self.buffer.publish("r-1", {"type": "agent_update", "data": {"status": "running"}})
        second = await self.buffer.publish("r-1", {"type": "agent_update", "data": {"status": "done"}})

        self.assertEqual([first.sequence, second.sequence], [1, 2])
        self.assertIn("id: 2\n", second.to_sse())
        payload = json.loads(next(line[6:] for line in second.to_sse().splitlines() if line.startswith("data: ")))
        self.assertEqual(payload["sequence"], 2)

    async def test_Last_Event_ID_从下一条补发且无重无漏(self) -> None:
        for number in range(1, 4):
            await self.buffer.publish("r-1", {"type": "progress", "data": {"number": number}})

        replay = await self.buffer.replay_after("r-1", 1)

        self.assertFalse(replay.truncated)
        self.assertEqual([event.sequence for event in replay.events], [2, 3])

    async def test_超过条数窗口时只推_replay_truncated(self) -> None:
        for number in range(1, 5):
            await self.buffer.publish("r-1", {"type": "progress", "data": {"number": number}})

        replay = await self.buffer.replay_after("r-1", 0)

        self.assertTrue(replay.truncated)
        self.assertEqual(len(replay.events), 1)
        self.assertEqual(replay.events[0].payload["type"], "replay_truncated")
        self.assertEqual(replay.events[0].sequence, 5)

    async def test_超过时间窗口时推_replay_truncated(self) -> None:
        await self.buffer.publish("r-1", {"type": "progress", "data": {"number": 1}})
        self.clock.advance(minutes=61)
        await self.buffer.publish("r-1", {"type": "progress", "data": {"number": 2}})

        replay = await self.buffer.replay_after("r-1", 0)

        self.assertTrue(replay.truncated)
        self.assertEqual(replay.events[0].payload["type"], "replay_truncated")

    async def test_错误事件原始载荷不被改写(self) -> None:
        raw = {"subtype": "success", "api_error_status": 429, "message": {"nested": ["原始信息"]}}
        event = await self.buffer.publish("r-1", {"type": "error", "raw": raw, "data": {"summary": "限流"}})

        self.assertEqual(event.payload["raw"], raw)

    async def test_已发布事件冻结嵌套快照不受后续状态修改(self) -> None:
        state = {"goals": [{"id": "goal-1", "status": "queued"}]}
        await self.buffer.publish(
            "r-1", {"type": "research_snapshot", "data": state}
        )

        state["goals"][0]["status"] = "done"
        replay = await self.buffer.replay_after("r-1", 0)

        self.assertEqual(
            replay.events[0].payload["data"]["goals"][0]["status"], "queued"
        )

    async def test_事件以数据库为事实源且跨缓冲器实例续号(self) -> None:
        from app.api.events import ResearchEventBuffer
        from app.store.dao import Store

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "owli.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            store = Store(database)
            store.create_report(
                id="r-restart",
                title="跨重启事件",
                research_question="事件序号能否连续？",
                created_at=self.clock().isoformat(),
            )
            before_restart = ResearchEventBuffer(store=store, clock=self.clock)
            first = await before_restart.publish(
                "r-restart", {"type": "progress", "data": {"number": 1}}
            )
            second = await before_restart.publish(
                "r-restart", {"type": "progress", "data": {"number": 2}}
            )

            after_restart = ResearchEventBuffer(store=Store(database), clock=self.clock)
            replay = await after_restart.replay_after("r-restart", first.sequence)
            third = await after_restart.publish(
                "r-restart", {"type": "progress", "data": {"number": 3}}
            )

        self.assertEqual([event.sequence for event in replay.events], [2])
        self.assertEqual(second.sequence, 2)
        self.assertEqual(third.sequence, 3)

    async def test_持久窗口截断按库中最小可回放序号判断(self) -> None:
        from app.api.events import ResearchEventBuffer
        from app.store.dao import Store

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "owli.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            store = Store(database)
            store.create_report(
                id="r-truncated",
                title="持久窗口",
                research_question="截断判据是否跨重启？",
                created_at=self.clock().isoformat(),
            )
            buffer = ResearchEventBuffer(
                store=store, max_events=2, max_age_seconds=3600, clock=self.clock
            )
            for number in range(1, 4):
                await buffer.publish(
                    "r-truncated", {"type": "progress", "data": {"number": number}}
                )

            restarted = ResearchEventBuffer(
                store=Store(database), max_events=2, max_age_seconds=3600,
                clock=self.clock,
            )
            replay = await restarted.replay_after("r-truncated", 0)

        self.assertTrue(replay.truncated)
        self.assertEqual([event.sequence for event in replay.events], [4])
        self.assertEqual(replay.events[0].payload["type"], "replay_truncated")


class ApiShapeTest(unittest.TestCase):
    def test_缓冲器可在服务运行循环启动时重绑通知器(self) -> None:
        self.assertTrue(EVENTS_PATH.is_file(), "app/api/events.py 尚未创建")
        from app.api.events import ResearchEventBuffer

        buffer = ResearchEventBuffer()

        async def exercise() -> None:
            buffer.bind_to_running_loop()
            event = await buffer.publish("r-loop", {"type": "progress"})
            waited = await buffer.wait_after("r-loop", 0, timeout=0.01)
            self.assertEqual(waited, (event,))

        asyncio.run(exercise())

    def test_注册_SSE_快照_需求提交与静态首页路由(self) -> None:
        from app.api.main import create_app

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            dist = temp / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<!doctype html><title>Owli</title>", encoding="utf-8")
            app = create_app(temp / "owli.db", SCHEMA_PATH, frontend_dist=dist)

        routes = {(getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", set())))) for route in app.routes}
        self.assertIn(("/api/researches/{research_id}/events", ("GET",)), routes)
        self.assertIn(("/api/researches/{research_id}", ("GET",)), routes)
        self.assertIn(("/api/researches", ("POST",)), routes)
        self.assertIn(("/", ("GET",)), routes)


if __name__ == "__main__":
    unittest.main()
