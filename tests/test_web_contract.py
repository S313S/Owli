import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class WebContractTest(unittest.TestCase):
    def test_需求输入_计划编辑_实时工作板_报告四个路由(self) -> None:
        app = WEB / "src" / "App.tsx"
        self.assertTrue(app.is_file(), "web/src/App.tsx 尚未创建")
        source = app.read_text(encoding="utf-8")
        self.assertIn("<ResearchInputPage", source)
        self.assertIn("<PlanEditorPage", source)
        self.assertIn("<WorkboardPage", source)
        # FE-1：报告页此前没有路由，/researches/<id>/report 会落到兜底渲染成
        # 需求输入页（任意端口皆然，含 8721），用户因此「找不到报告」。
        self.assertIn("<ReportPage", source)
        self.assertIn(r"^\/researches\/([^/]+)\/report$", source)

    def test_前端不写死后端地址(self) -> None:
        sources = list((WEB / "src").glob("**/*.ts")) + list((WEB / "src").glob("**/*.tsx"))
        self.assertTrue(sources, "web/src 尚无 TypeScript 源码")
        for path in sources:
            body = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("//")
            )
            self.assertNotIn(
                "127.0.0.1:8721", body,
                f"{path.name} 写死了后端地址；展示用地址请走 backendOrigin()",
            )
        origin = (WEB / "src" / "origin.ts").read_text(encoding="utf-8")
        self.assertIn("window.location.host", origin)

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

    def test_计划编辑器渲染历史复用来源(self) -> None:
        source = (WEB / "src" / "PlanEditorPage.tsx").read_text(encoding="utf-8")
        self.assertIn("baseline_source.startsWith('reused:')", source)
        self.assertIn("沿用自历史计划", source)

    def test_复用计划提供一次性实体映射而不是逐字段碰运气(self) -> None:
        source = (WEB / "src" / "PlanEditorPage.tsx").read_text(encoding="utf-8")
        types = (WEB / "src" / "types.ts").read_text(encoding="utf-8")
        self.assertIn("复用实体映射", source)
        self.assertIn("pendingEntityPlaceholders", source)
        self.assertIn("applyPendingEntityMapping", source)
        self.assertIn("entity: string | null", types)
        self.assertIn("if (agent.chapter)", source)
        self.assertIn("} & Record<string, unknown>) | null", types)

    def test_工作板渲染_deadline_倒计时和重跑次数(self) -> None:
        source = (WEB / "src" / "WorkboardPage.tsx").read_text(encoding="utf-8")
        card = (WEB / "src" / "ActionCardView.tsx").read_text(encoding="utf-8")
        self.assertIn("重跑第", source)
        self.assertIn("deadline", card)
        self.assertIn("超时后", card)

    def test_历史工作板展示报告账本缺失清单且没有操作组件(self) -> None:
        board = (WEB / "src" / "WorkboardPage.tsx").read_text(encoding="utf-8")
        history_path = WEB / "src" / "HistoricalResearchView.tsx"
        self.assertTrue(history_path.is_file(), "历史只读视图尚未创建")
        history = history_path.read_text(encoding="utf-8")
        stream = (WEB / "src" / "useResearchStream.ts").read_text(encoding="utf-8")

        self.assertIn("snapshot.snapshot_source === 'store'", board)
        self.assertIn("<HistoricalResearchView", board)
        for heading in ("历史只读", "报告产物", "章账本", "缺失清单"):
            self.assertIn(heading, history)
        for forbidden in ("ActionButtons", "ActionCardView", "<Button", "actions.map"):
            self.assertNotIn(forbidden, history)
        self.assertIn("loaded.snapshot_source === 'store'", stream)

    def test_历史只读页不再绕过运行面板(self) -> None:
        history = (WEB / "src" / "HistoricalResearchView.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("import RunPanel from './RunPanel'", history)
        self.assertIn(
            "<RunPanel researchId={snapshot.research_id} snapshot={snapshot} />",
            history,
        )

    def test_工作板与历史页都有报告页入口(self) -> None:
        # FE-1 货 4：路由补上了还得有人指过去，否则用户只能手敲 URL。
        board = (WEB / "src" / "WorkboardPage.tsx").read_text(encoding="utf-8")
        history = (WEB / "src" / "HistoricalResearchView.tsx").read_text(encoding="utf-8")
        for source in (board, history):
            self.assertIn('data-testid="open-report-page"', source)
            self.assertIn("/report`", source)
        # 历史只读页不放操作组件，入口只能是链接（见只读契约那条用例）
        self.assertIn("<a data-testid=\"open-report-page\"", history)

    def test_历史报告_JSON_信封优先展示正文而不是原始载荷(self) -> None:
        history = (WEB / "src" / "HistoricalResearchView.tsx").read_text(encoding="utf-8")
        self.assertIn("function readableReport", history)
        self.assertIn("JSON.parse", history)
        self.assertIn("parsed.sections", history)
        self.assertIn("parsed['报告正文']", history)

    def test_卡片统一走_respond_并携带客户端请求ID(self) -> None:
        source = (WEB / "src" / "ActionCardView.tsx").read_text(encoding="utf-8")
        self.assertIn("/api/cards/", source)
        self.assertIn("/respond", source)
        self.assertIn("X-Request-ID", source)

    def test_调整后继续_进入运行期计划编辑且可返回工作板(self) -> None:
        card = (WEB / "src" / "ActionCardView.tsx").read_text(encoding="utf-8")
        editor = (WEB / "src" / "PlanEditorPage.tsx").read_text(encoding="utf-8")

        self.assertIn("action.value === 'adjust'", card)
        self.assertIn("?runtime=1", card)
        self.assertIn("运行期调整", editor)
        self.assertIn("返回工作板继续", editor)

    def test_工作板状态不轮询(self) -> None:
        """状态一律走 SSE。

        §OBS-2 货 4 的运行面板是唯一例外，且它拉的不是状态：transcript 是
        引擎原始流文件，只在节**运行中**每 3 s 取一次增量（用户 2026-09-03
        午后拍板的方案原文）。所以豁免只给 RunPanel.tsx，且下面钉死它的
        定时器除了 transcript 接口不许拉别的。
        """

        sources = list((WEB / "src").glob("**/*.ts")) + list((WEB / "src").glob("**/*.tsx"))
        self.assertTrue(sources, "web/src 尚无 TypeScript 源码")
        merged = "\n".join(
            path.read_text(encoding="utf-8") for path in sources
            if path.name != "RunPanel.tsx"
        )
        self.assertNotRegex(merged, r"setInterval|setTimeout\s*\([^)]*fetch")

    def test_运行面板的定时拉取只碰节的只读视图(self) -> None:
        """§OBS-3：面板两栏各拉一条流——`/transcript`（日志）与 `/progress`（进程）。

        两条都是节的只读视图，跑完即停；除此之外不许再碰别的接口。
        """

        source = (WEB / "src" / "RunPanel.tsx").read_text(encoding="utf-8")
        endpoints = re.findall(r"fetch\(\s*\n?\s*`([^`]+)`", source)
        self.assertTrue(endpoints, "运行面板没有任何请求")
        for endpoint in endpoints:
            self.assertRegex(endpoint, r"/sections/.+/\$\{view\}\?|/(transcript|progress)\?")
        self.assertIn("'transcript'", source)
        self.assertIn("'progress'", source)
        # 只在跑的时候拉，跑完必须停。
        self.assertIn("if (!live || !tab) return", source)

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

    def test_入口页通过_SSE_接收并渲染历史候选卡(self) -> None:
        source = (WEB / "src" / "ResearchInputPage.tsx").read_text(encoding="utf-8")
        self.assertNotIn("result.data.similar", source)
        self.assertIn("EventSource", source)
        self.assertIn("card_update", source)
        self.assertIn("<ActionCardView", source)

    def test_入口刷新错过_SSE_但计划已就绪时直接恢复到计划页(self) -> None:
        source = (WEB / "src" / "ResearchInputPage.tsx").read_text(encoding="utf-8")
        self.assertIn("result.data.status === 'awaiting_review'", source)
        self.assertIn("pendingHistoryCards.current.size === 0", source)
        self.assertIn("window.location.assign", source)

    def test_历史候选卡显示复用价值和后端退化标签(self) -> None:
        source = (WEB / "src" / "ActionCardView.tsx").read_text(encoding="utf-8")
        self.assertIn("HISTORY_REUSE", source)
        self.assertIn("更快、已验证", source)
        self.assertIn("match_label", source)
        self.assertIn("关键词粗匹配", source)


if __name__ == "__main__":
    unittest.main()
