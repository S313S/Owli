"""D-020：产物 `platform` 列混进发布方名，必须收进闭集且不丢发布方信息。

现场（全五轮产物实测）：`item.platform` 取到过 `36氪AI测评 / 搜狐号 /
人人都是产品经理 / 提效录` 各 1 条，都是 web_search 通道的条目——引擎把「这条
内容发在哪个号/哪个站」写进了平台列，而 `app/sources/web_search.py` 里适配器
自己写的是 `platform="web_search"`。

`platform` 是闭集（适配器实际写的七个值）：下游按它分平台统计、判来源权重、
`app/reliability/crossref.py` 按它查域名归属。原样落库破坏闭集；硬抹成
`web_search` 又把发布方信息丢掉（没有别的列接得住）。D-019 因此只做到「不认识
的原样返回」，把这个决定留给本包。

本包的判法：**降级 + 留痕**——列收进闭集，越界原值原样进
`extra.artifact_platform`（与本文件既有的 `artifact_source_type` 同一套写法）。
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"

# 第 4 轮真实产物 runs/r-6215aa582053/goals/goal-1/data-collection.json 的四条
_PUBLISHER_ITEMS = [
    ("36氪AI测评", "https://ai.36kr.com/note-detail/3568010593718002"),
    ("搜狐号", "https://www.sohu.com/a/945849888_122506762"),
    ("人人都是产品经理", "https://www.woshipm.com/share/6140649.html"),
    ("提效录", "https://www.tixiaolu.com/posts/tongyi-tingwu-tutorial-2026/"),
]


def _artifact(directory: Path, items: list[dict]) -> Path:
    path = directory / "data-collection.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


class ResolvePlatformTest(unittest.TestCase):
    def test_闭集七值与闭集内的值一个字不动(self) -> None:
        from app.store.evidence_artifacts import PLATFORM_VOCABULARY, resolve_platform

        self.assertEqual(PLATFORM_VOCABULARY, frozenset({
            "xhs", "douyin", "web_search", "reddit", "product_hunt",
            "hacker_news", "x",
        }))
        for platform in sorted(PLATFORM_VOCABULARY):
            self.assertEqual(
                resolve_platform(platform, permalink="https://example.com/a"),
                (platform, None), platform,
            )

    def test_平台别名仍按D019归一且不算越界(self) -> None:
        from app.store.evidence_artifacts import resolve_platform

        self.assertEqual(
            resolve_platform(
                "xiaohongshu",
                permalink="https://www.xiaohongshu.com/explore/note-1",
            ),
            ("xhs", None),
        )

    def test_发布方名降级为web_search并把原值交回来(self) -> None:
        from app.store.evidence_artifacts import resolve_platform

        for publisher, permalink in _PUBLISHER_ITEMS:
            self.assertEqual(
                resolve_platform(publisher, permalink=permalink),
                ("web_search", publisher), publisher,
            )

    def test_链接域名指得出平台时按域名降级而不是一律web_search(self) -> None:
        """引擎把小红书笔记的 platform 写成博主名时，别把它算成网页搜索——
        判据 1 数的就是「正文引国内源条数」，误标会直接把数改掉。"""
        from app.store.evidence_artifacts import resolve_platform

        self.assertEqual(
            resolve_platform(
                "某某测评博主",
                permalink="https://www.xiaohongshu.com/explore/69157107000000000700dc5f",
            ),
            ("xhs", "某某测评博主"),
        )
        self.assertEqual(
            resolve_platform("某发布方", permalink="https://news.ycombinator.com/item?id=1"),
            ("hacker_news", "某发布方"),
        )

    def test_域名指不出平台时才回落到agent自报的信息源(self) -> None:
        from app.store.evidence_artifacts import resolve_platform

        self.assertEqual(
            resolve_platform("某发布方", permalink="https://example.com/a", hint="douyin"),
            ("douyin", "某发布方"),
        )
        # 域名比 agent 自报更硬：两者冲突时按域名
        self.assertEqual(
            resolve_platform(
                "某发布方",
                permalink="https://www.xiaohongshu.com/explore/note-2",
                hint="web_search",
            ),
            ("xhs", "某发布方"),
        )

    def test_产物没写平台时沿用旧行为不算越界(self) -> None:
        from app.store.evidence_artifacts import resolve_platform

        self.assertEqual(
            resolve_platform(None, permalink="https://example.com/a", hint="xhs"),
            ("xhs", None),
        )
        # 没平台也没唯一信息源：仍然交给调用方丢弃（本包不改这条）
        self.assertEqual(
            resolve_platform(None, permalink="https://example.com/a"), ("", None),
        )


class LoadEvidencePayloadsTest(unittest.TestCase):
    def test_四条真实发布方名条目投影后平台只出闭集且原值可读回(self) -> None:
        from app.store.evidence_artifacts import (
            ARTIFACT_PLATFORM_KEY, PLATFORM_VOCABULARY, load_evidence_payloads,
        )

        with tempfile.TemporaryDirectory() as temp:
            path = _artifact(Path(temp), [
                {
                    "platform": publisher,
                    "permalink": permalink,
                    "fetched_at": "2026-08-28T11:00:03+08:00",
                    "title": f"{publisher} 的一篇测评",
                    "summary": "正文摘要",
                }
                for publisher, permalink in _PUBLISHER_ITEMS
            ])
            payloads = load_evidence_payloads(
                path, report_id="r-d020", goal_id="goal-1",
                agent_name="data-collection", platform_hint=None,
            )

        self.assertEqual(len(payloads), 4)
        self.assertEqual({p["platform"] for p in payloads}, {"web_search"})
        self.assertTrue(
            {p["platform"] for p in payloads} <= PLATFORM_VOCABULARY,
        )
        self.assertEqual(
            [p["extra"][ARTIFACT_PLATFORM_KEY] for p in payloads],
            [publisher for publisher, _ in _PUBLISHER_ITEMS],
        )

    def test_没越界时不写留痕键(self) -> None:
        from app.store.evidence_artifacts import (
            ARTIFACT_PLATFORM_KEY, load_evidence_payloads,
        )

        with tempfile.TemporaryDirectory() as temp:
            path = _artifact(Path(temp), [{
                "platform": "xiaohongshu",
                "permalink": "https://www.xiaohongshu.com/explore/note-3",
                "fetched_at": "2026-08-28T11:00:03+08:00",
                "title": "标题",
            }])
            payloads = load_evidence_payloads(
                path, report_id="r-d020", goal_id="goal-1",
                agent_name="data-collection", platform_hint="xhs",
            )

        self.assertEqual(payloads[0]["platform"], "xhs")
        self.assertNotIn(ARTIFACT_PLATFORM_KEY, payloads[0]["extra"])

    def test_降级时按旧平台算的归一化三件套撤下并留痕(self) -> None:
        """留着会撞 dao._validate_normalization 的一致性校验，让整批回滚、
        把同批的合格证据一起丢掉（§D-015 的教训）。"""
        from app.store.evidence_artifacts import (
            ARTIFACT_NORM_CONTEXT_KEY, load_evidence_payloads,
        )

        norm_context = {
            "scope": "batch", "platform": "搜狐号", "metric": None, "n": 20,
            "formula": "f", "stats": {}, "computed_at": "2026-08-28T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as temp:
            path = _artifact(Path(temp), [{
                "platform": "搜狐号",
                "permalink": "https://www.sohu.com/a/945849888_122506762",
                "fetched_at": "2026-08-28T11:00:03+08:00",
                "title": "标题",
                "norm_method": "percentile_in_batch",
                "normalized_score": 0.5,
                "norm_context": norm_context,
            }])
            payloads = load_evidence_payloads(
                path, report_id="r-d020", goal_id="goal-1",
                agent_name="data-collection", platform_hint=None,
            )

        payload = payloads[0]
        self.assertEqual(payload["platform"], "web_search")
        self.assertIsNone(payload["norm_method"])
        self.assertIsNone(payload["normalized_score"])
        self.assertIsNone(payload["norm_context"])
        self.assertEqual(payload["extra"][ARTIFACT_NORM_CONTEXT_KEY], norm_context)

    def test_已消费平台索引也收进闭集(self) -> None:
        """`consumed_platform_index` 喂的是 validation 的「已消费平台都要在
        信息源清单里露面」——发布方名在那里也不该被当成一个独立平台。"""
        from app.store.evidence_artifacts import consumed_platform_index

        consumed_fields = {
            "score_authority": 1, "score_freshness": 1, "score_crossref": 1,
            "score_completeness": 1, "score_independence": 1,
            "rating_notes": "评级说明",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "r-d020"
            (root / "goals" / "goal-1").mkdir(parents=True)
            _artifact(root / "goals" / "goal-1", [
                {"platform": publisher, "permalink": permalink, **consumed_fields}
                for publisher, permalink in _PUBLISHER_ITEMS
            ])
            index = consumed_platform_index(root)

        self.assertEqual(set(index.values()), {"web_search"})


class DownstreamCostTest(unittest.TestCase):
    """闭集为什么非守不可：platform 被发布方名污染，crossref 的独立性判定会
    **静默地**从「两家不同机构」翻成「同一家」，白丢一票交叉验证。
    只读刻画 `app/reliability/crossref.py` 的现有行为，不改它。"""

    def test_平台被污染会翻掉机构独立性判定(self) -> None:
        from app.reliability.crossref import independence_checks

        left = {
            "permalink": "https://www.xiaohongshu.com/explore/n1", "platform": "xhs",
            "author_name": "甲", "published_at": "2026-01-01", "title": "A",
        }
        right = {
            "permalink": "https://www.xiaohongshu.com/explore/n2", "platform": "xhs",
            "author_name": "乙", "published_at": "2026-01-02", "title": "B",
        }
        self.assertIs(
            independence_checks(left, right, "claim-1")["institution_subject"], True,
        )

        polluted = {**right, "platform": "某发布方名"}
        self.assertIs(
            independence_checks(left, polluted, "claim-1")["institution_subject"],
            False,
            "平台词一脏，同平台两个不同账号就不再算两家机构",
        )


class UpsertClosedSetTest(unittest.TestCase):
    """入库这一侧：D-019(b) 让 permalink 认得出同一行之后，产物里的发布方名会
    顺着 UPDATE 覆盖掉采集期写对的 `platform`。这条是本包真正堵的洞。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "owli.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _store(self):
        from app.store.dao import Store

        store = Store(self.database_path)
        store.create_report(
            id="r-d020", title="D-020 平台闭集",
            research_question="发布方名会不会顶掉平台",
            created_at="2026-08-28T00:00:00+00:00",
        )
        return store

    def test_产物发布方名不覆盖采集期写对的平台且原值可从库里读回(self) -> None:
        from app.store.evidence_artifacts import (
            ARTIFACT_PLATFORM_KEY, PLATFORM_VOCABULARY, load_evidence_payloads,
        )

        store = self._store()
        publisher, permalink = _PUBLISHER_ITEMS[0]
        store.add_evidence(
            id="ev-adapter", report_id="r-d020", goal_id="goal-1",
            platform="web_search", permalink=permalink,
            fetched_at="2026-08-28T10:00:00+08:00", title="采集期入库",
        )

        with tempfile.TemporaryDirectory() as temp:
            path = _artifact(Path(temp), [{
                "platform": publisher, "permalink": permalink,
                "fetched_at": "2026-08-28T11:00:03+08:00",
                "title": "goal 收尾复写", "summary": "正文摘要",
            }])
            store.upsert_evidence_batch(load_evidence_payloads(
                path, report_id="r-d020", goal_id="goal-1",
                agent_name="data-collection",
            ))

        rows = store.list_evidence("r-d020")
        self.assertEqual(len(rows), 1, "同一 permalink 不许插出第二行")
        self.assertEqual(rows[0]["platform"], "web_search")
        self.assertTrue({row["platform"] for row in rows} <= PLATFORM_VOCABULARY)
        self.assertEqual(rows[0]["extra"][ARTIFACT_PLATFORM_KEY], publisher)


class DowngradeEventTest(unittest.TestCase):
    """降级不许静默：投影时发 `evidence_platform_downgraded`。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "owli.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, items: list[dict]) -> tuple[list[dict], list[dict]]:
        from app.orchestrator.runtime import RuntimeCoordinator
        from app.store.dao import Store

        store = Store(self.database_path)
        store.create_report(
            id="r-d020", title="D-020 事件",
            research_question="降级发不发事件",
            created_at="2026-08-28T00:00:00+00:00",
        )
        runs_root = self.root / "runs"
        artifact_dir = runs_root / "r-d020" / "goals" / "goal-1"
        artifact_dir.mkdir(parents=True)
        _artifact(artifact_dir, items)
        store.ensure_chapters(
            "r-d020", [{"goal_id": "goal-1", "chapter_id": "data-collection"}],
            updated_at="2026-08-28T00:00:00Z",
        )
        store.finish_chapter(
            "r-d020", "goal-1", "data-collection", status="done", reason=None,
            actual_output_path="goals/goal-1/data-collection.json",
            actual_count=len(items), updated_at="2026-08-28T00:00:01Z",
        )
        published: list[dict] = []

        class _Buffer:
            async def publish(self, research_id, payload):
                published.append(dict(payload))

        runtime = RuntimeCoordinator(
            store=store, event_buffer=_Buffer(), researches={}, cards={},
            adapter_factory=lambda: object(), runs_root=runs_root,
            routing_utc_clock=lambda: datetime.now(timezone.utc),
        )
        goal = SimpleNamespace(
            goal_id="goal-1",
            agents=[SimpleNamespace(
                agent_id="data-collection",
                chapter={"chapter_id": "data-collection"},
                output={"format": "json", "path": "goals/goal-1/data-collection.json"},
                capability={"sources": ["web_search"]},
            )],
        )
        asyncio.run(runtime._persist_goal_evidence(
            SimpleNamespace(research_id="r-d020"), goal,
        ))
        return published, store.list_evidence("r-d020")

    def test_越界时发事件并带上原值与闭集词表(self) -> None:
        published, rows = self._run([
            {
                "platform": publisher, "permalink": permalink,
                "fetched_at": "2026-08-28T11:00:03+08:00",
                "title": f"{publisher} 测评", "summary": "摘要",
            }
            for publisher, permalink in _PUBLISHER_ITEMS
        ])

        events = [e for e in published if e["type"] == "evidence_platform_downgraded"]
        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertEqual(data["count"], 4)
        self.assertEqual(data["goal_id"], "goal-1")
        self.assertEqual(
            [item["artifact_platform"] for item in data["items"]],
            [publisher for publisher, _ in _PUBLISHER_ITEMS],
        )
        self.assertEqual({item["platform"] for item in data["items"]}, {"web_search"})
        self.assertEqual(len(data["vocabulary"]), 7)
        self.assertEqual({row["platform"] for row in rows}, {"web_search"})

    def test_没越界就不发事件不制造噪音(self) -> None:
        published, rows = self._run([{
            "platform": "web_search",
            "permalink": "https://sspai.com/post/70960",
            "fetched_at": "2026-08-28T11:00:03+08:00",
            "title": "少数派一篇", "summary": "摘要",
        }])

        self.assertEqual(
            [e for e in published if e["type"] == "evidence_platform_downgraded"], [],
        )
        self.assertEqual({row["platform"] for row in rows}, {"web_search"})


if __name__ == "__main__":
    unittest.main()
