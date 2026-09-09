"""§PROT-1：采集章产物回投影，不许把评论抹成帖子、不许改写 goal_id。

现场（`r-3e04f808dffd`）：`app/adapters/source_mcp.py:413-450` 采集期写的就是
`kind='comment'` + `parent_permalink`，库里 188 行 `source_type='comment'` 至今
还在；但 `kind=comment` 只剩 reddit 91 条，xhs 128 / douyin 60 全成了 `post`，
附录因此说这两家「0 条评论」——`app/report/polish/tables.py:127` 数的是 kind。

机理：`evidence_artifacts._FROZEN_FIELDS` 里没有 `kind`/`parent_permalink`，
`load_evidence_payloads` 把它们当自定义键塞进 `extra`，payload 里根本没有这两列；
`goal_id` 则被显式写成**当前正在收尾的那个 goal**。`dao._update_evidence`
(`dao.py:1155`) 是全列 UPDATE，三列一起按 `dao.py:51,76-77` 的默认值落成
`post / NULL / 本 goal`。

⚠️ 适用范围：`goal_id` 这一支锁的是**机制**（证据行按 goal 分块编号，归属被回显
改写就等于被搬进别的号段），**不是**立包时那条「S1–S21 孤号由此而来」的归因——
后者在当前库上已证伪（goal-1 名下 0 条是 09-08 用户拍的数据归位；角标空洞遍布
全段，是 `set_citations` 只留成稿引到的号）。用例照留，防的是将来再被覆盖。

判法与 D-032 / D-049 同一手法（这是第三次）：三列进 `runtime.protected_fields`，
**库里已有值就不许回贴改写**，产物首次写入的行不受影响。
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

_REPORT_ID = "r-prot1"
# 真实链型：xhs 评论没有自己的链接，锚点挂在父帖链接的 query 上
# （`source_mcp._comment_permalink`，走 query 是因为 fragment 会被归一化抹掉）
_PARENT_PERMALINK = (
    "https://www.xiaohongshu.com/explore/68b1c0f0000000001c03a1d2"
)
_COMMENT_PERMALINK = f"{_PARENT_PERMALINK}?owli_comment=68b2f1a900000000"
# 抖音那 107 行的形态：采集期落在 goal-1，被 goal-3 的章产物当跨 goal 对照回显
_DOUYIN_PERMALINK = "https://www.douyin.com/video/7541234567890123456"


class ProjectionKeepsCommentIdentityTest(unittest.TestCase):
    """采集期落库 → 采集章产物回投影 → kind / parent_permalink / goal_id 不变。"""

    def setUp(self) -> None:
        from app.store.dao import Store

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "owli.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.store = Store(self.database_path)
        self.store.create_report(
            id=_REPORT_ID, title="PROT-1 保护名单",
            research_question="投影会不会把评论抹成帖子",
            created_at="2026-09-09T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _comment_row(self, *, goal_id: str = "goal-2") -> dict:
        """`source_mcp._comment_row` 直落库的评论行形态。"""

        return {
            "id": f"ev-{_REPORT_ID}-xhs-comment-1",
            "report_id": _REPORT_ID, "goal_id": goal_id,
            "agent_name": "data-collection-3", "platform": "xhs",
            "platform_item_id": "68b2f1a900000000",
            "permalink": _COMMENT_PERMALINK,
            "fetched_at": "2026-09-04T13:00:00Z",
            "fetch_method": "third_party_api",
            "source_type": "comment", "kind": "comment",
            "parent_permalink": _PARENT_PERMALINK,
            "title": "评论 · 用了两个月的真实体验",
            "content_excerpt": "白请一个实习生，交接成本比自己干还高",
            "author_name": "小红薯用户", "published_at": "2026-09-01T08:00:00Z",
            "raw_metrics": {"likes": 0},
            "extra": {"content_kind": "user_opinion"},
        }

    def _post_row(self, *, goal_id: str, permalink: str) -> dict:
        """抖音帖子行：采集期落在 goal-1。"""

        return {
            "id": f"ev-{_REPORT_ID}-douyin-post-1",
            "report_id": _REPORT_ID, "goal_id": goal_id,
            "agent_name": "data-collection-2", "platform": "douyin",
            "platform_item_id": "7541234567890123456",
            "permalink": permalink, "fetched_at": "2026-09-04T13:00:00Z",
            "fetch_method": "third_party_api", "source_type": "post",
            "kind": "post", "title": "抖音测评标题",
            "content_excerpt": "抖音测评正文", "author_name": "抖音作者",
            "published_at": "2026-09-01T08:00:00Z",
        }

    def _artifact_item(self, permalink: str, *, title: str) -> dict:
        """引擎回显形态：既没有 kind 也没有 parent_permalink。

        这不是造出来的坏产物——`_FROZEN_FIELDS` 不收这两个键，就算引擎照抄
        库里那一行写进 JSON，`load_evidence_payloads` 也只会把它们塞进 extra。
        """

        return {
            "permalink": permalink,
            "fetched_at": "2026-09-04T15:00:00Z",
            "title": title,
            "author": "引擎回显作者",
            "text": "引擎回显正文",
            "kind": "post",
            "parent_permalink": None,
        }

    def _project(
        self, items: list[dict], *, goal_id: str, sources: list[str],
        chapter_id: str,
    ) -> list[dict]:
        from app.orchestrator.runtime import RuntimeCoordinator

        runs_root = self.root / "runs"
        relative = f"goals/{goal_id}/{chapter_id}.json"
        artifact_path = runs_root / _REPORT_ID / relative
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8",
        )
        self.store.ensure_chapters(
            _REPORT_ID, [{"goal_id": goal_id, "chapter_id": chapter_id}],
            updated_at="2026-09-04T14:00:00Z",
        )
        self.store.finish_chapter(
            _REPORT_ID, goal_id, chapter_id, status="done", reason=None,
            actual_output_path=relative, actual_count=len(items),
            updated_at="2026-09-04T14:00:01Z",
        )

        class _Buffer:
            async def publish(self, research_id, payload):
                return None

        runtime = RuntimeCoordinator(
            store=self.store, event_buffer=_Buffer(), researches={}, cards={},
            adapter_factory=lambda: object(), runs_root=runs_root,
            routing_utc_clock=lambda: datetime.now(timezone.utc),
        )
        goal = SimpleNamespace(
            goal_id=goal_id,
            agents=[SimpleNamespace(
                agent_id=chapter_id,
                chapter={"chapter_id": chapter_id},
                output={"format": "json", "path": relative},
                capability={"sources": sources},
            )],
        )
        asyncio.run(runtime._persist_goal_evidence(
            SimpleNamespace(research_id=_REPORT_ID), goal,
        ))
        return self.store.list_evidence(_REPORT_ID)

    def test_采集章产物回投影不把评论抹成帖子(self) -> None:
        """造红支 ①：库里 kind='comment'，产物回显后必须还是 comment。"""

        self.store.upsert_evidence_batch([self._comment_row()])

        rows = self._project(
            [self._artifact_item(_COMMENT_PERMALINK, title="评论 · 回显标题")],
            goal_id="goal-2", sources=["xhs"], chapter_id="data-collection-3",
        )

        self.assertEqual(len(rows), 1, "同一 permalink 不许插出第二行")
        self.assertEqual(
            rows[0]["kind"], "comment",
            "kind 被抹成 post：附录的『其中评论』就是这么变成 0 的",
        )
        # source_type 早已受保护，两列必须继续对得上——这是修完复量的判据之一
        self.assertEqual(rows[0]["source_type"], "comment")

    def test_采集章产物回投影不抹掉parent_permalink(self) -> None:
        """造红支 ②：父帖链接是评论行唯一的归属，抹成 NULL 就再也接不回去。"""

        self.store.upsert_evidence_batch([self._comment_row()])

        rows = self._project(
            [self._artifact_item(_COMMENT_PERMALINK, title="评论 · 回显标题")],
            goal_id="goal-2", sources=["xhs"], chapter_id="data-collection-3",
        )

        self.assertEqual(rows[0]["parent_permalink"], _PARENT_PERMALINK)

    def test_跨goal回显不改写已入库行的goal_id(self) -> None:
        """造红支 ③：抖音 107 行属 goal-1，被 goal-3 的章回显后不许变成 goal-3。

        改了就等于把它们从 goal-1 的编号区间搬到 goal-3 的区间重编。
        （锁机制，不锁「S1–S21 孤号由此而来」那条归因——见模块 docstring。）
        """

        self.store.upsert_evidence_batch([
            self._post_row(goal_id="goal-1", permalink=_DOUYIN_PERMALINK),
        ])

        rows = self._project(
            [self._artifact_item(_DOUYIN_PERMALINK, title="抖音回显标题")],
            goal_id="goal-3", sources=["douyin"], chapter_id="data-collection-6",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["goal_id"], "goal-1",
            "goal_id 被跨 goal 回显改写：证据行会被搬进别的号段重编",
        )

    def test_产物首次写入的行不受保护名单影响(self) -> None:
        """反向闸：库里没有的 permalink 照旧按产物落，别把保护写成「不许写」。"""

        rows = self._project(
            [self._artifact_item(_DOUYIN_PERMALINK, title="抖音新行")],
            goal_id="goal-3", sources=["douyin"], chapter_id="data-collection-6",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["goal_id"], "goal-3")
        self.assertEqual(rows[0]["kind"], "post")


if __name__ == "__main__":
    unittest.main()
