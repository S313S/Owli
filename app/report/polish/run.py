"""正式稿撰写入口：一次引擎调用，把工作稿 + 证据池整理成人能读的咨询报告。

不采集、不评级、不碰工作稿产物——只读库与成稿，只写 `runs/<id>/exports/`。
引擎照 `app/reliability/backfill.py:630-647` 直调适配器（`model="opus"`，适配器已透传）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from app.observability.cost import UsageMeteringAdapter
from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineTask
from app.orchestrator.sectioning import (SectionWallClockExpired,  # 只 import，不改那个文件
                                         _run_before_section_deadline)
from app.report.polish.sharding import (FINDING_LINE, Finding, merge_shards, parse_findings,
                                        shard_paths, should_shard)
from app.report.polish.skills import Template, get_template, shared_rules
from app.report.polish.tables import OVERSEAS_COLUMN, collect_inputs

AGENT_ID = "report-polisher"
AGENT_KIND = "report_writing"
#: 引擎的伪 goal 目录名；与工作稿的 goal-N 不重名。
GOAL_ID = "polished"
#: 角标越池允许整稿重写一次；再越即 failed（提货单货 2）。
MAX_ATTEMPTS = 2
#: 低于这个字节数的产物按「没写成」算——D-045 那轮的占位节只有 124 B。
#: 但字节数只是下限，**真正的验收判据是骨架齐不齐**（`missing_sections`）：
#: 09-05 撞到过引擎写到一半 SDK `Stream closed`，落盘 4 805 B、只写到第一节，
#: 光看字节数就成了假绿。
MIN_DRAFT_BYTES = 2000
#: 单节低于这个字节数按「这一节没写」算。附录最短，但也远不止 200 B。
MIN_SECTION_BYTES = 200
_H1 = re.compile(r"^# +(.+)$", re.MULTILINE)


def missing_sections(markdown: str, sections: Sequence[str]) -> list[str]:
    """SKILL 声明的一级标题里，成稿还缺哪些。写手把标题降成二级也算缺。"""
    found = {line.strip() for line in _H1.findall(markdown)}
    return [name for name in sections if name not in found]


#: §RPT-2 货 4 ①：读者身份「不明」时，建议节改名——这一节不再是「给你的行动清单」，
#: 而是「你是谁决定了这份报告对你意味着什么」。标题由代码写，写手改不了也不用改。
ADVICE_SECTION = "建议"
ADVICE_SECTION_UNKNOWN_AUDIENCE = "对不同读者的含义"
#: §RPT-2 货 4 ②：竞品对比稿自带的「对提问方意味着什么」（借 competitor-profiling
#: 的 Competitive Implications 一节）。模板里已经有它，建议节就不必再改名——
#: 否则读者不明时会出现两节讲同一件事。
#: §RPT-4 C-11：原名「对提问方意味着什么」——「提问方」是这套系统内部的说法，客户读着像内部文档。
IMPLICATIONS_SECTION = "这意味着什么"


def sections_for(template: Template, data: Mapping[str, Any]) -> tuple[str, ...]:
    """这一稿实际要写的一级标题。读者不明就把「建议」换成「对不同读者的含义」。"""
    from app.plan.question import AUDIENCE_UNKNOWN

    role = str((data.get("audience") or {}).get("audience_role") or AUDIENCE_UNKNOWN)
    if role != AUDIENCE_UNKNOWN or IMPLICATIONS_SECTION in template.sections:
        return tuple(template.sections)
    return tuple(ADVICE_SECTION_UNKNOWN_AUDIENCE if name == ADVICE_SECTION else name
                 for name in template.sections)


def section_paths(runs_root: Path, research_id: str, template: str,
                  sections: Sequence[str]) -> list[tuple[str, Path]]:
    """每节各一个文件。

    09-05 实测：让写手用 Edit 往同一个文件里一节节追加，文件越长每次追加越贵，
    稳定写到第四五节就断（两轮都缺「建议、附录」）。改成一节一个文件、各写一次，
    骨架由本模块按声明顺序拼——顺带把「标题写错/降级/漏节」这一类失败整个根除。
    """
    root = (Path(runs_root) / research_id / "goals" / GOAL_ID / f"{template}-parts")
    return [(name, root / f"{index:02d}-{name}.md") for index, name in enumerate(sections, 1)]


#: 分节/分片文件名的形状：`02-关键发现.md`、`02-关键发现.shard-1.md`。
#: 只清这个形状，`.report-polisher-*.json` 之类的引擎旁产物不动。
_PART_GLOB = "[0-9][0-9]-*.md"


def stale_part_paths(runs_root: Path, research_id: str, template: str) -> list[Path]:
    """该格 parts 目录下现存的全部分节与分片文件（不管这一轮声明了哪几节）。

    不按 `section_paths()` 的清单删：读者身份一变，「建议」会改名成
    「对不同读者的含义」，上一轮那份 `04-建议.md` 就落在清单之外、留在树上。
    """
    root = Path(runs_root) / research_id / "goals" / GOAL_ID / f"{template}-parts"
    return sorted(path for path in root.glob(_PART_GLOB) if path.is_file()) \
        if root.is_dir() else []


def clear_stale_parts(runs_root: Path, research_id: str, template: str) -> list[str]:
    """开跑前把该格全部分节/分片清一遍，返回清掉的文件名。

    D-041/D-042 的销账动作。原先 `polish()` 只在每次尝试前清**当前这一节**，
    开跑前不清全部——09-07 实测树上躺着上一轮的 55 份分节。不分片时它只让
    `missing_sections` 少报（诊断失真）；**接了分片之后它升格成内容错误**：
    合并器按片序扫目录/清单取片，上一轮的旧片会被当本轮产物拼进正文，
    而且零报错。所以这一步是分片的硬前置，不是可选项。
    """
    removed = []
    for path in stale_part_paths(runs_root, research_id, template):
        path.unlink()
        removed.append(path.name)
    return removed


#: 附录里由程序生成、写手一个字都不写的那几块。标题在这里定义一次，
#: 尺子按它切掉程序块再量写手的篇幅——同一个概念两处两个定义是本项目现形过的假绿。
SOURCES_HEADING = "## 信息源清单"
MISSING_HEADING = "## 哪些没采到"
#: §RPT-7 货 1：从「哪些没采到」拆出去的两节。用户 09-18 读第三轮正式稿圈出那张表问
#: 「这是什么情况」——他在一张表里同时看见了三件根本不同的事：真的一条没采到、
#: 采到了只是没写成段、以及压根不在采的内部处理环节。一张表答不了三个问题。
#:
#: ⛔ 标题不写「采到了但没写进正文」：HN 那一行 `cited=2`（S38 在正文真被引），
#: 「没写进正文」对它就是假话，和货 3 ① 要堵的是同一类。缺的确实只有「没写成一段」。
COLLECTED_HEADING = "## 采到了但没写成段落"
#: ⛔ 不写「工序」：制造业的词，客户未必立刻对上号；而且这一节除了内部处理步骤，
#: 还会收「报告·第 N 节」这种没写成的撰写章，「环节」两头都罩得住。
PROCESS_HEADING = "## 哪些环节没跑完"
BASIS_HEADING = "## 各表口径"
CONFIDENCE_HEADING = "## 把握度读数（主张的交叉验证与被引证据等级）"
LEXICON_HEADING = "## 词表命中参考（只数触发词，不是情感判断）"
QUOTES_HEADING = "## 代表原声（逐字摘录，按互动量排序）"
CONTRAST_HEADING = "## 对照实体的评论（只作参照，不计入正文态度表）"
TIMESPAN_HEADING = "## 证据的时间范围"
#: §RPT-8 货 4②：单人线索区。⛔ 标题里就把「没被印证」写死——这一节最大的风险
#: 是读者把它当成结论读，防这件事只能靠标题，不能指望读者读完小字。
CLUES_HEADING = "## 值得核的线索（各只有一条信号，未经交叉验证，不是结论）"
#: ⚠️ §RPT-7（缺口三表）与 §RPT-8（线索表）各自往这张表里加过条目，合并时**两边都要在**：
#: 它是「剜掉哪些程序块再量写手篇幅」的唯一答案，漏一个会静默改掉篇幅判据的判域。
PROGRAM_APPENDIX_HEADINGS = (MISSING_HEADING, COLLECTED_HEADING, PROCESS_HEADING,
                             BASIS_HEADING, LEXICON_HEADING,
                             QUOTES_HEADING, SOURCES_HEADING, CONFIDENCE_HEADING,
                             CONTRAST_HEADING, TIMESPAN_HEADING, CLUES_HEADING)


def sources_table(sources: Sequence[Mapping[str, Any]],
                  cited: Iterable[int] | None = None) -> str:
    """信息源清单：由代码生成，不让写手誊抄。

    §RPT-3 货 3：`cited` 是正文实际出现过的角标号，给了就**只列这些**，池里没引的
    折成一行「引用池另有 N 条本稿未引用（其中对照实体 M 条）」。09-14 评审实测清单
    80 条、正文实引 12 条，其余多是对照实体的评论，读者翻清单以为报告靠它们撑着。
    不给 `cited` 行为不变（老调用方照列整个池）。

    09-05 实测：三格里两格死在「附录」，报错都是
    `API Error: The socket connection was closed unexpectedly`——本机代理掐长响应。
    附录之所以最长，就是因为它要逐条重打几十条角标/标题/链接/等级，而这些数据本就在
    `tables.json` 里。让模型重打一遍既贵、又正好落在唯一会断的地方，且可能抄错。
    """
    grade_note = {"A": "可独立支撑结论", "B": "较可靠，宜与他源同现",
                  "C": "只作旁证", "D": "线索级"}
    used = None if cited is None else {int(n) for n in cited}
    listed = [item for item in sources
              if used is None or int(str(item["mark"])[1:]) in used]
    note = ("（本节由程序按证据库直接生成，未经改写。**只列正文实际引用过的证据**，"
            "角标号沿用引用池里的编号，所以号码是跳着的，不是漏了几条。）"
            if used is not None else
            "（本节由程序按证据库直接生成，未经改写。**角标号沿用证据库里的编号**，"
            "列的是本报告的整个引用池，所以号码是跳着的，不是漏了几条。）")
    lines = [SOURCES_HEADING, "", note, "",
             "| 角标 | 等级 | 说明 | 标题 | 抓取时间 | 链接 |", "|---|---|---|---|---|---|"]
    for item in listed:
        grade = str(item.get("grade") or "?")
        title = source_title(item).replace("|", "｜") or "（无标题）"
        url = str(item.get("url") or "")
        lines.append(f"| {item['mark']} | {grade} | {grade_note.get(grade, '未评级')} "
                     f"| {title} | {_fetched_at_cell(item.get('fetched_at'))} | {url} |")
    unused = [item for item in sources if item not in listed]
    if used is not None and unused:
        contrast = sum(1 for item in unused if item.get("contrast") is True)
        tail = f"（其中对照实体 {contrast} 条）" if contrast else ""
        lines += ["", f"引用池另有 {len(unused)} 条本稿未引用{tail}，不在上表。"]
    return "\n".join(lines) + "\n"


#: 信息源清单标题的长度上限（字符）。抖音标题常把整段话题标签拼在后面。
SOURCE_TITLE_CHARS = 60
_COMMENT_PREFIX = re.compile(r"^评论\s*[·：:]\s*")


def source_title(item: Mapping[str, Any]) -> str:
    """信息源清单里的标题：只剩「评论 · 帖子标题」一种写法。§RPT-4 C-15。

    09-14 评审实测同一张清单里「评论 · 「…」」「「评论 · …」」「「评论：…」」三种写法并存，
    Reddit 条目在页面证据表里是英文原题、在正式稿里是中文译名——那是工作稿写手誊清单时
    各写各的。归一的规矩：
    - 这条源有独立标题（`title_independent`）且库里有原题（`raw_title`）⇒ 用原题，与页面证据表一致；
    - 没有独立标题（微博等，库里 title 是正文拷贝）⇒ 沿用工作稿的概括标题；
    - 两种都剥掉外层书名号、把「评论：」统一成「评论 · 」、压空白、按 60 字截断。
    ⛔ 不动 `app/adapters/source_mcp.py` 的前缀拼接（调度拍：出稿侧归一）。
    """
    raw = str(item.get("raw_title") or "")
    base = raw if (raw.strip() and item.get("title_independent", True)) else str(item.get("title") or "")
    text = " ".join(base.split())
    for _ in range(2):                      # 「评论 · 「X」」两层都剥
        if text.startswith("「") and text.endswith("」"):
            text = text[1:-1].strip()
        match = _COMMENT_PREFIX.match(text)
        if match:
            rest = text[match.end():].strip()
            if rest.startswith("「") and rest.endswith("」"):
                rest = rest[1:-1].strip()
            text = f"评论 · {rest}"
    if len(text) > SOURCE_TITLE_CHARS:
        text = text[:SOURCE_TITLE_CHARS].rstrip() + "…"
    return text


def _fetched_at_cell(raw: object) -> str:
    """抓取时间只在这一列出现，且写成人看的形状。

    §RULE-1 货 1：正文里 `fetched_at: 2026-09-06T15:25:58+08:00` 实测出现 38 次，
    是写手从工作稿证据契约带过来的习惯。堵住正文之后要给它一个合法落点，
    否则「什么时候采的」这件读者真会问的事就整份稿都查不到了。
    落点只此一处，且去掉 `T` 与时区尾巴——读者要的是「哪天采的」，不是 ISO 串。
    """
    text = str(raw or "").strip()
    if not text:
        return "—"
    return text.replace("T", " ")[:16]


#: 机器串 → 人话。**下放给程序不等于可以照抄机器词**——2026-09-09 那一轮
#: 尺子①「无内部词」红 13 处，**全是这两张程序生成的表**（`goal-3/ch-6/sec-1`、
#: `evidence.platform`、`citation_no`、`reports.extra`、`app/report/polish/lexicon.py`）。
#: 教训：下放换的是执行者，不是标准；模型照抄会被抓，程序照抄一样会被抓。
#: 而 SKILL 里那句「未经改写」应当是「不需要改写」的结果，不是「没做人话化」的托词。
_PLAIN_WORDS = {
    "evidence.platform": "证据的来源平台", "evidence.published_at": "证据的发布时间", "citation_no": "引用角标",
    "reports.extra.claims[].verdict": "主张的交叉验证结论",
    "reports.extra": "报告的附加数据", "evidence.extra.dimensions": "证据自带的维度标注",
     "app/report/polish/lexicon.py": "固定词表",
    "grade": "等级", "topic_polarity": "主题极性表", "entity_mentions": "实体提及表",
    "grade_mix": "等级分布表", "crossref_mix": "交叉验证分布表",
    "platform_mix": "平台分布表", "entity_dimension": "实体×维度表", "timeline": "时间分布表",
}


#: 「（词表见 <代码路径>）」这半句整条删掉，**不做词替换**——2026-09-09 实测机械替换
#: 会把「词表见 app/report/polish/lexicon.py」变成「词表见 固定词表」，读者读完等于没读。
#: 指向代码位置的句子对客户没有信息量，删掉比换个说法诚实。
#: 两种形态分开处理：带括号的整个括号删掉（句号留给前一句），不带括号的连句号一起删。
#: 一条正则通吃会把「…兜底（词表见 X）。」的句号也吃掉，两句黏成「兜底每行角标…」。
_CODE_POINTER = re.compile(r"[（(]词表见[^）)]*[）)]|词表见[^。；]*[。；]")


def plain_words(text: str) -> str:
    """把机器词换成人话。长键先换，避免 `reports.extra` 先把 `reports.extra.claims[]` 切碎。"""
    out = _CODE_POINTER.sub("", str(text or ""))
    for key in sorted(_PLAIN_WORDS, key=len, reverse=True):
        out = out.replace(key, _PLAIN_WORDS[key])
    # 机器词两侧原本靠空格与中文隔开（`按 evidence.platform 分组`），换成中文后
    # 那两个空格就成了多余的——中文之间不留空格。只收中文之间的，不动中英混排。
    return re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)


#: §RPT-7 货 3 ②：口径句是**两用**的——同一段字既进写手提示词（`build_prompt` 把
#: `tables[*]` 除 name 外整块塞过去，那里它是规矩、一个字不能改），又原样印进
#: 客户稿的「各表口径」（那里它是说明）。09-18 用户读到的是后者：
#: 「⛔ 不得把它们写成多数人的看法」「不设它这一格四成条目会凭空消失」
#: 「正文引它时出处行必须写「等级 C」」——是在客户面前对自己人下指令。
#:
#: ⇒ 只在**呈现层**改：改源等于拆掉写手的护栏（`tests/test_quote1_zero_engagement.py`
#: 就锁着那句原文）。⛔ 逐句改写、不做通用改写——口径句里有真实复核数（30/28 那种），
#: 一条机械正则扫过去迟早削到数字上。
_CLIENT_VOICE = (
    ("⛔ 不得把它们写成多数人的看法、也不得单独拎去当某一方的头号声音。",
     "它们不代表多数人的看法，也不是某一方的头号声音。"),
    ("只能说「提及…的条数」，不得说「X% 用户认为」。",
     "这张表给的是「提及…的条数」，不是「X% 用户认为」。"),
    ("正文引它时出处行必须写「等级 C」，读者据此把它当例子，不当依据。",
     "正文引到这几条时，出处行会标出「等级 C」，读者据此把它当例子，不当依据。"),
    ("不设它这一格四成条目会凭空消失。", "没有这一格，四成条目在表里就看不见了。"),
)
#: 内部版本号：客户不需要知道我们的词表/编码规则是第几版，留着只会让人追问「那 v1 呢」。
#: ⛔ 只削版本尾巴，不动它前面的名字——「模型编码」「固定词表」本来就是读得懂的话，
#: 换个说法反而是在编。⛔ 也绝不碰句子里的真实读数（30 条复核、28 条一致）。
_VERSION_TAG = re.compile(
    r"（v\d+(?:\.\d+)*）|(?<=[\u4e00-\u9fff])\s+v\d+(?:\.\d+)*(?![\d.])")
#: 兜底：⛔ 是本项目给写手下禁令的记号，客户稿里一个都不该有。逐句改写漏掉的那些
#: 整句删掉——它对客户没有信息量，留着只会让人看见我们内部怎么管的活。
_ORDER_CLAUSE = re.compile(r"⛔[^。；]*[。；]?")


def client_voice(text: str) -> str:
    """口径句的**客户版**：给写手的指令改成陈述句，内部版本号削掉。

    ⚠️ 只给附录渲染用。提示词那一头照旧读 `tables[*].basis` 原文，一个字不动。
    """
    out = str(text or "")
    for order, statement in _CLIENT_VOICE:
        out = out.replace(order, statement)
    out = _ORDER_CLAUSE.sub("", _VERSION_TAG.sub("", out))
    # 削掉版本尾巴会留下「固定词表 命中计数」这种中文之间的空格，按 `plain_words`
    # 同一条规矩收掉——两处口径必须同形。
    return re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)


def basis_words(text: str) -> str:
    """附录里印口径句的唯一一条路：机器词换人话，再把给写手的指令换成陈述句。

    四处渲染 `basis`（各表口径 / 词表命中 / 代表原声 / 对照实体）共用它——
    同一段字在一份稿里出现两种形态，是本项目现形过的假绿。
    """
    return client_voice(plain_words(text))


#: 机器 reason → 人话。SKILL 第 7 条明写「用人话改写，不要照抄
#: `goal-2/ch-3 empty_result` 这种」——既然是照着一张表改写，就没有理由让模型抄，
#: 抄错了还要被尺子抓。表外的 reason 统一写「原因未记录」（不瞎猜也不泄露机器词）。
#: §SRC-4：tool_unavailable 是章账本闭集里的正式成员（X 钱闸配置缺失、Product Hunt
#: 网络失败都归它），此前不在表里，正式稿上整行只剩「原因未记录」，读者以为没记。
#: §RPT-6 货 3：**「真实无料」与「没采到」是两回事，稿面上必须分得开。**
#: `empty_result` 在本项目里是有定义的一个词，不是「零结果」的泛称——
#: `prompts/common/sources-v1.md` 第 35–40 行与 `source_mcp.SourceUnavailableError`
#: （§D-066）明写：源报错且一条没取到，归 `tool_unavailable`／「源不可用」，
#: **只有工具正常返回空列表才算真的搜到 0 条**。D-064 真机上 TikHub 402 被写成
#: `empty_result`，余额问题伪装成「这个平台没人讨论」，那条路已经堵死。
#: 所以账本里这两种形态本来就分得出来，缺的只是稿面上的人话：原文「这一段跑完了但
#: 没采到任何内容」读起来像是我们的采集出了问题，而用户已拍「Product Hunt 的 0
#: 如实写明」——那个 0 是该平台在检索范围内确实没有条目，是**结论**，不是故障。
_MISSING_REASON = {
    "timeout": "这一段采集超时没跑完",
    "empty_result": "这一段的信息渠道正常跑通、没报错，是它在本次检索范围内确实没有相关内容",
    "conclusion_invalid": "这一段写出来了但没通过结论校验，未采用",
    "retry_exhausted": "这一段重试用尽仍未成功",
    "blocked": "这一段被权限或风控挡住",
    "tool_unavailable": "这一段用的信息渠道当时接不上（接口没配好或没连通），没采到内容",
    "quota_exhausted": "这一段的接口额度用完了，没跑完",
    "source_missing": "这一路信息渠道整轮都没取到内容",
    "source_degraded": "这一路信息渠道中途出过故障，只取到部分内容",
}

#: §RPT-6 货 3：缺口行的「其实已经入库」那一支。§D-060 只给 `timeout` 开了这一支，
#: 本包把它推广到**全部**理由码——**只要这一段真有内容落了库，「没采到」就是假话**，
#: 不管账本把死因记成了什么。
#:
#: 09-17 实例（§SRC-5 ch-13）：账本记 `tool_unavailable`，稿面于是写成「渠道接不上，
#: 没采到内容」，而那 7 条 Hacker News 其实已经直落库、评过级、进了引用池。
#: 对读者是**双重失实**：既不是渠道的问题，也不是没采到。
#: 判据落在 `yielded` 上：它数的是**库里真有多少行**（`tables.chapter_rows` 按
#: `agent_name` 计，直落库那条路也照样写 `agent_name`，见 `source_mcp` 第 309/440 行），
#: 所以「内容进没进库」这件事账本答得了。
#:
#: ⛔ 这里只说**账本里查得到的事实**（落库条数），不说死因——「是上游渠道断了，
#: 还是我们自己把这次调用拦下了」这两者账本里分不出来（`tool_unavailable` 一个码把
#: 两种都收了），硬写一句就是编。分不出的那一半已如实报调度另立卡。
_YIELDED_TAIL = {
    # §D-060 的原话，一字不改：超时那一支已经有真机验过的措辞。
    "timeout": "这一段的总结超时没写成，正文未能引用它们",
}
#: 别的理由码撞上「已入库」：只陈述事实，不认领死因，也不说「没采到」。
_YIELDED_TAIL_DEFAULT = "这一段没能记成完整的一段，但内容本身没丢"


def _yielded_tail(entry: Mapping[str, Any], reason: str) -> str:
    """「采到 N 条…」后面那半句。

    §RPT-7 货 3 ①：`timeout` 那一支写死「正文未能引用它们」，而 `chapter_rows`
    早就算好了 `cited`——**有角标真进了正文，这句就是假话**。09-18 真机 HN 那一章
    `yielded=7 / cited=2`（S38 在正文被引），只因死因记的是 `tool_unavailable`
    才侥幸没走到这一支。判据落在账本查得到的 `cited` 上，不落在死因上。
    """
    if int(entry.get("cited") or 0) > 0:
        return _YIELDED_TAIL_DEFAULT
    return _YIELDED_TAIL.get(reason, _YIELDED_TAIL_DEFAULT)


#: §RPT-7 货 1：**非采集章**的理由码人话。与 `_MISSING_REASON` 是两套，因为那一套
#: 每一句都带「采集」——套到一条都不在采的章上（打标签、一致性检查、报告撰写），
#: 「这一段采集超时没跑完」就是凭空给客户报了一次采集故障。09-18 真机三行全踩这一条。
#: 章型是闭集（`app/plan/chapters.py:CHAPTER_TYPES`），⛔ 分流按 `chapter_type`，不按章名猜。
_PROCESS_REASON = {
    "timeout": "这一步超时没跑完",
    "empty_result": "这一步正常跑通、没报错，但没有产出内容",
    "conclusion_invalid": "这一步写出来了但没通过结论校验，未采用",
    "retry_exhausted": "这一步重试用尽仍未成功",
    "blocked": "这一步被权限或风控挡住",
    "tool_unavailable": "这一步用的工具当时接不上（没配好或没连通），没跑完",
    "quota_exhausted": "这一步用的接口额度用完了，没跑完",
}


#: §RPT-7 货 2：章型（`app/plan/chapters.py:CHAPTER_TYPES` 的 11 项闭集）→ 客户读得懂的说法。
#:
#: 09-18 真机：`_chapter_label` 只认 `collection` / `report`，其余一律落
#: `display_name` 兜底，于是「标签」「一致性检查」——本项目内部的工序名——
#: 原样印进了给客户的附录。客户读不懂，读懂了也只会看见我们内部怎么排的活。
#:
#: ⛔ 这里只按**章型本身的字面含义**说一句，不编库里没有的工序含义：
#: - `tagging` 的任务原文是「对清洗后的语料按主题维度打标签」（真机 goal-2/ch-4）；
#: - `audit` 两种活都挂在它名下（`可靠度审计` 给证据评级、`一致性检查` 查同源矛盾），
#:   所以只能写两者都成立的那句「证据的质量核查」，⛔ 不许挑一种写死；
#: - `transport` 在 `_KIND_CHAPTER_TYPES` 里根本没有产出方（闭集成员但排不出来），
#:   编不出它干什么，就让它走兜底，⛔ 不猜。
_CHAPTER_TYPE_LABEL = {
    "data_cleaning": "采到的内容的清洗整理",
    "tagging": "给采到的内容打主题标签",
    "audit": "证据的质量核查",
    "cross_validation": "结论的多源交叉核对",
    "summary": "阶段小结",
    "excel_generation": "导出成表格文件",
    "code_execution": "按脚本跑的数据处理",
}
#: 认不出章型（闭集外、或产物里这一格是空的）时的说法。⛔ 不退回 `display_name`：
#: 那正是内部工序名漏出去的那条路。中性但不撒谎——它确实是中间的一道处理。
_CHAPTER_TYPE_FALLBACK = "中间处理步骤"
#: 撰写章：`report` 之外，`comparison` 也是（真机 goal-6/ch-4 的 `display_name` 就是
#: 「报告撰写」，章型记的却是 comparison）。两者都按「报告·第 N 节」写，不是处理步骤。
_REPORT_KINDS = frozenset({"report", "comparison"})


def _chapter_label(entry: Mapping[str, Any], section: str | None,
                   goal_titles: Sequence[str]) -> str:
    """「缺的是哪一段」：缺的单位是单源子章，不是整个目标——写「渠道（实体）」。

    §RPT-7 货 2：非采集、非撰写的章按 `chapter_type` 取客户说法，
    ⛔ 不按章名猜、也不再拿 `display_name` 兜底（内部工序名就是从那儿漏出去的）。
    """
    platforms = "、".join(str(x) for x in (entry.get("platforms") or []) if x)
    entity = str(entry.get("entity") or "").strip()
    goal_title = str(entry.get("goal_title") or "").strip()
    kind = str(entry.get("chapter_type") or "")
    if kind == "collection" and platforms:
        return f"{platforms}（{entity}）" if entity else platforms
    if kind in _REPORT_KINDS:
        head = f"「{goal_title}」的报告" if goal_title else "报告"
        number = section.removeprefix("sec-") if section else ""
        if number.isdigit() and 0 < int(number) <= len(goal_titles):
            return f"{head}·第 {number} 节（{goal_titles[int(number) - 1]}）"
        return head
    name = _CHAPTER_TYPE_LABEL.get(kind, _CHAPTER_TYPE_FALLBACK)
    return f"{name}（{goal_title}）" if goal_title else name


def missing_table(missing: Sequence[Mapping[str, Any]],
                  objectives: Sequence[Mapping[str, Any]] = (),
                  chapters: Sequence[Mapping[str, Any]] = ()) -> str:
    """缺失清单（人话）。由程序生成——它就是工作稿那张表的机械改写。

    §D-060 货 2：`chapters` 是 `tables.chapter_rows` 算好的逐章读数（tables.json 的
    `chapters` 键），按 (goal_id, 章号) 对上 missing 行：
    - 「缺的是哪一段」写「渠道（实体）」，如「微信公众号（文心一言）」，不再拿目标原话截句——
      缺的单位是单源子章，三行同目标会长得一模一样；
    - `timeout` 且这一章有入库条数 ⇒ 「采到 N 条，已入库并参与评级与统计；这一段的总结超时
      没写成，正文未能引用它们」（§RPT-3 货 5：旧文案「未纳入本章分析」失实——那些行照常
      评级、照常进平台分布等统计表，真正缺的只是角标）。
      §D-039 之后 timeout 的语义是「超时判 missing、已落库产物不作废」，写「没采到」是假话。

    §RPT-7 货 1：一张表变三张，按**账本查得到的两件事**分流，⛔ 不按章名猜——
    - `yielded > 0`：内容在库里，缺的只是没写成一段 ⇒ `COLLECTED_HEADING`；
    - `chapter_type != collection`：这一章压根不在采（打标签、一致性检查、报告撰写）
      ⇒ `PROCESS_HEADING`，理由句走 `_PROCESS_REASON`，一个「采集」字都不带；
    - 其余（真的是采集章、真的一条没入库）才留在 `MISSING_HEADING`。
    对不上章的行（源对账那路 `chapter_id=source/<平台>`）章型未知，仍留在原表不猜。
    不给 `chapters`（老调用方、老产物）行为逐字不变——三张表只有第一张会非空。
    """
    if not missing:
        return f"{MISSING_HEADING}\n\n（本次调研没有缺失的采集段落。）\n"
    # 段落名用**这一段在采什么**（目标原话），不用 `goal-x/ch-y`——后者是内部切块方式，
    # 读者不需要知道，尺子①也禁。取不到就退成「第 N 段」，两者都不泄露内部编号。
    by_goal = {str(g.get("goal_id")): str(g.get("objective") or "")
               for g in (objectives or []) if isinstance(g, Mapping)}
    by_chapter = {(str(c.get("goal_id")), str(c.get("chapter_id"))): c
                  for c in (chapters or []) if isinstance(c, Mapping)}
    goal_titles: list[str] = []
    for c in (chapters or []):
        if isinstance(c, Mapping) and str(c.get("goal_title") or "") not in goal_titles:
            goal_titles.append(str(c.get("goal_title") or ""))
    rows: dict[str, list[str]] = {MISSING_HEADING: [], COLLECTED_HEADING: [],
                                  PROCESS_HEADING: []}
    for index, item in enumerate(missing, 1):
        goal_id = str(item.get("goal_id") or "")
        chapter_id = str(item.get("chapter_id") or "")
        parent, _, section = chapter_id.partition("/")
        entry = by_chapter.get((goal_id, parent))
        reason = str(item.get("reason") or "").strip()
        bucket = MISSING_HEADING
        if entry is not None:
            where = plain_words(_chapter_label(entry, section or None, goal_titles))
            yielded = int(entry.get("yielded") or 0)
            if yielded > 0:
                bucket = COLLECTED_HEADING
                # ⛔ 不写「采集章」：内部词，尺子①禁（程序生成的文本照样被抓）。
                why = f"采到 {yielded} 条，已入库并参与评级与统计；" + _yielded_tail(entry, reason)
            elif str(entry.get("chapter_type") or "") != "collection":
                bucket = PROCESS_HEADING
                why = _PROCESS_REASON.get(reason, "原因未记录")
            else:
                why = _MISSING_REASON.get(reason, "原因未记录")
        else:
            objective = by_goal.get(goal_id, "")
            # 目标原话是一长句；按逗号切会切出「从豆包官网」这种不成句的残句，
            # 所以整句截断加省略号——读者要认出是哪一段，不是读完整句。
            text = plain_words(objective.strip())
            where = (text[:34] + "…") if len(text) > 34 else text
            why = _MISSING_REASON.get(reason, "原因未记录")
        rows[bucket].append(f"| {where or f'第 {index} 段'} | {why} |")
    blocks = [_missing_block(heading, rows[heading])
              for heading in (MISSING_HEADING, COLLECTED_HEADING, PROCESS_HEADING)
              if rows[heading]]
    return "\n".join(blocks)


#: 三张表各自的开头：一句说明 + 表头。⛔ 后两张一个「采集」字都不许有。
_MISSING_BLOCK_HEAD = {
    MISSING_HEADING: ("（本节由程序按调研过程记录生成。）", "缺的是哪一段", "为什么缺"),
    COLLECTED_HEADING: (
        "（本节由程序按调研过程记录生成。这几段的内容**已经采到、已经入库**，"
        "也参与了后面的评级与统计；缺的只是没把它单独写成正文里的一段。）",
        "是哪一段", "采到了多少、缺的是什么"),
    PROCESS_HEADING: (
        "（本节由程序按调研过程记录生成。这几段不去外面取内容，"
        "是拿已经取到的内容往下做的处理步骤。）", "是哪一个环节", "为什么没跑完"),
}


def _missing_block(heading: str, rows: Sequence[str]) -> str:
    note, left, right = _MISSING_BLOCK_HEAD[heading]
    return "\n".join([heading, "", note, "", f"| {left} | {right} |", "|---|---|",
                      *rows]) + "\n"


#: 交叉验证结论 → 人话。与 `build_prompt` 里给写手的那张同一套说法，⛔ 不出现 SINGLE/PASS
#: （尺子⑦禁开篇节写内部口径词；附录里也没理由让客户读英文枚举）。
_CROSSREF_WORDS = {"SINGLE": "单源", "WEAK": "偏弱", "PASS": "多源互证", "CONFLICT": "多源冲突"}
_CROSSREF_ORDER = ("SINGLE", "WEAK", "PASS", "CONFLICT")


#: §RPT-8 货 1：把握度的三档，强 → 弱。⛔ 不加第四档：读者要的是「能信几分」，
#: 四档以上他分不出差别，程序也判不出稳定的界。
CONFIDENCE_TIERS = ("高", "中", "低")
#: 编码表里一格有多少条才算「有量撑着」。10 条是这样定的：本形态的编码样本
#: 量级在 90 条上下（真机 r-3b3482ca7f8b），一格 10 条 ≈ 全样本的一成——
#: 低于这个数，那一格本身就只是几个人的说法，撑不起「高」。
#: ⛔ 这个数改了会改全部历史稿的档位，改之前先拿真稿两侧读数对一遍。
CODED_STRONG = 10
#: 交叉验证结论里唯一算「真有别人印证过」的那一个。
#: ⚠️ 每条角标的 `crossref` 是「引用它的那些主张里**最强**的那个结论」
#: （`tables._crossref_by_mark`），真机 79 条里 37 条是它 —— 所以它**只能当必要条件**，
#: 单独拿它判「高」会把一半的发现判成高。三个读数一起看才判得住。
_CROSSREF_CORROBORATED = "PASS"
#: 等级里「撑得住结论」的那两档（C 级共用规则写死只作旁证）。
_STRONG_GRADES = ("A", "B")
#: 主题表里的兜底格（`coding.TOPIC_NONE`）。不 import：那是禁区外的模块但属另一层，
#: 值变了这里的判定只会少算一格（更保守），不会静默判高。
_CODED_CATCHALL_TOPIC = "未归主题"


def _crossref_counts(tables: Mapping[str, Any]) -> tuple[int, list[tuple[str, int]]]:
    table = (tables or {}).get("crossref_mix")
    if not isinstance(table, Mapping):
        return 0, []
    counts = {str(r.get("交叉验证结论")): int(r.get("主张数") or 0)
              for r in table.get("rows") or [] if isinstance(r, Mapping)}
    order = [*_CROSSREF_ORDER, *sorted(k for k in counts if k not in _CROSSREF_ORDER)]
    return int(table.get("n") or 0), [(k, counts[k]) for k in order if counts.get(k)]


#: 角标号，**带不带方括号都认**。⚠️ 与 `_MARK`（只认 `[S01]`）分开是有意的：
#: 本包要读的两头写法不一样——`Finding.marks` 与表里的 `marks` 是裸的 `S01`，
#: 正文里是 `[S01]`。第一版只用了 `_MARK`，于是逐条把握度全判成「没带角标」、
#: 四条齐刷刷判低，读数看着像「规则没生效」其实是尺子自己读不到（先怀疑尺子）。
_MARK_ANY = re.compile(r"\[?S(\d{2,})\]?")


def _mark_numbers(values: Any) -> list[int]:
    """一段文字、或一串角标里出现过的角标号，升序去重。"""
    if not isinstance(values, str):
        values = " ".join(str(v) for v in values or [])
    return sorted({int(n) for n in _MARK_ANY.findall(values)})


def _sources_by_mark(sources: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    return {int(str(s["mark"])[1:]): s for s in sources or [] if s.get("mark")}


def _thread_groups(numbers: Sequence[int],
                   by_mark: Mapping[int, Mapping[str, Any]]) -> int:
    """这批角标其实来自几个**互相独立**的出处。

    同一个帖子下的两条评论不是两个来源——真机 r-3b3482ca7f8b 的第 4 条关键发现
    正是 [S17][S21]，同一条小红书帖下的两条评论，写成了「用户的负向声音落在……」。
    `sources[].same_thread`（`tables.thread_key` 算的）就是为这件事准备的读数。
    """
    seen: set[int] = set()
    groups = 0
    for number in numbers:
        if number in seen:
            continue
        groups += 1
        seen.add(number)
        for sibling in (by_mark.get(number) or {}).get("same_thread") or []:
            found = re.fullmatch(r"S(\d{2,})", str(sibling))
            if found:
                seen.add(int(found.group(1)))
    return groups


def _coded_support(numbers: Sequence[int], tables: Mapping[str, Any]) -> tuple[int, str]:
    """这批角标落在编码表的哪一格上、那一格有多少条。返回 (条数, 那一格的名字)。

    只认「UGC 逐条编码：主题 × 态度」这**一张**：它的一行 = 一个「谁在哪个主题上
    夸还是骂」的格子，`条数` 是逐条读完判的，`marks` 是这一格里**进了引用池**的那些
    （`coding._row_marks`）。一条发现讲的就是这样一格，量就该按这一格数。

    ⛔ 不并入另外两张，各有各的走样，两次实测都抬高了档位：
    - `scenario_counts` 只答「在哪谈」不答「夸还是骂」，一格动辄 44 条——第 4 条
      只有同帖两条评论，被「生活娱乐 44 条」抬起来；
    - `scenario_attitude` 的「其他」是兜底桶，第 2 条（付费）被桶里 15 条抬成「高」，
      而它在主题表里的正主是「价格与付费·中」5 条。
    兜底格（`未归主题`）同理跳过：它是「没命中任何主题」的那一堆，不是一个主题。
    词表命中表（`topic_polarity`）更不算——它只数触发词，不是态度判断，
    共用规则 §6.5.3 也不许拿它的数进正文。
    """
    best, where = 0, ""
    wanted = set(numbers)
    table = (tables or {}).get("attitude_by_topic")
    if not isinstance(table, Mapping):
        return best, where
    for row in table.get("rows") or []:
        if not isinstance(row, Mapping) or str(row.get("主题")) == _CODED_CATCHALL_TOPIC:
            continue
        if not wanted & set(_mark_numbers(row.get("marks"))):
            continue
        count = int(row.get("条数") or 0)
        if count > best:
            best, where = count, f"{row.get('主题')}·{row.get('态度')}"
    return best, where


def finding_confidence(marks: Sequence[str], sources: Sequence[Mapping[str, Any]],
                       tables: Mapping[str, Any]) -> tuple[str, str]:
    """§RPT-8 货 1：**一条**关键发现的把握度 + 一句人话依据。

    用户 09-18 读第三轮稿：「报告开头就写把握度低，目标客户看了会怎么想」。查下来
    那一句几乎必然是「低」——判它的规则只看「多数说法是不是单源」，而本形态语料里
    1358 条主张有 954 条**结构上**就只挂一条证据（主张文本形如「小红书有用户反馈 X」），
    于是整篇一刀切判低，连有 90 条编码评论撑着的那条发现也被一起劝退。

    改法：把握度**按条算**，读数只用三样，全都在 `tables.json` 里真的存在
    （提货单判据 4；取不到的读数本项目吃过多次亏）：

    - 每条角标的**等级**（`sources[].grade`，A/B/C/D）；
    - 每条角标的**交叉验证结论**（`sources[].crossref`）与它的**出处独立性**
      （`sources[].same_thread`、`sources[].platform`）；
    - 这条发现落在编码表哪一格、那一格有多少条（`tables.attitude_by_topic` 等的 `条数`）。

    三档的界：

    - **高**：≥2 个独立出处、≥2 个平台、最强等级 A/B、有多源互证，且编码表那一格
      ≥ `CODED_STRONG` 条。五条全中才算高——⛔ 少一条就不是高，这是有意写死的：
      真机四条发现按这把尺子是 高1/中2/低1，与用户自己读出来的强弱次序一致。
    - **中**：≥2 个独立出处、最强等级 A/B，且「多平台」与「有量撑着」至少占一样。
    - **低**：其余（单出处、或最强只有 C 级、或既没跨平台也没量）。

    ⛔ 不许把「低」直接改成「中」了事（提货单原话）：这里改的是**判法**，
    判出来还是低的那条就该是低——真机第 4 条（同一帖下两条评论）判的仍是低。
    """
    numbers = _mark_numbers(marks)
    by_mark = _sources_by_mark(sources)
    rows = [by_mark[n] for n in numbers if n in by_mark]
    if not rows:
        return CONFIDENCE_TIERS[-1], "这条发现没带角标，判不了把握度"
    grades = [str(r.get("grade") or "") for r in rows]
    best_grade = next((g for g in _STRONG_GRADES if g in grades), grades and "C" or "")
    strong_grade = best_grade in _STRONG_GRADES
    corroborated = any(str(r.get("crossref") or "") == _CROSSREF_CORROBORATED for r in rows)
    threads = _thread_groups(numbers, by_mark)
    platforms = len({str(r.get("platform") or "") for r in rows if r.get("platform")})
    coded, where = _coded_support(numbers, tables)
    enough = coded >= CODED_STRONG
    if threads >= 2 and platforms >= 2 and strong_grade and corroborated and enough:
        tier = CONFIDENCE_TIERS[0]
    elif threads >= 2 and strong_grade and (platforms >= 2 or enough):
        tier = CONFIDENCE_TIERS[1]
    else:
        tier = CONFIDENCE_TIERS[-1]
    parts = [f"{threads} 个独立出处", f"{platforms} 个平台",
             f"最强等级 {best_grade or '未评级'}"]
    parts.append("有别的来源印证" if corroborated else "没有别的来源印证")
    parts.append(f"「{where}」那一格 {coded} 条评论撑着" if coded else "没有成规模的评论撑着")
    return tier, "、".join(parts)


def tiering_available(sources: Sequence[Mapping[str, Any]],
                      tables: Mapping[str, Any]) -> bool:
    """这一轮的读数够不够判分层把握度。**不够就一条都不判**，显示与门禁一起退场。

    三条腿里「编码表那一格有多少条」是本包新加进来的那一条，也正是提货单说旧规则
    「完全不看」的那一条（§一.1）。它不在场时，判出来的还是只看单源/等级的老口径，
    摆出来只会让每条发现齐刷刷显示「低」——那是本包要治的病，不是本包该产出的东西。

    ⛔ 不做「缺了就按两条腿判」的兜底：本项目的教训是**读数不全时别判**
    （判不了就写判不了），假装判得出来会静默地把结论说歪。
    没有已编码 UGC 的那些稿（纯媒体材料的竞品矩阵等）因此行为一字不变。
    """
    if not sources:
        return False
    table = (tables or {}).get("attitude_by_topic")
    return isinstance(table, Mapping) and bool(table.get("rows"))


def finding_confidences(findings: Sequence[Finding], sources: Sequence[Mapping[str, Any]],
                        tables: Mapping[str, Any]) -> list[tuple[int, str, str]]:
    """逐条关键发现的 (条号, 把握度, 依据)。摘要那一行与附录那张表读的是同一个它。

    读数不全（`tiering_available` 为假）就返回空表——调用方据此整块不出，
    ⛔ 不出一张全是「低」的表。
    """
    if not tiering_available(sources, tables):
        return []
    return [(f.index, *finding_confidence(f.marks, sources, tables)) for f in findings or []]


def overall_confidence(tiers: Sequence[str]) -> str:
    """整篇那一句总括怎么由各条汇总：**取中位档，条数为偶数时取偏低的那一个**。

    ⛔ 不取最低：一条弱发现就把整篇拉成「低」，正是用户读到的那个毛病。
    ⛔ 不取最高：那是把报告说得比证据强。取中位、平局偏低，既能分出差别，
    又不会把一份四条里三条弱的稿说成「中」。
    """
    order = {tier: i for i, tier in enumerate(CONFIDENCE_TIERS)}
    ranked = sorted((order.get(t, len(CONFIDENCE_TIERS) - 1) for t in tiers or []))
    if not ranked:
        return ""
    # `CONFIDENCE_TIERS` 是强 → 弱，所以下标大的是弱的那一档。偶数条时两个中位取
    # **下标大的**那个 = 偏低。⚠️ 第一版写的是 `(len-1)//2`（偏高），与这段说明
    # 正好相反，真机四条 低2/中1/高1 会判成「中」；量出来对不上先查尺子，这里是尺子。
    return CONFIDENCE_TIERS[ranked[len(ranked) // 2]]


def confidence_line(tables: Mapping[str, Any], counts: Mapping[str, Any] | None = None,
                    findings: Sequence[Finding] = (),
                    sources: Sequence[Mapping[str, Any]] = ()) -> str:
    """§RPT-3 货 4：执行摘要把握度那句之后的一行数字，程序从 crossref_mix 取、不经模型。

    评审实测：摘要写「绝大多数结论只有一个来源撑着」，那个数（255/310）读者全文找不到。
    §RPT-4 C-11：去掉「（程序按交叉验证结论计数）」前缀（机器话）；`counts` 里有正文实引数
    （`BODY_CITED_KEY`，`polish` 组装后回填）就接一句「正文实际引用证据 N 条」——
    09-14 评审实测摘要写「支撑本报告结论的是 80 条被引证据」，80 是引用池条数，正文实引 28。

    §RPT-8 货 1 + 货 2：**这一行换内容了**（⚠️ 旧语义「主张 1358 条：单源 1254 / …」
    被本包有意替换，锁它的那两条老用例跟着改，不是改尺子作弊）。理由：那串数是
    内部计数，客户读到「单源 1254」只会更慌，而它回答不了「这几条发现我能信几分」。
    真实数字一条不改，整段挪进附录「把握度读数」那一节，配一句人话（`confidence_tables`）。

    换上来的是**逐条关键发现的把握度**（货 1）：给了 `findings` 与 `sources` 就逐条算，
    并写出整篇那句总括是**怎么由各条汇总**的（`overall_confidence`：取中位、平局偏低）。
    两样有一样没给（老调用方、或摘要里读不出发现列表）就退回只写正文实引数——
    **不退回旧的那串内部计数**，那是本包要拿掉的东西。
    """
    tail = ""
    body_cited = (counts or {}).get(BODY_CITED_KEY)
    if isinstance(body_cited, int) and not isinstance(body_cited, bool):
        pool = (counts or {}).get("cited")
        tail = (f"正文实际引用证据 {body_cited} 条"
                + (f"（引用池共 {pool} 条）" if isinstance(pool, int) and pool != body_cited else "")
                + "。")
    tiers = finding_confidences(findings, sources, tables)
    if not tiers:
        return tail
    listed = "、".join(f"第 {index} 条{tier}" for index, tier, _ in tiers)
    overall = overall_confidence([tier for _, tier, _ in tiers])
    return (f"这 {len(tiers)} 条关键发现的把握度：{listed}——"
            f"按每条自己引的证据算（来源独立到什么程度、等级多高、有多少条已编码评论撑着，"
            f"逐条依据见附录「把握度读数」）；整篇取各条的中位档，即「{overall}」。{tail}")


#: `tables.json` 的 `counts` 里「正文实引条数」的键。组装完才知道，由 `polish` 回填。
BODY_CITED_KEY = "正文实引"


def confidence_tables(tables: Mapping[str, Any],
                      findings: Sequence[Finding] = (),
                      sources: Sequence[Mapping[str, Any]] = ()) -> str:
    """§RPT-3 货 4：交叉验证分布 + 被引证据等级分布两张表，照 `lexicon_reference_table` 的形态挂附录。

    §RPT-8 货 1：**最前面再加一张逐条关键发现的把握度表**——摘要那一行只给档位，
    「凭什么是这一档」得看得见，否则又成了一个读者查不动的判断。
    §RPT-8 货 2：主张计数那串数从摘要挪到这里（真实数字一条不改），配一句人话说清
    它为什么天生偏大：一条主张挂几条证据是**采写形态**决定的，不是结论可不可信的直接读数。
    """
    total, pairs = _crossref_counts(tables)
    grade = (tables or {}).get("grade_mix")
    grade_rows = [r for r in (grade.get("rows") or []) if isinstance(r, Mapping)] \
        if isinstance(grade, Mapping) else []
    tiers = finding_confidences(findings, sources, tables)
    if not pairs and not grade_rows and not tiers:
        return ""
    lines = [CONFIDENCE_HEADING, "",
             "（本节由程序按主张登记与证据评级直接计数，未经改写。执行摘要里「把握度」"
             "那句的依据就是下面这几张表。）"]
    if tiers:
        lines += ["", "| 关键发现 | 把握度 | 凭什么是这一档 |", "|---|---|---|"]
        for index, tier, reason in tiers:
            lines.append(f"| 第 {index} 条 | {tier} | {reason} |")
        overall = overall_confidence([tier for _, tier, _ in tiers])
        # ⚠️ 这一句里**不许写「偏低」**：它是尺子⑥ 的评价词，会被判成「没写清在说谁」。
        # 本包的整跑前离线复核当场抓到（`var/rpt8/after-draft-check.txt` 第一版）——
        # 程序块自己也要过尺子，写的时候容易忘。
        lines += ["", f"整篇那句总括取各条的中位档（条数为偶数时取更弱的那一档），"
                      f"本稿是「{overall}」。一条发现要判「高」，"
                      f"得同时做到：来自 ≥2 个互相独立的出处、跨 ≥2 个平台、"
                      f"最强证据是 A 或 B 级、有别的来源印证过、"
                      f"并且落在一格 ≥{CODED_STRONG} 条评论的编码格上。"]
    if pairs:
        meaning = {"SINGLE": "只有一个来源撑着", "WEAK": "有多个来源但证据偏弱",
                   "PASS": "多个独立来源互相印证", "CONFLICT": "多个来源说法互相冲突"}
        lines += ["", "| 交叉验证结论 | 主张数 | 占比 | 含义 |", "|---|---|---|---|"]
        for key, count in pairs:
            share = f"{round(count * 100 / total, 1):g}%" if total else "—"
            lines.append(f"| {_CROSSREF_WORDS.get(key, '未登记')} | {count} | {share} "
                         f"| {meaning.get(key, '未登记')} |")
        # §RPT-8 货 2：这串数以前印在执行摘要里，客户第一眼读到的就是「单源 1254」。
        # 数一个字没改，挪到这里并说清它为什么天生偏大——否则读者会把「采写形态」
        # 当成「结论不可信」，而那正是用户 09-18 读稿时的反应。
        lines += ["", f"主张共 {total} 条。**这张表偏向「单源」是这类研究的常态，"
                      f"不等于结论都不可信**：一条主张记的往往就是「某平台有用户反馈 X」，"
                      f"它结构上只会挂一条证据，多挂一条反而说明两处说了同一件事。"
                      f"所以判「这几条发现能信几分」看的是上面那张逐条表，"
                      f"不是这张分布表的占比。"]
    if grade_rows:
        label = {"?": "未评级"}
        lines += ["", "| 等级 | 被引条数 | 全库条数 | 含义 |", "|---|---|---|---|"]
        for row in grade_rows:
            key = str(row.get("等级"))
            lines.append(f"| {label.get(key, key)} | {_cell(row.get('被引条数'))} "
                         f"| {_cell(row.get('全库条数'))} | {_cell(row.get('含义'))} |")
        lines += ["", f"被引证据 {grade.get('n')} 条｜口径：{plain_words(str(grade.get('basis') or ''))}"]
    return "\n".join(lines) + "\n"


#: §RPT-8 货 4②：线索原文里不许出现的内部标记。⛔ 这**不是**验收尺子那份
#: `check_polished.FORBIDDEN` 的第二份定义——判据仍归尺子，这里只是**投料前的筛子**：
#: 主张原文是各章 agent 写的，偶尔带 `goal-1`、`ch-3` 这类切块标记，照抄进附录
#: 就成了尺子①的红。宁可少放一条线索，也不放一条带内部标记的进去。
_CLUE_REJECT = re.compile(r"goal-\d|sec-\d|ch-\d|本片|本节样本|本章样本|上游目标|[\w/.-]+\.py")
#: 附录线索区最终摆几条。⛔ 一处定义：从 `tables` 那边取，别在这里另写一个数——
#: 候选池上限（`CLUE_POOL`）与呈现上限是两件事，两处各写一个数迟早对不上。
from app.report.polish.tables import CLUE_LIMIT  # noqa: E402


def clues_block(clues: Sequence[Mapping[str, Any]], exclude: Iterable[int] = ()) -> str:
    """§RPT-8 货 4②：把单人线索照抄成附录的一节。程序写，⛔ 不经模型。

    **为什么归程序、归附录，而不是让写手写进正文**：§SEL-1 09-18 查实，写手把 79 条
    池位一条不漏全引了，丢是丢在正式稿改写那一层；而写手正文已经 8 674 字符、
    模板带宽上限 9 000——再要它多写一节只能靠删别处（限定句是最先被删的那种）。
    程序块不计入写手篇幅（`check_polished.writer_body` 按 `PROGRAM_APPENDIX_HEADINGS`
    剜掉程序块），所以这一节是**加出来的**，不是换来的。

    **为什么摆成表而不是列表**：验收尺子④「数字有出处」按行扫正文数字，表格行它整行
    跳过。线索原文里带数字（「用了俩礼拜」「三级订阅」）很常见，而这些数字的出处就是
    那条评论本身、不在任何一张聚合表里——摆成列表会一条条判红。

    `exclude` 给正文已经引过的角标：正文写过的就不在这里再摆一遍（同一条东西出现两次，
    读者第二次只会跳过）。⚠️ **封顶封在剔除之后**——反过来会把该露面的那几条挡在
    名额之外（本包实测踩过，8 条筛完只剩 2 条且全不是要的）。
    """
    skipped = {int(n) for n in exclude or ()}
    rows = []
    for clue in clues or []:
        if not isinstance(clue, Mapping) or not clue.get("text"):
            continue
        mark = str(clue.get("mark") or "")
        if mark and int(mark[1:]) in skipped:
            continue
        if _CLUE_REJECT.search(str(clue["text"])):
            continue
        rows.append(clue)
        if len(rows) >= CLUE_LIMIT:
            break
    if not rows:
        return ""
    lines = [CLUES_HEADING, "",
             "下面每一条都只有**一个人、一条内容**说过，本轮没有第二个来源印证，"
             "所以它们不进关键发现、也不做建议的依据。列在这里是因为它们足够具体，"
             "值得下一轮专门去核：核实了就是结论，核不出来就该划掉。",
             "", "| 线索 | 出处 |", "|---|---|"]
    for clue in rows:
        mark = str(clue.get("mark") or "")
        where = (f"[{mark}]" if mark
                 else f"{clue.get('platform') or '社媒'}评论 · 本轮未进引用池，点不到原文")
        lines.append(f"| {clue['text']} | {where} |")
    lines += ["", f"共 {len(rows)} 条。没有角标的那几条是本轮**没进引用池**的证据——"
                  f"库里有这条内容、正文点不到它的原文，要核得下一轮重新采。"]
    return "\n".join(lines) + "\n"


#: 执行摘要位的节名（三模板）。与验收尺子 `check_polished.OPENING_SECTIONS` 同一组。
OPENING_SECTIONS = ("执行摘要", "总体倾向")


#: §RPT-4 货 2（C-7）：总体态度行的四档顺序，与编码闭集 `coding.ATTITUDES` 同序。
_ATTITUDE_ORDER = ("正", "负", "中", "混合")


def attitude_line(tables: Mapping[str, Any], subjects: Sequence[str] = ()) -> str:
    """执行摘要关键发现列表**之后**的一行：已编码评论的总体态度分布，程序从表取、不经模型。

    09-14 评审实测：摘要第一句是「情感陪伴被夸、回答质量被骂」，全篇没有一句「整体上
    正多还是负多」——那是「大家怎么看」最直接的答案，数在论据章的表里，结论没人写。
    数从 `scenario_attitude` 取（每条一个场景一个态度，四档相加 = 表的 n）；§RPT-4 货 2
    之后这张表只数点名了研究对象的评论，所以这一行的分母也是主体。
    """
    table = (tables or {}).get("scenario_attitude")
    if not isinstance(table, Mapping) or not table.get("n"):
        return ""
    counts: dict[str, int] = {}
    for row in table.get("rows") or []:
        if isinstance(row, Mapping):
            key = str(row.get("态度"))
            counts[key] = counts.get(key, 0) + int(row.get("条数") or 0)
    order = [*_ATTITUDE_ORDER, *sorted(k for k in counts if k not in _ATTITUDE_ORDER)]
    parts = " / ".join(f"{k} {counts.get(k, 0)}" for k in order if k in _ATTITUDE_ORDER or counts.get(k))
    who = "、".join(str(s) for s in subjects if s)
    scope = (f"只数点名了{who}的评论；这批评论取自引用池，不是随机抽样，口径见附录「各表口径」"
             if who else "这批评论取自引用池，不是随机抽样，口径见附录「各表口径」")
    return (f"已编码评论 {int(table['n'])} 条：{parts}（{scope}）。"
            + attitude_traceability(tables))


#: §RPT-8 货 4①：这一行要交代可追溯性的那个态度档。⛔ 只挑「负」一个：
#: 客户读到负向条数会去找那几条原文，找不到就是这份稿最难看的一处（用户 09-18 报的
#: 正是「摘要写负 17 条，只有 2 条点得到」）；四档都写一遍会把这一行撑成一段。
_TRACED_ATTITUDE = "负"


def attitude_traceability(tables: Mapping[str, Any]) -> str:
    """§RPT-8 货 4①：负向那几条里，有多少条点得到原文、多少条本轮没进引用池。

    用户 09-18 读第三轮稿：摘要写「负 17 条」，翻遍全稿只有 2 条能点开。这不是
    写手偷懒——那 17 条里只有 2 条进了引用池，其余 15 条库里有编码、没有可点的角标。
    **说清楚比藏起来强**，所以这句话由程序算、程序写：

    - 条数从 `scenario_attitude` 的负向各行相加（与上一行的「负 N」同一个数，
      同一张表同一次读，⛔ 不另算一遍）；
    - 点得到的那些从各行的 `marks` 取并集（`coding._row_marks` 已经算好：
      这一格里**进了引用池**的角标，一个角标对一条证据，所以并集的个数就是条数）。

    真机 r-3b3482ca7f8b 读出来是 17 条里 2 条（[S17][S21]），其余 15 条没进池。
    """
    table = (tables or {}).get("scenario_attitude")
    if not isinstance(table, Mapping):
        return ""
    total, marks = 0, set()
    for row in table.get("rows") or []:
        if not isinstance(row, Mapping) or str(row.get("态度")) != _TRACED_ATTITUDE:
            continue
        total += int(row.get("条数") or 0)
        marks |= set(_mark_numbers(row.get("marks")))
    if not total:
        return ""
    if not marks:
        return (f"其中{_TRACED_ATTITUDE}向 {total} 条本轮一条都没进引用池，"
                f"正文点不到它们的原文——这是取数的缺口，不是没人这么说。")
    listed = "".join(f"[S{n:02d}]" for n in sorted(marks))
    rest = total - len(marks)
    tail = (f"，其余 {rest} 条本轮没进引用池、正文点不到原文" if rest > 0 else "")
    return (f"其中{_TRACED_ATTITUDE}向 {total} 条里有 {len(marks)} 条点得到原文"
            f"（{listed}）{tail}。")


def _confidence_index(lines: Sequence[str]) -> int | None:
    """把握度那一句在第几行。

    §D-072 货 3：原先只认**引用块**里的把握度（`> 本报告结论的把握度为…`），
    可写手常把它写成普通段落——r-20271e8a5028 那份咨询体稿就是普通段落，
    于是匹配失灵、态度行被兜底扔到了节末（= 落在把握度句之后）。
    形态不该决定位置，认词不认 `>`；`>` 前缀照样命中（`lstrip("> ")` 之后再看）。
    """
    return next((i for i, text in enumerate(lines)
                 if "把握度" in text and text.strip()), None)


def _block_end(lines: Sequence[str], start: int,
               stops: Callable[[str], bool] | None = None) -> int:
    """从 `start` 这一行起，跨到它所在那一段的最后一行。

    §D-072 货 4 抽出来给两个注入点共用：「一段」就是**连续的非空行**——引用块
    （`> …` 多行）与普通段落是同一个形状，按空行断，不按 `>` 前缀断。原先两处各写
    一份跨段规则（一处认 `>`、一处认非空行），换个写法就会一处跟得上一处跟不上。
    `stops` 给调用方再加一条提前刹车（发现列表那处要在下一条发现、或把握度句前停）。
    """
    end = start
    while end + 1 < len(lines) and lines[end + 1].strip():
        if stops is not None and stops(lines[end + 1]):
            break
        end += 1
    return end


def _append_at_section_end(body: str, line: str) -> str:
    """两个注入点共用的最后一级兜底：锚点一个都找不着就接在节末（老行为）。"""
    return body.rstrip() + "\n\n" + line


def _inject_after_findings(body: str, line: str) -> str:
    """把 `line` 插在关键发现编号列表**之后**（= 把握度那句之前）。

    §D-072 货 3：态度行回答的是「整体正多还是负多」，读者读完那 3–5 条发现正想问
    这个，所以它的位置是**贴着发现列表**，不是贴着把握度句——把握度句在场与否、
    是不是引用块，都不该把这一行推到别处去（RATE-5 报的就是这个：把握度句不是
    引用块，插入点匹配失败，行落到了节末）。

    列表定位复用 `sharding.FINDING_LINE`（切片数片就是按它读的），找不到列表才退回
    「把握度之前」，再找不到才接节末——两级兜底都是老行为，不是新失败。
    """
    lines = body.split("\n")
    last = next((i for i in range(len(lines) - 1, -1, -1)
                 if FINDING_LINE.match(lines[i])), None)
    if last is None:
        hit = _confidence_index(lines)
        if hit is None:
            return _append_at_section_end(body, line)
        return "\n".join([*lines[:hit], line, "", *lines[hit:]])
    # 一条发现写成一行是 SKILL 的硬规定，但写手偶尔会折行；紧跟着的非空行仍属于
    # 这条发现（markdown 的惰性续行），一并跨过去，免得把态度行插进某条发现中间。
    end = _block_end(lines, last,
                     stops=lambda text: bool(FINDING_LINE.match(text)) or "把握度" in text)
    return "\n".join([*lines[:end + 1], "", line, *lines[end + 1:]])


def _inject_after_confidence(body: str, line: str) -> str:
    """把 `line` 插在把握度那一段**之后**；稿里没有那句就接在节末。

    §D-072 货 4：原先只认**引用块**里的把握度（`startswith(">")`），与货 3 修掉的
    是**同一条失灵路**——把握度写成普通段落时匹配失灵，主张行同样被兜底扔到节末。
    货 3 那轮它没显形，只因那份稿 `confidence_line()` 返回空（没有交叉验证读数）；
    换一份有读数的稿就会复发，所以这里跟着走同一套锚定与兜底，**不另造一套**：
    锚点同用 `_confidence_index()`（认词不认 `>`），跨段同用 `_block_end()`
    （引用块与普通段落都是「连续非空行」，一个规则两种形态都吃得下）。
    """
    lines = body.split("\n")
    hit = _confidence_index(lines)
    if hit is None:
        return _append_at_section_end(body, line)
    end = _block_end(lines, hit)
    return "\n".join([*lines[:end + 1], "", line, *lines[end + 1:]])


def basis_table(tables: Mapping[str, Any]) -> str:
    """各表口径。`basis` 是每张表自己带的字段，照列即可，不必让模型誊抄。"""
    rows = [(plain_words(str(v.get("title") or k)), basis_words(str(v.get("basis") or "").strip()))
            for k, v in (tables or {}).items() if isinstance(v, Mapping)]
    if not rows:
        return ""
    lines = [BASIS_HEADING, "",
             "（本节由程序按每张表登记的口径说明生成。）", "",
             "| 表 | 口径 |", "|---|---|"]
    for title, basis in rows:
        lines.append(f"| {title.replace('|', '｜')} | {basis.replace('|', '｜') or '（未登记）'} |")
    return "\n".join(lines) + "\n"


#: 词表命中表的表名。用户 2026-09-09 拍：**正文只留模型逐条编码那一套**（n=287，
#: 逐条读完再判正负），词表命中表降为附录参考。两张表方向相反过——同一个「价格与付费」，
#: 词表说正 17 / 负 3，编码说正 6 / 负 19，执行摘要第 2 条引词表、第 3 条引编码，
#: 读者看不出为什么。落法是把它从三份 SKILL 的 `tables:` 行里拿掉（写手根本看不见它，
#: `build_prompt` 只投模板点名过的表），再由程序把它挂进附录——
#: ⛔ 不许两张都投给写手再靠提示词自觉：本项目已证「单靠提示词不够」。
LEXICON_TABLE = "topic_polarity"


def lexicon_reference_table(tables: Mapping[str, Any]) -> str:
    """词表命中表的附录参考版。写手看不见它，这里由程序照数据照列。"""
    table = (tables or {}).get(LEXICON_TABLE)
    if not isinstance(table, Mapping) or not table.get("rows"):
        return ""
    columns = [str(c) for c in table.get("columns") or []]
    if not columns:
        return ""
    lines = [LEXICON_HEADING, "",
             "（本节由程序按固定词表的命中计数生成，未经改写。这张表只统计触发词出现在多少条"
             "证据里，**不是情感判断**——一条证据里出现「免费」既可能是在夸也可能是在骂，"
             "词表分不出来。态度的结论一律以逐条编码那张表为准；两张表口径不同，"
             "数字对不上是正常的。）", "",
             "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in table.get("rows") or []:
        lines.append("| " + " | ".join(
            str(row.get(column, "")).replace("|", "｜") for column in columns) + " |")
    lines += ["", f"样本量 {table.get('n')} 条｜口径：{basis_words(str(table.get('basis') or ''))}"]
    return "\n".join(lines) + "\n"


#: 原声表的表名。用户 2026-09-11 拍：**整张表挪到附录**。
#: 他的取舍是「原话是证据不是论点，放附录和信息源清单在一起更合位置」，
#: 而且不跟他 09-09 刚拍过的「正文要短」打架——上一轮这张表就是被篇幅预算挤没的，
#: 表没渲染出来，D-059 修好的那两处（夸竞品那句消失、互动量变真数）用户一点都看不见。
QUOTES_TABLE = "quotes"

#: ⚠️ 与词表命中表**不同**：那张表是从三份 SKILL 的 `tables:` 行里**拿掉**的，
#: 写手根本看不见。原声表**必须继续投给写手**，因为共用规则 §5.6 步骤 4 要求
#: 每个主题段引 2–3 条原声写成引用块（`> 原文…`），而**那是整份稿里唯一让读者
#: 听见真人的地方**；写手拿不到原话就一句都引不出来。
#:
#: ⇒ **「表」和「引用块」是两个产物**：表由程序照列挂附录，引用块仍归写手写在正文。
#: 正文不许再摆这张**表**（缺陷 8「同一张表出现两次」），但引用块照写、不受影响。
#: 提示词侧的规矩写在三份 SKILL 的附录节与共用规则 §5.6；⛔ 只靠提示词不够
#: （本项目已证），所以验收侧另有一道程序门禁 `check_polished` ⑰ 兜底。


def _cell(value: Any) -> str:
    """表格单元格的字面。整数值的浮点去掉小数尾巴：`176.0` 写成 `176`。

    ⛔ 不是为了好看（虽然「176.0 个赞」在给客户的稿子里确实读着别扭）：
    验收尺子 ④「数字有出处」的白名单是把 `tables.json` 里的数**削掉小数尾巴**
    收进去的（`check_polished._fmt`），写 `176.0` 等于往稿子里放一个白名单里
    没有的数——一张程序自己生成的表，反而会被自己的尺子判成「这个数没出处」。
    两处口径必须同形。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value if value is not None else "").replace("|", "｜")
    text = f"{value:.10f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return text or "0"


def timespan_block(tables: Mapping[str, Any]) -> str:
    """§RPT-4 C-14：附录一句「证据发布时间集中在 A 至 B，占 N%」，程序从时间表取。

    09-14 评审实测：时间表算出来了（n=942）但正文一处不引，客户分不清是这个月的舆论还是去年的。
    """
    table = (tables or {}).get("timeline")
    if not isinstance(table, Mapping):
        return ""
    coverage = table.get("coverage") or {}
    window = coverage.get("集中区间")
    if not isinstance(window, Mapping):
        return ""
    share = f"{round(float(window['占比']) * 100):g}%"
    span = (f"{window['起']}" if window["起"] == window["止"] else f"{window['起']} 至 {window['止']}")
    return (f"{TIMESPAN_HEADING}\n\n证据发布时间集中在 {span}，占有发布时间的 "
            f"{coverage.get('有发布时间')} 条里的 {share}（全库 {coverage.get('总条数')} 条，"
            "拿不到发布时间的不计入）。\n")


#: §RPT-4 货 2：对照实体的评论表。写手看不见（三份 SKILL 的 `tables:` 行不点它），由程序挂附录。
CONTRAST_TABLE = "contrast_attitude"


def contrast_reference_table(tables: Mapping[str, Any]) -> str:
    """对照实体评论的附录版，照 `lexicon_reference_table` 的形态。"""
    table = (tables or {}).get(CONTRAST_TABLE)
    if not isinstance(table, Mapping) or not table.get("rows"):
        return ""
    columns = [str(c) for c in table.get("columns") or []]
    lines = [CONTRAST_HEADING, "",
             "（本节由程序按逐条编码结果直接计数，未经改写。这些评论说的是用来对照的产品，"
             "不是研究对象，所以不进正文的态度表；放在这里是为了让读者知道它们没有被丢掉。）", "",
             "| " + " | ".join([*columns, "角标"]) + " |", "|" + "---|" * (len(columns) + 1)]
    for row in table.get("rows") or []:
        marks = "".join(f"[{m}]" for m in (row.get("marks") or [])) or "—"
        lines.append("| " + " | ".join([*(_cell(row.get(c, "")) for c in columns), marks]) + " |")
    lines += ["", f"样本量 {table.get('n')} 条｜口径：{basis_words(str(table.get('basis') or ''))}"]
    return "\n".join(lines) + "\n"


def quotes_reference_table(tables: Mapping[str, Any]) -> str:
    """原声表的附录版。写手照样拿得到数据写引用块，这张**表**由程序照列。

    ⛔ 不重新排序、不重新挑句——`coding_tables` 出表时已经按互动量排过、也已经
    过了实体闸与「这是不是人说的话」那道筛。这里只负责把它渲染出来：
    再挑一次就会出现「附录这张表和写手引的句子对不上」，而那种不一致是静默的。
    """
    table = (tables or {}).get(QUOTES_TABLE)
    if not isinstance(table, Mapping) or not table.get("rows"):
        return ""
    columns = [str(c) for c in table.get("columns") or []]
    if not columns:
        return ""
    lines = [QUOTES_HEADING, "",
             "（本节由程序按编码结果直接生成，未经改写。每句都是从原文**逐字摘出**、"
             "程序校验过是正文子串的话，只收**点名了研究对象**的原声——夸别家产品的话"
             "不在这里。**原声是例子不是分布**：读它不能替代读上面的条数表，"
             "更不能把某一句写成「多数人的看法」。）", "",
             "| " + " | ".join([*columns, "角标"]) + " |",
             "|" + "---|" * (len(columns) + 1)]
    rows = list(table.get("rows") or [])
    for row in rows:
        cells = [_cell(row.get(column, "")) for column in columns]
        marks = "".join(f"[{m}]" for m in (row.get("marks") or [])) or "—"
        lines.append("| " + " | ".join([*cells, marks]) + " |")
    # §RPT-7 货 4：**这张表的样本量按表内行数写**，不按 `n`。
    # 真机 r-3b3482ca7f8b：`n=6`、表里 4 行——`coding_tables` 的 `n` 数的是挑出来的
    # 那 6 条，而 `rows` 又过了一道「没角标就不进表」的筛（那一道在本包禁区里，
    # ⛔ 不动挑选逻辑）。于是 4 行的表底下写着「样本量 6 条」，读者数得出来对不上。
    # 以行数为准的依据：这张表**一行就是一条样本**（别的附录表不是——词表命中表的
    # `n` 是证据条数、行是主题，所以那几张照旧用 `n`）。表尾紧挨着行，它说的
    # 必须是这几行。差额不抹掉，补一句交代去向——只说账本查得到的事实（没有角标），
    # ⛔ 不认领挑选逻辑。
    total = table.get("n")
    dropped = (int(total) - len(rows)) if isinstance(total, int) else 0
    gap = f"（另有 {dropped} 条入选原声没有可引用的角标，未列入本表）" if dropped > 0 else ""
    lines += ["", f"样本量 {len(rows)} 条{gap}｜口径："
                  f"{basis_words(str(table.get('basis') or ''))}"]
    return "\n".join(lines) + "\n"


def assemble(parts: Sequence[tuple[str, Path]],
             sources: Sequence[Mapping[str, Any]] = (),
             appendix_blocks: Sequence[str] = (),
             tables: Mapping[str, Any] | None = None,
             subjects: Sequence[str] = (),
             counts: Mapping[str, Any] | None = None) -> str:
    """把各节拼成成稿：一级标题由代码写，写手只交正文。

    §RPT-3：给了 `tables` 就在执行摘要把握度那句后面注入主张计数行（货 4）；
    信息源清单只列写手各节里实际出现过的角标（货 3）。
    """
    bodies = []
    for name, path in parts:
        body = path.read_text(encoding="utf-8").strip()
        # 写手偶尔仍会把标题写进正文，重复的那一行去掉，免得出现两个同名一级标题。
        # 只认「整第一行就是标题」，不能按前缀剥——正文第一句常以节名开头
        # （「建议正文……」会被剥成「正文……」，用例抓到过）。
        first, _, rest = body.partition("\n")
        if first.strip() in (f"# {name}", f"## {name}", name):
            body = rest.lstrip("\n")
        bodies.append((name, body))
    # 实引角标只数写手写的正文，在挂程序块之前数——原声附录表也带角标，数进去就不是「正文实引」了。
    cited = {int(n) for _, body in bodies for n in _MARK.findall(body)}
    counts = {**dict(counts or {}), BODY_CITED_KEY: len(cited)}
    chunks = []
    for name, body in bodies:
        if tables and name in OPENING_SECTIONS and attitude_line(tables, subjects):
            body = _inject_after_findings(body, attitude_line(tables, subjects))
        # ⚠️ `if tables` 这一道老守卫留着：没有表就一行都不注入（`assemble(parts)`
        # 裸调用是骨架用例锁着的老行为）。⛔ 别顺手去掉——本包第一版去掉了它，
        # 骨架用例当场判红。
        if tables and name in OPENING_SECTIONS:
            # §RPT-8 货 1：把握度那一行要**逐条**算，发现列表就在这一节的正文里，
            # 用切片时同一个 `parse_findings` 读——两处各写一份解析，写手换个写法
            # 就会一处跟得上一处跟不上（§D-072 踩过同一形状的坑）。
            line = confidence_line(tables or {}, counts, parse_findings(body), sources)
            if line:
                body = _inject_after_confidence(body, line)
        chunks.append(f"# {name}\n\n{body}")
    # §POOL-1 丁′：缺失清单与各表口径也由程序生成，与信息源清单一样挂在末节。
    # 它们本来就是「把现成字段照列一遍」，让模型誊抄既费引擎又会抄错。
    # 09-05 那次附录连死两格，修法正是把信息源清单下放给程序；这是同一条路再走一步。
    for block in appendix_blocks:
        if block.strip():
            chunks[-1] = chunks[-1].rstrip() + "\n\n" + block
    if sources:
        # 清单挂在最后一节（三个模板的末节都是附录）末尾。
        chunks[-1] = chunks[-1].rstrip() + "\n\n" + sources_table(sources, cited)
    return "\n\n".join(chunks) + "\n"
_MARK = re.compile(r"\[S(\d{2,})\]")


def body_marks(parts: Sequence[tuple[str, Path]]) -> set[int]:
    """写手各节正文里出现过的角标号（与 `assemble` 数实引同一口径：程序块挂上之前）。"""
    return {int(n) for _, path in parts if path.is_file()
            for n in _MARK.findall(path.read_text(encoding="utf-8"))}


def artifact_paths(runs_root: Path, research_id: str, template: str) -> tuple[Path, Path]:
    """(正式稿 markdown, 确定性表 JSON)。两者同名前缀，便于尺子成对取。"""
    # 模板名本身带点会被 with_suffix 当成后缀吃掉（`.consulting` → `.md`），故直接拼串。
    exports = Path(runs_root) / research_id / "exports"
    stem = f"{research_id}.polished.{template}"
    return exports / f"{stem}.md", exports / f"{stem}.tables.json"


def engine_draft_path(runs_root: Path, research_id: str, template: str) -> Path:
    """引擎那一头的落点。

    `app/adapters/claude.py:194` 对所有写工具硬性要求 `actual.relative_to(_goal_root(task))`
    ——引擎只写得进 `runs/<id>/goals/<goal_id>/`，写 `exports/` 一定被拒（真机实测：
    permission_denials 里全是路径越界）。adapters 是本包禁区，所以让引擎写
    `goals/polished/`（一个新目录，不碰任何工作稿的 goal），落盘后由本模块拷进 exports/。
    """
    return (Path(runs_root) / research_id / "goals" / GOAL_ID
            / f"{research_id}.polished.{template}.md")


def _work_view(data: Mapping[str, Any], report_text: str) -> str:
    """工作稿给写手看的那一份：标题 + 结论行 + 缺失清单 + 正文全文。"""
    from app.report.render import parse_report

    view = parse_report(report_text)
    conclusions = "\n".join(f"- {line}" for line in view.get("conclusions") or [])
    # §RPT-3 货 5：给写手看**程序改好的人话表**，不给机器行（`ch-1：timeout`）。
    # 机器行里读不出「这些条目已入库、参与了评级和统计，只是没角标」，写手于是把
    # 「小红书被引不足一成」写成语料问题（09-14 实测）。这张表就是附录里挂的那张。
    missing = missing_table(view.get("missing") or [], data.get("objectives") or [],
                            chapters=data.get("chapters") or []) if view.get("missing") else ""
    body = "\n\n".join(str(s.get("markdown") or "") for s in view.get("sections") or [])
    return (f"### 工作稿标题\n{data.get('title')}\n\n### 工作稿的结论行（原样）\n{conclusions}\n\n"
            f"### 缺口清单（程序已写成人话，原样挂附录，你不必誊抄）\n{missing or '（无）'}\n\n"
            f"### 工作稿正文全文\n{body}")


def _task_head(template: Template, output_path: Path, parts: Sequence[tuple[str, Path]],
               current: str | None, finding: Finding | None,
               findings: Sequence[Finding]) -> str:
    """开头那段任务说明。整节写一份、写片写另一份，别的区一律共用。"""
    # 必须给绝对路径：只给文件名时引擎会拿工作区根去猜，两次都被 capability 判越界。
    # 一次只写一节：适配器每次任务硬墙钟 300 秒（`DEFAULT_CLAUDE_TIMEOUT_SECONDS`，
    # 在本包禁区里改不得），整份五节塞不进去，09-05 实测每轮都写到第三节被掐。
    skeleton = " / ".join(name for name, _ in parts)
    where = (f"用 Write 写到：\n\n`{output_path}`\n\n"
             "这是你本轮唯一能写的路径，写别处一定被拒。**别的节这轮不要碰、不要写。**\n")
    if finding is None:
        return (f"# 任务\n这是一份《{template.title}》正式稿，一共 {len(parts)} 节，"
                f"由多轮分头写。**本轮你只写「{current}」这一节**，{where}"
                f"全篇骨架（给你看上下文，不是让你都写）：{skeleton}\n"
                f"**文件里只写「{current}」这一节的正文，不要写标题行**——一级标题由程序统一加，"
                "你写了反而会重复。\n"
                "只重新组织与解读，不做新的调研，不编造任何事实与数字。")
    # 片：边界写死在「这一条发现」上。兄弟片只给**标题行**不给正文——给了正文
    # 提示词按片翻倍，正好把分片省下的那点又还回去（§SHARD-1 §八 兜底 1）。
    siblings = "\n".join(f.title_line for f in findings)
    return (f"# 任务\n这是一份《{template.title}》正式稿的「{current}」这一节，"
            f"按执行摘要里的 {len(findings)} 条关键发现分头写，"
            f"**本轮你只写第 {finding.index} 条**，{where}"
            f"全篇骨架（给你看上下文，不是让你都写）：{skeleton}\n\n"
            f"## 你这一片要展开的那条发现（执行摘要里的原话）\n{finding.title_line}\n\n"
            f"## 这一节全部 {len(findings)} 条发现的标题行（给你看边界，不是让你都写）\n"
            f"{siblings}\n\n"
            "**只展开你这一条，别复述别条，别下与别条冲突的判断。**\n"
            f"按本模板骨架里「{current}」那一节的四步写这一条："
            "行动式二级标题 → 一张表 → 三到五句解读 → 反证或限定；"
            "**第 4 步只在这一条结论真的撑不住时才写**，撑得住就把话说完整、"
            "别为了免责补一句（模板原话里就带着这个条件，先前这里把它漏掉了）。\n"
            # §RPT-6 货 1：⒜ 的成因就在这两行。规则的单位是「节」（共用规则 §5.1
            # 「限定词一节最多两处」），写作的单位却是「片」，而兄弟片只给标题行、
            # 不给正文（理由见上面那段注释）。09-16 真稿逐片实测 0/1/1/2 处——
            # **每片单独都守住了上限 2**，合并成节就是 4 处，其中 3 处说的还是同一件事。
            # 片之间看不见彼此，所以这里得把「你不是一个人在写」说破；
            # 合并处另有一道 `sharding.fold_repeated_hedges` 兜底。
            f"⚠️ 这一节 {len(findings)} 条发现是 {len(findings)} 轮分头写的，"
            "**谁都看不见别人写了什么**——整份样本共有的那些局限（证据等级偏低、"
            "多数说法只有一个来源、不能外推为普遍看法），程序已经在摘要末尾那句把握度里"
            "说过一次了；你这一片再说一遍，合起来就是说了好几遍，读者读到的是心虚不是严谨。"
            "第 4 步**只写这一条独有**的东西：哪条证据指向相反方向、"
            "这条结论在什么具体情况下不成立。\n"
            # §RPT-6 货 2：⒝ 的成因是预算表只给上限。片只看得见自己那一条，
            # 得由程序告诉它这一节共几条、该查哪一档；档位与数字都留在 SKILL 里
            # （模板内容与代码零耦合，代码不解析模板正文）。
            f"篇幅：照本模板「篇幅预算」表里「关键发现·每条发现（{len(findings)} 条时）」"
            "那一行的区间写，**两头都要守**——上一版每一节都只写到上限的六七成，"
            "合起来低于全篇下限，所以⛔ 不许写不到区间下限就收尾。\n"
            f"**文件里只写这一条的正文，从 `## ` 二级标题起**——一级标题「{current}」"
            "由程序统一加，你写了反而会重复。\n"
            f"这条发现自带的角标是 {'、'.join(finding.marks) or '（无）'}，"
            "至少要引到其中一个；池子外的角标一个都不许出现。\n"
            "只重新组织与解读，不做新的调研，不编造任何事实与数字。")


def build_prompt(template: Template, data: Mapping[str, Any], report_text: str,
                 output_path: Path, errors: tuple[str, ...] = (),
                 parts: Sequence[tuple[str, Path]] = (), current: str | None = None,
                 finding: Finding | None = None,
                 findings: Sequence[Finding] = ()) -> str:
    """共用硬规则 + 模板正文 + 输入区；重写轮把上一轮的错误原样附在最后。

    `finding` 非空 = 这一轮写的是一个**片**（这一节里的某一条发现）。除了开头那段
    任务说明，其余各区**一个字都不变**——每片的活只有「把这一条发现展开」，
    共用硬规则原样带、不加码（CODE-1 货 1 踩过：提示词加活会按片翻倍撞墙钟）。
    """
    objectives = "\n".join(f"- {g.get('objective')}" for g in data.get("objectives") or []
                           if g.get("objective"))
    verdicts = {"PASS": "多源互证", "CONFLICT": "多源冲突", "WEAK": "证据偏弱", "SINGLE": "单源孤证"}
    # §RPT-3 货 2：引得了的评论行（A/B/C 级，§RPT-5 起含 C）在末尾多一栏「原话：…」
    # （`tables.quote_prefix`，程序截取）。
    # 写手按**评论内容**归题、可直接从这一栏摘原声；没有这一栏的行照旧只有标题。
    # §RPT-4 货 3（C-3）：讲别家产品的源多一栏「旁证·对照实体」——这一支照旧不进关键发现。
    # §SEL-4（用户 09-18 拍乙）：来自海外平台的源另有一栏「海外社区」，⛔ 栏名里**没有
    # 「旁证」二字**——它已经放行，可以进关键发现与建议，这一栏是在告诉写手
    # 「引它的时候必须让读者看得出这是海外的声音」，不是在拦它。两栏字面必须分得开，
    # 写手只读得到这一行字，栏名含糊等于规则没下达。
    pool = "\n".join(
        f"- {s['mark']}｜{s.get('grade') or '?'} 级｜{verdicts.get(str(s.get('crossref')), '未登记')}"
        + (f"｜旁证·{s['offtopic']}" if s.get("offtopic") else "")
        + (f"｜{OVERSEAS_COLUMN}" if s.get("overseas") else "")
        # §RPT-4 C-10：同一帖子下的别的池内评论，写手引用时并排标注、不当成独立用户。
        + (f"｜同帖：{'、'.join(s['same_thread'])}" if s.get("same_thread") else "")
        + f"｜{s.get('title') or ''}｜{s.get('url') or ''}"
        + (f"｜原话：{s['quote_prefix']}" if s.get("quote_prefix") else "")
        for s in data.get("sources") or [])
    # 按中文表名交给写手，机器表名（topic_polarity 之类）一律不进提示词——
    # 首稿里「来源：见 topic_polarity」就是照抄 JSON 键来的（用户 09-05 裁决条 3）。
    tables = json.dumps({data["tables"][name].get("title") or name:
                         {k: v for k, v in data["tables"][name].items() if k != "name"}
                         for name in template.tables if name in data["tables"]},
                        ensure_ascii=False, indent=1)
    blocks = [
        _task_head(template, output_path, parts, current, finding, findings),
        f"# 共用硬规则\n\n{shared_rules()}",
        f"# 本模板骨架\n\n{template.body}",
        f"# 调研问题\n{data.get('research_question')}",
        # 读者身份决定「所以呢」写给谁看（§RPT-2 货 1 的 q-1）。没答就是「不明」，
        # 此时不要假装知道读者是谁——模板会改成分读者给含义。
        # 只出这一个小标题：早先 main 与本包各写了一段，合起来是两段讲同一件事，
        # 白占提示词长度（提示词加活会按片翻倍撞墙钟）。
        f"# 这份报告给谁看\n{_audience_view(data)}",
        f"# 本次研究的目标\n{objectives}",
        f"# 涉及的实体\n{'、'.join(data.get('entities') or [])}",
        f"# 信息源池（只能引这些角标，一个都不许多；第三栏是这条源的交叉验证结论；"
        f"带「原话」栏的是评论，标题是它挂的父帖，归题看原话不看标题；"
        f"带「旁证」栏的与题面的对象或地域对不上，怎么用见共用规则 §5.7）\n{pool}",
        f"# 确定性数据表（数字的唯一来源，一个数都不许改）\n```json\n{tables}\n```",
        f"# 工作稿\n\n{_work_view(data, report_text)}",
    ]
    if errors:
        blocks.append("# 上一轮被打回的原因（必须改掉）\n" + "\n".join(f"- {e}" for e in errors))
    return "\n\n".join(blocks)


def _audience_view(data: Mapping[str, Any]) -> str:
    """读者身份给写手看的那一段。「不明」也要明说，不然写手会自己脑补一个读者。"""
    from app.plan.question import AUDIENCE_UNKNOWN

    # 形状有两种：`collect_inputs` 现在平铺 `audience_role`（main 上 `_audience` 那一版），
    # 早先的产物把它放在 `audience` 子对象里。两种都认，老产物不会读不出读者身份。
    nested = data.get("audience") or {}
    role = str(data.get("audience_role") or nested.get("audience_role") or AUDIENCE_UNKNOWN)
    stake = str(data.get("audience_stake") or nested.get("audience_stake") or AUDIENCE_UNKNOWN)
    # 第一行只写身份本身（main 那一版的形状，别的地方按这个形状读）；
    # 第二行起才是本包加的写法要求。
    if role == AUDIENCE_UNKNOWN:
        # §RPT-4 C-11：09-14 评审实测正文写出「对读者身份未知的这份稿而言」——
        # 提示词里的「身份未知」被原样搬进了正文，等于告诉客户他跳过了问卷。
        return (f"{AUDIENCE_UNKNOWN}\n这份稿没有指定读者。建议节改名「对不同读者的含义」，"
                "分三行分别写给竞品团队 / 本产品团队 / 投资分析，"
                "每行一句「这对你意味着什么」+ 一个动作。"
                "⛔ 正文里不许出现「读者身份未知 / 不明」这类话，直接写这件事对哪一类人最要紧。")
    lines = [role, f"每条建议的第一句必须先写「对{role}意味着什么」，再写动作。"]
    if stake and stake != AUDIENCE_UNKNOWN:
        lines.append(f"他们最想知道的：{stake}")
    return "\n".join(lines)


#: §RPT-4 C-11：系统自己的话。验收尺子 ⒡ 与写作期闸共用这一份（`check_polished` import 它）。
#: 前六条是 09-14 评审逐条抓到的；`⛔` 与「按规矩」是 09-15 重出稿读到的——写手把提示词里的
#: 规矩符号和「按规矩挂附录」原样写给了客户。
MACHINE_TALK_PATTERNS = (r"程序按", r"占比\s*0\.\d", r"身份未知", r"读者身份不明", r"提问方",
                         r"\d+\s*条被引证据", r"⛔", r"按规矩")


def machine_talk_lines(markdown: str) -> list[tuple[int, str]]:
    """写手文本里命中机器话的行：`(行号, 命中的字)`。原声引用块（`>` 开头）不查——那是发帖人的话。"""
    hits = []
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.lstrip().startswith(">"):
            continue
        for pattern in MACHINE_TALK_PATTERNS:
            found = re.search(pattern, line)
            if found:
                hits.append((index, found.group(0)))
                break
    return hits


def offpool_marks(markdown: str, pool: frozenset[int]) -> list[str]:
    """成稿里越出信息源池的角标，升序去重。与工作稿的 `_shard_stale_citations` 同思路。"""
    used = {int(n) for n in _MARK.findall(markdown)}
    return [f"S{n:02d}" for n in sorted(used - pool)]


#: 原声块的出处行（`—— 平台 · 等级 A [S12]`）。角标写在这一行上，所以角标要按
#: **块**收，不按行收——按行收会把每一条原声都判成「没有角标」。
_ATTRIBUTION_LINE = re.compile(r"^\s*(?:——|—|--)")
#: 写手常把长引语用省略号接起来。省略号两侧各自仍应是原文的子串，所以按它切开分段比。
_ELLIPSIS = re.compile(r"…+|\.{3,}|。{3,}")
#: 太短的片段不值得比：一两个字撞上原文纯属巧合，判红只会白烧一轮重写。
QUOTE_MIN_CHARS = 8
#: 原声能引的等级（共用规则 §5.6 第 4 步）。§RPT-5：放宽到 C 级——评论在五维结构下
#: 天花板就是 C（权威 0 / 交叉 0 / 完整 0 分居多），只收 A/B 等于原声永远为空，写手只能
#: 写「没有可引的原声」，而库里明明有原文（r-20271e8a5028 S01「不许碰我的电子闺蜜」）。
#: C 级作原声的条件是**稿面标出等级**（`unlabeled_quotes`），读者看得见这句只是旁证。
#: D 与未评级正文仍不引。
QUOTE_GRADES = frozenset({"A", "B", "C"})


def quote_blocks(markdown: str) -> list[tuple[int, list[str], list[int]]]:
    """成稿里的**原声**块：`(起始行号, 引语正文各行, 块内角标号)`。

    连着的 `>` 行算一块；块里以「——」开头的是出处行，不是人说的话。

    ⚠️ 不是每个 `>` 块都是原声：模板要求摘要末尾那句把握度提示
    （`> 本报告结论的把握度为**低**，主要因为……`）也写成引用块，而它是**写手自己的话**，
    没有原文可比、也没有等级可查。所以只认**带角标或带出处行**的块——
    §5.6 第 4 步规定的原声格式两样都有，把握度那句两样都没有。
    （这一条是真稿夹具当场抓出来的：不加这个判别，摘要那一节每轮都被闸退回。）
    """
    blocks: list[tuple[int, list[str], list[int]]] = []
    start, texts, marks, attributed = 0, [], [], False
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.lstrip().startswith(">"):
            if not texts and not marks:
                start = index
            body = line.lstrip().lstrip(">").strip()
            marks.extend(int(n) for n in _MARK.findall(body))
            body = _MARK.sub("", body).strip()
            if not body:
                continue
            if _ATTRIBUTION_LINE.match(body):
                attributed = True
            else:
                texts.append(body)
            continue
        if (texts or marks) and (marks or attributed):
            blocks.append((start, texts, marks))
        texts, marks, attributed = [], [], False
    if (texts or marks) and (marks or attributed):
        blocks.append((start, texts, marks))
    return blocks


def quote_corpus(data: Mapping[str, Any], report_text: str) -> str:
    """原声比对的底本：编码逐条摘出的原声 + 工作稿全文，都去掉空白。

    两样都是**流水线自己产出的原文**：`quotes` 表那一列在编码那头已经程序校验过
    是正文子串（`coding.py` 的同一道闸），工作稿正文里的引文同理。写手照抄任一处都过，
    改一个字就过不了——缺陷 3 里「白情一假」被写成「白请一假」正是这一类。
    """
    from app.reliability.coding import _squeeze          # ⛔ 只 import，不改那个文件

    chunks = [str(report_text or "")]
    # §RPT-3 货 2：写手池里的原话栏也是底本——那是程序从 content_excerpt 截的原文前缀，
    # 写手照抄它必须过闸，改一个字照样过不了。
    chunks += [str(s["quote_prefix"]) for s in data.get("sources") or []
               if isinstance(s, Mapping) and s.get("quote_prefix")]
    for table in (data.get("tables") or {}).values():
        if not isinstance(table, Mapping):
            continue
        for row in table.get("rows") or []:
            if isinstance(row, Mapping) and row.get("原声"):
                chunks.append(str(row["原声"]))
    return _squeeze("\n".join(chunks))


#: §RPT-4：引号类字符在逐字比对里一律视作同一个。写手的工具链会把全角弯引号“”落成半角 `"`，
#: 字一个没改——09-15 沙盒实测 S28「在我看来“是否下载猫箱”是一场典型的囚徒博弈」被这道闸
#: 连退两次、整稿判失败（09-14 RPT-3 那轮同一句也被退过一次）。只折引号，别的标点照旧逐字比。
_QUOTE_MARKS = str.maketrans({c: '"' for c in '“”„‟＂「」『』〝〞'} | {c: "'" for c in "‘’‚‛＇"})


def _fold_quote_marks(text: str) -> str:
    return str(text).translate(_QUOTE_MARKS)


def altered_quotes(markdown: str, corpus: str) -> list[str]:
    """子串闸：`>` 引语行里对不上原文的那些。

    规则早写在共用规则 §5.6 第 4 步「不改写、不润色」里，但正式稿层一直没人执行它
    ——`offpool_marks` 只查角标越池。稿子自称「逐字校验」，写手却在顺手改对错别字。
    """
    from app.reliability.coding import _squeeze          # ⛔ 只 import，不改那个文件

    problems = []
    folded_corpus = _fold_quote_marks(corpus)
    for line_no, texts, _ in quote_blocks(markdown):
        for text in texts:
            for piece in _ELLIPSIS.split(text):
                squeezed = _fold_quote_marks(_squeeze(piece))
                if len(squeezed) >= QUOTE_MIN_CHARS and squeezed not in folded_corpus:
                    problems.append(
                        f"第 {line_no} 行起的原声与原文对不上：「{piece.strip()[:40]}」。"
                        "原声必须逐字照抄，一个字都不许改（错别字也照抄，那是发帖人写的）。")
    return problems


def lowgrade_quotes(markdown: str, grade_by_mark: Mapping[int, Any]) -> list[str]:
    """等级闸：拿 D 级或未评级证据作原声的那些块（§RPT-5 起 C 级放行，须标等级）。

    共用规则 §5.6 第 4 步写着原声只从 `QUOTE_GRADES` 里挑——之前没人执行，缺陷 9 里
    正文自己写明 S39 是 C 级不得作原声，另一段又拿 S39 当案例。C 级放行后这道闸
    只拦 D 与未评级；C 级有没有标等级由 `unlabeled_quotes` 管。
    """
    problems = []
    for line_no, texts, marks in quote_blocks(markdown):
        if not texts:
            continue
        bad = [(n, str(grade_by_mark.get(n) or "未评级")) for n in sorted(set(marks))
               if str(grade_by_mark.get(n) or "") not in QUOTE_GRADES]
        if bad:
            listed = "、".join(f"S{n:02d}（{g} 级）" for n, g in bad)
            problems.append(
                f"第 {line_no} 行起的原声引的是 {listed}：原声只能从 A/B/C 级证据里挑，"
                "D 与未评级正文不引。换一条 A/B/C 级的原声，"
                "或者把这一段改成不带原声的解读。")
    return problems


#: 出处行上的等级标：「等级 C」或「C 级」。写手偶有全角问号写未评级，一并认成 ?。
_GRADE_LABEL = re.compile(r"等级\s*([ABCD?？])|(?<![A-Za-z])([ABCD])\s*级")


def quote_attributions(markdown: str) -> list[tuple[int, str, list[int]]]:
    """成稿里每个**原声**块的出处行：`(起始行号, 出处行正文合并, 块内角标号)`。

    认块的口径与 `quote_blocks` 一字不差（只认带角标或带出处行的 `>` 块），
    差别只在它留的是出处行而不是引语行——`unlabeled_quotes` 要看的是出处行上有没有等级。
    """
    blocks: list[tuple[int, str, list[int]]] = []
    start, attributions, marks, has_text = 0, [], [], False
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.lstrip().startswith(">"):
            if not attributions and not marks and not has_text:
                start = index
            body = line.lstrip().lstrip(">").strip()
            marks.extend(int(n) for n in _MARK.findall(body))
            body = _MARK.sub("", body).strip()
            if not body:
                continue
            if _ATTRIBUTION_LINE.match(body):
                attributions.append(body)
            else:
                has_text = True
            continue
        if (has_text or marks) and (marks or attributions):
            blocks.append((start, " ".join(attributions), marks))
        start, attributions, marks, has_text = 0, [], [], False
    if (has_text or marks) and (marks or attributions):
        blocks.append((start, " ".join(attributions), marks))
    return blocks


def unlabeled_quotes(markdown: str, grade_by_mark: Mapping[int, Any]) -> list[str]:
    """§RPT-5 货 1：每条原声的出处行必须标等级，且标的要与池里一致。

    C 级放进原声之后，读者分得清「这是旁证」的唯一办法就是稿面那个「等级 C」——
    没标、标错，当轮打回这一节/片，不等验收。没角标的块（把握度那句）不上闸。
    """
    problems = []
    for line_no, attribution, marks in quote_attributions(markdown):
        if not marks:
            continue
        labels = {(a or b).replace("？", "?") for a, b in _GRADE_LABEL.findall(attribution)}
        if not labels:
            problems.append(
                f"第 {line_no} 行起的原声出处行没有标等级：写成"
                f"「—— 平台 · 等级 X [S{marks[0]:02d}]」，X 照信息源池里那条的等级填"
                "（C 级也要引就必须标出来，读者要看得见它只是旁证）。")
            continue
        for n in sorted(set(marks)):
            grade = str(grade_by_mark.get(n) or "?")
            if grade not in labels:
                problems.append(
                    f"第 {line_no} 行起的原声把 S{n:02d} 的等级标错了（写的是 "
                    f"{'/'.join(sorted(labels))}，池里是 {grade} 级）：等级照池里填，不许自己定。")
    return problems


#: §RPT-5 货 2：与库事实相反的模板句。库里有这条评论的正文（写手池的「原话：」栏就是它），
#: 稿面却说「未截取到」——读者顺着角标点进去一看原文在，整份稿的可信度归零。
#: 09-16 r-20271e8a5028 咨询体稿关键发现第 2 条实测。原声引用块不查，那是发帖人的话。
FALSE_FACT_PATTERNS = (r"未截取到评论正文", r"证据池未截取", r"未截取到正文", r"未截取到.{0,6}原话",
                       r"未抓取到评论正文", r"没有截取到.{0,6}正文")


def false_fact_lines(markdown: str) -> list[tuple[int, str]]:
    """写手文本里与库事实相反的行：`(行号, 命中的字)`。"""
    hits = []
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.lstrip().startswith(">"):
            continue
        for pattern in FALSE_FACT_PATTERNS:
            found = re.search(pattern, line)
            if found:
                hits.append((index, found.group(0)))
                break
    return hits


#: 写手那句总括把握度的档位。`**低**`、`低`、`「低」` 都认——形态不该决定判定
#: （§D-072 那条教训：只认引用块，写成普通段落就匹配失灵）。
_CONFIDENCE_SAID = re.compile(r"把握度[^\n]{0,8}?为[*_`「\"'【\[]*\s*([高中低])")


def stated_confidence(markdown: str) -> str:
    """写手在开篇节写的那个档位；写了别的形态读不出来就返回空串。"""
    for line in markdown.splitlines():
        if "把握度" not in line:
            continue
        found = _CONFIDENCE_SAID.search(line)
        if found:
            return found.group(1)
    return ""


def confidence_mismatch(markdown: str, sources: Sequence[Mapping[str, Any]],
                        tables: Mapping[str, Any]) -> list[str]:
    """§RPT-8 货 1：那句总括把握度必须与**逐条汇总出来的**那一档一致。

    不这么锁的话，把握度还是写手拍脑袋定的一个词——提货单写死「⛔ 不许让把握度
    脱离库读数（要能从 tables 读得出来）」，能读出来但没人对，等于没读。

    读不出发现列表（写手没按格式写编号行）就不判：那是**另一道闸**的事
    （切片解析失灵有它自己的兜底），在这里顺手判红会把两个毛病混成一条判词。
    """
    findings = parse_findings(markdown)
    if not findings:
        return []
    tiers = [tier for _, tier, _ in finding_confidences(findings, sources, tables)]
    expected = overall_confidence(tiers)
    said = stated_confidence(markdown)
    if not expected or not said or said == expected:
        return []
    listed = "、".join(f"第 {i} 条{t}" for i, t in enumerate(tiers, 1))
    return [f"那句总括把握度写的是「{said}」，但按各条关键发现自己引的证据汇总出来是"
            f"「{expected}」（{listed}；取中位档，偶数条取更弱的那一档）。"
            f"把那句改成「{expected}」并把理由改成一句人话——⛔ 不许改各条的角标来凑档位。"]


#: §RPT-8 货 3：普遍化断言词。一条把握度低的发现不许拿它们当标题——
#: 真机第 4 条只有同一帖下两条评论，标题却写成「豆包的负向声音多落在……」。
OVERREACH_WORDS = ("普遍", "大多数", "多数用户", "大部分用户", "主流", "集体",
                   "广泛", "人人", "所有用户", "一致认为", "成为共识", "无一例外")
#: 交代了证据规模/范围的写法。低把握度的发现行必须带一处，读者才知道这句话有多大。
#: 数量词（「两篇」「10 条」）与范围词（「同一帖」「本轮样本」）都算。
_SCALE_WORDS = ("同一帖", "同一条", "同一篇", "同一个", "本轮", "本批", "这一批",
                "单条", "单篇", "至少在", "仅在", "只在", "样本", "一例", "个别")
_SCALE_COUNT = re.compile(r"[0-9０-９]+\s*[条篇个位人家]|[一二两三四五六七八九十]\s*[条篇个位人家]")


def _has_scale(text: str) -> bool:
    return bool(_SCALE_COUNT.search(text)) or any(w in text for w in _SCALE_WORDS)


def oversized_finding_lines(markdown: str, sources: Sequence[Mapping[str, Any]],
                            tables: Mapping[str, Any]) -> list[str]:
    """§RPT-8 货 3：发现的标题不得强过它的证据。判的是**摘要里那几条发现行**。

    两条判词，都只对把握度判「低」的那几条生效（`finding_confidence` 同一把尺子）：

    ① 不许出现普遍化断言词（`OVERREACH_WORDS`）——「多数用户」这种话，两条同帖
       评论撑不起来；
    ② 必须在同一行里交代证据规模（几条/几篇，或「同一帖下」「本轮样本」这类范围词）。
       实测真机第 1 条（两篇公众号，其中一篇自述有利益关系）写的是「媒体侧已把豆包
       定位切到办公与生产力入口」，整行读不出它只有两篇文章——②判的就是这个。

    ⛔ 为什么挡在**写作期**（`_write_target`）而不是只留给验收尺子：红一条发现
    = 重写一片，挡在尺子里要等整轮 50 分钟跑完才看得见（§WRITE-1 货 4 同一条道理）。
    验收那头留一条同名判据作兜底，两处 import 同一个函数。
    """
    if not tiering_available(sources, tables):
        return []
    problems = []
    for finding in parse_findings(markdown):
        tier, reason = finding_confidence(finding.marks, sources, tables)
        if tier != CONFIDENCE_TIERS[-1]:
            continue
        hit = next((w for w in OVERREACH_WORDS if w in finding.text), None)
        if hit:
            problems.append(
                f"第 {finding.index} 条发现的把握度是低（{reason}），标题里却写了"
                f"「{hit}」：这是普遍化断言，它的证据撑不起。改成只说这批样本里的事，"
                f"例如「在本轮采到的 N 条里，……」。")
        if not _has_scale(finding.text):
            problems.append(
                f"第 {finding.index} 条发现的把握度是低（{reason}），整行读不出它的证据"
                f"有多大：在这一行里加一处规模或范围，例如「两篇公众号文章显示……」"
                f"「同一条帖子下的两条评论显示……」「本轮样本里……」。")
    return problems


def oversized_shard_title(text: str, finding: Finding | None,
                          sources: Sequence[Mapping[str, Any]],
                          tables: Mapping[str, Any]) -> list[str]:
    """§RPT-8 货 3 的另一半：展开那一节的二级标题也不许强过证据。

    这里只查普遍化断言词，**不查规模**：二级标题是一句结论，塞进「本轮 12 条」
    会把标题写成口径行。规模归发现行（上面那条）与节内正文管。
    """
    if finding is None or not tiering_available(sources, tables):
        return []
    tier, reason = finding_confidence(finding.marks, sources, tables)
    if tier != CONFIDENCE_TIERS[-1]:
        return []
    problems = []
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        hit = next((w for w in OVERREACH_WORDS if w in line), None)
        if hit:
            problems.append(
                f"这条发现的把握度是低（{reason}），二级标题里却写了「{hit}」："
                f"把标题改成只说这批样本里的事（{line.strip()[:34]}）。")
    return problems


#: 建议降级区的小标题。共用规则 §6.5.4：全是孤证的想法机械降级放进这里。
DOWNGRADE_HEADING = "值得进一步验证的方向"
#: 建议节的节名（含读者不明时的改名与竞品稿自带的那一节）。三处都受同一道门禁管。
ADVICE_SECTIONS = frozenset({ADVICE_SECTION, "需要回应的点",
                             ADVICE_SECTION_UNKNOWN_AUDIENCE, IMPLICATIONS_SECTION})
_ENTRY_HEAD = re.compile(r"^\s*\d+[.)、]\s")
#: §D-083：降级区**不只有独立小标题一种形态**。§REISSUE-1 第 2 轮真机两次都把它写成
#: 编号条目标题的起始前缀（原文 `3. **值得进一步验证的方向：办公与生产力场景的真实使用度**`），
#: 闸只认 `#` 开头的行，看不见 break 点，把已经降级过的条目照旧判红 ⇒ 连红两次整轮中止
#: （2 h 37 min、$7.77 白付）。写手照提示词改两次都改不到点上，因为规则没写死形态。
#:
#: ⛔ 判定范围只到「条目号 + 紧跟的强调/引号标记之后**紧接**该词」。
#: 放宽成「整行任意位置出现该词即 break」，写手在一条普通建议里顺口提一句
#: 「这条也算值得进一步验证的方向」，后面整段建议就全免检了——那是把闸修哑。
_DOWNGRADE_ENTRY_HEAD = re.compile(
    r"^\s*\d+[.)、]\s+[*_~`“\"'「『（(【\[]*\s*" + re.escape(DOWNGRADE_HEADING))


def singlesource_advice(lines: Sequence[str], crossref: Mapping[int, Any]) -> list[str]:
    """建议门禁：一条建议所引角标若**全是**单源孤证，必须降级到「值得进一步验证的方向」。

    一条建议横跨两行（建议行 + 依据行），角标分散在两行里；按行判会把只引孤证的那半行
    单独判红（09-05 九格实测两格误报）。**按「条」聚合才对。**

    降级区认两种形态（§D-083，与共用规则 §6.5.4 写死的两种写法一一对应）：
    独立小标题（`## 值得进一步验证的方向`），或编号条目标题的**起始**前缀
    （`3. **值得进一步验证的方向：……**`）。两种都从命中处起、往后不再管。

    这个函数是**生产与验收共用的那一个**：验收尺子 `check_polished._advice_entry_problems`
    直接 import 它。同一个概念两处两个定义，是本项目现形过的一种假绿。
    """
    problems = []
    for entry in advice_entries(lines):
        text = "\n".join(entry)
        marks = _mark_numbers(text)
        verdicts = {str(crossref.get(n)) for n in marks if crossref.get(n)}
        if marks and verdicts and verdicts == {"SINGLE"}:
            problems.append(
                f"这条建议只有单源孤证撑着，应降级到「{DOWNGRADE_HEADING}」："
                f"{text.strip()[:46]}")
    return problems


def advice_entries(lines: Sequence[str], *,
                   include_downgraded: bool = False) -> list[list[str]]:
    """建议节里**受门禁管的**那几条，按「条」切开（降级区之后的默认不返回）。

    §RPT-8 货 5 把它从 `singlesource_advice` 里抽出来：本包要在同一批条目上再加一条
    判词（证据太弱的建议），两道闸必须切在同一个地方。⛔ 不许另写一份切法——
    切得不一样时，一道闸认为降级区已经开始、另一道还在判，写手照两份相反的判词改，
    改不到点上（§D-083 那 2 h 37 min 就是这个形状）。

    `include_downgraded=True`（§SEL-4 加的）：连降级区那几条一起返回。⚠️ 它**不是**
    给证据强度门禁用的——降级区的条目本来就已经降过一级，再判一次强度是重复处罚。
    它是给「引了海外就得标明出身」那道闸用的：**标明出身跟证据强不强没有关系**，
    读者读降级区那几条时同样有权知道依据来自海外社区（实测：第三轮真稿里唯一一条
    海外引用 S38 就落在降级区，按旧判域整道闸一条都量不到，那是假绿）。
    ⛔ 切法仍是**这一个函数**，只多一个开关——别再另写一份。
    """
    entries: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip().startswith("#"):
            if DOWNGRADE_HEADING in line and not include_downgraded:
                break                   # 降级区之后的都不受门禁管
            continue
        if _DOWNGRADE_ENTRY_HEAD.match(line) and not include_downgraded:
            # 行内前缀形态：这一条**本身**就是降级区的第一条，它和它后面的都不受管。
            # 已攒的 `current`（前缀之前的那些条）留给循环外收尾，照旧判。
            break
        if _ENTRY_HEAD.match(line) and current:
            entries.append(current)
            current = []
        current.append(line)
    if current:
        entries.append(current)
    return entries


def weakevidence_advice(lines: Sequence[str], sources: Sequence[Mapping[str, Any]],
                        tables: Mapping[str, Any]) -> list[str]:
    """§RPT-8 货 5：建议只能从**够格**的发现推；不够格的要么降级、要么写明证据强度。

    与 `singlesource_advice` 的关系是**扩展，不是另起一套**（提货单原话：⛔ 不许两套
    规则互相打架）。分工写死在这里，看一眼就分得清：

    - `singlesource_advice` 管**交叉验证结论**：所引角标全是单源孤证 ⇒ 必须降级；
    - 本函数管**证据强度**：把这条建议所引的角标当成一条发现来判把握度
      （`finding_confidence`，同一把尺子），判出「低」⇒ 要么降级，要么在这一条里
      写明它的证据有多弱（「把握度：低」这种明说就放行）。

    两道闸切的是**同一批条目**（`advice_entries`），降级区之后两道都不管，所以
    不会出现「一道说要降级、另一道说这条不用管」。全是孤证的那种两道都会命中，
    判词各说各的那一面，写手照任一条改都落在同一个动作上（降级），不冲突。
    """
    if not tiering_available(sources, tables):
        return []
    problems = []
    for entry in advice_entries(lines):
        # ⛔ 只判**编号条目**。`advice_entries` 会把第一条编号之前的那一段（读者含义
        # 那几个 bullet、引子句）也归成一条，那不是一条建议，判它等于判错人。
        # `singlesource_advice` 的老行为不动——那是既有判词，本包不改它的判域。
        if not entry or not _ENTRY_HEAD.match(entry[0]):
            continue
        text = "\n".join(entry)
        marks = _mark_numbers(text)
        if not marks:
            continue
        tier, reason = finding_confidence([f"S{n:02d}" for n in marks], sources, tables)
        if tier != CONFIDENCE_TIERS[-1] or _STATED_WEAK.search(text):
            continue
        problems.append(
            f"这条建议的证据撑不起一句行动建议（{reason}），要么降级到"
            f"「{DOWNGRADE_HEADING}」，要么在这一条里写明「把握度：低（一句理由）」："
            f"{text.strip()[:46]}")
    return problems


#: 已经把证据弱说在明处的写法。写了就放行——共用规则要的是「别让读者以为它很硬」，
#: 不是「不许提」。⛔ 不匹配整行任意位置的「低」：「成本低」「门槛低」不是在说证据。
_STATED_WEAK = re.compile(r"把握度[：:]\s*[*_`]*低|证据(?:强度)?[：:]\s*[*_`]*弱|"
                          r"单源孤证|仅一条|只有一条|尚待(?:其他)?(?:来源|印证)")


def _ctx(path: Path, research_id: str, runs_root: Path) -> validation.Ctx:
    # runs_root 必须显式传：早先按 `path.parents[3]` 反推，分节目录多一层之后
    # 它指到了研究目录而不是 runs 根，capability 于是把每次 Write 都判成越界
    # （09-05 实测 permission_denials 全是节文件路径，引擎写不下去只好 blocked）。
    return validation.Ctx(
        output_path=path, output_format="markdown", research_id=research_id,
        goal_id=GOAL_ID, agent_id=AGENT_ID,
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None, source_domains=frozenset(), runs_root=runs_root,
    )


def _task(body: str, output_path: Path, research_id: str, model: str,
          runs_root: Path) -> EngineTask:
    return EngineTask(
        body=body, output_path=output_path, output_format="markdown",
        research_id=research_id, goal_id=GOAL_ID, agent_id=AGENT_ID,
        agent_kind=AGENT_KIND, validators=["file_exists"], model=model,
        runs_root=runs_root,
        capability=Capability(
            # 分节落盘要 Edit 追加，Edit 得先 Read 回自己刚写的那一段，故 read 也开在 exports/。
            profile="readonly-analyst", tools=("fs.write", "fs.read"),
            # 相对本次调研产物根（runs/<id>/）：只准动 goals/polished/，工作稿的
            # goal-1/2/3 与 _report_target() 一概碰不到。
            fs=FileSystemScope(read=(f"goals/{GOAL_ID}/**",), write=(f"goals/{GOAL_ID}/**",)),
        ),
    )


#: 正式稿单节撰写的墙钟。适配器默认 300 s（`DEFAULT_CLAUDE_TIMEOUT_SECONDS`），
#: 而提货单 §3.4 自己估的就是「3–8 分钟」——默认值本来就低于本包的预估工时。
#: 09-05 实测：补了用户裁决四条之后内容变厚，单节两次都卡在 300 s 整。
#: 这里只用 `ClaudeAdapter` 的公开构造参数把这一类任务放宽，**不改 `app/adapters/`
#: 里任何文件**（那是本包禁区）；口径变更已报调度。
#: 900 → 1800：实测单节耗时分布是 2–22 分钟，「关键发现」（五条发现各带表+解读+限定）
#: 是长尾。900 s 正好切在尾巴上——**一次超时白烧 15 分钟再重来，比一次给足更贵**，
#: 所以放宽反而更省。r-b10812f664d2 那格就是「关键发现」连撞两次 900 s 才判红的。
SECTION_TIMEOUT_SECONDS = 1800.0


class _CodexModelShim:
    """回退到 Codex 时把 Claude 的模型名摘掉，让 Codex 用它自己的默认档。

    09-05 夜实测：Claude 撞五小时限额（路由日志 `utilization: 0.95`）后路由层回退
    Codex，而任务里带着 `model="opus"` 被原样传过去，Codex 直接 400——
    `The 'opus' model is not supported when using Codex with a ChatGPT account`，
    九格里五格就这么全废了。模型名是跟引擎走的，不能跨引擎照抄。
    `app/adapters/` 是本包禁区，所以在本模块套一层壳，不改适配器本身。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:      # timeout_seconds 等一律透传
        return getattr(self._inner, name)

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> Any:
        from dataclasses import replace

        if getattr(task, "model", None) is not None:
            task = replace(task, model=None)      # None = 用 Codex 自己的默认档
        return await self._inner.run(task, ctx, on_event=on_event)


def default_adapter() -> Any:
    """本包自用的适配器：撰写墙钟放宽，且回退 Codex 时不带 Claude 的模型名。"""
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.codex import CodexAdapter
    from app.adapters.routing import RoutedAdapter

    return RoutedAdapter(adapters={"claude": ClaudeAdapter(timeout_seconds=SECTION_TIMEOUT_SECONDS),
                                   "codex": _CodexModelShim(CodexAdapter())})


def _failure_detail(result: Any) -> str:
    """把引擎为什么没写出来说清楚：报错、权限拒绝、校验失败，一样不少。"""
    bits = []
    for field in ("engine_error", "conclusion_error"):
        value = getattr(result, field, None)
        if value:
            bits.append(f"{field}={value}")
    denials = list(getattr(result, "permission_denials", None) or [])
    if denials:
        bits.append(f"被拒路径/工具 {len(denials)} 次，例如 {denials[0]}")
    report = getattr(result, "validation", None)
    for item in getattr(report, "results", []) or []:
        if str(getattr(item, "verdict", "")).endswith("fail"):
            bits.append(f"校验 {item.name} 未过：{str(getattr(item, 'detail', ''))[:120]}")
    return ("；".join(bits))[:500]


def _timeout_hint(detail: str, finding: Finding | None) -> str:
    """超时的定向提示**分层，不删干净**（§SHARD-1 §七 第 4 条）。

    整节那句「把每条的解读压到三句以内」是拿内容深度换写得完；分片之后前提没了
    ——一片只有一条发现，没有「每条」可压。但**单片仍可能超时**（某条发现角标
    特别多），那时同一个道理对单片仍成立，所以换成片级的一句，而不是删掉。
    """
    if "超时" not in detail:
        return ""
    scope = "这一条" if finding is not None else "每条"
    unit = "这一片" if finding is not None else "这一节"
    return (f"\n上一轮是**超时**被掐的：这一轮把{scope}的解读压到三句以内、"
            f"该引的角标照引，先把{unit}写完整比写满更重要。")


def findings_for(skill: Template, section: str, parts: Sequence[tuple[str, Path]]) -> list[Finding]:
    """这一节要切成几片。声明了才切，且大纲节得先写成——读不出编号列表就退回整节写。

    退回整节是**老行为**，不是新的失败路径：解析失灵最坏也就回到 09-07 之前的样子。
    """
    if section not in skill.shard_sections or not parts:
        return []
    outline = parts[0][1]
    if not outline.is_file():
        return []
    findings = parse_findings(outline.read_text(encoding="utf-8"))
    return findings if should_shard(findings) else []


#: 打回事件的 type。⛔ 不新建表——`events` 是既有的，payload 里带全字段就够了。
REJECT_EVENT_TYPE = "polish_reject"
#: 货 2 的早停线：同一节/同一片连续被**同一道闸**打回这么多次，就不再往下重试。
#: 09-11 调度给的就是这个数（「同一条判词连续打回 3 次就停」）。
REJECT_STREAK_DEFAULT = 3
#: ⛔ 开关默认 **off**：环境变量不设、设成空或 0，`_write_target` 逐字还是老行为。
#: 放环境变量不放 `app/config.py`——那个文件的数值行是禁区。
REJECT_STREAK_ENV = "OWLI_POLISH_REJECT_STREAK"


def reject_streak_limit() -> int:
    """早停线的 N。0 = 关（默认）。`on/true/yes` 等同于默认的 3。

    读不懂的值一律当**关**处理：观测性开关写错一个字母就把正稿跑法改掉，
    是比没有这个开关更坏的事。
    """
    raw = (os.environ.get(REJECT_STREAK_ENV) or "").strip()
    if not raw:
        return 0
    if raw.lower() in {"on", "true", "yes"}:
        return REJECT_STREAK_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def rejects_path(runs_root: Path, research_id: str, template: str) -> Path:
    """这一格的打回台账：`runs/<id>/goals/polished/<template>.rejects.jsonl`。

    与引擎草稿同目录（`GOAL_ID`），一行一条 JSON，追加写不覆盖。
    """
    return Path(runs_root) / research_id / "goals" / GOAL_ID / f"{template}.rejects.jsonl"


def record_reject(store: Any, *, runs_root: Path, research_id: str, template: str,
                  section: str, shard: int | None, attempt: int, gate: str,
                  errors: Sequence[str], streak: int = 1) -> dict[str, Any]:
    """每被打回一次落一行（jsonl）+ 发一条 `polish_reject` 事件。

    **它是读数不是闸**：一个字都不改重试逻辑、不改早停、不改 prompt 文本，
    只是把本来就有的判词多写一份（§OBS-6 货 1）。

    为什么非要这一份：打回原因原先只拼进下一轮 prompt（`build_prompt` 末尾
    「上一轮被打回的原因」那一区），而 **prompt 不进转录**；账本又只在一格跑完
    才落盘。于是 09-11 那条早停线「同一条判词连续打回 3 次就停」根本没有读数可读，
    包终端只能自造尺子，结果把写手正确遵守规则的限定句数成了「被打回」，
    13:43 差点掐掉一轮健康的跑。

    落盘与发事件**双双吞异常**：这是观测，不许因为观测本身把一轮正稿跑挂——
    `store` 可能是只读副本，也可能是不带 `append_event` 的测试替身。
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "template": template,
        "section": section,
        "shard": shard,
        "attempt": attempt,
        "gate": gate,
        # 这一条判词已经连着打回第几次了。早停线读的就是它——把「连续」算在
        # 写的那一刻，读的人不必再自己按 ts 排一遍（自造尺子正是 09-11 的病根）。
        "streak": streak,
        "errors": list(errors),
    }
    try:
        path = rejects_path(runs_root, research_id, template)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 观测落盘失败不许影响正稿
        pass
    append = getattr(store, "append_event", None)
    if callable(append):
        try:
            append(research_id, event_type=REJECT_EVENT_TYPE,
                   payload={"type": REJECT_EVENT_TYPE, **entry},
                   created_at=entry["ts"])
        except Exception:  # noqa: BLE001 — 同上：观测不许反噬正稿
            pass
    return entry


async def _write_target(adapter: Any, skill: Template, data: Mapping[str, Any],
                        report_text: str, *, path: Path, label: str, unit: str,
                        research_id: str, runs_root: Path, pool: frozenset[int],
                        parts: Sequence[tuple[str, Path]], current: str,
                        finding: Finding | None = None, findings: Sequence[Finding] = (),
                        on_event: Any = None, store: Any = None,
                        deadline: float | None = None) -> tuple[bool, tuple[str, ...], int]:
    """写一个目标——整节或一片，最多 `MAX_ATTEMPTS` 次。返回 (成功, 最后的错误, 尝试数)。

    整节与片走的是同一条重试路，只是目标文件、提示词开头与判据措辞不同：
    重试的代价从「整节」降到「一片」（§SHARD-1 §六）。
    """
    errors: tuple[str, ...] = ()
    attempts = 0

    # 货 2：同一道闸连着打回几次了。⛔ `streak_limit == 0`（默认）时它只是个计数，
    # 一个分支都不参与——老行为逐字不变。
    streak, streak_gate = 0, ""
    streak_limit = reject_streak_limit()

    def _reject(gate: str) -> None:
        """把这一次打回落进台账与 events（§OBS-6 货 1）。只写，不判。

        每个 `errors = (...)` 后面各跟一句，`gate` 就是那一条判词的出处；
        少跟一处，读数就会比 `attempts` 少一条，判据 1 当场量得出来。

        顺带把「连续第几次」算在写的那一刻。换了一道闸就从 1 重新数——
        「引语被退两次、又越池一次」不是同一条判词连打三次，早停线不该被它触发。
        """
        nonlocal streak, streak_gate
        streak = streak + 1 if gate == streak_gate else 1
        streak_gate = gate
        record_reject(store, runs_root=runs_root, research_id=research_id,
                      template=skill.name, section=current,
                      shard=finding.index if finding is not None else None,
                      attempt=attempts, gate=gate, errors=errors, streak=streak)

    # 引语闸的底本，一节只算一次：改过字的引语、D 级原声、没标等级的原声都在这里被挡回去。
    corpus = quote_corpus(data, report_text)
    grade_by_mark = {int(str(item["mark"])[1:]): item.get("grade")
                     for item in data.get("sources") or [] if item.get("mark")}
    # 货 4：建议门禁也移到写作期。共用规则 §6.5.4 早写着「全是孤证的建议不算建议」，
    # 之前只有验收尺子 ⑧ 在查——查出来时整轮 37.6 分钟已经付掉了。
    crossref = {int(str(item["mark"])[1:]): item.get("crossref")
                for item in data.get("sources") or [] if item.get("mark")}
    for _ in range(MAX_ATTEMPTS):
        # 货 2 的早停线，挡在**下一轮起跑前**：同一条判词已经连着打回 N 次，
        # 再付一轮引擎也是同样的判词。判完走的是现成的「片失败」路（D-051 语义：
        # 片失败节不判 done、半份稿只进 .rejected），⛔ 不新开失败路径。
        # `streak_limit == 0` 时这个 if 恒假——默认 off，老行为一个字不变。
        if streak_limit and streak >= streak_limit:
            errors = errors + (
                f"「{label}」同一条判词（{streak_gate}）连着打回 {streak} 次，"
                f"到了早停线（{REJECT_STREAK_ENV}={streak_limit}），不再重试。",)
            break
        attempts += 1
        path.unlink(missing_ok=True)
        body = build_prompt(skill, data, report_text, path, errors, parts, current,
                            finding=finding, findings=findings)
        try:
            # `deadline=None`（不分片的节）时这个包装器就是 `await adapter.run(...)`
            # 本身，一行分支都不多走——所以不分片的节行为逐字不变。
            result = await _run_before_section_deadline(
                adapter, _task(body, path, research_id, skill.model, runs_root),
                _ctx(path, research_id, runs_root), on_event, deadline)
        except asyncio.CancelledError:
            raise                       # 取消要往上传，别当成一次失败尝试吞掉
        except SectionWallClockExpired:
            # **必须接在兜底 `except Exception` 前面**：它继承 `TimeoutError` 也就是
            # `Exception`，落到兜底里死因会串成「引擎进程异常退出」，查的人要绕远路。
            # 到点就不再重试——再进来一次剩余已是负数，包装器立刻抛，白走一圈。
            errors = (f"「{label}」没写完，这一节的总墙钟到点了（上限 = 片数 × "
                      f"{SECTION_TIMEOUT_SECONDS:g} 秒）。这是墙钟，不是引擎崩。",)
            _reject("wallclock")
            break
        except Exception as exc:        # noqa: BLE001
            # SDK 子进程整个崩掉时（09-05 实测「Error in hook callback」→「Stream closed」）
            # 异常会冲出 adapter。一片崩了只算这一片一次失败，别丢掉已经写好的别的片。
            errors = (f"「{label}」这一轮引擎进程异常退出：{type(exc).__name__}: {exc}"[:400],)
            _reject("engine_crash")
            continue
        # 判据落在产物上不落在返回码上：传输层报错但落盘了就认；返回 succeeded 但没落盘判没写。
        if not path.is_file() or path.stat().st_size < MIN_SECTION_BYTES:
            detail = _failure_detail(result)
            errors = (f"「{label}」{unit}没写出来或写得过短。" + detail
                      + _timeout_hint(detail, finding),)
            _reject("missing_or_short")
            continue
        text = path.read_text(encoding="utf-8")
        offpool = offpool_marks(text, pool)
        if offpool:
            # 越池改**片级**重写：代价从整节降到一片。
            errors = (f"{unit}引用了信息源池里没有的角标：{'、'.join(offpool)}。"
                      f"池内只有 {len(pool)} 个角标，把越池的那几处删掉或换成池内角标。",)
            _reject("offpool")
            continue
        # 货 2 两道闸：规则早写在共用规则里，正式稿层一直没有程序执行它。
        # 挡在这里而不是挡在验收尺子里——挡在这里当轮就重写，挡在尺子里要等整轮跑完。
        quote_problems = (altered_quotes(text, corpus) + lowgrade_quotes(text, grade_by_mark)
                          # §RPT-5 货 1：C 级放行的代价是稿面必标等级，没标/标错当轮打回。
                          + unlabeled_quotes(text, grade_by_mark))
        if current in ADVICE_SECTIONS:
            quote_problems += singlesource_advice(text.splitlines(), crossref)
            # §RPT-8 货 5：与上一行同一批条目、同一个切法（`advice_entries`），
            # 上一行管交叉验证结论，这一行管证据强度。分工写在 `weakevidence_advice`。
            quote_problems += weakevidence_advice(
                text.splitlines(), data.get("sources") or [], data.get("tables") or {})
        if current in OPENING_SECTIONS:
            # §RPT-8 货 1 + 货 3：把握度那句要与逐条汇总对得上，把握度低的那几条
            # 发现不许把标题写得比证据大。挡在写作期 = 当轮重写这一节；挡在验收尺子
            # 里要等整轮跑完才看得见（§WRITE-1 货 4 同一条道理）。
            quote_problems += confidence_mismatch(
                text, data.get("sources") or [], data.get("tables") or {})
            quote_problems += oversized_finding_lines(
                text, data.get("sources") or [], data.get("tables") or {})
        # §RPT-8 货 3 的另一半：展开那一片的二级标题。只在切了片、且这一片
        # 对得上某条发现时才判（`finding` 为 None 就不判，退回老行为）。
        quote_problems += oversized_shard_title(
            text, finding, data.get("sources") or [], data.get("tables") or {})
        # §RPT-4 C-11：机器话挡在写作期。09-15 重出稿软检读到「被程序按互动量取为代表」「⛔ 不能读成」，
        # 只判黄的话整轮 50 分钟付完才看得见；这几个词在客户稿里没有合法用法，当轮重写这一节/片。
        quote_problems += [
            f"第 {line_no} 行写了系统自己的话「{word}」：这类说明是写给你的，不是写给读者的，"
            "换成人话或删掉（取数、挑原声的过程不写；规矩符号 ⛔ 不写）。"
            for line_no, word in machine_talk_lines(text)]
        # §RPT-5 货 2：与库事实相反的话当轮打回——池里有原话栏却说「未截取到」。
        quote_problems += [
            f"第 {line_no} 行写了与库事实相反的话「{word}」：库里有这条评论的正文（信息源池的"
            "「原话：」栏就是它），不许说「未截取到」。真没有原文才写「本轮这一格没有可引的原声」；"
            "有原文但等级是 C，就引它并在出处行标「等级 C」。"
            for line_no, word in false_fact_lines(text)]
        if quote_problems:
            errors = tuple(f"{unit}{p}" for p in quote_problems)
            _reject("quote")
            continue
        if finding is not None and finding.marks and not any(m in text for m in finding.marks):
            # 片级引用契约：照 D-052「池里每条都要被用到」同思路降级到片级。
            # 这条发现自己一个角标都没有时不要求——不能要求引用不存在的东西。
            errors = (f"{unit}一个自带角标都没引到。这条发现在执行摘要里带的角标是 "
                      f"{'、'.join(finding.marks)}，至少要引到其中一个。",)
            _reject("finding_marks")
            continue
        return True, (), attempts
    return False, errors, attempts


def citation_preflight(store: Any, research_id: str, data: Mapping[str, Any]) -> list[str]:
    """起跑前两条硬断言。任一条不过就不许起写手（2026-09-08 用户拍）。

    **为什么挡在这里而不是挡在验收脚本里**：任何调用方都会经过 `polish()`，
    挡在这里连接口直调也拦得住；挡在矩阵脚本里只拦得住矩阵脚本。

    **背景（这两条各自都真出过事）**：正式稿的角标号取自**工作稿文件**
    （`parse_report(report_text)` → `sources[].mark`），而等级/抓取时间/交叉验证结论
    是**按号码从库里回查**的（`grade_by_mark` 等）。两边号码一旦对不上，
    等级不报错、静默变成 `None`，写手拿不到真等级就**自己编一个**
    ——2026-09-08 那一格实测五条发现里编错四条，「空是缺信息，编是假信息」。
    而两边会对不上，是因为 `backfill.py` 的 `_write_artifact_then_citations`
    **同时改写工作稿文件和重排库里的 citation_no**，只要 rescore 跑在另一个库上就分家。
    """
    problems: list[str] = []
    sources = list(data.get("sources") or [])
    doc = {int(str(item["mark"])[1:]) for item in sources if item.get("mark")}
    rows = list(store.list_evidence(research_id) or [])
    db = {int(r["citation_no"]) for r in rows if r.get("citation_no") is not None}
    if doc and db and (min(doc), max(doc)) != (min(db), max(db)):
        problems.append(
            f"工作稿文件与库里的角标不是同一套：文件 S{min(doc):02d}~S{max(doc):02d}、"
            f"库 S{min(db):02d}~S{max(db):02d}。多半是 rescore 跑在了另一个库上——"
            "跑稿用的库必须与改写过工作稿的那个库是同一个。")
    ungraded = [str(item.get("mark")) for item in sources if not item.get("grade")]
    if ungraded:
        problems.append(
            f"信息源池里有 {len(ungraded)} 条查不到等级（例如 {'、'.join(ungraded[:5])}）。"
            "等级是按角标号从库里回查的，查不到说明号码对不上；"
            "这时候起写手，它会自己编等级。")
    return problems


async def polish(store: Any, research_id: str, runs_root: Path, report_text: str, *,
                 template: str | None = None, adapter: Any = None,
                 on_event: Callable[[Any], Awaitable[None]] | None = None) -> dict[str, Any]:
    """整理一次正式稿。返回 `{status, template, path, tables_path, attempts, offpool}`。"""
    skill = get_template(template)
    data = collect_inputs(store, research_id, report_text)
    md_path, tables_path = artifact_paths(runs_root, research_id, skill.name)
    draft_path = engine_draft_path(runs_root, research_id, skill.name)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    tables_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pool = frozenset(int(s["mark"][1:]) for s in data.get("sources") or [])
    # 起跑前两条硬断言：不过就不起写手，一次引擎都不付。
    blockers = citation_preflight(store, research_id, data)
    if blockers:
        return {"status": "failed", "template": skill.name, "path": str(md_path),
                "draft_path": str(draft_path), "tables_path": str(tables_path),
                "attempts": 0, "offpool": [], "cleared": [], "shards": {},
                "errors": blockers}
    if adapter is None:
        adapter = default_adapter()
    # §OBS-7 货 2：正式稿不进章账本，终态 usage 记到 reports.extra.llm_usage_offledger。
    adapter = UsageMeteringAdapter(adapter, store=store, research_id=research_id,
                                   path_name=f"polish:{skill.name}")
    parts = section_paths(runs_root, research_id, skill.name, sections_for(skill, data))
    parts[0][1].parent.mkdir(parents=True, exist_ok=True)
    # 开跑前清全部旧分节/旧分片，再进节循环（D-041/D-042 销账；见 clear_stale_parts）。
    cleared = clear_stale_parts(runs_root, research_id, skill.name)
    attempts = 0

    def _failed(section: str, path: Path, errors: Sequence[str]) -> dict[str, Any]:
        return {"status": "failed", "template": skill.name, "path": str(md_path),
                "draft_path": str(draft_path), "tables_path": str(tables_path),
                "attempts": attempts, "failed_section": section, "cleared": cleared,
                "shards": shard_counts,
                # 按 mtime 判本轮真写成了哪几节：光看「文件在不在」会少报——开跑前
                # 已经清干净了，所以这里的「在」就是本轮写的（D-041/D-042 修完的红利）。
                "missing_sections": [n for n, q in parts if not q.is_file()],
                "offpool": offpool_marks(path.read_text(encoding="utf-8"), pool)
                if path.is_file() else [], "errors": list(errors)}

    shard_counts: dict[str, int] = {}
    for name, path in parts:
        findings = findings_for(skill, name, parts)
        shard_counts[name] = len(findings)
        if not findings:
            ok, errors, used = await _write_target(
                adapter, skill, data, report_text, path=path, label=name, unit="这一节",
                research_id=research_id, runs_root=runs_root, pool=pool, parts=parts,
                current=name, on_event=on_event, store=store)
            attempts += used
            if not ok:
                return _failed(name, path, errors)
            continue
        # 分片的节：一条发现一片，片数由摘要定（`findings_for`）。
        # 节级总上限（货 5，沿用 `sectioning._run_before_section_deadline`）：
        # **片墙钟一个字不改**——每片仍旧各拿适配器那份 SECTION_TIMEOUT_SECONDS
        # （standard 档口径，`config.py` 的 chapter_wall_clock_seconds 同数），
        # 只在整节头上多扣一个绝对时刻。隔壁 `sectioning.py:2043` 试过让几片**共用**
        # 一个节闹钟并否掉了：「共用的话第 1 片跑掉 221 s，剩下三片分 109 s，必全灭」，
        # 所以这里夹的是**上界**不是共用。
        # 它封的是「重试把上限乘出去」：没有它，一节最坏 = 片数 × MAX_ATTEMPTS × 墙钟；
        # 有了它 = 片数 × 墙钟，与分片前的每节口径同一个数量级。
        deadline = (asyncio.get_running_loop().time()
                    + len(findings) * SECTION_TIMEOUT_SECONDS)
        paths = shard_paths(path, len(findings))
        for finding, spath in zip(findings, paths):
            label = f"{name} 第 {finding.index} 条发现"
            ok, errors, used = await _write_target(
                adapter, skill, data, report_text, path=spath, label=label, unit="这一片",
                research_id=research_id, runs_root=runs_root, pool=pool, parts=parts,
                current=name, finding=finding, findings=findings, on_event=on_event,
                store=store, deadline=deadline)
            attempts += used
            if not ok:
                # D-051：任一片没写成，这一节不算 done——残缺的合并稿不许往下走。
                path.unlink(missing_ok=True)
                return _failed(name, spath, errors)
        # 合并 = 按片序拼接片正文；信息源表不在这里动，由 `assemble` 最后统一追加。
        path.write_text(merge_shards(paths), encoding="utf-8")
        # 角标检查从一处变两处：每片写完在 `_write_target` 里查过一次（早失败早重写），
        # 合并后再查一次兜跨片的情况。片都干净而合并脏，只可能是拼错了片——
        # 这是保险丝不是重写口，所以直接判红，别再付一轮引擎。
        merged_offpool = offpool_marks(path.read_text(encoding="utf-8"), pool)
        if merged_offpool:
            return _failed(name, path, [
                f"「{name}」各片单独都没越池，合并后却出现越池角标 "
                f"{'、'.join(merged_offpool)}——合并取错片了。"])
    from app.report.render import parse_report

    # §RPT-8 货 1：附录那张逐条把握度表读的发现列表，与摘要那一行、与切片用的
    # 是同一份 `parse_findings`（开篇节的正文）。⛔ 不另起一条解析路。
    opening_findings = next(
        (parse_findings(path.read_text(encoding="utf-8"))
         for name, path in parts if name in OPENING_SECTIONS and path.is_file()), [])
    markdown = assemble(parts, data.get("sources") or [], tables=data.get("tables") or {},
                        subjects=data.get("subjects") or (),
                        counts=data.get("counts") or {},
                        appendix_blocks=(
        # §RPT-3 货 4：把握度的两张分布表由程序挂附录，摘要那句的依据读者看得见。
        confidence_tables(data.get("tables") or {}, opening_findings,
                          data.get("sources") or []),
        missing_table(parse_report(report_text).get("missing") or [],
                      data.get("objectives") or [],
                      chapters=data.get("chapters") or []),
        basis_table(data.get("tables") or {}),
        # 词表命中表只在这里露面：写手拿不到它，附录给读者留个对照（用户 09-09 拍）。
        lexicon_reference_table(data.get("tables") or {}),
        # 原声表整张挪到附录（用户 09-11 拍）。⚠️ 与上一行不同：写手**仍然拿得到**
        # 这张表的数据，因为正文每个主题段要引 2–3 条原声写成引用块（共用规则 §5.6
        # 步骤 4）。挪的是「表」，不是「原话」。
        quotes_reference_table(data.get("tables") or {}),
        # §RPT-4 货 2：对照实体的评论只作参照，挂附录。
        contrast_reference_table(data.get("tables") or {}),
        # §RPT-4 C-14：证据时间范围一句。
        timespan_block(data.get("tables") or {}),
        # §RPT-8 货 4②：单人线索。正文引过的不再摆一遍，所以要先数正文实引角标
        # （`body_marks` 是纯函数，这里数与下面回填 `counts` 数的是同一批片）。
        clues_block(data.get("clues") or [], body_marks(parts)),
    ))
    # §RPT-4 C-11：正文实引条数组装完才知道，回填进 tables.json 的 counts——
    # 摘要里注入的「正文实际引用证据 N 条」要在尺子 ④ 的白名单里有出处。
    data.setdefault("counts", {})[BODY_CITED_KEY] = len(body_marks(parts))
    tables_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    draft_path.write_text(markdown, encoding="utf-8")
    # 引擎只写得进 goals/polished/；exports/ 这一份由本模块搬，接口与登记都指它。
    md_path.write_text(markdown, encoding="utf-8")
    # 落盘后当场回读。用户 09-07 在 8977 上撞到过 goals/ 有三份成稿、exports/ 一份都没有
    # 的现场：页面只认 exports/，于是三个模板全 404、静默退回工作稿，而这一头照报 ok。
    # 搬运没成功就必须当场判失败，别把「写了 goals 没写 exports」报成成功。
    if not md_path.is_file():
        return {"status": "failed", "template": skill.name, "path": str(md_path),
                "draft_path": str(draft_path), "tables_path": str(tables_path),
                "attempts": attempts, "offpool": [], "cleared": cleared,
                "shards": shard_counts,
                "errors": [f"正式稿没落到 exports/：{md_path}（goals/ 那份在 {draft_path}）"]}
    return {"status": "ok", "template": skill.name, "path": str(md_path),
            "draft_path": str(draft_path), "tables_path": str(tables_path),
            "attempts": attempts, "offpool": [], "cleared": cleared,
            "shards": shard_counts}
