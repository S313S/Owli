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

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import (ADVICE_SECTION_UNKNOWN_AUDIENCE,  # noqa: E402
                                   IMPLICATIONS_SECTION)
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
             "fetched_at", "抓取时间", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
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
ADVICE_SECTIONS = frozenset({"建议", "需要回应的点",
                            ADVICE_SECTION_UNKNOWN_AUDIENCE, IMPLICATIONS_SECTION})
DOWNGRADE_HEADING = "值得进一步验证的方向"
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
HEDGE_WORDS = ("单源", "不能外推", "待核实", "不足以")
HEDGE_PER_SECTION = 2
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
    "对不同读者的含义", "对提问方意味着什么",
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


def _advice_entry_problems(lines: list[str], crossref: dict[int, str]) -> list[str]:
    # 一条建议横跨两行（建议行 + 依据行），角标分散在两行里；按行判会把
    # 只引孤证的那半行单独判红（09-05 九格实测两格误报）。按「条」聚合才对。
    problems, entries, current = [], [], []
    for line in lines:
        if line.strip().startswith("#"):
            if DOWNGRADE_HEADING in line:
                break          # 降级区之后的都不受门禁管
            continue
        if re.match(r"^\s*\d+[.)、]\s", line) and current:
            entries.append(current)
            current = []
        current.append(line)
    if current:
        entries.append(current)
    for entry in entries:
        text = "\n".join(entry)
        marks = [int(n) for n in MARK_ANY.findall(text)]
        verdicts = {crossref.get(n) for n in marks if crossref.get(n)}
        if marks and verdicts and verdicts == {"SINGLE"}:
            problems.append(
                f"这条建议只有单源孤证撑着，应降级到「{DOWNGRADE_HEADING}」：{text.strip()[:46]}")
    return problems

def check_ratio_phrases(markdown: str) -> list[str]:
    """§CODE-1：编码是模型判断，正式稿只能写条数，不能推及全网（用户 09-05 拍甲）。

    表格行不参与——表里的占比是数据本身，不是写手的断言。
    """
    from app.reliability.coding import ratio_phrase_offenders

    problems = []
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.startswith("|"):
            continue
        for offender in ratio_phrase_offenders(line):
            problems.append(
                f"第 {index} 行的 {offender!r} 把编码结果说成了全网比例；"
                "编码是模型判断，只能写「N 条里 M 条编码为正向」")
    return problems


def check_pipeline_out_of_findings(markdown: str, template) -> list[str]:
    """§RPT-2 货 2 闸 ⑨：管道自诊不许占主体节。

    诚实感不靠这个撑——它归开篇节末尾那句人话把握度和附录「样本怎么来的」。
    """
    bodies = _section_bodies(markdown)
    problems = []
    for name in resolved_sections(markdown, template):
        if name in NON_FINDING_SECTIONS or name not in bodies:
            continue
        for offset, line in enumerate(bodies[name], start=1):
            hit = next((w for w in PIPELINE_WORDS if w in line), None)
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


#: 编号连续了：⑨⑩ 归 §RPT-2（管道自诊不是发现 / 摘要不许单写采集总数），
#: ⑪ 归 §CODE-1（不许把编码结果说成全网比例）。三包改同一个文件，
#: 合并序 RPT-1 → RPT-2 → CODE-1；本次 rebase 是 RPT-2 那一棒，⑨⑩ 就位。
CHECKS = ("① 无内部词", "② 一级标题齐", "③ 角标不越池", "④ 数字有出处", "⑤ 行动式标题",
          "⑥ 评价句写清说谁", "⑦ 摘要口径与把握度", "⑧ 建议门禁",
          "⑨ 管道自诊不占主体节", "⑩ 摘要样本数口径", "⑪ 不许推及全网",
          "⑫ 假设与不确定性只写一次", "⑬ 只出表不出图")
#: 判黄的那些：报出来给人看，但不掀掉这一格。红一格 = 写手整节重写（实测 60–80 分钟），
#: 文风密度这种事不值当付这个钱；调度 09-07 拍的也是「>2 判黄」。
WARNINGS = ("⒜ 限定句密度",)


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
    entities = [str(e) for e in (data.get("entities") or [])]
    findings = {
        CHECKS[0]: check_no_internal_words(markdown),
        CHECKS[1]: check_sections(markdown, template),
        CHECKS[2]: check_marks_in_pool(markdown, pool),
        CHECKS[3]: check_numbers(markdown, allowed),
        CHECKS[4]: check_action_titles(markdown, template),
        CHECKS[5]: check_judgement_has_subject(markdown, entities),
        CHECKS[6]: check_opening_section(markdown, template),
        CHECKS[7]: check_advice_gate(markdown, template, crossref),
        CHECKS[8]: check_pipeline_out_of_findings(markdown, template),
        CHECKS[9]: check_summary_sample_size(markdown, template, data.get("counts") or {}),
        CHECKS[10]: check_ratio_phrases(markdown),
        CHECKS[11]: check_uncertainty_once(markdown),
        CHECKS[12]: check_no_charts(markdown),
    }
    if pool != work_marks:
        findings[CHECKS[2]].append(
            f"信息源池与工作稿角标对不上：池 {len(pool)} 个、工作稿 {len(work_marks)} 个")
    return findings


def warnings_of(md_path: Path) -> dict[str, list[str]]:
    """判黄的那几条。与 `run()` 分开返回：调用方（`rpt1_matrix`）按 `run()` 判过不过，
    黄的只记进账本给人看——混进 `run()` 会让一格因为文风被判红重写。"""
    markdown = md_path.read_text(encoding="utf-8")
    return {WARNINGS[0]: hedge_density(markdown)}


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
    for name, problems in warnings_of(md_path).items():
        print(f"{'OK  ' if not problems else 'WARN'}  {name}"
              + (f"（{len(problems)} 处）" if problems else ""))
        for problem in problems[:12]:
            print(f"        · {problem}")
    failed = [name for name in CHECKS if findings[name]]
    print(("× 未过：" + "、".join(failed)) if failed else f"√ {len(CHECKS)} 条判据全过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
