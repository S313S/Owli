"""§QUOTE-1 货 1：原声不许停在半句上。

判据 2 要的是**反向验证**：造一条切在半句的 quote，加闸前必须过、加闸后必须退。
这份用例只守加闸后那一半（加闸前那一半在 worklog 里有 A/B 实测记录，
git 上的旧码跑不进用例文件）。
"""

from app.reliability import coding


SOURCE = (
    "豆包的「壞行為」三大罪狀強行對話（Forced Chatting）："
    "開發團隊寫死了特定程式碼。這導致它不管有沒有解決問題，都會生硬地反問。"
)


def _items(quote: str) -> list[dict[str, object]]:
    return [{"id": "ev-x", "audience": "不明", "scenario": "其他", "attitude": "负",
             "topics": [], "quote": quote}]


def _quote_errors(quote: str, source: str = SOURCE) -> list[str]:
    return [error for error in coding.coding_errors(
        _items(quote), [{"id": "ev-x"}], {"ev-x": source}) if "quote" in error]


def test_停在半句上的原声被退回():
    # 实测的那一条：只摘了 16 字，离 QUOTE_MAX=40 还远——所以调大上限治不了。
    errors = _quote_errors("豆包的「壞行為」三大罪狀強行對話")
    assert errors and "停在半句上" in errors[0]


def test_停在括号收尾但句子没完的也被退回():
    assert _quote_errors("豆包的「壞行為」三大罪狀強行對話（ForcedChatting）")


def test_摘到句末标点的原声照过():
    assert _quote_errors("開發團隊寫死了特定程式碼。") == []


def test_省掉末尾句号也算说完了():
    # 模型常把最后那个句号漏掉，不能因此判红——紧跟其后的是句号就算落在句末。
    assert _quote_errors("開發團隊寫死了特定程式碼") == []


def test_整个标题就是一句话时不判半句():
    # `source_text` 拿换行接标题与正文，标题末尾要当句末，否则摘标题必被判红。
    source = "豆包：我惹祸吃官司了\n昨天用它查了个案子，结果全是编的。"
    assert _quote_errors("豆包：我惹祸吃官司了", source) == []


def test_正文通篇没有标点也没有换行时不设闸():
    # 抖音口播稿实测有这种；设了闸整批只会重打三次再整批作废，一条都落不了库。
    source = "这个模型真的强一次全过而且是多轮任务遥遥领先"
    assert _quote_errors("这个模型真的强", source) == []


def test_不是原文子串时只报子串一条错():
    # 两条错一起报会把重打提示词里前 10 条错误的名额吃掉一半。
    errors = _quote_errors("豆包根本不能用")
    assert len(errors) == 1 and "不是原文子串" in errors[0]


def test_空原声照旧允许():
    assert _quote_errors("") == []


def test_三轮都没说完时作废那句原声而不是赔掉整批():
    """闸按条判、退回按批退——一条摘不好赔掉 40 条聚合编码，方向反了。"""

    import asyncio
    import json
    from pathlib import Path
    import tempfile

    class _Adapter:
        """每轮都把 quote 摘成半句的假引擎。"""

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, task, ctx, on_event=None):
            self.calls += 1
            Path(task.output_path).write_text(json.dumps([
                {"id": "ev-000", "audience": "不明", "scenario": "其他",
                 "attitude": "负", "topics": [], "quote": "豆包的「壞行為」三大罪狀強行對話"},
                {"id": "ev-001", "audience": "不明", "scenario": "其他",
                 "attitude": "正", "topics": [], "quote": "開發團隊寫死了特定程式碼。"},
            ], ensure_ascii=False), encoding="utf-8")
            return type("R", (), {"succeeded": True})()

    rows = [{"id": "ev-000", "platform": "xhs", "title": "t", "content_excerpt": SOURCE},
            {"id": "ev-001", "platform": "xhs", "title": "t", "content_excerpt": SOURCE}]
    adapter = _Adapter()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "a" / "b" / "c" / "d" / "batch.json"
        labels = asyncio.run(coding._code_batch(
            rows, adapter=adapter, output_path=out,
            report_id="r-x", goal_id="goal-1", engine_preference=None))
        assert adapter.calls == coding.MAX_ATTEMPTS, "该三轮都打过再兜底"
        assert labels is not None, "整批不该因为一句半话全丢"
        assert labels[0]["quote"] == "", "没说完的那句要作废"
        assert labels[1]["quote"] == "開發團隊寫死了特定程式碼。", "说完的那句要留着"
        assert labels[0]["attitude"] == "负", "聚合编码要保住"
        note = json.loads(out.with_suffix(".errors.json").read_text(encoding="utf-8"))
        assert "已作废置空" in note["errors"][0], "赔掉几句原声不许静默"
