"""§RPT-8：把握度按条分层、内部计数挪走、标题不得大过证据、负向可追溯 + 线索节、建议与证据匹配。

用户 09-18 读第三轮正式稿：「报告开头就写把握度低，目标客户看了会怎么想」。查下来
那句几乎必然是低——判它的规则只看「多数说法是不是单源」，而 1358 条主张里 954 条
结构上就只挂一条证据，于是整篇一刀切，连有 90 条编码评论撑着的那条发现也被劝退。

夹具形状全部抄自真机 r-3b3482ca7f8b×consulting（`sources[].grade/crossref/platform/
same_thread`、`tables.attitude_by_topic` 的 `条数` 与 `marks`）——这几条判词量的是
真稿会出现的形状，不是我造的形状。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.report.polish.run import (CLUES_HEADING, CONFIDENCE_HEADING,
                                   PROGRAM_APPENDIX_HEADINGS, advice_entries, assemble,
                                   attitude_line, attitude_traceability, clues_block,
                                   confidence_line, confidence_mismatch, confidence_tables,
                                   finding_confidence, finding_confidences,
                                   oversized_finding_lines, oversized_shard_title,
                                   overall_confidence, singlesource_advice, stated_confidence,
                                   tiering_available, weakevidence_advice)
from app.report.polish.sharding import Finding, parse_findings
from app.report.polish.tables import _clues

# ── 夹具：真机四条关键发现各自的角标读数 ────────────────────────────────────

#: 第 1 条：两篇公众号（不同帖、同一平台），没有编码格撑着 ⇒ 低。
#: 第 2 条：公众号 + 微博（两平台），编码格只有 5 条 ⇒ 中。
#: 第 3 条：小红书 + 抖音（两平台），编码格 18 条 ⇒ 高。
#: 第 4 条：同一条小红书帖下的两条评论（**一个出处**）⇒ 低——用户读出来的也是这条最弱。
SOURCES = [
    {"mark": "S01", "grade": "B", "crossref": "PASS", "platform": "wechat_mp", "same_thread": []},
    {"mark": "S10", "grade": "B", "crossref": "SINGLE", "platform": "wechat_mp", "same_thread": []},
    {"mark": "S08", "grade": "B", "crossref": "SINGLE", "platform": "wechat_mp", "same_thread": []},
    {"mark": "S31", "grade": "C", "crossref": "PASS", "platform": "weibo", "same_thread": []},
    {"mark": "S19", "grade": "B", "crossref": "PASS", "platform": "xhs", "same_thread": []},
    {"mark": "S36", "grade": "C", "crossref": "PASS", "platform": "douyin",
     "same_thread": ["S39"]},
    {"mark": "S17", "grade": "B", "crossref": "SINGLE", "platform": "xhs",
     "same_thread": ["S18", "S21"]},
    {"mark": "S21", "grade": "B", "crossref": "PASS", "platform": "xhs",
     "same_thread": ["S17", "S18"]},
]

TABLES = {
    "attitude_by_topic": {"n": 90, "rows": [
        {"主题": "功能与能力", "态度": "正", "条数": 18,
         "marks": ["S19", "S27", "S36", "S96"]},
        {"主题": "功能与能力", "态度": "负", "条数": 10, "marks": ["S17", "S21"]},
        {"主题": "价格与付费", "态度": "中", "条数": 5, "marks": ["S04", "S31"]},
        # 兜底格：一格 44 条，但它不是一个主题。⛔ 判把握度不许拿它当「有量撑着」。
        {"主题": "未归主题", "态度": "中", "条数": 44, "marks": ["S01", "S10"]},
    ]},
    "scenario_attitude": {"n": 90, "rows": [
        {"场景": "生活娱乐", "态度": "负", "条数": 9, "marks": ["S17", "S21"]},
        {"场景": "其他", "态度": "负", "条数": 5, "marks": []},
        {"场景": "编程", "态度": "负", "条数": 2, "marks": []},
        {"场景": "情感陪伴", "态度": "负", "条数": 1, "marks": []},
        {"场景": "生活娱乐", "态度": "正", "条数": 27, "marks": ["S19"]},
        {"场景": "生活娱乐", "态度": "中", "条数": 38, "marks": []},
        {"场景": "生活娱乐", "态度": "混合", "条数": 8, "marks": []},
    ]},
    # 只答「在哪谈」不答「夸还是骂」，一格 44 条。⛔ 同样不许拿它抬档位。
    "scenario_counts": {"n": 90, "rows": [
        {"场景": "生活娱乐", "条数": 44, "marks": ["S17", "S19", "S21"]}]},
    "crossref_mix": {"n": 1358, "rows": [
        {"交叉验证结论": "SINGLE", "主张数": 1254}, {"交叉验证结论": "WEAK", "主张数": 34},
        {"交叉验证结论": "PASS", "主张数": 51}, {"交叉验证结论": "CONFLICT", "主张数": 19}]},
}

FINDINGS = [Finding(index=1, text="媒体侧已把豆包定位切到办公入口[S01][S10]",
                    marks=("S01", "S10")),
            Finding(index=2, text="豆包在 2026 年出现付费转向[S08][S31]", marks=("S08", "S31")),
            Finding(index=3, text="用户侧最鲜活的场景是生活娱乐[S19][S36]", marks=("S19", "S36")),
            Finding(index=4, text="负向声音落在语音演唱这一交互点[S17][S21]",
                    marks=("S17", "S21"))]


# ── 货 1：把握度按条分层 ──────────────────────────────────────────────────

def test_货1_四条发现判出三档_同帖两条评论不算两个来源() -> None:
    """⛔ 这条锁的是「不许把低直接改成中/高了事」：判法换了，判出来还是低的仍是低。"""
    tiers = [tier for _, tier, _ in finding_confidences(FINDINGS, SOURCES, TABLES)]
    assert tiers == ["低", "中", "高", "低"], "真机四条应判出三档，不是一刀切"
    # 第 4 条：S17 与 S21 在同一条帖子下（`same_thread`），是一个出处不是两个。
    tier, reason = finding_confidence(("S17", "S21"), SOURCES, TABLES)
    assert tier == "低" and "1 个独立出处" in reason
    # 第 3 条的依据里要看得见那三个读数，读者才查得动。
    _, reason3 = finding_confidence(("S19", "S36"), SOURCES, TABLES)
    for expected in ("2 个独立出处", "2 个平台", "最强等级 B", "18 条评论撑着"):
        assert expected in reason3, reason3


def test_货1_兜底格与场景表不许把档位抬起来() -> None:
    """「未归主题」是没命中任何主题的那一堆，`scenario_counts` 只答在哪谈。

    两处都曾把第 4 条（同帖两条评论）抬成「有 44 条评论撑着」——本包实测踩过两次。
    """
    _, reason = finding_confidence(("S01", "S10"), SOURCES, TABLES)
    assert "没有成规模的评论撑着" in reason, "兜底格被当成了有量撑着"
    _, reason4 = finding_confidence(("S17", "S21"), SOURCES, TABLES)
    assert "功能与能力·负" in reason4 and "44" not in reason4


def test_货1_整篇总括取中位档_偶数条取偏低的那一个() -> None:
    assert overall_confidence(["高", "中", "低", "低"]) == "低"
    assert overall_confidence(["高", "高", "中", "低"]) == "中"
    assert overall_confidence(["高", "中", "低"]) == "中"
    assert overall_confidence([]) == ""
    # 真机四条 低/中/高/低 ⇒ 低。⛔ 不许因为「低不好看」就改成偏高。
    tiers = [t for _, t, _ in finding_confidences(FINDINGS, SOURCES, TABLES)]
    assert overall_confidence(tiers) == "低"


def test_货1_摘要那一行逐条报档并说清怎么汇总() -> None:
    line = confidence_line(TABLES, {"正文实引": 22, "cited": 79}, FINDINGS, SOURCES)
    assert "第 1 条低、第 2 条中、第 3 条高、第 4 条低" in line
    assert "中位档" in line and "「低」" in line, "总括必须说明它是怎么由各条汇总的"
    assert "正文实际引用证据 22 条（引用池共 79 条）" in line
    for word in ("SINGLE", "PASS", "WEAK", "CONFLICT"):
        assert word not in line, "摘要不许出现内部口径词（尺子⑦）"


def test_货1_附录给出逐条依据表_把握度不脱离库读数() -> None:
    block = confidence_tables(TABLES, FINDINGS, SOURCES)
    assert block.startswith(CONFIDENCE_HEADING)
    assert "| 关键发现 | 把握度 | 凭什么是这一档 |" in block
    assert "| 第 3 条 | 高 |" in block and "| 第 4 条 | 低 |" in block
    assert "18 条评论撑着" in block, "依据要落到具体读数上，不是一句形容"


def test_货1_总括与逐条汇总对不上当轮打回() -> None:
    body = ("摘要一段。\n\n" + "\n".join(f.title_line for f in FINDINGS)
            + "\n\n> 本报告结论的把握度为**高**，主要因为证据充分。\n")
    assert stated_confidence(body) == "高"
    problems = confidence_mismatch(body, SOURCES, TABLES)
    assert problems and "写的是「高」" in problems[0] and "是「低」" in problems[0]
    # 写对了就放行。
    assert confidence_mismatch(body.replace("**高**", "**低**"), SOURCES, TABLES) == []


def test_货1_读数不全就一条都不判_不出一张全是低的表() -> None:
    """⛔ 「判不了就别判」：没有已编码 UGC 的稿（纯媒体材料）行为一字不变。"""
    thin = {"crossref_mix": TABLES["crossref_mix"]}
    assert tiering_available(SOURCES, TABLES) and not tiering_available(SOURCES, thin)
    assert not tiering_available([], TABLES)
    assert finding_confidences(FINDINGS, SOURCES, thin) == []
    assert confidence_mismatch("把握度为**高**\n\n1. 发现[S01]", SOURCES, thin) == []
    assert confidence_tables(thin, FINDINGS, SOURCES).count("关键发现 | 把握度") == 0


# ── 货 2：内部计数不进客户稿 ──────────────────────────────────────────────

def test_货2_主张计数不进摘要_整段挪进附录且数字一个没改() -> None:
    line = confidence_line(TABLES, {"正文实引": 22}, FINDINGS, SOURCES)
    for internal in ("主张 1358 条", "单源 1254", "偏弱 34", "多源互证 51", "多源冲突 19"):
        assert internal not in line, f"内部计数还在摘要里：{internal}"
    block = confidence_tables(TABLES, FINDINGS, SOURCES)
    assert "主张共 1358 条。" in block
    for count in ("1254", "34", "51", "19"):
        assert count in block, f"真实数字不许改，{count} 必须还在附录里"
    assert "不等于结论都不可信" in block, "挪过去要配一句人话，否则读者照旧读成不可信"


def test_货2_组装后开篇节里没有内部计数(tmp_path: Path) -> None:
    summary = tmp_path / "a.md"
    summary.write_text("摘要一段。\n\n" + "\n".join(f.title_line for f in FINDINGS)
                       + "\n\n> 本报告结论的把握度为**低**，主要因为……\n\n收尾句。\n",
                       encoding="utf-8")
    appendix = tmp_path / "b.md"
    appendix.write_text("无。\n", encoding="utf-8")
    md = assemble([("执行摘要", summary), ("附录", appendix)], SOURCES, tables=TABLES,
                  appendix_blocks=(confidence_tables(TABLES, FINDINGS, SOURCES),))
    opening = md.split("# 附录")[0]
    assert "主张 1358 条" not in opening and "单源 1254" not in opening
    assert "第 1 条低、第 2 条中、第 3 条高、第 4 条低" in opening
    assert "主张共 1358 条。" in md.split("# 附录")[1], "数要在附录里找得到"


# ── 货 3：标题不得大过证据 ────────────────────────────────────────────────

def test_货3_低把握度的发现不许写普遍化断言() -> None:
    body = ("1. 【B】多数用户认为豆包的负向声音落在语音演唱[S17][S21]\n")
    problems = oversized_finding_lines(body, SOURCES, TABLES)
    assert any("多数用户" in p for p in problems), problems
    # 高把握度那条不受这条管——它的证据撑得起。
    ok = "1. 【B】多数用户把豆包用在生活娱乐[S19][S36]\n"
    assert not any("多数用户" in p for p in oversized_finding_lines(ok, SOURCES, TABLES))


def test_货3_低把握度的发现必须在同一行交代证据规模() -> None:
    """真机第 1 条（两篇公众号，其中一篇自述有利益关系）整行读不出它只有两篇。"""
    bad = "1. 【B】媒体侧已把豆包定位切到办公与生产力入口[S01][S10]\n"
    problems = oversized_finding_lines(bad, SOURCES, TABLES)
    assert any("读不出它的证据有多大" in p for p in problems), problems
    for good in ("1. 【B】两篇公众号文章把豆包划入办公入口[S01][S10]\n",
                 "1. 【B】本轮样本里媒体已把豆包划入办公入口[S01][S10]\n",
                 "1. 【B】同一帖下的评论显示豆包被划入办公入口[S01][S10]\n"):
        assert oversized_finding_lines(good, SOURCES, TABLES) == [], good


def test_货3_展开那一节的二级标题只查普遍化断言_不查规模() -> None:
    weak = Finding(index=4, text="负向声音[S17][S21]", marks=("S17", "S21"))
    text = "## 用户普遍不满意豆包的语音演唱\n\n正文[S17]。\n"
    assert any("普遍" in p for p in oversized_shard_title(text, weak, SOURCES, TABLES))
    # 二级标题里不塞口径行：不带规模词也放行。
    assert oversized_shard_title("## 豆包的负向声音落在语音演唱这一交互点\n", weak,
                                 SOURCES, TABLES) == []
    # 没切片（`finding` 为 None）就不判，退回老行为。
    assert oversized_shard_title(text, None, SOURCES, TABLES) == []


# ── 货 4①：负向可追溯 ────────────────────────────────────────────────────

def test_货4_负向条数要落到可点的角标_点不到的要说出有几条() -> None:
    """真机：摘要写「负 17 条」，全稿只有 2 条点得开。说清楚比藏起来强。"""
    traced = attitude_traceability(TABLES)
    assert "负向 17 条里有 2 条点得到原文（[S17][S21]）" in traced
    assert "其余 15 条本轮没进引用池、正文点不到原文" in traced
    # 摘要那一行自己就带上它——写手不必再写一遍，也没法写错。
    line = attitude_line(TABLES, ["豆包"])
    assert "已编码评论 90 条：正 27 / 负 17 / 中 38 / 混合 8" in line and traced in line


def test_货4_一条都没进池时说清是取数缺口_不是没人这么说() -> None:
    nothing = {"scenario_attitude": {"n": 9, "rows": [
        {"场景": "生活娱乐", "态度": "负", "条数": 9, "marks": []}]}}
    traced = attitude_traceability(nothing)
    assert "一条都没进引用池" in traced and "不是没人这么说" in traced
    assert attitude_traceability({}) == ""
    # 负向为 0 时整句不出，不摆一句「负 0 条里有 0 条」。
    assert attitude_traceability({"scenario_attitude": {"n": 1, "rows": [
        {"场景": "学习", "态度": "正", "条数": 1, "marks": []}]}}) == ""


# ── 货 4②：单人线索节 ────────────────────────────────────────────────────

#: ⚠️ `ev-3`（Windows 输入法那条）**没有 citation_no**——真机实测：用户点名要的那五条
#: 信号，`citation_no` 全是 None，它们是 C 级评论、本轮压根没进引用池。要求带角标
#: 等于把这一货要的东西全筛掉，所以线索**不要求进池**，没进池的写明点不到原文。
CLAIM_ROWS = [{"id": "ev-1", "citation_no": 11, "platform": "hacker_news"},
              {"id": "ev-2", "citation_no": 27, "platform": "weibo"},
              {"id": "ev-3", "citation_no": None, "platform": "xhs"},
              {"id": "ev-4", "citation_no": 40, "platform": "xhs"},
              {"id": "ev-5", "citation_no": 41, "platform": "xhs"}]
CLAIMS = [
    {"verdict": "SINGLE", "firsthand": ["ev-3"], "evidence_ids": ["ev-3"],
     "text": "小红书评论区有用户明确反馈 Windows 端豆包输入法起步晚、体验落后于微信输入法。"},
    {"verdict": "SINGLE", "firsthand": ["ev-2"], "evidence_ids": ["ev-2"],
     "text": "微博有用户自述每天和豆包打视频电话、称「要疯了」。"},
    # 对照实体：§RPT-4 有意加的那道闸，本块照它办。⛔ 不绕过。
    # ⚠️ §SEL-4 起**海外平台不再被这道闸挡**（`ev-1` 是 hacker_news，现在收得进来）——
    # 这一条锁的正是被替换掉的旧语义，改了不是放水，是口径翻案（用户 09-18 拍乙）。
    {"verdict": "SINGLE", "firsthand": ["ev-1"], "evidence_ids": ["ev-1"],
     "text": "Reddit 玩家因游戏素材遗留豆包水印而抱怨开发者偷懒。"},
    {"verdict": "SINGLE", "firsthand": ["ev-4"], "evidence_ids": ["ev-4"],
     "text": "有用户反馈 DeepSeek 的联网检索更稳。"},
    # 不是单源孤证 ⇒ 它撑得起结论，该进正文而不是线索区。
    {"verdict": "PASS", "firsthand": ["ev-5"], "evidence_ids": ["ev-5"],
     "text": "多个来源都提到豆包开始收费。"},
    # 不是亲历：转述的不进。
    {"verdict": "SINGLE", "firsthand": [], "evidence_ids": ["ev-5"],
     "text": "有媒体转述称豆包用户量大涨。"},
    # 自己写着「噪声」的不是线索，是各章留的登记备注。
    {"verdict": "SINGLE", "firsthand": ["ev-5"], "evidence_ids": ["ev-5"],
     "text": "另有一条评论只求头像图，与豆包无直接关联，仅登记为长尾噪声。"},
]


def test_货4_线索只收单源亲历非对照实体_且不要求进引用池() -> None:
    """⚠️ §SEL-4 改了这一条的第 3 条筛法：改前排除「对照实体 + 海外平台」，
    改后**只排除对照实体**。`ev-1` 是 hacker_news 的海外证据，改前被筛掉、改后收进来。
    """
    clues = _clues(CLAIMS, CLAIM_ROWS, contrast_ids={"ev-4"})
    assert [c["mark"] for c in clues] == [None, "S27", "S11"], clues
    assert "Windows 端" in clues[0]["text"] and clues[0]["platform"] == "小红书"
    assert "要疯了" in clues[1]["text"]
    # 海外的那条确实是靠放行进来的，不是靠别的筛法漏进来的。
    assert "Reddit" in clues[2]["text"] and clues[2]["platform"] == "Hacker News"
    # ⛔ 对照实体那一支一字未动：DeepSeek 那条照旧进不来。
    assert not any("DeepSeek" in c["text"] for c in clues)
    # 按主张在库里的登记次序，⛔ 不按字数（最长的那句往往是最啰嗦的那句）。
    assert clues[0]["text"].startswith("小红书评论区有用户明确反馈")


def test_货4_线索节挂附录_说死未经交叉验证且不进关键发现() -> None:
    block = clues_block([{"mark": None, "platform": "小红书",
                          "text": "Windows 端输入法落后于微信输入法。"},
                         {"mark": "S27", "platform": "微博",
                          "text": "每天打视频电话，要疯了。"}])
    assert block.startswith(CLUES_HEADING) and CLUES_HEADING in PROGRAM_APPENDIX_HEADINGS
    assert "未经交叉验证" in CLUES_HEADING and "不是结论" in CLUES_HEADING
    assert "不进关键发现" in block and "也不做建议的依据" in block
    # 摆成表：尺子④ 按行扫数字，表格行整行跳过（线索原文常带数字）。
    assert "| 线索 | 出处 |" in block
    assert "| Windows 端输入法落后于微信输入法。 | 小红书评论 · 本轮未进引用池，点不到原文 |" in block
    assert "| 每天打视频电话，要疯了。 | [S27] |" in block
    # 正文已经引过的不再摆一遍。
    assert "S27" not in clues_block(
        [{"mark": "S27", "text": "每天打视频电话。"}], exclude=[27])
    assert clues_block([]) == ""


def test_货4_封顶封在剔除之后_不许把该露面的挡在名额外() -> None:
    """⛔ 先封顶再剔除会把用户点名要的那几条挤掉——本包实测踩过（8 条筛完只剩 2 条）。"""
    from app.report.polish.tables import CLUE_LIMIT

    pool = [{"mark": f"S{n:02d}", "text": f"线索{n}。"} for n in range(1, CLUE_LIMIT + 4)]
    kept = clues_block(pool, exclude=list(range(1, CLUE_LIMIT + 1)))
    assert kept.count("| 线索") == 4, "剔除之后还该补满名额（表头 1 行 + 3 条）"
    assert f"S{CLUE_LIMIT + 1:02d}" in kept


def test_货4_带内部标记的线索宁可不放() -> None:
    """主张原文是各章 agent 写的，偶尔带 `goal-1`、`ch-3` 这类切块标记。"""
    assert clues_block([{"mark": "S31", "text": "goal-1 的用户说输入法落后。"}]) == ""
    assert clues_block([{"mark": "S31", "text": "见 app/report/polish/run.py 的口径。"}]) == ""


# ── 货 5：建议与证据匹配 ──────────────────────────────────────────────────

ADVICE = [
    "1. **本产品团队把「可验证」设为默认机制**",
    "   依据：同一小红书帖子下的两条评论[S17][S21]。",
    "2. **竞品把主战场放在任务完成得更深**",
    "   依据：生活娱乐 44 条、办公 2 条[S19][S36]。",
]


def test_货5_证据撑不起的建议要降级或写明证据强度() -> None:
    problems = weakevidence_advice(ADVICE, SOURCES, TABLES)
    assert len(problems) == 1 and "1 个独立出处" in problems[0], problems
    assert "值得进一步验证的方向" in problems[0] and "把握度：低" in problems[0]
    # 明说了证据弱就放行——共用规则要的是别让读者以为它很硬，不是不许提。
    stated = [ADVICE[0], "   依据：同一小红书帖子下的两条评论[S17][S21]；把握度：低（单帖）。",
              *ADVICE[2:]]
    assert weakevidence_advice(stated, SOURCES, TABLES) == []


def test_货5_与旧闸分工不打架_同一批条目同一个切法() -> None:
    """`singlesource_advice` 管交叉验证结论，本条管证据强度；两道闸切的是同一批条目。"""
    lines = [*ADVICE, "### 值得进一步验证的方向", "3. **核一核办公场景**",
             "   依据：[S17][S21]。"]
    # 降级区之后两道闸都不管——切法是同一个 `advice_entries`。
    assert len(advice_entries(lines)) == 2
    assert all("核一核办公场景" not in p for p in weakevidence_advice(lines, SOURCES, TABLES))
    crossref = {int(s["mark"][1:]): s["crossref"] for s in SOURCES}
    assert all("核一核办公场景" not in p for p in singlesource_advice(lines, crossref))
    # 全是孤证的那一条两道都会命中，判词各说各的那一面，改法都是降级，不冲突。
    only_single = ["1. **建议**", "   依据：[S10][S08]。"]
    assert singlesource_advice(only_single, crossref)
    assert weakevidence_advice(only_single, SOURCES, TABLES)


def test_货5_编号之前那一段不是建议_不判它() -> None:
    """`advice_entries` 会把第一条编号之前的引子也归成一条，那不是一条建议。"""
    lines = ["- **对竞品团队**：差异化不宜只比聊天参数[S17][S21]。", *ADVICE]
    assert len(advice_entries(lines)) == 3
    assert all("对竞品团队" not in p for p in weakevidence_advice(lines, SOURCES, TABLES))


# ── 验收尺子 ⑱⑲⑳：判词函数与写作期门禁共用同一批 ────────────────────────

_RULER_MD = """# 执行摘要

摘要一段[S19]。

1. 【B】媒体侧已把豆包定位切到办公与生产力入口[S01][S10]
2. 【B】两篇媒体文章显示豆包在 2026 年出现付费转向[S08][S31]
3. 【B】本轮样本里用户侧最鲜活的场景是生活娱乐[S19][S36]
4. 【B】同一个帖子下的两条评论都指向语音演唱[S17][S21]

> 本报告结论的把握度为**高**，主要因为证据充分。

# 关键发现

## 用户普遍把豆包当办公入口

正文[S01][S10]。

## 豆包以三级订阅把办公能力推向付费

正文[S08][S31]。

## 用户的鲜活记忆来自生活娱乐

正文[S19][S36]。

## 负向声音落在语音演唱这一交互点

正文[S17][S21]。

# 论据与数据

## 四个对手各有能力标签

正文[S19]。

# 建议

1. **本产品团队把「可验证」设为默认机制**
   依据：回指第 4 条发现[S17][S21]。

# 附录

## 方法与样本

正文。

## 假设与不确定性

正文。
"""


def _ruler(tmp_path: Path, markdown: str) -> dict[str, list[str]]:
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts/acceptance/rpt1"))
    import check_polished

    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(markdown, encoding="utf-8")
    tables = tmp_path / "r-t.polished.consulting.tables.json"
    tables.write_text(json.dumps({
        "entities": ["豆包"], "counts": {}, "sources": SOURCES, "tables": TABLES,
    }, ensure_ascii=False), encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("".join(f"[{s['mark']}]" for s in SOURCES), encoding="utf-8")
    return check_polished.run(md, tables, work)


def test_尺子_三条新判据各自判红_判词与写作期门禁同源(tmp_path: Path) -> None:
    findings = _ruler(tmp_path, _RULER_MD)
    assert any("写的是「高」" in p for p in findings["⑱ 把握度按条分层"])
    # ⑲ 两半都要量得到：摘要那条发现行（第 1 条读不出规模）+ 二级标题的普遍化断言。
    titles = findings["⑲ 标题不得大过证据"]
    assert any("读不出它的证据有多大" in p for p in titles), titles
    assert any("普遍" in p for p in titles), titles
    assert any("撑不起一句行动建议" in p for p in findings["⑳ 建议与证据强度匹配"])


def test_尺子_写对了就全过_三条不误伤(tmp_path: Path) -> None:
    good = (_RULER_MD
            .replace("1. 【B】媒体侧已把豆包定位切到办公与生产力入口",
                     "1. 【B】两篇公众号文章把豆包划进办公与生产力入口这一档")
            .replace("> 本报告结论的把握度为**高**，主要因为证据充分。",
                     "> 本报告结论的把握度为**低**，主要因为四条里两条还立不住。")
            .replace("## 用户普遍把豆包当办公入口", "## 两篇媒体把豆包划进办公入口这一档")
            .replace("   依据：回指第 4 条发现[S17][S21]。",
                     "   依据：回指第 4 条发现[S17][S21]；把握度：低（只有同帖两条评论）。"))
    findings = _ruler(tmp_path, good)
    for name in ("⑱ 把握度按条分层", "⑲ 标题不得大过证据", "⑳ 建议与证据强度匹配"):
        assert findings[name] == [], (name, findings[name])
    # 老的 ⑰ 条一条都不许被本包带红。
    assert all(not v for k, v in findings.items()
               if k not in ("⑱ 把握度按条分层", "⑲ 标题不得大过证据", "⑳ 建议与证据强度匹配")), \
        {k: v for k, v in findings.items() if v}


def test_尺子_读数不全时三条都不判_老形态的稿行为不变(tmp_path: Path) -> None:
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts/acceptance/rpt1"))
    import check_polished

    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(_RULER_MD, encoding="utf-8")
    tables = tmp_path / "r-t.polished.consulting.tables.json"
    tables.write_text(json.dumps({
        "entities": ["豆包"], "counts": {}, "sources": SOURCES,
        "tables": {"crossref_mix": TABLES["crossref_mix"]},   # 没有已编码 UGC
    }, ensure_ascii=False), encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("".join(f"[{s['mark']}]" for s in SOURCES), encoding="utf-8")
    findings = check_polished.run(md, tables, work)
    for name in ("⑱ 把握度按条分层", "⑲ 标题不得大过证据", "⑳ 建议与证据强度匹配"):
        assert findings[name] == [], name


# ── 规则也要锁：纯文档不锁会漂（§D-083 的教训） ──────────────────────────

def test_规则_旧的一刀切判法在三份模板里都已废止() -> None:
    """⛔ 「多数说法只有单一来源时把握度就写低」——这句必须从模板里消失。

    §RPT-8 的病根就在它：本项目语料上它几乎恒真，于是每份稿开篇都劝读者别信。
    """
    from app.report.polish.skills import get_template, shared_rules

    rules = shared_rules()
    assert "多数说法只有单一来源时，把握度就写「低」" not in rules
    for name in ("consulting", "sentiment-brief", "competitor-matrix"):
        body = get_template(name).body
        assert "多数说法只有单一来源时，把握度就写「低」" not in body, name
        assert "那个档位不是你定的" in body, f"{name} 没写清档位由程序按每条发现算"
        assert "标题不得大过证据" in body, f"{name} 缺货 3 的标题规矩"


def test_规则_共用规则写清三个读数与三档的界() -> None:
    from app.report.polish.skills import shared_rules

    rules = shared_rules()
    assert "那个档位不是你定的，是程序按每条发现算出来的" in rules
    for reading in ("几个互相独立的出处", "证据等级", "那一格有多少条"):
        assert reading in rules, reading
    assert "整篇那一句取各条的中位档" in rules
    # 货 3：可照抄的降级写法要真的给出来（提货单原话「并给可照抄的降级写法」）。
    assert "可照抄的降级写法" in rules
    assert "两篇公众号文章**把豆包划进" in rules
    # 货 4：负向可追溯 + 线索节归附录、不混进关键发现。
    assert "写了「负 N 条」就要让人点得到" in rules
    assert "单人线索归附录那一节，⛔ 不许混进关键发现" in rules
    # 货 5：与旧闸的分工写清楚，不打架——共用规则写分工，咨询体写两条出路。
    assert "还有第二道，跟上面这道分工不重叠" in rules
    assert "上面那道看的是**交叉验证结论**" in rules and "第二道看的是**证据强度**" in rules
    assert "两道闸切的是同一批**编号条目**" in rules
    from app.report.polish.skills import get_template

    body = get_template("consulting").body
    assert "两道闸，分工不重叠" in body and "把握度：低（理由）" in body


# ── 判据 4：读数必须真的取得到 ────────────────────────────────────────────

def test_判据4_三个读数在真机_tables_json_里都取得到() -> None:
    """⛔ 不许要求一个取不到的读数（本项目吃过多次亏）。

    这条量的是**真机产物**：`var/rpt8/` 下那份第三轮成稿的 tables.json。
    文件不在就跳过（别的 worktree 跑用例时不该因为缺一份真机产物而红）。
    """
    real = Path(__file__).resolve().parents[1] / "var/rpt8" \
        / "r-3b3482ca7f8b.polished.consulting.tables.json"
    if not real.is_file():
        import pytest
        pytest.skip(f"真机产物不在这棵树上：{real}")
    data = json.loads(real.read_text(encoding="utf-8"))
    sources, tables = data["sources"], data["tables"]
    assert all(s.get("grade") for s in sources), "每条源要有等级"
    assert all(s.get("crossref") for s in sources), "每条源要有交叉验证结论"
    assert tables["attitude_by_topic"]["n"] == 90, "编码表要有 n"
    assert tiering_available(sources, tables)
    md = (real.parent / "r-3b3482ca7f8b.polished.consulting.md").read_text(encoding="utf-8")
    tiers = finding_confidences(parse_findings(md.split("# 关键发现")[0]), sources, tables)
    assert [t for _, t, _ in tiers] == ["低", "中", "高", "低"], tiers
