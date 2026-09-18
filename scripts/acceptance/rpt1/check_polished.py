#!/usr/bin/env python3
"""§RPT-1 货 2 尺子：核一份正式稿是否合规。

    python3 scripts/acceptance/rpt1/check_polished.py <正式稿.md> <tables.json> <工作稿>

十条判据（全过才 PASS）：
  ① 正文零内部词（goal- / sec- / ch- / 本片 / 本节样本 …）
  ② 一级标题 ⊇ 模板 SKILL.md 声明的 sections
  ③ 角标 ⊆ 工作稿信息源池
  ④ 正文每个数字能在 tables.json 里找到，或同句带角标
  ⑤ 行动式标题带量级（弱检：有数字或程度词）
判据落在产物上，不落在日志上：读的是成稿文件本身。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import (ADVICE_SECTION_UNKNOWN_AUDIENCE,  # noqa: E402
                                   IMPLICATIONS_SECTION)
from app.report.polish.run import ADVICE_SECTIONS as _RUN_ADVICE_SECTIONS  # noqa: E402
from app.report.polish.run import DOWNGRADE_HEADING as _RUN_DOWNGRADE_HEADING  # noqa: E402
from app.report.polish.skills import load_templates  # noqa: E402

#: 内部词：读者不知道也不需要知道研究是怎么切块的，也不需要知道库长什么样。
#: 后半截（表名 / 字段名 / 代码路径）是用户 2026-09-05 读稿后加的（裁决条 3）——
#: 首稿写了「来源：见 topic_polarity」「词表见 app/report/polish/lexicon.py」。
FORBIDDEN = (r"goal-\d", r"sec-\d", r"ch-\d", "本片", "本节样本", "本章样本", "上游目标",
             "采集章", "撰写章",
             "topic_polarity", "entity_mentions", "grade_mix", "crossref_mix", "platform_mix",
             "entity_dimension", "timeline", "citation_no", "evidence.platform", "reports.extra",
             r"[\w/.-]+\.py",
             # §RULE-1 货 1（评审 #9）：抓取时间是工作稿的证据契约，不是给读者的。
             # 实测竞品稿正文里 `（fetched_at: 2026-09-06T15:25:58+08:00）` 出现 38 次，
             # 同一份稿里还有五种不同时间，正文自己都在解释「这不是抓取时间」。
             # 合法落点只剩信息源清单那一列（程序填），所以本条只查写手写的部分。
             "fetched_at", "抓取时间", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
             # §D-072 货 2：**内部角色名**这一类此前一条都没收，所以 09-15 那份咨询体
             # 正式稿附录「各表口径」里的「包终端复核 30 条一致 28 条」17 条判据全过
             # 却照样漏给客户看（串来自 `app/reliability/coding.py` 写死的文案）。
             # 前半截是本项目的干活角色，后半截是本项目的流程件——客户读不懂，
             # 读懂了也只会看见「这家在给我看他们内部怎么排班」。
             "包终端", "调度会话", "提货单", "奏折", "哨兵")
#: 裁决条 1：带这些评价词的句子里必须写出被评的是谁。
JUDGEMENT_WORDS = ("偏浅", "偏弱", "不足", "较差", "套路化", "敷衍", "不够", "有限",
                   "偏低", "偏高", "薄弱", "欠缺")
#: ⑥ 的情态用法：「不足以 / 不够以 + 动词」说的是「撑不起某个结论」，不是在评价谁。
#: 依据 r-b10812f664d2×consulting 第 194 行「适合启动诊断，不足以直接支持全面改版」。
MODAL_NOT_ENOUGH = re.compile(r"(?:不足|不够)以")
#: ⑥ 的否定/劝诫用法：句子在说「不能这么解释」，评价词是被否定掉的那个读法。
#: 依据 r-3e04f808dffd×competitor-matrix 第 195 行「也不能把空白格解释为产品能力较差」。
NEGATED_READING = re.compile(r"不(?:表示|等于|意味着|代表|能把|应把|该把)")
#: 链接不是句子：剥掉链接后没中文的「句」是引文行，不上 ⑥；④ 也不从链接里取数。
#: 依据 r-3e04f808dffd×competitor-matrix 第 64 行被切出的 `(https://m.weibo.cn/…` 片段，
#: 与 r-b10812f664d2×sentiment-brief 第 65/71/77 行「来源链接：https://…」。
URL_IN_TEXT = re.compile(r"https?://\S+")
CJK = re.compile(r"[\u4e00-\u9fff]")
#: 裁决条 2：方法论口径词不许进开篇节；开篇节必须给一句人话把握度。
METHOD_WORDS = ("SINGLE", "PASS", "WEAK", "CONFLICT")
CONFIDENCE_WORD = "把握度"
#: 各模板的开篇节（摘要位）与建议节、降级节。尺子按模板认，不写死一套标题。
OPENING_SECTIONS = frozenset({"执行摘要", "总体倾向"})
#: §RPT-2 货 4 ①：读者身份「不明」时 run.py 把「建议」改名成「对不同读者的含义」，
#: 尺子要认这个别名——否则改名后判据 ② 误报「缺『建议』」，建议门禁还会
#: 因为按模板名找不到节而静默放行（假绿）。
#: §RPT-2 货 4 ②：竞品对比稿的「对提问方意味着什么」写的也是「凭这些证据你该怎么看」，
#: 同样受「弱证据不撑强建议」的门禁管——它和「建议」会同时出现，门禁要两节都过一遍。
#: §WRITE-1 货 4：节名与降级区标题都改从 `run.py` 取——生产侧门禁按同一份名单认节，
#: 两处各写一份的话，加一个模板节名只改一处就是静默漏查。
ADVICE_SECTIONS = _RUN_ADVICE_SECTIONS
DOWNGRADE_HEADING = _RUN_DOWNGRADE_HEADING
#: §RPT-2 货 2 闸 ⑨：主体节（关键发现、正/负/争议/诉求、对比总览…）只讲调研对象。
#: 「主体节」= 模板声明的节里去掉开篇、建议、集中列表、时间线与附录剩下的那些——
#: 这样加模板不用回来改尺子。
NON_FINDING_SECTIONS = (OPENING_SECTIONS | ADVICE_SECTIONS
                        | frozenset({"论据与数据", "时间线", "附录"}))
#: 取数口径词。09-05 首稿的第一条关键发现标题就是「小红书 296 条采集里 0 条被最终引用」，
#: 配一张平台 × 采集条数 × 被引条数的表——最显眼的位置给了工具的自我检讨。
PIPELINE_WORDS = ("被引", "采集条数", "采集量", "采集总数")
#: §RULE-1 货 2/货 3（评审 #10/#11）：限定词与「假设与不确定性」小节。
#: 实测竞品稿两万字里这四个词出现 40 次、该小节出现 5 处，内容大同小异——
#: 读者读到第三遍开始跳过，免责说满了等于一句也没说。
#: §RPT-6：词表与上限**下放给生产侧**（`sharding`），这里 import 回来，值一个没改。
#: 合并器要按同一把尺子收跨片重复的限定；两处各写一份的话，尺子量的与合并器收的
#: 就不是同一件事了（本项目现形过的一种假绿）。
from app.report.polish.sharding import HEDGE_PER_SECTION, HEDGE_WORDS  # noqa: E402
UNCERTAINTY_HEADING = "假设与不确定性"
APPENDIX_SECTION = "附录"
#: 句子切分：中文句号/问号/叹号/分号与换行都算一句到头。
SENTENCE_SPLIT = re.compile(r"[。！？；\n]")
#: ⑤ 的程度词：没数字时至少要有一个判断的力度。含对比与转折——「A 在 X 不在 Y」
#: 是最典型的行动式标题句式，早先漏收，把三个合格标题误判成红（09-05 首稿实测）。
DEGREE_WORDS = ("最", "更", "近半", "过半", "多数", "少数", "普遍", "集中", "聚焦", "远",
                "几乎", "全部", "唯一", "首", "领先", "落后", "不足", "超过", "翻倍", "零",
                "不在", "而非", "不是", "并非", "相比", "相较", "统一", "形成", "转向",
                "推向", "延展", "扩张", "缺少", "缺乏", "撑不起", "主要", "仅", "只",
                "未", "没有", "反而", "却")
#: ⑤ 不管这些一级节里的小标题——它们的措辞是模板自己规定的（附录四件事、
#: 建议段的「值得进一步验证的方向」），要求它们写成结论句是尺子越界。
#: 后两个是 §RPT-2 货 4 的节名，本包先收进豁免名单——加名字只会让 ⑤ 少查一节，
#: 不可能把任何一格判红（这个集合只在 `_body_subheadings` 里做排除）。
STRUCTURAL_SECTIONS = frozenset({
    "论据与数据", "建议", "附录", "需要回应的点", "谁强在哪", "对比总览", "时间线",
    "对不同读者的含义", "对提问方意味着什么", IMPLICATIONS_SECTION,
})
MARK = re.compile(r"\[S(\d{2,})\]")
#: 附录的信息源清单里角标是裸写的（`S01｜A 级｜…`），也得当角标认，
#: 否则 ④ 会把 `S01` 里的 `01` 当成一个没出处的数字。
MARK_ANY = re.compile(r"\[?S(\d{2,})\]?")
NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
#: 日期、版本号、列表序号这些不是「论据数字」，不纳入 ④。
SKIP_NUMBER_CONTEXT = re.compile(r"^\s*\d+[.)、]\s|20\d\d[-年]\d|v\d")
#: 标准编号（「字母串 + 空格 + 数字」）是名字的一部分，不是论据数字。
#: 依据 r-b10812f664d2×sentiment-brief 第 34 行与 r-3e04f808dffd×competitor-matrix
#: 第 173 行的「符合 ISO 8601 的抓取时间」——被拆出 8601 判红。
STANDARD_CODE = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,9}\s+\d+(?:-\d+)?")
#: ``` 围栏里是图表源码不是正文，轴刻度不是论据数字。
#: 依据 r-b10812f664d2×sentiment-brief 第 99 行 mermaid 的 `y-axis "证据条数" 0 --> 80`。
FENCE = re.compile(r"^\s*```")


def _numbers_from(node: object, out: set[str]) -> None:
    """把 tables.json 里出现过的数收进白名单，比率额外收它的百分号写法。"""
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        out.add(_fmt(node))
        if isinstance(node, float) and 0 < node <= 1:
            out.update({_fmt(round(node * 100, 2)), _fmt(round(node * 100, 1)),
                        _fmt(round(node * 100))})
        return
    if isinstance(node, str):
        out.update(NUMBER.findall(node))
    elif isinstance(node, dict):
        for key, value in node.items():
            out.update(NUMBER.findall(str(key)))
            _numbers_from(value, out)
    elif isinstance(node, list):
        for item in node:
            _numbers_from(item, out)


def _fmt(value: float) -> str:
    text = f"{value:.10f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return text or "0"


def _sections_of(markdown: str, level: int) -> list[str]:
    prefix = "#" * level + " "
    return [line[len(prefix):].strip() for line in markdown.splitlines()
            if line.startswith(prefix)]


def _body_subheadings(markdown: str) -> list[str]:
    """要求写成行动式标题的二级标题：结构节（附录/建议等）底下的一概不算。"""
    picked, current = [], ""
    for line in markdown.splitlines():
        if line.startswith("# "):
            current = line[2:].strip()
        elif line.startswith("## ") and current not in STRUCTURAL_SECTIONS:
            picked.append(line[3:].strip())
    return picked


def _template_for(markdown: str, tables_path: Path):
    """模板名从 tables.json 的文件名里认：<id>.polished.<模板>.tables.json。"""
    name = tables_path.name.split(".polished.", 1)[-1].removesuffix(".tables.json")
    for template in load_templates():
        if template.name == name:
            return template
    raise SystemExit(f"× 认不出模板 {name!r}（文件名要形如 <id>.polished.<模板>.tables.json）")


#: 程序生成的信息源清单（`run.sources_table`）。它的表头就带「抓取时间」，
#: 而 ① 禁的是**写手写的**正文——尺子拿自己生成的文本判自己红是假红。
#: 只切尾巴，不改前面的行号。
SOURCES_TABLE_HEADING = "## 信息源清单"


def writer_text(markdown: str) -> str:
    """成稿里写手负责的那部分：砍掉文末程序生成的信息源清单。"""
    cut = markdown.rfind("\n" + SOURCES_TABLE_HEADING)
    return markdown if cut < 0 else markdown[:cut + 1]


def check_no_internal_words(markdown: str) -> list[str]:
    hits = []
    body = writer_text(markdown)
    for pattern in FORBIDDEN:
        for match in re.finditer(pattern, body):
            line = body[:match.start()].count("\n") + 1
            hits.append(f"第 {line} 行命中内部词 {match.group(0)!r}")
    return hits


def resolved_sections(markdown: str, template) -> list[str]:
    """模板骨架落到这一稿上的实际标题。

    只有一处会变名：读者身份「不明」时，`run.sections_for` 把建议节改成
    「对不同读者的含义」。尺子按稿子里实际有哪个来认，不按模板写死。
    """
    found = set(_sections_of(markdown, 1))
    return [ADVICE_SECTION_UNKNOWN_AUDIENCE
            if (name in ADVICE_SECTIONS and name not in found
                and ADVICE_SECTION_UNKNOWN_AUDIENCE in found)
            else name
            for name in template.sections]


def check_sections(markdown: str, template) -> list[str]:
    found = set(_sections_of(markdown, 1))
    return [f"缺一级标题 {s!r}" for s in resolved_sections(markdown, template)
            if s not in found]


def check_marks_in_pool(markdown: str, pool: set[int]) -> list[str]:
    used = {int(n) for n in MARK_ANY.findall(markdown)}
    offpool = sorted(used - pool)
    problems = [f"角标 S{n:02d} 不在信息源池里" for n in offpool]
    if not used:
        problems.append("全文一个角标都没有")
    return problems


def check_numbers(markdown: str, allowed: set[str]) -> list[str]:
    problems = []
    fenced = False
    for index, line in enumerate(markdown.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue  # 图表源码不是正文
        if line.startswith("|") or line.startswith(">") or SKIP_NUMBER_CONTEXT.search(line):
            continue  # 表格行、原文引用、列表序号不纳入
        # 数字不从链接里取：permalink 里的 5338737804574917、44000000001702 之类
        # 是 id 不是论据（依据 b108×sentiment-brief 第 65/71/77 行「来源链接：…」）。
        naked = STANDARD_CODE.sub("", URL_IN_TEXT.sub("", MARK_ANY.sub("", line)))
        for number in NUMBER.findall(naked):
            if number in allowed or number.rstrip("%") in allowed:
                continue
            if MARK_ANY.search(line):
                continue  # 同句带角标 = 有出处
            problems.append(f"第 {index} 行的数字 {number!r} 在数据表里找不到、同句也没角标")
    return problems


#: 话题式标题：短名词短语 + 「分析/说明/概况…」这类壳子词收尾。
#: 早先靠「有没有命中程度词」反着判，连着冤枉了五个真结论句
#: （「豆包的口碑呈…双面结构」「Kimi 押…豆包押…」之类），
#: 于是改成**正面认话题式标题**——要抓的本来就是「XX 分析」这一种，
#: 不是去穷举结论句的所有写法。
TOPIC_TITLE = re.compile(
    r"^[^，。：；、,;!?—…]{2,14}(分析|说明|概况|情况|对比|介绍|综述|汇总|一览|数据|结果|概述)$")


def check_action_titles(markdown: str, template) -> list[str]:
    problems = []
    for title in _body_subheadings(markdown):
        if title in template.sections:
            continue
        stripped = re.sub(r"^[一二三四五六七八九十\d]+[、.．]\s*", "", title).strip()
        if TOPIC_TITLE.match(stripped):
            problems.append(f"二级标题 {title!r} 是话题式标题，不是一句结论")
    return problems


def _section_bodies(markdown: str) -> dict[str, list[str]]:
    """一级标题 → 该节的正文行（含二级标题原文，建议段的降级小标题要认得出）。"""
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        matched = re.match(r"^# +(.+)$", line)
        if matched:
            current = matched.group(1).strip()
            bodies.setdefault(current, [])
        elif current is not None:
            bodies[current].append(line)
    return bodies


def check_judgement_has_subject(markdown: str, entities: list[str]) -> list[str]:
    """裁决条 1：带评价词的句子里要写出被评的是谁（实体名，或「本报告/本次样本」这类）。"""
    # 被评对象不一定是产品：口径段里评的是语料本身（「带立场标注的主张不足…」），
    # 那些句子主语写得很清楚，尺子不该拿它们开刀（09-05 二稿实测误报 5 处）。
    # 「材料/评论/反馈/说法/用户/受访/支撑」同属这一类被评对象，09-06 九格里被冤枉了
    # 一批（如 r-045acebc352b×sentiment-brief 第 29 行「后者所在材料同时包含相反评价」、
    # r-3e04f808dffd×consulting 第 175 行「对长期口碑演变的支撑有限」）。
    subjects = [*entities, "本报告", "本次样本", "本轮", "本批", "样本", "证据池",
                "证据", "主张", "语料", "数据", "口径", "覆盖", "来源", "样本量", "结论",
                "材料", "评论", "反馈", "说法", "用户", "受访", "支撑"]
    problems = []
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.startswith(("|", "#", ">")):
            continue
        if not CJK.search(URL_IN_TEXT.sub("", line)):
            continue  # 整行只是一条链接
        for sentence in SENTENCE_SPLIT.split(MARK_ANY.sub("", line)):
            probe = URL_IN_TEXT.sub("", sentence)
            if not CJK.search(probe) or NEGATED_READING.search(probe):
                continue
            hit = next((w for w in JUDGEMENT_WORDS if w in MODAL_NOT_ENOUGH.sub("", probe)), None)
            if hit and not any(name and name in sentence for name in subjects):
                problems.append(f"第 {index} 行「{hit}」没写清是在说谁：{sentence.strip()[:40]}")
    return problems


def check_opening_section(markdown: str, template) -> list[str]:
    """裁决条 2：开篇节零方法论术语，且必须给一句人话把握度。"""
    bodies = _section_bodies(markdown)
    opening = next((name for name in template.sections if name in OPENING_SECTIONS), None)
    if opening is None:
        return []
    if opening not in bodies:
        return [f"缺开篇节「{opening}」"]
    text = "\n".join(bodies[opening])
    problems = [f"开篇节出现内部口径词 {w}" for w in METHOD_WORDS if w in text]
    if CONFIDENCE_WORD not in text:
        problems.append(f"开篇节没有那句「本报告结论的{CONFIDENCE_WORD}为…，主要因为…」")
    return problems


def check_advice_gate(markdown: str, template, crossref: dict[int, str]) -> list[str]:
    """裁决条 4：一条建议所引角标若全是单源孤证，必须降级到「值得进一步验证的方向」。"""
    bodies = _section_bodies(markdown)
    return [p for name in resolved_sections(markdown, template)
            if name in ADVICE_SECTIONS and name in bodies
            for p in _advice_entry_problems(bodies[name], crossref)]


def _opening_body(markdown: str, template) -> str:
    """开篇节（执行摘要 / 总体倾向）的正文。⑱⑲ 都只量这一节。"""
    bodies = _section_bodies(markdown)
    opening = next((name for name in template.sections if name in OPENING_SECTIONS), None)
    return "\n".join(bodies.get(opening) or []) if opening else ""


def check_confidence_tiers(markdown: str, template, sources, tables) -> list[str]:
    """§RPT-8 ⑱：那句总括把握度要与逐条关键发现汇总出来的档一致。

    判词函数与写作期门禁是**同一个** `run.confidence_mismatch`——同一个概念两处
    两个定义，是本项目现形过的一种假绿（⑧ 与 §WRITE-1 货 4 同一条路）。
    """
    from app.report.polish.run import confidence_mismatch

    return confidence_mismatch(_opening_body(markdown, template), sources, tables)


def check_title_within_evidence(markdown: str, template, sources, tables) -> list[str]:
    """§RPT-8 ⑲：把握度低的那几条发现，标题不得大过证据。

    摘要那几条发现行用 `run.oversized_finding_lines`；展开那几节的二级标题按
    **出现次序**对上发现（切片本来就是按次序切的），条数对不上就只判发现行——
    对不上时硬猜一个映射，判词会指错条，比不判更费事。
    """
    from app.report.polish.run import Finding, oversized_shard_title, oversized_finding_lines
    from app.report.polish.sharding import parse_findings

    opening = _opening_body(markdown, template)
    problems = list(oversized_finding_lines(opening, sources, tables))
    findings = parse_findings(opening)
    titles = _findings_subheadings(markdown, template)
    if findings and len(titles) == len(findings):
        for finding, title in zip(findings, titles):
            problems += oversized_shard_title(
                f"## {title}", Finding(finding.index, finding.text, finding.marks),
                sources, tables)
    return problems


def _findings_subheadings(markdown: str, template) -> list[str]:
    """「关键发现」那一节下的二级标题，按出现次序。模板没有这一节就返回空表。"""
    section = next((n for n in template.sections if n == "关键发现"), None)
    if section is None:
        return []
    return [line[3:].strip() for line in _section_bodies(markdown).get(section, [])
            if line.startswith("## ")]


def check_advice_evidence_strength(markdown: str, template, sources, tables) -> list[str]:
    """§RPT-8 ⑳：建议只能从够格的发现推；不够格的要么降级、要么写明证据强度。

    与 ⑧ 分工不重叠：⑧ 看交叉验证结论，本条看证据强度。两条切的是同一批编号条目
    （`run.advice_entries`），判词函数同样与写作期门禁共用（`run.weakevidence_advice`）。
    """
    from app.report.polish.run import weakevidence_advice

    bodies = _section_bodies(markdown)
    return [p for name in resolved_sections(markdown, template)
            if name in ADVICE_SECTIONS and name in bodies
            for p in weakevidence_advice(bodies[name], sources, tables)]


def _advice_entry_problems(lines: list[str], crossref: dict[int, str]) -> list[str]:
    """判据函数与生产侧门禁是**同一个** `run.singlesource_advice`。

    §WRITE-1 货 4：这道门禁原先只在验收这一头查，查出来时整轮 37.6 分钟已经付掉了；
    现在生产侧写完一节当场查，尺子这边留一个薄壳继续查成稿（两处口径不许分家）。
    """
    from app.report.polish.run import singlesource_advice

    return singlesource_advice(lines, crossref)

#: §WRITE-1 货 5：**否定掉的推及不是推及。** 成稿第 270 行原文是
#: 「…属多源互证；但事件本身单一场景，**尚不能外推为**国内用户普遍关注」——
#: 这是限定句，是 §5 门禁**要求**写手写的那种话，尺子却只匹配「用户普遍」四字，
#: 没看见前面的「尚不能外推」，把守规矩的句子判成了违规。
#: 「量出的异常是尺子的」第七次现形——⛔ 改尺子不改稿。
#:
#: 只认**同一小句里、出现在违禁词之前**的否定：写在后面的不算
#: （「用户普遍不满意」照旧是推及全网的断言，只是断的是负面）。
NEGATED_EXTRAPOLATION = re.compile(
    r"不能外推|不可外推|不宜外推|不足以|不代表|不等于|不意味着|不能说明|不能读作|"
    r"无法据此|并不能|尚不|还不能|谈不上|算不上|不是说")
#: 小句的边界。整句切太粗：「…属多源互证；但事件本身单一场景，尚不能外推为…」
#: 里两个分句一个是断言一个是限定，按整行判会互相盖住。
_CLAUSE_BREAKS = "。！？；\n"


def _clause_before(line: str, position: int) -> str:
    """违禁词所在的那一小句里，它前面的那一截。"""
    head = line[:position]
    cut = max(head.rfind(char) for char in _CLAUSE_BREAKS)
    return head[cut + 1:]


def _is_negated(line: str, offender: str, occurrence: int) -> bool:
    """这一次出现是不是被否定掉的。同一行里同一个词出现多次时逐次判。"""
    start = -1
    for _ in range(occurrence + 1):
        start = line.find(offender, start + 1)
        if start < 0:
            return False
    return bool(NEGATED_EXTRAPOLATION.search(_clause_before(line, start)))


def check_ratio_phrases(markdown: str) -> list[str]:
    """§CODE-1：编码是模型判断，正式稿只能写条数，不能推及全网（用户 09-05 拍甲）。

    表格行不参与——表里的占比是数据本身，不是写手的断言。
    被否定掉的那些也不参与（§WRITE-1 货 5），见 `NEGATED_EXTRAPOLATION`。
    """
    from app.reliability.coding import ratio_phrase_offenders

    problems = []
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.startswith("|"):
            continue
        seen: dict[str, int] = {}
        for offender in ratio_phrase_offenders(line):
            occurrence = seen.get(offender, 0)
            seen[offender] = occurrence + 1
            if _is_negated(line, offender, occurrence):
                continue
            problems.append(
                f"第 {index} 行的 {offender!r} 把编码结果说成了全网比例；"
                "编码是模型判断，只能写「N 条里 M 条编码为正向」")
    return problems


def _without_source_footnote(line: str, titles: Sequence[str]) -> str:
    """把「来源：<中文表名>」这段溯源脚注剥掉再判。

    §RULE-1 外加一条（调度 09-07 拍乙）：违禁词的本意是**禁止正文做管道自我检讨**，
    而「来源：各平台采集量与被引量对照」是溯源脚注——恰恰是 §6.5.3 要求写的那一行。
    只剥「来源：」后面**真的是某张表的中文名**的那一段：改表名常量（甲）面太大，
    让写手换说法（丙）与「按中文表名交给写手」的既有裁决打架。
    """
    for title in titles:
        if title:
            line = re.sub(rf"来源[：:]\s*{re.escape(title)}", "", line)
    return line


def check_pipeline_out_of_findings(markdown: str, template,
                                   table_titles: Sequence[str] = ()) -> list[str]:
    """§RPT-2 货 2 闸 ⑨：管道自诊不许占主体节。

    诚实感不靠这个撑——它归开篇节末尾那句人话把握度和附录「样本怎么来的」。
    """
    bodies = _section_bodies(markdown)
    problems = []
    for name in resolved_sections(markdown, template):
        if name in NON_FINDING_SECTIONS or name not in bodies:
            continue
        for offset, line in enumerate(bodies[name], start=1):
            probe = _without_source_footnote(line, table_titles)
            hit = next((w for w in PIPELINE_WORDS if w in probe), None)
            if hit:
                problems.append(
                    f"主体节「{name}」第 {offset} 行出现取数口径词「{hit}」，"
                    f"管道自诊只能进附录：{line.strip()[:40]}")
    return problems


def check_uncertainty_once(markdown: str) -> list[str]:
    """货 3：「假设与不确定性」全篇只在附录写一次。

    每节提示词各带一次「写限定」，写手就每节起一个同名小节——竞品稿 5 处、
    舆情简报 6 处，说的都是同一件事（没有抓取时间）。
    """
    bodies = _section_bodies(writer_text(markdown))
    hits = [(name, line) for name, lines in bodies.items() for line in lines
            if line.lstrip().startswith("#") and UNCERTAINTY_HEADING in line]
    problems = [f"「{UNCERTAINTY_HEADING}」出现 {len(hits)} 处，全篇只许在附录写一次"] \
        if len(hits) > 1 else []
    problems += [f"「{UNCERTAINTY_HEADING}」写在「{name}」节里，它归附录"
                 for name, _ in hits if name != APPENDIX_SECTION]
    return problems


def hedge_density(markdown: str) -> list[str]:
    """货 2：限定句密度。**判黄不判红**——密度是文风，红了要写手整节重写，不值当。"""
    problems = []
    for name, lines in _section_bodies(writer_text(markdown)).items():
        text = "\n".join(line for line in lines if not line.lstrip().startswith(("|", ">")))
        count = sum(text.count(word) for word in HEDGE_WORDS)
        if count > HEDGE_PER_SECTION:
            problems.append(f"「{name}」节里限定词出现 {count} 处（上限 {HEDGE_PER_SECTION}）"
                            "，把握度在摘要末尾说一次就够")
    return problems


#: §WRITE-1 货 3（用户 09-09 拍「2.5 万压到 1.2–1.5 万」）。**单位是字符，不是中文字**：
#: 底料 `r-3e04f808dffd×consulting` 实测全文 25 197 字符 / 47 314 B / 中文字才 9 933——
#: 用户读到的「2.5 万」对得上的是**字符数**。开工时按中文字定预算差了三倍，
#: 会要求这份稿变长（本包实测当场推翻，这是「量出异常先怀疑尺子」第八次现形）。
#:
#: 数的是**写手写的那部分**：程序生成的四块（缺失清单 / 各表口径 / 词表命中参考 /
#: 信息源清单）不计。它们合计 11 041 字符、占全文 44%，其中信息源清单一块就 9 434——
#: 那是 50 条源的标题与链接，读者不读它、只查它，写手也压不动它。
#: 全文要真进 1.2–1.5 万，得动信息源清单——那是可追溯性的骨架。
#: **用户 2026-09-09 拍甲：保住它，全文停在 1.8–2 万字符。** 理由是可追溯正是这份稿
#: 现在唯一站得住的长处（评审的加分项里三条都指向它），另两个选项各自要拿掉
#: 「读者点得到原链接」或「哪些采到了但没用上」，都不换。
#: 所以本尺子只管**写手正文砍一半**：14 154 → 7 000–9 000 字符，程序那四块不计。
#:
#: **判黄不判红**——篇幅超了要整稿重写，一轮 37.6 分钟；这条给合流那一轮一个读数。
LENGTH_MAX_CHARS = 9000
LENGTH_MIN_CHARS = 7000


def writer_body(markdown: str) -> str:
    """成稿里写手真正写的那部分：**全部**程序生成块都剜掉。

    ⚠️ 与上面的 `writer_text` 不是一回事，两个都留着是有意的：
    `writer_text` 只砍文末的信息源清单，好几条老门禁（内部词、不出图…）一直
    架在它上面，改它等于静默改那几条的判域。新门禁一律用这一个。
    ⛔ 剜哪些块只有一处答案（`run.PROGRAM_APPENDIX_HEADINGS`）——
    同一个概念两处两个定义是本项目现形过的假绿。
    """
    from app.report.polish.run import PROGRAM_APPENDIX_HEADINGS

    text = markdown
    for heading in PROGRAM_APPENDIX_HEADINGS:
        cut = text.rfind("\n" + heading)
        if cut >= 0:
            # 程序块之间不保证顺序，逐块按「这一块到下一个二级标题」剜掉。
            rest = text[cut + 1:]
            end = rest.find("\n## ", len(heading))
            text = text[:cut + 1] + (rest[end + 1:] if end >= 0 else "")
    return text


def writer_length(markdown: str) -> int:
    """写手写的那部分有多少字符。表格与引语照数——它们也占读者的阅读时间。"""
    return len(writer_body(markdown))


def length_budget(markdown: str) -> list[str]:
    """货 3：篇幅。判黄不判红；本包不出判据，读数留给合流那一轮。"""
    count = writer_length(markdown)
    if count > LENGTH_MAX_CHARS:
        return [f"写手正文 {count} 字符，超出上限 {LENGTH_MAX_CHARS}："
                "先查同一张表出没出两次、两条发现讲没讲同一件事、"
                "论据节是不是把关键发现的表重排了一遍。⛔ 不许靠删限定句压"]
    if count < LENGTH_MIN_CHARS:
        return [f"写手正文 {count} 字符，低于下限 {LENGTH_MIN_CHARS}——"
                "压过头了，看是不是把限定句或反证删掉了"]
    return []


#: §RULE-1 货 4（评审 #8，调度拍乙）：正式稿只出表不出图。
#: 前端没有图表渲染器（`web/src` 与 skill 目录里都没有 mermaid），写手自选的
#: `xychart-beta ... line [27, 4, ...]` 在真实页面上整段显示成裸代码。
CHART_WORDS = ("mermaid", "xychart", "```")


def check_no_charts(markdown: str) -> list[str]:
    """成稿里不许有围栏与图表源码——围栏里除了图表源码没别的东西该进正式稿。"""
    problems = []
    for index, line in enumerate(writer_text(markdown).splitlines(), start=1):
        hit = next((w for w in CHART_WORDS if w in line), None)
        if hit:
            problems.append(f"第 {index} 行出现 {hit!r}：页面没有图表渲染器，"
                            f"图会渲染成裸代码；趋势改成表 + 一句结论（{line.strip()[:30]}）")
    return problems


def check_quotes_are_speech(markdown: str, titles: Sequence[str]) -> list[str]:
    """§RULE-1 货 5（评审 #7）：引语块里必须是人说的话。

    判据函数与程序侧候选过滤是**同一个** `tables.is_speech_quote`——同一个概念
    两处两个定义，是 09-07 现形过的一种假绿。
    """
    from app.report.polish.tables import is_speech_quote

    problems = []
    for index, line in enumerate(writer_text(markdown).splitlines(), start=1):
        body = line.lstrip()
        if not body.startswith(">"):
            continue
        text = MARK_ANY.sub("", body.lstrip(">").strip())
        if not text or text.startswith(("——", "—", "--")):
            continue        # 出处行不是引语本身
        if not is_speech_quote(text, titles):
            problems.append(f"第 {index} 行引的不是人说的话（帖子标题/产品公告/推广）："
                            f"{text[:40]}")
    return problems


#: §RULE-1 货 6（评审 #2）：解释编码表某格的语义时，只能引该格内有角标的行。
#: 实测咨询体把「价格与付费」19 条负向解读成「嫌豆包太便宜」，依据只是 Reddit
#: 那几条水印帖——写手只看得见进池的角标，看不见这一格背后的原文，于是拿池内证据
#: 反推整格语义。这一格没角标或角标覆盖不到三分之一时，只准写条数，不准归因。
ATTRIBUTION_WORDS = ("并非", "而是", "其实是", "原因在于", "原因是", "反映出", "背后是")
ATTITUDE_HINTS = {"负": ("负向", "负面", "吐槽", "抱怨", "差评"),
                  "正": ("正向", "正面", "好评", "夸")}
CELL_MARK_COVERAGE = 1 / 3
CODED_CELL_TABLE = "attitude_by_topic"
#: §D-084：「不再只是 X，而是 Y」里的 X 是被否定掉的那一半，句子并没有在讲 X。
#: 真机第三轮咨询体第 35 行「衡量它的不再只是回答质量或模型强弱，而是能否接住一项
#: 完整工作」——整段讲的是媒体定位表，一个字都没解释「回答质量」这一格的编码语义，
#: 判据却只看见「而是」+ 主题名同时出现就判红（与 §D-083「顺口提一句被当成结构
#: 信号」同族）。跨度从否定词起、到 `而是 / 而非 / 而在于` 或分句逗号止：**右半句
#: 才是这句真正主张的那一半**，它里面的主题名照旧要管。
NEGATED_HALF = re.compile(
    r"(?:不再只是|不再仅仅是|不再仅是|不再是|不只是|不仅仅是|不仅是|"
    r"并非只是|并非|并不是|不是)"
    r"(?:(?!而是|而非|而在于)[^，,])*")


def _coded_cells(tables: Mapping[str, Any]) -> dict[tuple[str, str], tuple[int, set[int]]]:
    """(主题, 态度) → (这一格几条, 这一格有角标的是哪几条)。表不在就是空 dict。"""
    cells: dict[tuple[str, str], tuple[int, set[int]]] = {}
    for row in ((tables.get(CODED_CELL_TABLE) or {}).get("rows") or []):
        key = (str(row.get("主题") or ""), str(row.get("态度") or ""))
        marks = {int(n) for n in MARK_ANY.findall(" ".join(row.get("marks") or []))}
        count, seen = cells.get(key, (0, set()))
        cells[key] = (count + int(row.get("条数") or 0), seen | marks)
    return cells


def check_cell_attribution(markdown: str, tables: Mapping[str, Any]) -> list[str]:
    cells = _coded_cells(tables)
    if not cells:
        return []
    problems = []
    for index, line in enumerate(writer_text(markdown).splitlines(), start=1):
        if line.startswith(("|", ">")):
            continue
        # 按**句**判，不按行判：一行里常常前半句在念条数、后半句才在解释语义。
        # 整行判会把「豆包在这个主题里并非一边倒的好评」这种只讲分布的句子也判红
        # （09-07 拿三份真稿实测，整行判误伤两处）。分句连破折号一起切。
        for sentence in re.split(r"[。！？；\n]|——", line):
            if not any(w in sentence for w in ATTRIBUTION_WORDS):
                continue
            used = {int(n) for n in MARK_ANY.findall(sentence)}
            problems += _cell_attribution_problems(sentence, cells, used, index)
    return problems


def _topic_only_in_negated_half(sentence: str, topic: str) -> bool:
    """主题名**只**出现在被否定掉的那半句里（两边都出现的，照旧要管）。

    抠掉被否定的那几段后主题名就不见了，说明这句话没在主张关于它的任何事。
    替换成全角空格而不是删掉，免得抠完把两截拼出一个原本不存在的主题名。
    """
    return topic not in NEGATED_HALF.sub("　", sentence)


def _sentence_pins_the_cell(sentence: str, count: int, wanted: set[str],
                            used: set[int], marks: set[int]) -> bool:
    """句子是不是仍然明摆着在谈这一格：写了正/负向、引了这一格自己的角标、或念出条数。

    ⛔ 这**不是**「有角标就放行」——恰恰相反：带上这一格的角标会让上面那条豁免失效。
    有这三样之一，就算主题名落在否定句里也照旧判——否则写手换一句「说的并非 X 太贵，
    而是……」就能绕过去，09-07 那个病象会原样复活。
    """
    if wanted or (used & marks):
        return True
    return count in {int(n) for n in re.findall(r"\d+", sentence)}


def _cell_attribution_problems(sentence: str, cells: dict[tuple[str, str], tuple[int, set[int]]],
                               used: set[int], index: int) -> list[str]:
    problems = []
    for topic in sorted({key[0] for key in cells}):
        if not topic or topic not in sentence:
            continue
        # 句子里说了正向/负向，就只比那一半的格；没说就把这个主题的几格合起来看。
        wanted = {a for a, hints in ATTITUDE_HINTS.items() if any(h in sentence for h in hints)}
        picked = [v for (t, a), v in cells.items() if t == topic and (not wanted or a in wanted)]
        count = sum(c for c, _ in picked)
        marks = {m for _, ms in picked for m in ms}
        if not count:
            continue
        # §D-084：主题名整个落在被否定的那半句里，且句子再没有一处指向这一格——
        # 这不是在给这一格归因，是顺口提了一句它的名字。
        if (_topic_only_in_negated_half(sentence, topic)
                and not _sentence_pins_the_cell(sentence, count, wanted, used, marks)):
            continue
        if not marks or len(marks) < count * CELL_MARK_COVERAGE:
            problems.append(
                f"第 {index} 行在给「{topic}」这一格归因，但这一格 {count} 条里"
                f"只有 {len(marks)} 条进了引用池——只准写「{count} 条，本轮未进引用」"
                f"：{sentence.strip()[:40]}")
        elif used and not (used & marks):
            problems.append(
                f"第 {index} 行解释「{topic}」这一格，引的角标不在这一格里"
                f"（这一格是 {sorted(f'S{m:02d}' for m in marks)}）：{sentence.strip()[:40]}")
    return problems


#: §RULE-1 货 7（评审 #12）：一个表格子里最多几个角标。真机截图坐实：竞品矩阵
#: 每格塞同一串 9–13 个角标，整张表横着读不了。出处归表下面那一行，不进格子。
MARKS_PER_CELL = 3


def check_marks_per_cell(markdown: str) -> list[str]:
    problems = []
    for index, line in enumerate(writer_text(markdown).splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            continue
        for cell in line.strip().strip("|").split("|"):
            used = MARK_ANY.findall(cell)
            # 只判「条数 + 一串角标」那种格——那才是读不了的矩阵格（评审 #12 的原样）。
            # 整列只放角标的出处列（`| 强在哪 | … | 角标 |` 那种）不在此列：
            # 它本来就是给人查出处用的，判红只会逼写手把出处删掉。
            if len(used) > MARKS_PER_CELL and NUMBER.search(MARK_ANY.sub("", cell)):
                problems.append(
                    f"第 {index} 行有一格塞了 {len(used)} 个角标（上限 {MARKS_PER_CELL}）"
                    f"，格里只写条数、出处写到表下面那一行去：{cell.strip()[:40]}")
    return problems


def check_summary_sample_size(markdown: str, template, counts: dict) -> list[str]:
    """§RPT-2 货 2 闸 ⑩：开篇节写样本量不许单写采集总数。

    「本次调研的 562 条证据显示……」是误导——撑起结论的是被引的 33 条。
    合法写法只有两种：只写被引数，或者两个数一起写。
    """
    total, cited = counts.get("evidence"), counts.get("cited")
    if not total or cited is None:
        return []  # 两个数缺一个就判不了，尺子不猜
    bodies = _section_bodies(markdown)
    opening = next((name for name in template.sections if name in OPENING_SECTIONS), None)
    if opening is None or opening not in bodies:
        return []
    problems = []
    for sentence in SENTENCE_SPLIT.split("\n".join(bodies[opening])):
        if not re.search(rf"(?<!\d){total}(?!\d)\s*条", sentence):
            continue
        if cited is not None and re.search(rf"(?<!\d){cited}(?!\d)", sentence):
            continue  # 两个数一起写，合法
        problems.append(
            f"开篇节单写了采集总数 {total}：{sentence.strip()[:46]}"
            f"（只能写被引数 {cited}，或者「{total} 条采集、{cited} 条进入引用」）")
    return problems


#: 原声表的表头认记号。⛔ 认列名不认表名：写手誊抄时表名常被改成行动式标题
#: （「用户原话怎么说」），表名对不上就漏判；而那六列的组合是这张表独有的。
_QUOTE_TABLE_COLUMNS = ("原声", "互动量", "代表性", "态度")


def check_quotes_table_not_in_body(markdown: str) -> list[str]:
    """⑰ 原声表只许出现在附录的程序块里，正文一次都不许摆（用户 2026-09-11 拍）。

    ⛔ 判的是**表**，不是引语：`> 原文…` 那种引用块是共用规则 §5.6 步骤 4 要求的，
    是整份稿里唯一让读者听见真人的地方，**一条都不许少**。这里只挡表格行。

    为什么要有这道程序闸：同一件事 WRITE-1 已经在提示词里写过规矩（缺陷 8），
    但**单靠提示词不够**是本项目反复现形过的——原声表的数据现在仍然投给写手
    （他要拿它挑句子），所以「摆成表格」这件事结构上做得到，只能在验收侧兜住。
    """
    problems = []
    for index, line in enumerate(writer_body(markdown).splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        hit = [name for name in _QUOTE_TABLE_COLUMNS if name in stripped]
        if "原声" in hit and len(hit) >= 3:
            problems.append(
                f"第 {index} 行把原声表摆进了正文：{stripped[:48]}——"
                "整张表由程序挂在附录，正文只写引用块（共用规则 §5.6 步骤 4）")
    return problems


#: 编号连续了：⑨⑩ 归 §RPT-2（管道自诊不是发现 / 摘要不许单写采集总数），
#: ⑪ 归 §CODE-1（不许把编码结果说成全网比例）。三包改同一个文件，
#: 合并序 RPT-1 → RPT-2 → CODE-1；本次 rebase 是 RPT-2 那一棒，⑨⑩ 就位。
CHECKS = ("① 无内部词", "② 一级标题齐", "③ 角标不越池", "④ 数字有出处", "⑤ 行动式标题",
          "⑥ 评价句写清说谁", "⑦ 摘要口径与把握度", "⑧ 建议门禁",
          "⑨ 管道自诊不占主体节", "⑩ 摘要样本数口径", "⑪ 不许推及全网",
          "⑫ 假设与不确定性只写一次", "⑬ 只出表不出图", "⑭ 原声是人说的话",
          "⑮ 归因只引该格内的角标", "⑯ 表格一格 ≤3 个角标",
          "⑰ 原声表只在附录",
          # §RPT-8 新增三条，判词函数与生产侧门禁**是同一批**（`run.confidence_mismatch` /
          # `run.oversized_finding_lines` / `run.weakevidence_advice`），这边只留薄壳。
          # ⚠️ 它们在 2026-09-18 那份第三轮稿上判红是**预期**：那正是用户读出来的三处
          # 毛病（整篇一刀切判低、标题比证据大、建议超出证据），是下一版稿要修的内容，
          # 不是本包的红。前 ⑰ 条对那份稿仍全过。
          "⑱ 把握度按条分层", "⑲ 标题不得大过证据", "⑳ 建议与证据强度匹配",
          # §SEL-4（用户 2026-09-18 拍乙）：海外源放行进关键发现与建议，**代价是这道硬闸**。
          # ⛔ 它不是软检：放行之后没人标出身，读者会把海外网友的话读成国内用户的声音，
          # 那正是 09-14 那次事故的形状（关键发现拿 Reddit 一个水印帖当结论）。
          "㉑ 引了海外就得标明")
#: 判黄的那些：报出来给人看，但不掀掉这一格。红一格 = 写手整节重写（实测 60–80 分钟），
#: 文风密度这种事不值当付这个钱；调度 09-07 拍的也是「>2 判黄」。
WARNINGS = ("⒜ 限定句密度", "⒝ 篇幅",
            # §RPT-4 货 3：客户视角四道软检。判黄理由同上——红一格要整节重写，
            # 这几条是提示词规矩的兜底读数，调度 09-14 拍「提示词 + 程序软检判黄，不硬拦」。
            "⒞ 摘要关键发现跑题", "⒟ 两条关键发现角标重合", "⒠ 同一张表正文重复", "⒡ 机器话残留")


#: ⒞：一条关键发现的角标里**对照实体**占到这个比例就判黄。
#: ⚠️ §SEL-4 起判域**只剩对照实体**：海外 09-18 放行进关键发现，再按它判跑题就是拿
#: 一条已作废的规矩判红。海外那一支改由硬判据 ㉑ 管（要的不是「别引」，是「引了要标明」）。
OFFTOPIC_SHARE = 0.8
#: ⒟：两条关键发现的角标集合，重合数 / 较小那条的角标数 ≥ 这个比例就判黄。
MARK_OVERLAP = 2 / 3
#: 开篇节里关键发现那几行：`1. 【A】结论句 [S12][S18]`。
FINDING_LINE = re.compile(r"^\s*\d+[.)、]\s*【")
#: ⒡：09-14 评审逐条抓到的机器话。「N 条被引证据」是把引用池条数当成正文实引写进了摘要。
from app.report.polish.run import MACHINE_TALK_PATTERNS as MACHINE_TALK  # noqa: E402  写作期闸同一份


def summary_findings(markdown: str) -> list[tuple[str, set[int]]]:
    """开篇节里的关键发现行与各自的角标。"""
    bodies = _section_bodies(markdown)
    lines = [line for name in OPENING_SECTIONS for line in bodies.get(name, [])]
    return [(line.strip(), {int(n) for n in MARK.findall(line)})
            for line in lines if FINDING_LINE.match(line)]


def offtopic_findings(markdown: str, offtopic: set[int]) -> list[str]:
    """⒞ 关键发现的角标大半是**对照实体**——题面问的是研究对象，它答的是别家。"""
    problems = []
    for index, (line, marks) in enumerate(summary_findings(markdown), start=1):
        if marks and len(marks & offtopic) / len(marks) >= OFFTOPIC_SHARE:
            problems.append(f"第 {index} 条关键发现的角标 {len(marks & offtopic)}/{len(marks)} "
                            f"是对照实体：{line[:40]}")
    return problems


# ---------------------------------------------------------------- ㉑ 引了海外就得标明

#: 认法之一：**地域说法**。读者看到这些词就知道说话的人不在国内。
#: ⚠️ 单独一张词表就是「孤零零的关键词」，本项目在词面尺子上栽过多次，所以它
#: **不是唯一认法**——下面那张平台名表是另一条独立的路，命中任一条即算标明。
OVERSEAS_LOCALE_WORDS = ("海外", "国外", "境外", "外网", "英文社区", "英文论坛",
                         "英文圈", "国际社区", "欧美", "非国内")
#: 认法之二：**点名平台**。中文读者看到「Reddit」「Hacker News」就知道这是海外社区，
#: 不必再说一遍「海外」，所以点名平台**单独成立**（与地域说法是「或」不是「且」：
#: 要求两样都写会逼出「海外社区 Reddit 上的海外用户」这种车轱辘话）。
#:
#: ⛔ **`x` 不在这张表里，这是有意的。** X（推特）上大量是中文帖——第三轮真稿池里
#: S23「doubao真有这么好用吗」、S28「apple输入法你再这么烂我真得换doubao了」都是中文。
#: 写「X 上有用户说……」，读者完全看不出这是海外来源；而且「X」是单字母，
#: 拿它做词面匹配会把「X 轴」「产品 X」全认成命中（假绿）。
#: ⇒ 引了 X 的条目**必须用地域说法**，光点名平台不算标明。
OVERSEAS_PLATFORM_WORDS = {
    "reddit": ("Reddit", "reddit", "r/"),
    "hacker_news": ("Hacker News", "HackerNews", "Hacker news", "hacker news"),
    "product_hunt": ("Product Hunt", "ProductHunt", "Product hunt"),
    "x": (),                       # ⛔ 故意留空，理由见上
}
#: 整条全是海外时的加码要求：还得写死适用范围。两类词都要有，缺一不可——
#: 只说「只有一条」不够（没说是海外），只说「海外」也不够（没说不代表国内）。
OVERSEAS_SCOPE_WORDS = ("只反映", "仅反映", "只代表", "仅代表", "只见于", "仅见于",
                        "不代表国内", "不能代表国内", "不等于国内", "不能推及国内",
                        "国内语料", "国内未", "国内没有", "国内暂无", "未见国内",
                        "本轮国内", "没有对应", "无对应")


def _overseas_marks(sources: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """角标 → 平台（只收海外那些）。⚠️ 认两种产物形态，⛔ 不猜。

    - **新产物**（§SEL-4 起）：`sources[].overseas` 是布尔位，`offtopic` 只剩对照实体。
    - **老产物**（09-18 之前）：两支揉在 `offtopic` 一个字符串里，字面是
      「海外平台」/「对照实体」。按**字面**拆回两支，⛔ 不按真值一把抓——
      一把抓会把老稿里的对照实体也当成海外，这道闸就会对着 DeepSeek 的角标
      要求写「这是海外社区的声音」（假红）。
    """
    got: dict[int, str] = {}
    for source in sources or []:
        mark = str(source.get("mark") or "")
        if not mark.startswith("S") or not mark[1:].isdigit():
            continue
        overseas = source.get("overseas")
        if overseas is None:                       # 老产物：只有那个揉在一起的字符串
            overseas = str(source.get("offtopic") or "") == "海外平台"
        if overseas:
            got[int(mark[1:])] = str(source.get("platform") or "")
    return got


def _overseas_attributed(text: str, platforms: Sequence[str]) -> bool:
    """这一条里，读者认得出「这是海外社区的声音」吗？两条独立认法命中任一即可。"""
    if any(word in text for word in OVERSEAS_LOCALE_WORDS):
        return True
    return any(word in text
               for platform in platforms
               for word in OVERSEAS_PLATFORM_WORDS.get(platform, ()))


def _judged_entries(markdown: str, template) -> list[tuple[str, str]]:
    """㉑ 的判域：`(条目说明, 这一条的正文)`。三处，都是「一条结论」的粒度。

    1. 开篇节里的编号关键发现行（与软检 ⒞ 同一份 `summary_findings` 的取法）；
    2. 「关键发现」节里的每个二级小节（标题 + 它的正文，到下一个二级标题为止）；
    3. 建议节里的每条编号建议——⚠️ **连降级区那几条一起判**
       （`run.advice_entries(include_downgraded=True)`）。理由：标明出身与证据强不强
       无关，读者读降级区那几条时同样有权知道依据来自海外。实测第三轮真稿里
       **唯一**一条海外引用就落在降级区，按门禁那个判域整道闸一条都量不到 ⇒ 假绿。
    """
    from app.report.polish.run import advice_entries

    bodies = _section_bodies(markdown)
    judged: list[tuple[str, str]] = []
    for index, (line, _marks) in enumerate(summary_findings(markdown), start=1):
        judged.append((f"开篇节第 {index} 条关键发现", line))
    section = next((n for n in template.sections if n == "关键发现"), None)
    if section:
        title, block = "", []
        for line in bodies.get(section, []):
            if line.startswith("## "):
                if title:
                    judged.append((f"「关键发现」小节「{title[:20]}」", "\n".join(block)))
                title, block = line[3:].strip(), [line]
            elif title:
                block.append(line)
        if title:
            judged.append((f"「关键发现」小节「{title[:20]}」", "\n".join(block)))
    for name in resolved_sections(markdown, template):
        if name not in ADVICE_SECTIONS or name not in bodies:
            continue
        # ⚠️ `advice_entries` 会把第一条之前的空行攒成一个空条目（既有行为，
        # 生产侧两道门禁对空条目本来就判不出词，看不出来）。这里要把条号写进判词给人读，
        # 空条目占掉「第 1 条」会让人对不上稿子 ⇒ 只给**有正文的**条目编号。
        entries = [entry for entry in advice_entries(bodies[name], include_downgraded=True)
                   if any(line.strip() for line in entry)]
        for index, entry in enumerate(entries, start=1):
            judged.append((f"「{name}」第 {index} 条", "\n".join(entry)))
    return judged


def check_overseas_attributed(markdown: str, template,
                              sources: Sequence[Mapping[str, Any]]) -> list[str]:
    """㉑ 引了海外角标的那一条，必须让读者看得出这是海外社区的声音。

    §SEL-4：用户 2026-09-18 拍乙，海外平台的证据放行进关键发现与建议，
    **放行的条件就是这道闸**。⛔ 它不替代写手规则（`_shared/writing-rules.md §5.7.2`），
    两边写的是同一件事——规则给写法，这里判到底写没写（§D-083：规则与闸各说各话，
    两边都「对」而稿子被打回）。

    判词两档：
    - **掺了海外**（角标里有海外、也有国内）：这一条里要有地域说法，或点名了它引的
      那个海外平台（`x` 除外，理由见 `OVERSEAS_PLATFORM_WORDS`）。
    - **整条全是海外**：加码——还得写死适用范围（「只反映海外社区」「国内语料没有对应说法」
      一类）。这是 09-14 那次事故的防线：一条关键发现全靠 Reddit 撑着，
      读者会以为那是国内用户的声音。
    """
    overseas = _overseas_marks(sources)
    if not overseas:
        return []
    problems = []
    for label, text in _judged_entries(markdown, template):
        marks = {int(n) for n in MARK.findall(text)}
        hit = marks & set(overseas)
        if not hit:
            continue
        platforms = sorted({overseas[n] for n in hit})
        shown = "、".join(f"S{n:02d}" for n in sorted(hit))
        if not _overseas_attributed(text, platforms):
            problems.append(
                f"{label}引了海外角标（{shown}，平台 {'/'.join(platforms)}）却没标明出身，"
                f"读者会当成国内的声音：{text.strip()[:40]}")
        elif hit == marks and not any(w in text for w in OVERSEAS_SCOPE_WORDS):
            problems.append(
                f"{label}的角标{shown}**全部**来自海外（{'/'.join(platforms)}），"
                f"只标出身还不够，要写明这条只反映海外社区：{text.strip()[:40]}")
    return problems


def overlapping_findings(markdown: str) -> list[str]:
    """⒟ 两条关键发现引的是同一批证据——它们多半是一条。"""
    found = summary_findings(markdown)
    problems = []
    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            a, b = found[i][1], found[j][1]
            if a and b and len(a & b) / min(len(a), len(b)) >= MARK_OVERLAP:
                problems.append(f"第 {i + 1} 条与第 {j + 1} 条关键发现角标重合 "
                                f"{len(a & b)}/{min(len(a), len(b))}，多半是同一件事")
    return problems


def _table_pairs(block: list[str]) -> set[tuple[str, str]]:
    """一张表的「行标签 × 数」集合：每个数据行取第一格作标签，其余格里的整数各配一对。

    按集合比、不按字面比：09-14 评审那份稿里「情感陪伴 正 10 / 中 1 / 负 4」以透视表、
    长表、带合计的表三种形状摆了三次，逐字比一次也抓不到。角标（S12）不算数。
    """
    pairs: set[tuple[str, str]] = set()
    for line in block[2:]:                       # 跳过表头与分隔行
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = "".join(cells[0].split())
        for cell in cells[1:]:
            for number in re.findall(r"(?<![S\d])\d+", MARK_ANY.sub("", cell)):
                pairs.add((label, number))
    return pairs


def duplicate_tables(markdown: str) -> list[str]:
    """⒠ 同一组数在写手正文里摆了不止一次（换了表形也算）。"""
    earlier: list[tuple[int, set[tuple[str, str]]]] = []
    problems = []
    block: list[str] = []
    start = 0
    lines = writer_body(markdown).splitlines()
    for index, line in enumerate([*lines, ""], start=1):
        if line.strip().startswith("|"):
            if not block:
                start = index
            block.append(line)
            continue
        if len(block) >= 3:            # 表头 + 分隔行 + 至少一行数据
            pairs = _table_pairs(block)
            for first, seen in earlier:
                small = min(len(pairs), len(seen))
                if small >= 2 and len(pairs & seen) / small >= MARK_OVERLAP:
                    problems.append(f"第 {start} 行的表与第 {first} 行的表是同一组数"
                                    f"（{len(pairs & seen)}/{small} 对重合）：后面写「见第 N 条发现的表」即可")
                    break
            earlier.append((start, pairs))
        block = []
    return problems


def machine_talk(markdown: str) -> list[str]:
    """⒡ 系统自己的话漏进写手正文（程序块剜掉之后再查，程序块自己的说明不算）。"""
    problems = []
    for index, line in enumerate(writer_body(markdown).splitlines(), start=1):
        for pattern in MACHINE_TALK:
            hit = re.search(pattern, line)
            if hit:
                problems.append(f"第 {index} 行有机器话「{hit.group(0)}」：{line.strip()[:40]}")
    return problems


def run(md_path: Path, tables_path: Path, work_path: Path) -> dict[str, list[str]]:
    markdown = md_path.read_text(encoding="utf-8")
    data = json.loads(tables_path.read_text(encoding="utf-8"))
    template = _template_for(markdown, tables_path)
    pool = {int(s["mark"][1:]) for s in data.get("sources") or []}
    # 池子的第二把尺子：工作稿正文里出现过的角标。两者对不上就是取料出了问题。
    work_marks = {int(n) for n in MARK.findall(work_path.read_text(encoding="utf-8"))}
    allowed: set[str] = set()
    _numbers_from(data.get("tables"), allowed)
    _numbers_from(data.get("counts"), allowed)
    crossref = {int(s["mark"][1:]): str(s["crossref"]) for s in data.get("sources") or []
                if s.get("crossref")}
    # §RPT-8 ⑱⑲⑳ 的三个读数都在这两处，原样传下去、⛔ 不在这里重算一遍。
    sources = data.get("sources") or []
    tables = data.get("tables") or {}
    entities = [str(e) for e in (data.get("entities") or [])]
    # 中文表名：写手标溯源用的就是这些名字（`build_prompt` 只把中文名投给它）。
    table_titles = [str(t.get("title") or "") for t in (data.get("tables") or {}).values()
                    if isinstance(t, dict)]
    findings = {
        CHECKS[0]: check_no_internal_words(markdown),
        CHECKS[1]: check_sections(markdown, template),
        CHECKS[2]: check_marks_in_pool(markdown, pool),
        CHECKS[3]: check_numbers(markdown, allowed),
        CHECKS[4]: check_action_titles(markdown, template),
        CHECKS[5]: check_judgement_has_subject(markdown, entities),
        CHECKS[6]: check_opening_section(markdown, template),
        CHECKS[7]: check_advice_gate(markdown, template, crossref),
        CHECKS[8]: check_pipeline_out_of_findings(markdown, template, table_titles),
        CHECKS[9]: check_summary_sample_size(markdown, template, data.get("counts") or {}),
        CHECKS[10]: check_ratio_phrases(markdown),
        CHECKS[11]: check_uncertainty_once(markdown),
        CHECKS[12]: check_no_charts(markdown),
        # §D-060 货 1：名单只收有独立标题的源——微博等平台的 `title` 是正文拷贝，
        # 进了名单会把真人博文判成「标题」（S33）。标志由 `tables.build_tables` 算好随源带来；
        # 老产物没这个键按 True 读，行为不变。
        CHECKS[13]: check_quotes_are_speech(
            markdown, [str(s.get("title") or "") for s in data.get("sources") or []
                       if s.get("title_independent", True)]),
        CHECKS[14]: check_cell_attribution(markdown, data.get("tables") or {}),
        CHECKS[15]: check_marks_per_cell(markdown),
        CHECKS[16]: check_quotes_table_not_in_body(markdown),
        # §RPT-8：三条新判据读的是每条源自己的等级/交叉验证结论/出处独立性，
        # 加上编码表那一格的条数——读数不全时三条函数各自返回空表（`tiering_available`），
        # 老形态的稿因此行为不变。
        CHECKS[17]: check_confidence_tiers(markdown, template, sources, tables),
        CHECKS[18]: check_title_within_evidence(markdown, template, sources, tables),
        CHECKS[19]: check_advice_evidence_strength(markdown, template, sources, tables),
        # §SEL-4：海外放行的代价。老产物（`sources[].overseas` 不存在）按 `offtopic`
        # 的字面认回海外那一支，所以旧稿也量得出来，⛔ 不会因为字段没有就静默放行。
        CHECKS[20]: check_overseas_attributed(markdown, template, sources),
    }
    if pool != work_marks:
        findings[CHECKS[2]].append(
            f"信息源池与工作稿角标对不上：池 {len(pool)} 个、工作稿 {len(work_marks)} 个")
    return findings


def warnings_of(md_path: Path, tables_path: Path | None = None) -> dict[str, list[str]]:
    """判黄的那几条。与 `run()` 分开返回：调用方（`rpt1_matrix`）按 `run()` 判过不过，
    黄的只记进账本给人看——混进 `run()` 会让一格因为文风被判红重写。

    ⒞ 要读 tables.json 里每条源的「旁证·对照实体」标；不给 `tables_path` 时按成稿同名
    规则去找，找不到就当没有（⒞ 读数为空，不猜）。

    ⚠️ §SEL-4：⒞ 的判域**只剩对照实体**。老产物（09-18 之前）的 `offtopic` 里两支
    揉在一起，字面是「海外平台」/「对照实体」——这里按**字面**只取对照实体那一支，
    ⛔ 不按真值一把抓：一把抓会把已经放行的海外照旧判成跑题（拿作废的规矩判黄）。"""
    markdown = md_path.read_text(encoding="utf-8")
    if tables_path is None:
        guess = md_path.with_name(md_path.name.removesuffix(".md") + ".tables.json")
        tables_path = guess if guess.is_file() else None
    offtopic: set[int] = set()
    if tables_path is not None:
        data = json.loads(tables_path.read_text(encoding="utf-8"))
        offtopic = {int(str(s["mark"])[1:]) for s in data.get("sources") or []
                    if s.get("mark") and str(s.get("offtopic") or "") == "对照实体"}
    return {WARNINGS[0]: hedge_density(markdown),
            WARNINGS[1]: length_budget(markdown),
            WARNINGS[2]: offtopic_findings(markdown, offtopic),
            WARNINGS[3]: overlapping_findings(markdown),
            WARNINGS[4]: duplicate_tables(markdown),
            WARNINGS[5]: machine_talk(markdown)}


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    md_path, tables_path, work_path = (Path(p) for p in argv[1:])
    findings = run(md_path, tables_path, work_path)
    print(f"正式稿：{md_path}（{md_path.stat().st_size} B）")
    for name in CHECKS:
        problems = findings[name]
        print(f"{'PASS' if not problems else 'FAIL'}  {name}"
              + (f"（{len(problems)} 处）" if problems else ""))
        for problem in problems[:12]:
            print(f"        · {problem}")
        if len(problems) > 12:
            print(f"        · …另有 {len(problems) - 12} 处")
    for name, problems in warnings_of(md_path, tables_path).items():
        print(f"{'OK  ' if not problems else 'WARN'}  {name}"
              + (f"（{len(problems)} 处）" if problems else ""))
        for problem in problems[:12]:
            print(f"        · {problem}")
    failed = [name for name in CHECKS if findings[name]]
    print(("× 未过：" + "、".join(failed)) if failed else f"√ {len(CHECKS)} 条判据全过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
