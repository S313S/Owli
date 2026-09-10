"""§WRITE-1：正文只留一套尺子 + 引语两道程序闸 + 尺子 ⑪ 认否定句。

判据落在**产物与实际入参**上：打 `build_prompt` 的入参看词表数进没进正文，
造一份改过字的引语看闸退不退回。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.report.polish.run import build_prompt, lexicon_reference_table
from app.report.polish.skills import load_templates, shared_rules

LEXICON_TABLE = {
    "name": "topic_polarity", "title": "主题提及量与极性词命中（Top 8）",
    "columns": ["主题", "提及条数", "含正向词", "含负向词", "正负同现"],
    "rows": [{"主题": "价格与付费", "提及条数": 247, "含正向词": 17,
              "含负向词": 3, "正负同现": 1, "marks": ["S22"]}],
    "n": 247, "basis": "固定词表 v1 命中计数，不是情感判断。", "coverage": {},
}
CODING_TABLE = {
    "name": "attitude_by_topic", "title": "UGC 逐条编码：主题 × 态度条数",
    "columns": ["主题", "态度", "条数"],
    "rows": [{"主题": "价格与付费", "态度": "负", "条数": 19, "marks": ["S23"]}],
    "n": 287, "basis": "对 287 条 UGC 逐条模型编码后计数。", "coverage": {},
}


def _data() -> dict:
    return {"title": "t", "research_question": "q", "objectives": [], "entities": ["豆包"],
            "sources": [], "audience_role": "不明", "audience_stake": "",
            "tables": {"topic_polarity": dict(LEXICON_TABLE),
                       "attitude_by_topic": dict(CODING_TABLE)}}


@pytest.mark.parametrize("template", [t.name for t in load_templates()])
def test_lexicon_table_not_declared_by_any_template(template):
    """三份模板的 `tables:` 行都不许再点名词表表——写手看得见它，就会引它的数。"""
    skill = next(t for t in load_templates() if t.name == template)
    assert "topic_polarity" not in skill.tables


@pytest.mark.parametrize("template", [t.name for t in load_templates()])
def test_lexicon_numbers_never_reach_the_prompt(template, tmp_path):
    """判据 1：打 `build_prompt` 的实际入参，词表表的数一个都不在里面。"""
    skill = next(t for t in load_templates() if t.name == template)
    body = build_prompt(skill, _data(), "# 工作稿\n", tmp_path / "x.md")
    assert '"含正向词": 17' not in body
    assert "主题提及量与极性词命中" not in body      # 中文表名也不许露面
    assert "UGC 逐条编码：主题 × 态度条数" in body    # 编码表照旧投给写手


def test_shared_rules_example_names_a_table_the_writer_actually_gets():
    """共用规则里那个「来源：<中文表名>」的例子，不能再举一张写手拿不到的表。"""
    assert "主题提及量与极性词命中" not in shared_rules()


def test_lexicon_reference_table_goes_to_appendix_with_the_caveat():
    """判据 1 后半：附录里有它，且写明「只数触发词、不是情感判断」。"""
    block = lexicon_reference_table(_data()["tables"])
    assert "## 词表命中参考（只数触发词，不是情感判断）" in block
    assert "不是情感判断" in block
    assert "| 价格与付费 | 247 | 17 | 3 | 1 |" in block


def test_lexicon_reference_table_absent_when_no_rows():
    """没有词表命中行就整块不出——空表会被读成「没人谈」。"""
    assert lexicon_reference_table({"topic_polarity": {**LEXICON_TABLE, "rows": []}}) == ""
    assert lexicon_reference_table({}) == ""


# —— 货 2：引语两道程序闸（子串闸 + 等级闸）————————————————————

from app.report.polish.run import (altered_quotes, lowgrade_quotes,  # noqa: E402
                                   quote_blocks, quote_corpus)

ORIGINAL = "老板批了三天，我白情一假，回来还得加班。"
QUOTED_OK = f"> {ORIGINAL}\n> —— 微博 · 等级 A [S01]\n"
QUOTED_ALTERED = f"> {ORIGINAL.replace('白情', '白请')}\n> —— 微博 · 等级 A [S01]\n"


def _corpus() -> str:
    return quote_corpus({"tables": {}}, f"# 工作稿\n\n{ORIGINAL}\n")


def test_quote_blocks_把角标按块收不按行收():
    """角标写在出处行上；按行收会把每一条原声都判成「没有角标」。"""
    blocks = quote_blocks("正文\n\n" + QUOTED_OK + "\n后文\n")
    assert len(blocks) == 1
    line_no, texts, marks = blocks[0]
    assert (line_no, texts, marks) == (3, [ORIGINAL], [1])


def test_子串闸_照抄的原声放行():
    assert altered_quotes(QUOTED_OK, _corpus()) == []


def test_子串闸_改一个字必须退回():
    """缺陷 3：库里原文「白情一假」是发帖人自己手误，写手顺手改成了「白请一假」。"""
    problems = altered_quotes(QUOTED_ALTERED, _corpus())
    assert len(problems) == 1
    assert "与原文对不上" in problems[0]


def test_子串闸_省略号接起来的长引语按段比():
    """写手常用省略号接两段原文，两段各自仍是子串，不该判红。"""
    joined = "> 老板批了三天……回来还得加班。\n> —— 微博 · 等级 A [S01]\n"
    assert altered_quotes(joined, _corpus()) == []


def test_子串闸_不比太短的片段():
    """一两个字撞上原文纯属巧合，判红只会白烧一轮重写。"""
    assert altered_quotes("> 真香\n> —— 微博 · 等级 A [S01]\n", _corpus()) == []


def test_子串闸_出处行不参与比对():
    """「—— 微博 · 等级 A」是程序规定的出处格式，不是人说的话。"""
    assert altered_quotes("> —— 微博 · 等级 A 这一行不比\n", _corpus()) == []


def test_子串闸_复用_squeeze_不另造归一化函数():
    """判据 4：换行与空格重排照样认，用的是编码那头同一个 `_squeeze`。"""
    from app.reliability.coding import _squeeze

    import app.report.polish.run as run_module
    assert "_squeeze" in run_module.altered_quotes.__code__.co_names
    assert altered_quotes("> 老板批了三天，\n> 我白情一假\n", _squeeze(ORIGINAL)) == []


def test_等级闸_AB_级放行():
    assert lowgrade_quotes(QUOTED_OK, {1: "A"}) == []
    assert lowgrade_quotes(QUOTED_OK, {1: "B"}) == []


def test_等级闸_C_级必须退回():
    """缺陷 9：正文自己写明 S39 是 C 级不得作原声，另一段又拿 S39 当案例。"""
    problems = lowgrade_quotes(QUOTED_OK, {1: "C"})
    assert len(problems) == 1
    assert "S01（C 级）" in problems[0]


def test_等级闸_未评级与_D_级同样退回():
    assert lowgrade_quotes(QUOTED_OK, {1: "D"})
    assert lowgrade_quotes(QUOTED_OK, {})


def test_等级闸_块里混着一条_C_级也退回():
    """一块里 A 和 C 都引了，C 那条照样不许作原声。"""
    block = f"> {ORIGINAL}\n> —— 微博 · 等级 A [S01][S02]\n"
    assert lowgrade_quotes(block, {1: "A", 2: "C"})


def test_quote_corpus_收编码表摘出的原声():
    """`quotes` 表那一列在编码那头已程序校验过是正文子串，照抄它必须放行。"""
    data = {"tables": {"quotes": {"rows": [{"原声": ORIGINAL, "marks": ["S01"]}]}}}
    assert altered_quotes(QUOTED_OK, quote_corpus(data, "# 工作稿\n")) == []


# —— 货 2 端到端：闸接在写作重试路上，退回的是「这一节重写」不是「验收报红」——

import asyncio  # noqa: E402


class _GateStore:
    """两条证据：S01 是 A 级（原声合法），S02 是 C 级（不得作原声）。"""

    def get_report(self, rid):
        return {"id": rid, "title": "T", "research_question": "q", "plan_snapshot": {},
                "extra": {"claims": []}}

    def list_evidence(self, rid):
        return [{"id": "ev-1", "platform": "weibo", "kind": "post", "citation_no": 1,
                 "title": "帖一", "content_excerpt": ORIGINAL, "grade": "A",
                 "published_at": None, "extra": "{}"},
                {"id": "ev-2", "platform": "weibo", "kind": "post", "citation_no": 2,
                 "title": "帖二", "content_excerpt": "随便一句", "grade": "C",
                 "published_at": None, "extra": "{}"}]


GATE_WORK = (f"# 工作稿\n\n{ORIGINAL}\n\n## 信息源\n\n"
             "- [S01] [帖一](https://e.com/a)\n- [S02] [帖二](https://e.com/b)\n")


def _quoting_adapter(quote_block: str):
    """每节都写同一段正文 + 同一个原声块。写够 `MIN_SECTION_BYTES`。"""

    class _Adapter:
        async def run(self, task, ctx, on_event=None):
            task.output_path.write_text(
                "这一节的正文[S01]。" * 40 + "\n\n" + quote_block, encoding="utf-8")
            return type("R", (), {"succeeded": True, "engine_error": None})()

    return _Adapter()


def _polish(tmp_path, quote_block):
    from app.report.polish.run import polish

    return asyncio.run(polish(_GateStore(), "r-gate", tmp_path / "runs", GATE_WORK,
                              template="consulting",
                              adapter=_quoting_adapter(quote_block)))


def test_照抄的原声一路写到底(tmp_path):
    """反向对照：同一条路，原声照抄就该通过——闸不能把合法的稿也挡了。"""
    result = _polish(tmp_path, QUOTED_OK)
    assert result["status"] == "ok", result.get("errors")


def test_改过字的原声被闸退回(tmp_path):
    """判据 2：造一份把引语改一个字的产物，`polish()` 必须判红，不许落成成稿。"""
    result = _polish(tmp_path, QUOTED_ALTERED)
    assert result["status"] == "failed"
    assert any("与原文对不上" in e for e in result["errors"]), result["errors"]
    assert result["attempts"] == 2, "改过字要给写手一次重写机会，不是一次就判死"


def test_引_C_级作原声被闸退回(tmp_path):
    """判据 3：造一份拿 C 级角标作原声的产物，`polish()` 必须判红。"""
    block = f"> {ORIGINAL}\n> —— 微博 · 等级 C [S02]\n"
    result = _polish(tmp_path, block)
    assert result["status"] == "failed"
    assert any("S02（C 级）" in e for e in result["errors"]), result["errors"]


def test_把握度那句引用块不是原声_不上闸():
    """模板要求摘要末尾那句把握度也写成 `>` 块，它是写手自己的话——

    没有原文可比、也没有等级可查。真稿夹具当场抓到过：不作区分的话，
    摘要那一节每轮都被子串闸退回，两次重写全白烧。
    """
    line = "> 本报告结论的把握度为**低**，主要因为绝大多数说法都只有一个来源撑着。\n"
    assert quote_blocks(line) == []
    assert altered_quotes(line, _corpus()) == []
    assert lowgrade_quotes(line, {}) == []


def test_带出处行但没角标的原声照样上子串闸():
    """两样凭据有一样就是原声：出处行在，就算角标漏了也要比对原文。"""
    block = "> 我白请一假，回来还得加班。\n> —— 微博 · 等级 A\n"
    assert altered_quotes(block, _corpus())


# —— 货 3：篇幅（判黄不判红；本包不出判据，读数留给合流那一轮）——————————

import sys as _sys  # noqa: E402

_sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "acceptance" / "rpt1"))
import check_polished as _ruler  # noqa: E402


def _doc(writer_chars: int) -> str:
    from app.report.polish.run import BASIS_HEADING, SOURCES_HEADING

    return ("# 执行摘要\n\n" + "正" * writer_chars + "\n\n"
            + BASIS_HEADING + "\n\n" + "口" * 3000 + "\n\n"
            + SOURCES_HEADING + "\n\n" + "源" * 9000 + "\n")


def test_篇幅只数写手写的那部分():
    """程序生成的四块（缺失清单/各表口径/词表命中参考/信息源清单）不算写手的账。

    信息源清单一块就 9 434 字符、占底料全文 37%，读者不读它只查它，写手也压不动。
    """
    assert _ruler.writer_length(_doc(5000)) == pytest.approx(5000, abs=40)


def test_篇幅单位是字符不是中文字():
    """开工时按中文字定预算差了三倍——底料全文 25 197 字符、中文字才 9 933，

    用户读到的「2.5 万」对得上的是字符数。按中文字定，尺子会要求这份稿变长。
    """
    assert _ruler.LENGTH_MAX_CHARS == 9000 and _ruler.LENGTH_MIN_CHARS == 7000
    assert _ruler.length_budget(_doc(14154))          # 上一稿那个体量要判黄
    assert _ruler.length_budget(_doc(8000)) == []     # 砍一半之后不判


def test_篇幅压过头也报():
    """⛔ 不许靠删限定句压篇幅——压过头同样要报出来给人看。"""
    assert "低于下限" in _ruler.length_budget(_doc(3000))[0]


def test_篇幅判黄不判红():
    """篇幅超了要整稿重写、一轮 37.6 分钟；这条只给读数，不掀掉这一格。"""
    assert "⒝ 篇幅" in _ruler.WARNINGS and "⒝ 篇幅" not in _ruler.CHECKS


# —— 货 4：单源建议降级，门禁从验收期挪到写作期 ————————————————

from app.report.polish.run import singlesource_advice  # noqa: E402

ADVICE = ("1. **给创作者做一版水印自查清单**（做什么，不是应该重视什么）\n"
          "   依据：回指第 4 条发现；把握度：低[S01]\n")


def test_全是孤证的建议被门禁退回():
    """上一轮遗留 ⑧：三条行动建议各只有单源孤证，尺子判红是对的。"""
    problems = singlesource_advice(ADVICE.splitlines(), {1: "SINGLE"})
    assert len(problems) == 1 and "值得进一步验证的方向" in problems[0]


def test_有一条多源互证撑着就放行():
    assert singlesource_advice(ADVICE.splitlines(), {1: "PASS"}) == []


def test_降级区里的条目不受门禁管():
    """已经降级过的就别再退回——否则写手照做了还是过不了，只能瞎改。"""
    text = "## 值得进一步验证的方向\n\n" + ADVICE
    assert singlesource_advice(text.splitlines(), {1: "SINGLE"}) == []


def test_一条建议横跨两行按条聚合不按行判():
    """建议行只带 PASS、依据行只带 SINGLE；按行判会把依据行单独判红（09-05 两格误报）。"""
    entry = "1. **建议一句话**[S01]\n   依据：回指第 2 条发现[S02]\n"
    assert singlesource_advice(entry.splitlines(), {1: "PASS", 2: "SINGLE"}) == []


def test_尺子与生产侧用的是同一个函数():
    """判据：同一个概念两处两个定义是本项目现形过的假绿。"""
    import inspect

    src = inspect.getsource(_ruler._advice_entry_problems)
    assert "singlesource_advice" in src
    assert _ruler.ADVICE_SECTIONS is __import__(
        "app.report.polish.run", fromlist=["x"]).ADVICE_SECTIONS


def test_建议节写作期就挡住_不等整轮跑完(tmp_path):
    """货 4 落点：门禁原先只在验收那一头查，查出来时 37.6 分钟已经付掉了。"""
    from app.report.polish.run import ADVICE_SECTIONS, ADVICE_SECTION_UNKNOWN_AUDIENCE

    assert ADVICE_SECTION_UNKNOWN_AUDIENCE in ADVICE_SECTIONS   # 读者不明时的改名也算建议节
    assert "建议" in ADVICE_SECTIONS


# —— 货 5：改尺子不改稿——⑪「不许推及全网」要认否定句 ——————————————

REAL_LINE_270 = ("   依据：回指海外 Reddit 手游忘删豆包水印事件的多源围观[S63][S66]；"
                 "把握度：中（同一 gacha 手游事件多人独立表态、跨立场一致谴责，"
                 "属多源互证；但事件本身单一场景，尚不能外推为国内用户普遍关注）。")


def test_尺子11_真稿第270行不再报红():
    """成稿第 270 行是**限定句**，是 §5 门禁要求写手写的那种话。

    尺子只匹配「用户普遍」四字、没看见前面的「尚不能外推」，把守规矩的句子判成违规。
    ⛔ 改尺子不改稿——「量出的异常是尺子的」第七次现形。
    """
    assert _ruler.check_ratio_phrases(REAL_LINE_270) == []


def test_尺子11_真的肯定句仍要报红():
    """判据 5 的另一半：否则就是把规则删了，不是改对了。"""
    assert _ruler.check_ratio_phrases("国内用户普遍关注水印问题，这是最集中的诉求。")


def test_尺子11_否定词写在违禁词后面不算否定():
    """「用户普遍不满意」照旧是推及全网的断言，只是断的是负面。"""
    assert _ruler.check_ratio_phrases("用户普遍不满意豆包的回答深度。")


def test_尺子11_否定只在同一小句里管用():
    """整句切太粗：分号前是断言、分号后是限定，按整行判会互相盖住。"""
    assert _ruler.check_ratio_phrases("这一点不足以定论；国内用户普遍在谈价格。")


def test_尺子11_整份真稿零命中():
    """交付前读真生成物：改完之后这份稿 ⑪ 一处都不报。"""
    path = Path("../Owli-rpt1/var/runs/r-3e04f808dffd/exports/"
                "r-3e04f808dffd.polished.consulting.md")
    if not path.is_file():
        pytest.skip("底料不在这台机器上")
    assert _ruler.check_ratio_phrases(path.read_text(encoding="utf-8")) == []
