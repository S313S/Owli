"""正式稿撰写入口：一次引擎调用，把工作稿 + 证据池整理成人能读的咨询报告。

不采集、不评级、不碰工作稿产物——只读库与成稿，只写 `runs/<id>/exports/`。
引擎照 `app/reliability/backfill.py:630-647` 直调适配器（`model="opus"`，适配器已透传）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineTask
from app.orchestrator.sectioning import (SectionWallClockExpired,  # 只 import，不改那个文件
                                         _run_before_section_deadline)
from app.report.polish.sharding import (Finding, merge_shards, parse_findings, shard_paths,
                                        should_shard)
from app.report.polish.skills import Template, get_template, shared_rules
from app.report.polish.tables import collect_inputs

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
IMPLICATIONS_SECTION = "对提问方意味着什么"


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


def sources_table(sources: Sequence[Mapping[str, Any]]) -> str:
    """信息源清单：由代码生成，不让写手誊抄。

    09-05 实测：三格里两格死在「附录」，报错都是
    `API Error: The socket connection was closed unexpectedly`——本机代理掐长响应。
    附录之所以最长，就是因为它要逐条重打几十条角标/标题/链接/等级，而这些数据本就在
    `tables.json` 里。让模型重打一遍既贵、又正好落在唯一会断的地方，且可能抄错。
    """
    grade_note = {"A": "可独立支撑结论", "B": "较可靠，宜与他源同现",
                  "C": "只作旁证", "D": "线索级"}
    lines = ["## 信息源清单", "",
             "（本节由程序按证据库直接生成，未经改写。）", "",
             "| 角标 | 等级 | 说明 | 标题 | 抓取时间 | 链接 |", "|---|---|---|---|---|---|"]
    for item in sources:
        grade = str(item.get("grade") or "?")
        title = str(item.get("title") or "").replace("|", "｜").strip() or "（无标题）"
        url = str(item.get("url") or "")
        lines.append(f"| {item['mark']} | {grade} | {grade_note.get(grade, '未评级')} "
                     f"| {title} | {_fetched_at_cell(item.get('fetched_at'))} | {url} |")
    return "\n".join(lines) + "\n"


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


def assemble(parts: Sequence[tuple[str, Path]],
             sources: Sequence[Mapping[str, Any]] = ()) -> str:
    """把各节拼成成稿：一级标题由代码写，写手只交正文。"""
    chunks = []
    for name, path in parts:
        body = path.read_text(encoding="utf-8").strip()
        # 写手偶尔仍会把标题写进正文，重复的那一行去掉，免得出现两个同名一级标题。
        # 只认「整第一行就是标题」，不能按前缀剥——正文第一句常以节名开头
        # （「建议正文……」会被剥成「正文……」，用例抓到过）。
        first, _, rest = body.partition("\n")
        if first.strip() in (f"# {name}", f"## {name}", name):
            body = rest.lstrip("\n")
        chunks.append(f"# {name}\n\n{body}")
    if sources:
        # 清单挂在最后一节（三个模板的末节都是附录）末尾。
        chunks[-1] = chunks[-1].rstrip() + "\n\n" + sources_table(sources)
    return "\n\n".join(chunks) + "\n"
_MARK = re.compile(r"\[S(\d{2,})\]")


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
    missing = "\n".join(
        f"- {item.get('chapter_id') or ''}：{item.get('reason') or ''} {item.get('text') or ''}".strip()
        for item in view.get("missing") or [])
    body = "\n\n".join(str(s.get("markdown") or "") for s in view.get("sections") or [])
    return (f"### 工作稿标题\n{data.get('title')}\n\n### 工作稿的结论行（原样）\n{conclusions}\n\n"
            f"### 工作稿的缺失清单（要在附录里改成人话）\n{missing or '（无）'}\n\n"
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
            "行动式二级标题 → 一张表 → 三到五句解读 → 反证或限定。\n"
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
    pool = "\n".join(
        f"- {s['mark']}｜{s.get('grade') or '?'} 级｜{verdicts.get(str(s.get('crossref')), '未登记')}"
        f"｜{s.get('title') or ''}｜{s.get('url') or ''}"
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
        f"# 信息源池（只能引这些角标，一个都不许多；第三栏是这条源的交叉验证结论）\n{pool}",
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
        return (f"{AUDIENCE_UNKNOWN}\n读者身份未知。建议节改名「对不同读者的含义」，"
                "分三行分别写给竞品团队 / 本产品团队 / 投资分析，"
                "每行一句「这对你意味着什么」+ 一个动作。")
    lines = [role, f"每条建议的第一句必须先写「对{role}意味着什么」，再写动作。"]
    if stake and stake != AUDIENCE_UNKNOWN:
        lines.append(f"他们最想知道的：{stake}")
    return "\n".join(lines)


def offpool_marks(markdown: str, pool: frozenset[int]) -> list[str]:
    """成稿里越出信息源池的角标，升序去重。与工作稿的 `_shard_stale_citations` 同思路。"""
    used = {int(n) for n in _MARK.findall(markdown)}
    return [f"S{n:02d}" for n in sorted(used - pool)]


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


async def _write_target(adapter: Any, skill: Template, data: Mapping[str, Any],
                        report_text: str, *, path: Path, label: str, unit: str,
                        research_id: str, runs_root: Path, pool: frozenset[int],
                        parts: Sequence[tuple[str, Path]], current: str,
                        finding: Finding | None = None, findings: Sequence[Finding] = (),
                        on_event: Any = None,
                        deadline: float | None = None) -> tuple[bool, tuple[str, ...], int]:
    """写一个目标——整节或一片，最多 `MAX_ATTEMPTS` 次。返回 (成功, 最后的错误, 尝试数)。

    整节与片走的是同一条重试路，只是目标文件、提示词开头与判据措辞不同：
    重试的代价从「整节」降到「一片」（§SHARD-1 §六）。
    """
    errors: tuple[str, ...] = ()
    attempts = 0
    for _ in range(MAX_ATTEMPTS):
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
            break
        except Exception as exc:        # noqa: BLE001
            # SDK 子进程整个崩掉时（09-05 实测「Error in hook callback」→「Stream closed」）
            # 异常会冲出 adapter。一片崩了只算这一片一次失败，别丢掉已经写好的别的片。
            errors = (f"「{label}」这一轮引擎进程异常退出：{type(exc).__name__}: {exc}"[:400],)
            continue
        # 判据落在产物上不落在返回码上：传输层报错但落盘了就认；返回 succeeded 但没落盘判没写。
        if not path.is_file() or path.stat().st_size < MIN_SECTION_BYTES:
            detail = _failure_detail(result)
            errors = (f"「{label}」{unit}没写出来或写得过短。" + detail
                      + _timeout_hint(detail, finding),)
            continue
        text = path.read_text(encoding="utf-8")
        offpool = offpool_marks(text, pool)
        if offpool:
            # 越池改**片级**重写：代价从整节降到一片。
            errors = (f"{unit}引用了信息源池里没有的角标：{'、'.join(offpool)}。"
                      f"池内只有 {len(pool)} 个角标，把越池的那几处删掉或换成池内角标。",)
            continue
        if finding is not None and finding.marks and not any(m in text for m in finding.marks):
            # 片级引用契约：照 D-052「池里每条都要被用到」同思路降级到片级。
            # 这条发现自己一个角标都没有时不要求——不能要求引用不存在的东西。
            errors = (f"{unit}一个自带角标都没引到。这条发现在执行摘要里带的角标是 "
                      f"{'、'.join(finding.marks)}，至少要引到其中一个。",)
            continue
        return True, (), attempts
    return False, errors, attempts


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
    if adapter is None:
        adapter = default_adapter()
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
                current=name, on_event=on_event)
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
                deadline=deadline)
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
    markdown = assemble(parts, data.get("sources") or [])
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
