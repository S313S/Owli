"""§D-060 货 1：「这条证据有没有独立标题」先判，再判「这句是不是标题」。

零引擎、零采集。每条先造红再造绿（记忆 `verification-ruler-needs-verifying`）。
实证（r-3e04f808dffd，归一后「标题 = 正文开头」）：weibo 27/27、web_search 19/21、
douyin 47/107、xhs 30/296、reddit 0/111。微博没有标题这个东西——采集器把博文正文同时
塞进 `title` 与 `content_excerpt`，于是 S33 那段真人博文的结尾被判成了「标题」。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.report.polish.tables import (
    _drop_non_speech_quotes, has_independent_title, independent_titles, is_speech_quote)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_polished", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)

#: S33 原样（微博·post）：title 是正文去掉结尾「吧#WorkBuddy##AI#」的拷贝。
S33_BODY = ("WorkBuddy把微信用户洗过一遍，那岂不是就都洗完了？国内还有哪个用户基盘有比微信更大的吗？"
            "WorkBuddy的微信生态胜在规模大，飞书和豆包胜在场景和垂直吧#WorkBuddy##AI#")
S33_TITLE = S33_BODY[:-len("吧#WorkBuddy##AI#")]
S33_QUOTE = "飞书和豆包胜在场景和垂直"
WEIBO = {"title": S33_TITLE, "content_excerpt": S33_BODY, "platform": "weibo"}
#: Reddit 帖子：标题独立于正文。
REDDIT = {"title": "Has anyone tried doubao (豆包）? It blew my mind honestly",
          "content_excerpt": "It’s ChatGPT/ai on another level. U essentially video call w/ it",
          "platform": "reddit"}


# ── 判「有没有独立标题」：按数据不按平台 ─────────────────────────────────
@pytest.mark.parametrize("row, expected, why", [
    (WEIBO, False, "标题是正文的前缀（正文拷贝被截断）= 没有独立标题"),
    ({"title": "豆包用着还行", "content_excerpt": "豆包用着还行"}, False, "一字不差"),
    ({"title": "豆包用着还行，写文案比我自己快，就是长文会车轱辘",
      "content_excerpt": "豆包用着还行，写文案比我自己快"}, False,
     "正文反过来是标题的前缀（标题才是全文、正文被截）且正文归一后 ≥8 字"),
    ({"title": "豆包用着还行，写文案比我自己快", "content_excerpt": "豆包用着还行"}, True,
     "正文归一后只有 6 字（<8）——短句撞车是巧合，按有标题算"),
    (REDDIT, True, "标题与正文各说各的 = 真标题"),
    ({"title": "豆包，开始收费了", "content_excerpt": "最近，豆包在小范围内开启了专业版的灰度测试。"},
     True, "小红书的笔记标题是独立写的"),
    ({"title": "豆包用着还行", "content_excerpt": ""}, True, "没有正文可比，按有标题算——不拆闸"),
    ({"title": "", "content_excerpt": "正文"}, False, "没标题就是没标题"),
])
def test_has_independent_title_judges_by_data_not_platform(row, expected, why):
    assert has_independent_title(row) is expected, why


def test_independent_titles_keeps_only_real_titles():
    assert independent_titles([WEIBO, REDDIT]) == [REDDIT["title"]]


# ── 造红：S33 那句改前被否决、改后通过；Reddit 真标题改前改后都被否决 ──────
def test_s33_sentence_was_vetoed_when_every_title_was_on_the_list():
    """改前的名单（全部 title 字段）：博文结尾那句被判成「标题」——这就是 ⑭ 那个假红。"""
    assert is_speech_quote(S33_QUOTE, [WEIBO["title"], REDDIT["title"]]) is False


def test_s33_sentence_passes_with_the_independent_list():
    assert is_speech_quote(S33_QUOTE, independent_titles([WEIBO, REDDIT])) is True


def test_a_real_reddit_title_is_still_vetoed_either_way():
    """不能把闸拆了：真标题当原声，改前改后都得拦。"""
    assert is_speech_quote(REDDIT["title"], [WEIBO["title"], REDDIT["title"]]) is False
    assert is_speech_quote(REDDIT["title"], independent_titles([WEIBO, REDDIT])) is False


def test_candidate_filter_keeps_the_weibo_sentence_and_drops_the_reddit_title():
    coding = {"quotes": {"rows": [{"原声": S33_QUOTE}, {"原声": REDDIT["title"]}],
                         "basis": "口径。"}}
    dropped = _drop_non_speech_quotes(coding, [WEIBO, REDDIT])
    assert dropped == 1
    assert [r["原声"] for r in coding["quotes"]["rows"]] == [S33_QUOTE]


def test_old_fixture_rows_without_body_still_count_as_titles():
    """既有用例喂的行只有 title 没有正文——按有标题算，旧行为一个字不变。"""
    coding = {"quotes": {"rows": [{"原声": "豆包用着还行"}, {"原声": "写文案是真的快"}],
                         "basis": "口径。"}}
    assert _drop_non_speech_quotes(coding, [{"title": "豆包用着还行"}]) == 1


# ── 尺子 ⑭：按 `title_independent` 筛名单；缺键按 True（老产物行为不变）──────
GOOD = """# 执行摘要

豆包在国内讨论量最大，562 条采集、33 条进入引用[S01]。

本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。

# 关键发现

1. 【A】豆包的正向说法集中在内容生产[S01]

## 豆包的正向说法集中在内容生产，19 条里最密的就是这一类

正文解读[S01]。

> QUOTE
> —— 微博 · 等级 B [S01]

# 论据与数据

| 主题 | 提及条数 |
| --- | --- |
| 价格与付费 | 19 |

来源：主题提及量与极性词命中

# 建议

1. 补一轮小红书精读[S02]

# 附录

样本怎么来的：562 条采集、33 条进入引用。

## 假设与不确定性

本次样本的代表性有限[S01]。
"""


def _ruler(tmp_path: Path, quote: str, sources: list[dict]) -> list[str]:
    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(GOOD.replace("QUOTE", quote), encoding="utf-8")
    tables = tmp_path / "r-t.polished.consulting.tables.json"
    tables.write_text(json.dumps({
        "entities": ["豆包"], "sources": sources, "counts": {"evidence": 562, "cited": 33},
        "tables": {"topic_polarity": {"title": "主题提及量与极性词命中",
                                      "rows": [{"主题": "价格与付费", "提及条数": 19}], "n": 562}},
    }, ensure_ascii=False), encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("[S01][S02]", encoding="utf-8")
    return check_polished.run(md, tables, work)["⑭ 原声是人说的话"]


def _sources(weibo_flag):
    weibo = {"mark": "S01", "title": S33_TITLE, "url": "u", "grade": "B", "crossref": "PASS"}
    if weibo_flag is not None:
        weibo["title_independent"] = weibo_flag
    reddit = {"mark": "S02", "title": REDDIT["title"], "url": "u2", "grade": "A",
              "crossref": "PASS", "title_independent": True}
    return [weibo, reddit]


def test_ruler_is_red_on_s33_when_the_flag_is_missing(tmp_path):
    """老产物（没有 `title_independent`）：全部 title 进名单 = 09-11 那个红，照旧能复现。"""
    assert _ruler(tmp_path, S33_QUOTE, _sources(None))


def test_ruler_turns_green_on_s33_when_the_source_has_no_independent_title(tmp_path):
    assert not _ruler(tmp_path, S33_QUOTE, _sources(False))


def test_ruler_still_rings_on_a_real_title_quote(tmp_path):
    """尺子要还能响：引一条真标题，改后仍红。"""
    assert _ruler(tmp_path, REDDIT["title"], _sources(False))


def test_build_tables_stamps_the_flag_on_every_source():
    """标志在 `build_tables` 算好随源带走——尺子与候选过滤从此看同一份判断。"""
    from app.report.polish.tables import build_tables

    evidence = [dict(WEIBO, citation_no=1, grade="B", platform="weibo", kind="post"),
                dict(REDDIT, citation_no=2, grade="A", platform="reddit", kind="post")]
    view = {"sources": [{"citation_no": 1, "title": WEIBO["title"], "url": "u"},
                        {"citation_no": 2, "title": REDDIT["title"], "url": "u2"}],
            "title": "t"}
    data = build_tables(report={"id": "r-t"}, plan={"goals": []}, evidence=evidence,
                        claims=[], view=view)
    flags = {s["mark"]: s["title_independent"] for s in data["sources"]}
    assert flags == {"S01": False, "S02": True}
