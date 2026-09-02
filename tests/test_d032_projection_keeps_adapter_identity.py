"""D-032：采集章 JSON 产物回投影，不许顶掉薄源已入库的身份/来源列。

现场（`r-f59fdba77cd7` goal-2/ch-1，源 weibo）：池读薄源采集期已把 25 行落库
（`platform=weibo / platform_item_id=533… / fetch_method=media_crawler /
extra.provider=media_crawler`），goal 收尾 `_persist_goal_evidence` 拿引擎自己
写的 `data-collection-3.json`（只有 permalink/fetched_at/author/text）重新投影，
`dao._update_evidence` 全列 UPDATE，把三列一并顶成
`web_search / NULL / official_api`，`extra` 里池读痕迹全丢。

判法：已入库行**只允许产物补空，不允许顶掉非空**——身份/来源列进保护名单，
`extra` 做合并不做替换。产物首次写入的行（`existing` 里没有）不受影响，
D-020 的发布方名降级留痕照旧。
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

_WEIBO_ITEM_ID = "5338017690552465"
_WEIBO_PERMALINK = f"https://m.weibo.cn/detail/{_WEIBO_ITEM_ID}"
_MP_ITEM_ID = "2247486000"
_MP_PERMALINK = "https://mp.weixin.qq.com/s/AbCdEfGhIjKlMnOpQrSt"
# D-020 第 4 轮真实产物里的发布方名四条（库里没有先行行，必须照旧降级留痕）
_PUBLISHER_ITEMS = [
    ("36氪AI测评", "https://ai.36kr.com/note-detail/3568010593718002"),
    ("搜狐号", "https://www.sohu.com/a/945849888_122506762"),
    ("人人都是产品经理", "https://www.woshipm.com/share/6140649.html"),
    ("提效录", "https://www.tixiaolu.com/posts/tongyi-tingwu-tutorial-2026/"),
]


class ProjectionIdentityTest(unittest.TestCase):
    """池读先落库 → 采集章产物回投影 → 身份列逐字不变。"""

    def setUp(self) -> None:
        from app.store.dao import Store

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "owli.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.store = Store(self.database_path)
        self.store.create_report(
            id="r-d032", title="D-032 投影",
            research_question="投影会不会改写身份列",
            created_at="2026-09-02T00:00:00+00:00",
        )
        self.published: list[dict] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _pool_row(self, *, platform: str, item_id: str, permalink: str) -> dict:
        """薄源 `app/sources/_pool_source.py:116-126` 直落库的行形态。"""

        return {
            "id": f"ev-r-d032-{platform}-{item_id}",
            "report_id": "r-d032", "goal_id": "goal-2",
            "agent_name": "precollect", "platform": platform,
            "platform_item_id": item_id, "permalink": permalink,
            "fetched_at": "2026-09-02T13:00:00Z",
            "fetch_method": "media_crawler", "source_type": "post",
            "title": "池读标题", "content_excerpt": "池读正文",
            "author_name": "池读作者", "published_at": "2026-09-01T08:00:00Z",
            "extra": {"provider": "media_crawler", "precollect_batch": "b-1"},
        }

    def _artifact_item(self, permalink: str) -> dict:
        """引擎回显形态：只有 permalink/fetched_at/author/text，无平台无 item_id。"""

        return {
            "permalink": permalink,
            "fetched_at": "2026-09-02T15:00:00Z",
            "author": "引擎回显作者",
            "text": "引擎回显正文",
            "engagement": {"like_count": 12},
            "entity": "workbuddy",
        }

    def _project(
        self, items: list[dict], *, sources: list[str],
        chapter_id: str = "data-collection-3",
    ) -> list[dict]:
        from app.orchestrator.runtime import RuntimeCoordinator

        runs_root = self.root / "runs"
        artifact_dir = runs_root / "r-d032" / "goals" / "goal-2"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        relative = f"goals/goal-2/{chapter_id}.json"
        (runs_root / "r-d032" / relative).write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8",
        )
        self.store.ensure_chapters(
            "r-d032", [{"goal_id": "goal-2", "chapter_id": chapter_id}],
            updated_at="2026-09-02T14:00:00Z",
        )
        self.store.finish_chapter(
            "r-d032", "goal-2", chapter_id, status="done", reason=None,
            actual_output_path=relative, actual_count=len(items),
            updated_at="2026-09-02T14:00:01Z",
        )
        published = self.published

        class _Buffer:
            async def publish(self, research_id, payload):
                published.append(dict(payload))

        runtime = RuntimeCoordinator(
            store=self.store, event_buffer=_Buffer(), researches={}, cards={},
            adapter_factory=lambda: object(), runs_root=runs_root,
            routing_utc_clock=lambda: datetime.now(timezone.utc),
        )
        goal = SimpleNamespace(
            goal_id="goal-2",
            agents=[SimpleNamespace(
                agent_id=chapter_id,
                chapter={"chapter_id": chapter_id},
                output={"format": "json", "path": relative},
                capability={"sources": sources},
            )],
        )
        asyncio.run(runtime._persist_goal_evidence(
            SimpleNamespace(research_id="r-d032"), goal,
        ))
        return self.store.list_evidence("r-d032")

    def _rating_writeback(self) -> None:
        """复刻 `runtime._rating_payloads`：拿库里那一行做底，只盖评分列。"""

        payloads = []
        for row in self.store.list_evidence("r-d032"):
            payload = dict(row)
            payload.update({
                "score_authority": 1, "score_freshness": 2,
                "score_crossref": 1, "score_completeness": 1,
                "score_independence": 1, "rated_by": "agent:rating-1",
                "rating_notes": (
                    "权威1:平台原帖 · 时效2:时间窗内 · 交叉1:弱交叉 · "
                    "完整1:字段齐全 · 无关1:无利益关系"
                ),
            })
            payloads.append(payload)
        self.store.upsert_evidence_batch(payloads)

    def test_池读微博行经采集章产物投影后platform仍为weibo(self) -> None:
        self.store.upsert_evidence_batch([self._pool_row(
            platform="weibo", item_id=_WEIBO_ITEM_ID, permalink=_WEIBO_PERMALINK,
        )])

        rows = self._project(
            [self._artifact_item(_WEIBO_PERMALINK)], sources=["weibo"],
        )

        self.assertEqual(len(rows), 1, "同一 permalink 不许插出第二行")
        self.assertEqual(rows[0]["platform"], "weibo")

    def test_投影不抹掉platform_item_id与fetch_method(self) -> None:
        self.store.upsert_evidence_batch([self._pool_row(
            platform="weibo", item_id=_WEIBO_ITEM_ID, permalink=_WEIBO_PERMALINK,
        )])

        rows = self._project(
            [self._artifact_item(_WEIBO_PERMALINK)], sources=["weibo"],
        )

        self.assertEqual(rows[0]["platform_item_id"], _WEIBO_ITEM_ID)
        self.assertEqual(rows[0]["fetch_method"], "media_crawler")
        self.assertEqual(rows[0]["published_at"], "2026-09-01T08:00:00Z")
        self.assertEqual(rows[0]["extra"]["provider"], "media_crawler")
        self.assertEqual(rows[0]["extra"]["precollect_batch"], "b-1")
        # 合并不是替换：产物带来的新键照收
        self.assertEqual(rows[0]["extra"]["entity"], "workbuddy")

    def test_评级回写后身份列不变(self) -> None:
        """goal 收尾 → 评级回写 → 终态补扫再投影一次，身份列逐字不变。"""

        self.store.upsert_evidence_batch([self._pool_row(
            platform="weibo", item_id=_WEIBO_ITEM_ID, permalink=_WEIBO_PERMALINK,
        )])
        self._project([self._artifact_item(_WEIBO_PERMALINK)], sources=["weibo"])

        self._rating_writeback()
        rows = self._project(
            [self._artifact_item(_WEIBO_PERMALINK)], sources=["weibo"],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "weibo")
        self.assertEqual(rows[0]["platform_item_id"], _WEIBO_ITEM_ID)
        self.assertEqual(rows[0]["fetch_method"], "media_crawler")
        # 评分列不在本卡的保护名单里：终态补扫时由评级章产物重贴（
        # `_persist_rating_chapter` 在采集 payload 之后跑），本用例只锁身份列。
        self.assertEqual(rows[0]["extra"]["provider"], "media_crawler")

    def test_公众号池读行同形不被改写(self) -> None:
        self.store.upsert_evidence_batch([self._pool_row(
            platform="wechat_mp", item_id=_MP_ITEM_ID, permalink=_MP_PERMALINK,
        )])

        rows = self._project(
            [self._artifact_item(_MP_PERMALINK)], sources=["wechat_mp"],
            chapter_id="data-collection-4",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "wechat_mp")
        self.assertEqual(rows[0]["platform_item_id"], _MP_ITEM_ID)
        self.assertEqual(rows[0]["fetch_method"], "media_crawler")
        self.assertEqual(rows[0]["extra"]["provider"], "media_crawler")

    def test_投影不抹掉归一化三件套(self) -> None:
        """薄源按真平台算过的 norm 三件套，产物里没有，也不许被抹成 NULL。"""

        row = self._pool_row(
            platform="weibo", item_id=_WEIBO_ITEM_ID, permalink=_WEIBO_PERMALINK,
        )
        row.update({
            "norm_method": "percentile_in_batch",
            "normalized_score": 0.62,
            "norm_context": {
                "scope": "batch", "platform": "weibo", "metric": "liked_count",
                "n": 25, "formula": "percentile", "stats": {"p50": 3},
                "computed_at": "2026-09-02T13:00:00Z",
            },
        })
        self.store.upsert_evidence_batch([row])

        rows = self._project(
            [self._artifact_item(_WEIBO_PERMALINK)], sources=["weibo"],
        )

        self.assertEqual(rows[0]["normalized_score"], 0.62)
        self.assertEqual(rows[0]["norm_method"], "percentile_in_batch")
        self.assertEqual(rows[0]["norm_context"]["metric"], "liked_count")

    def test_已入库行不再重复报降级(self) -> None:
        """行没被改就不该报降级；库里旧留痕合并回来也不许重复计数。"""

        from app.store.evidence_artifacts import ARTIFACT_PLATFORM_KEY

        row = self._pool_row(
            platform="weibo", item_id=_WEIBO_ITEM_ID, permalink=_WEIBO_PERMALINK,
        )
        row["extra"][ARTIFACT_PLATFORM_KEY] = "weibo"  # 上一轮留下的旧留痕
        self.store.upsert_evidence_batch([row])

        rows = self._project(
            [self._artifact_item(_WEIBO_PERMALINK)], sources=["weibo"],
        )

        events = [
            item for item in self.published
            if item["type"] == "evidence_platform_downgraded"
        ]
        self.assertEqual(events, [], "行没被改就不许报降级")
        self.assertEqual(rows[0]["platform"], "weibo")
        self.assertEqual(rows[0]["extra"][ARTIFACT_PLATFORM_KEY], "weibo")

    def test_guard_产物首次写入的发布方名仍降级并留痕(self) -> None:
        """D-020 那四条库里没有先行行：照旧降级为 web_search，原值留 extra。"""

        from app.store.evidence_artifacts import (
            ARTIFACT_PLATFORM_KEY, PLATFORM_VOCABULARY,
        )

        rows = self._project([
            {
                "platform": publisher, "permalink": permalink,
                "fetched_at": "2026-09-02T15:00:03+08:00",
                "title": f"{publisher} 测评", "summary": "摘要",
            }
            for publisher, permalink in _PUBLISHER_ITEMS
        ], sources=["web_search"], chapter_id="data-collection-5")

        self.assertEqual(len(rows), 4)
        self.assertEqual({row["platform"] for row in rows}, {"web_search"})
        self.assertTrue({row["platform"] for row in rows} <= PLATFORM_VOCABULARY)
        self.assertEqual(
            {row["extra"][ARTIFACT_PLATFORM_KEY] for row in rows},
            {publisher for publisher, _ in _PUBLISHER_ITEMS},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
