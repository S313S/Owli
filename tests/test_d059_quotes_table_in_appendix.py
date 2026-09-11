"""§D-059 货 3：代表原声「表」挪到附录，正文只留「引用块」。

用户 2026-09-11 拍的取舍：**原话是证据不是论点**，放附录和信息源清单在一起更合位置，
而且不跟他 09-09 刚拍过的「正文要短」打架——上一轮这张表就是被篇幅预算挤没的。

⚠️ 本文件要同时守住两件**相反**的事，少守一件就换来另一个缺陷：
  ⒜ 整张**表**由程序挂附录，正文一次都不许摆（缺陷 8「同一张表出现两次」）；
  ⒝ 正文里的**引用块**（`> 原文…`）照写，一条都不许少——共用规则 §5.6 步骤 4，
     **那是整份稿里唯一让读者听见真人的地方**。

⛔ 所以 `quotes` 必须**继续留在三份模板的 `tables:` 行里**（写手拿不到原话就一句都引不出），
这跟词表命中表「从 tables: 里拿掉」的落法**不一样**，别照抄那一条。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "acceptance" / "rpt1"))

from app.report.polish.run import (  # noqa: E402
    PROGRAM_APPENDIX_HEADINGS, QUOTES_HEADING, QUOTES_TABLE, quotes_reference_table)
from app.report.polish.skills import load_templates  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "check_polished", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)


def _tables(rows=None):
    return {QUOTES_TABLE: {
        "title": "UGC 代表原声", "n": 6,
        "columns": ["主题", "态度", "原声", "平台", "互动量", "代表性"],
        "basis": "从原文逐字摘出、程序校验过是正文子串的原声。",
        "rows": rows if rows is not None else [
            {"主题": "功能与能力", "态度": "正", "原声": "豆包可以搞定了…",
             "平台": "xhs", "互动量": 3.0, "代表性": "", "marks": ["S30"]},
            {"主题": "价格与付费", "态度": "负", "原声": "it does violate Doubao's user agreement.",
             "平台": "reddit", "互动量": 176.0, "代表性": "", "marks": ["S66"]},
        ]}}


def test_原声表标题登记在程序块清单里():
    """⛔ 没登记，尺子量写手篇幅时就不会剜掉它——这张表会被算成写手写的，
    篇幅当场超标，而超标的修法是「删限定句」，等于用一个缺陷换另一个。"""
    assert QUOTES_HEADING in PROGRAM_APPENDIX_HEADINGS


def test_渲染出的表行数与数据行一致():
    block = quotes_reference_table(_tables())
    body = [ln for ln in block.splitlines()
            if ln.startswith("|") and not set(ln) <= set("|- ")]

    assert QUOTES_HEADING in block
    assert len(body) - 1 == 2, "表头之外必须正好两行，不许多也不许少"
    assert "[S30]" in block and "[S66]" in block, "角标要带上，不然读者查不回去"


def test_互动量按尺子的口径去掉小数尾巴():
    """⛔ 不是为了好看：验收尺子 ④ 的白名单是削掉小数尾巴收的，
    写 `176.0` 等于往稿子里放一个白名单外的数，程序自己生成的表被自己的尺子判红。"""
    block = quotes_reference_table(_tables())

    assert "| 176 |" in block and "176.0" not in block
    assert check_polished._fmt(176.0) == "176", "两处口径必须同形"


def test_没有原声就整块不出而不是出空表():
    """空表会被读成「没人这么说」——把**没数据**写成**没人谈**，是往报告里塞假结论。"""
    assert quotes_reference_table(_tables(rows=[])) == ""
    assert quotes_reference_table({}) == ""


def test_三份模板都还把原声表投给写手():
    """⛔ 这条是本包最容易做反的一条。

    拿掉 `quotes` 正文引语会整体塌掉——写手就没有任何一句原话可引了。
    挪的是「表」，不是「原话」。
    """
    templates = load_templates()
    assert len(templates) == 3, "模板没全加载到，这条用例等于没验"
    for template in templates:
        assert QUOTES_TABLE in template.tables, \
            f"{template.name} 的 tables: 里没有 quotes，写手会一句原话都引不出来"


def test_门禁挡住正文里的原声表():
    """货 3 造红：把表摆进正文必须判红。"""
    body = ("# 关键发现\n\n三到五句解读……\n\n"
            "| 主题 | 态度 | 原声 | 平台 | 互动量 | 代表性 |\n"
            "|---|---|---|---|---|---|\n"
            "| 功能与能力 | 正 | 豆包可以搞定了… | xhs | 3 |  |\n")

    problems = check_polished.check_quotes_table_not_in_body(body)

    assert len(problems) == 1
    assert "摆进了正文" in problems[0]


def test_门禁不误伤正文里的引用块():
    """⛔ 引语是 §5.6 步骤 4 要求的，**一条都不许少**；挡表不能顺手把它挡了。"""
    body = ("# 关键发现\n\n> 你说的这些现在豆包可以搞定了…\n> —— xhs · 等级 B [S30]\n\n"
            "三到五句解读……\n")

    assert check_polished.check_quotes_table_not_in_body(body) == []


def test_门禁不误伤附录里的程序块():
    """表就该在那儿——挡在正文，不是挡在附录。"""
    document = ("# 关键发现\n\n三到五句解读……\n\n# 附录\n\n"
                + quotes_reference_table(_tables()))

    assert check_polished.check_quotes_table_not_in_body(document) == []
    assert QUOTES_HEADING not in check_polished.writer_body(document), \
        "程序块必须被剜掉，否则它会被算进写手篇幅"


def test_程序块的正文不占写手篇幅():
    """判据：挪进附录之后，这张表的内容一个字都不许算进写手预算。

    ⚠️ 量的是「块内容没被算进去」，不是「两个数一模一样」——块与块之间那个
    分隔换行会留在剜完的串里（`writer_body` 保留 `text[:cut + 1]`），
    差 1 个字符是那个换行，不是表的内容。⛔ 写成严格相等会是一条假判据：
    它量的是换行怎么切的，不是这一包要保的那件事。
    """
    before = "# 关键发现\n\n三到五句解读……\n\n# 附录\n\n方法与样本……\n"
    block = quotes_reference_table(_tables())
    after = before + "\n" + block

    grew = check_polished.writer_length(after) - check_polished.writer_length(before)
    assert grew <= 1, f"写手篇幅涨了 {grew} 字符，说明这张表被算进了他的预算"
    assert len(block) > 300, "夹具前提：这张表本身几百字符，真被算进去一眼就看得出"
