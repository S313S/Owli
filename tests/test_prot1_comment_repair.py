"""§PROT-1 货 2：评论行数据修复的两把工具本身也要验。

修复脚本自己就是一把尺子，尺子量出来的「越界 0 行」如果是因为它压根不会报
越界，那这句话一文不值（[[verification-ruler-needs-verifying]]）。所以这里
既验「剥参数还原父帖链接」是不是真还原，也验「全表比对」抓不抓得住越界。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.acceptance.prot1.repair_comment_kind import parent_of, verify

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
_REPORT_ID = "r-prot1-repair"


class ParentOfTest(unittest.TestCase):
    """剥 owli_comment 参数 = 采集期写进 parent_permalink 的那个值。"""

    def test_xhs带签名参数的链接只剥掉评论锚点(self) -> None:
        parent = (
            "https://www.xiaohongshu.com/explore/6a79c7b100000000320226a2"
            "?xsec_token=YBe3PQ8rAcoUn5zQ7yRqqtY08uUd6ZqhFfZJKDnaqI5U4%3D"
            "&xsec_source=pc_feed"
        )
        self.assertEqual(parent_of(f"{parent}&owli_comment=6a89189d0000"), parent)

    def test_douyin链接锚点是唯一参数时剥完不留问号(self) -> None:
        parent = "https://www.douyin.com/video/7664016551611206950"
        self.assertEqual(parent_of(f"{parent}?owli_comment=7664709460837614374"), parent)

    def test_没有锚点的链接原样返回(self) -> None:
        parent = "https://www.douyin.com/video/7664016551611206950"
        self.assertEqual(parent_of(parent), parent)


class VerifyCatchesOutOfBoundsTest(unittest.TestCase):
    """比对函数必须真抓得住越界，否则「越界 0 行」只是它不会报而已。"""

    def setUp(self) -> None:
        from app.store.dao import Store

        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.before = root / "before.db"
        self.after = root / "after.db"
        for path in (self.before, self.after):
            with sqlite3.connect(path) as connection:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            store = Store(path)
            store.create_report(
                id=_REPORT_ID, title="修复比对", research_question="比对抓不抓得住",
                created_at="2026-09-09T00:00:00+00:00",
            )
            store.upsert_evidence_batch([{
                "id": "ev-1", "report_id": _REPORT_ID, "goal_id": "goal-2",
                "agent_name": "data-collection-3", "platform": "xhs",
                "permalink": "https://www.xiaohongshu.com/explore/6a79c7b1?owli_comment=1",
                "fetched_at": "2026-09-04T13:00:00Z", "source_type": "comment",
                "title": "评论 · 标题", "content_excerpt": "正文", "kind": "post",
            }])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _patch(self, sql: str, *params) -> dict:
        with sqlite3.connect(self.after) as connection:
            connection.execute(sql, params)
        return verify(self.before, self.after, _REPORT_ID)

    def test_只改两列时越界为零且差异计入允许列(self) -> None:
        report = self._patch(
            "UPDATE evidence SET kind='comment', parent_permalink=? WHERE id='ev-1'",
            "https://www.xiaohongshu.com/explore/6a79c7b1",
        )
        self.assertEqual(report["out_of_bounds"], 0)
        self.assertEqual(report["allowed_diff"], 2)
        self.assertGreater(report["compared"], 20, "比的是整行所有列，不是两列")

    def test_顺手改了别的列会被判越界(self) -> None:
        report = self._patch("UPDATE evidence SET title='被顺手改了' WHERE id='ev-1'")
        self.assertEqual(report["out_of_bounds"], 1)
        self.assertIn("ev-1.title", report["offenders"])

    def test_多出一行或少一行都算越界(self) -> None:
        with sqlite3.connect(self.after) as connection:
            connection.execute("DELETE FROM evidence WHERE id='ev-1'")
        report = verify(self.before, self.after, _REPORT_ID)
        self.assertEqual(report["rows_missing"], 1)


if __name__ == "__main__":
    unittest.main()
