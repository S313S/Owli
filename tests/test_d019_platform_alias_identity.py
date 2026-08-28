"""D-019：平台名两套词不得让 upsert_evidence_batch 撞 permalink 唯一键。

现场（§W-1 第 5 轮 `r-70ce258e7f67`）：采集期适配器按 `xhs` 入库，goal 收尾时从
引擎产物读到 `xiaohongshu`，`_evidence_identity` 只按 (report_id, platform,
platform_item_id) 查、查不到就 INSERT，于是撞 `UNIQUE(report_id, permalink)`，
`_persist_goal_evidence` 抛 IntegrityError → goal-2 failed → 撰写 goal 整个 skipped。

两处修都要钉住：
- (a) `evidence_artifacts.normalize_platform`：产物里的自由文本归到适配器词表；
- (b) `dao._evidence_identity`：native identity 认不出时回落 permalink 再查一次。
(b) 是保底——(a) 的词表漏掉任何新词时，仍然不许插出唯一键冲突。
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


class PlatformAliasTest(unittest.TestCase):
    def test_平台别名归一到适配器词表(self) -> None:
        from app.store.evidence_artifacts import normalize_platform

        for alias in ("xiaohongshu", "XiaoHongShu", "redbook", "rednote",
                      "little_red_book", "小红书", "xhs"):
            self.assertEqual(normalize_platform(alias), "xhs", alias)
        self.assertEqual(normalize_platform("抖音"), "douyin")
        self.assertEqual(normalize_platform("douyin.com"), "douyin")
        self.assertEqual(normalize_platform("twitter"), "x")
        self.assertEqual(normalize_platform("hackernews"), "hacker_news")
        self.assertEqual(normalize_platform("websearch"), "web_search")

    def test_不认识的平台文本原样保留不猜(self) -> None:
        from app.store.evidence_artifacts import normalize_platform

        # 产物里出现过的发布方名（web_search 条目），不是平台别名，硬映射会抹掉来源
        for publisher in ("36氪AI测评", "搜狐号", "人人都是产品经理", "提效录"):
            self.assertEqual(normalize_platform(publisher), publisher)
        self.assertEqual(normalize_platform(""), "")

    def test_产物里的平台自由文本在落库前已归一(self) -> None:
        from app.store.evidence_artifacts import load_evidence_payloads

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data-collection-6.json"
            path.write_text(json.dumps([{
                "platform": "xiaohongshu",
                "platform_item_id": "69157107000000000700dc5f",
                "permalink": "https://www.xiaohongshu.com/explore/69157107000000000700dc5f",
                "title": "飞书妙记用了三个月",
                "content_excerpt": "转写准确率还行，导出有点绕。",
                "author_name": "某用户",
                "fetched_at": "2026-08-28T04:50:00+00:00",
            }]), encoding="utf-8")
            payloads = load_evidence_payloads(
                path, report_id="r-d019", goal_id="goal-2",
                agent_name="data-collection-6", platform_hint="xhs",
            )

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["platform"], "xhs")


class EvidenceIdentityFallbackTest(unittest.TestCase):
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
            id="r-d019",
            title="D-019 平台词不一致",
            research_question="平台词对不上会不会撞唯一键",
            created_at="2026-08-28T00:00:00+00:00",
        )
        return store

    def _evidence(self, **changes):
        item = {
            "id": "ev-1",
            "report_id": "r-d019",
            "goal_id": "goal-2",
            "platform": "xhs",
            "platform_item_id": "69157107000000000700dc5f",
            "permalink": "https://www.xiaohongshu.com/explore/69157107000000000700dc5f",
            "title": "采集期入库",
            "fetched_at": "2026-08-28T04:40:00+00:00",
            "raw_metrics": {"liked_count": 12},
        }
        # 不带 normalization 字段：现场那 16 条从产物读出来的 payload 就是这个形状
        # （`load_evidence_payloads` 不产 normalized_score/norm_method），
        # 带上反而会先死在 `_validate_normalization` 的平台词表上，测不到唯一键这一层。
        item.update(changes)
        return item

    def _rows(self):
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute(
                "SELECT id, platform, title FROM evidence WHERE report_id = ?",
                ("r-d019",),
            ).fetchall()

    def test_平台词对不上时按permalink认出同一行而不是插入(self) -> None:
        """(b) 单独成立：用一个**不在别名表里**的词，只能靠 permalink 回落认出。"""
        store = self._store()
        store.upsert_evidence_batch([self._evidence()])

        store.upsert_evidence_batch([self._evidence(
            id="ev-2",
            platform="某个词表里没有的新平台名",
            title="goal 收尾复写",
        )])

        rows = self._rows()
        self.assertEqual(len(rows), 1, "同一 permalink 不许插出第二行")
        self.assertEqual(rows[0][0], "ev-1", "应命中原行更新，不是换 id 插新行")
        self.assertEqual(rows[0][2], "goal 收尾复写")

    def test_同一批里平台词不一致也不抛IntegrityError(self) -> None:
        store = self._store()
        store.upsert_evidence_batch([self._evidence()])

        try:
            store.upsert_evidence_batch([
                self._evidence(id="ev-2", platform="xiaohongshu", title="第二次"),
                self._evidence(id="ev-3", platform="redbook", title="第三次"),
            ])
        except sqlite3.IntegrityError as exc:  # pragma: no cover - 回归时才会走到
            self.fail(f"平台词不一致不应撞唯一键：{exc}")

        self.assertEqual(len(self._rows()), 1)

    def test_无原生id时仍按permalink幂等(self) -> None:
        """回落改动不得破坏原来的 permalink 路径。"""
        store = self._store()
        store.upsert_evidence_batch([self._evidence(platform_item_id=None)])
        store.upsert_evidence_batch([
            self._evidence(id="ev-2", platform_item_id=None, title="第二次"),
        ])

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "第二次")


if __name__ == "__main__":
    unittest.main()
