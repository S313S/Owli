import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class WebContractTest(unittest.TestCase):
    def test_只有需求输入与实时工作板两个路由(self) -> None:
        app = WEB / "src" / "App.tsx"
        self.assertTrue(app.is_file(), "web/src/App.tsx 尚未创建")
        source = app.read_text(encoding="utf-8")
        self.assertIn("<ResearchInputPage", source)
        self.assertIn("<WorkboardPage", source)
        self.assertNotIn("PlanPage", source)
        self.assertNotIn("ReportPage", source)

    def test_前端不轮询(self) -> None:
        sources = list((WEB / "src").glob("**/*.ts")) + list((WEB / "src").glob("**/*.tsx"))
        self.assertTrue(sources, "web/src 尚无 TypeScript 源码")
        merged = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertNotRegex(merged, r"setInterval|setTimeout\s*\([^)]*fetch")

    def test_操作按钮由后端_actions_数组渲染(self) -> None:
        board = WEB / "src" / "WorkboardPage.tsx"
        self.assertTrue(board.is_file(), "工作板页面尚未创建")
        source = board.read_text(encoding="utf-8")
        self.assertRegex(source, r"snapshot\.actions\.map")
        self.assertNotRegex(source, r"status\s*===.*(?:暂停|终止|继续)")

    def test_引擎字段只按普通字符串展示(self) -> None:
        sources = list((WEB / "src").glob("**/*.ts")) + list((WEB / "src").glob("**/*.tsx"))
        merged = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertNotRegex(merged, re.compile(r"if\s*\([^)]*engine|engine\s*===|engine\s*==", re.I))

    def test_POST_响应不直接驱动工作板状态(self) -> None:
        for filename in ("ActionButtons.tsx", "ActionCardView.tsx"):
            source = (WEB / "src" / filename).read_text(encoding="utf-8")
            self.assertNotIn("onSnapshot", source)


if __name__ == "__main__":
    unittest.main()
