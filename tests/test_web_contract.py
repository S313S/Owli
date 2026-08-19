import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class WebContractTest(unittest.TestCase):
    def test_需求输入_计划编辑_实时工作板三个路由(self) -> None:
        app = WEB / "src" / "App.tsx"
        self.assertTrue(app.is_file(), "web/src/App.tsx 尚未创建")
        source = app.read_text(encoding="utf-8")
        self.assertIn("<ResearchInputPage", source)
        self.assertIn("<PlanEditorPage", source)
        self.assertIn("<WorkboardPage", source)
        self.assertNotIn("ReportPage", source)

    def test_计划编辑器包含_origin_恢复_追问_批准冻结与409提示(self) -> None:
        editor = WEB / "src" / "PlanEditorPage.tsx"
        self.assertTrue(editor.is_file(), "web/src/PlanEditorPage.tsx 尚未创建")
        source = editor.read_text(encoding="utf-8")
        for contract in (
            "已自定义", "已修改", "恢复初始化", "批准并开始执行",
            "计划已冻结", "decision_balance", "plan_rev", "409", "重新加载",
            "V1.0 暂不生效", "执行策略，V1.0 只读", "公共前缀 common/v1 不可编辑",
        ):
            self.assertIn(contract, source)

    def test_工作板渲染_deadline_倒计时和重跑次数(self) -> None:
        source = (WEB / "src" / "WorkboardPage.tsx").read_text(encoding="utf-8")
        card = (WEB / "src" / "ActionCardView.tsx").read_text(encoding="utf-8")
        self.assertIn("重跑第", source)
        self.assertIn("deadline", card)
        self.assertIn("超时后", card)

    def test_卡片统一走_respond_并携带客户端请求ID(self) -> None:
        source = (WEB / "src" / "ActionCardView.tsx").read_text(encoding="utf-8")
        self.assertIn("/api/cards/", source)
        self.assertIn("/respond", source)
        self.assertIn("X-Request-ID", source)

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
