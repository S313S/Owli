"""正式稿撰写入口：一次引擎调用，把工作稿 + 证据池整理成人能读的咨询报告。

不采集、不评级、不碰工作稿产物——只读库与成稿，只写 `runs/<id>/exports/`。
引擎照 `app/reliability/backfill.py:630-647` 直调适配器（`model="opus"`，适配器已透传）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineTask
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


def section_paths(runs_root: Path, research_id: str, template: str,
                  sections: Sequence[str]) -> list[tuple[str, Path]]:
    """每节各一个文件。

    09-05 实测：让写手用 Edit 往同一个文件里一节节追加，文件越长每次追加越贵，
    稳定写到第四五节就断（两轮都缺「建议、附录」）。改成一节一个文件、各写一次，
    骨架由本模块按声明顺序拼——顺带把「标题写错/降级/漏节」这一类失败整个根除。
    """
    root = (Path(runs_root) / research_id / "goals" / GOAL_ID / f"{template}-parts")
    return [(name, root / f"{index:02d}-{name}.md") for index, name in enumerate(sections, 1)]


def assemble(parts: Sequence[tuple[str, Path]]) -> str:
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


def build_prompt(template: Template, data: Mapping[str, Any], report_text: str,
                 output_path: Path, errors: tuple[str, ...] = (),
                 parts: Sequence[tuple[str, Path]] = (), current: str | None = None) -> str:
    """共用硬规则 + 模板正文 + 输入区；重写轮把上一轮的错误原样附在最后。"""
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
    parts = [
        # 必须给绝对路径：只给文件名时引擎会拿工作区根去猜，两次都被 capability 判越界。
        # 一次只写一节：适配器每次任务硬墙钟 300 秒（`DEFAULT_CLAUDE_TIMEOUT_SECONDS`，
        # 在本包禁区里改不得），整份五节塞不进去，09-05 实测每轮都写到第三节被掐。
        f"# 任务\n这是一份《{template.title}》正式稿，一共 {len(parts)} 节，"
        f"由多轮分头写。**本轮你只写「{current}」这一节**，用 Write 写到：\n\n"
        f"`{output_path}`\n\n"
        "这是你本轮唯一能写的路径，写别处一定被拒。**别的节这轮不要碰、不要写。**\n"
        f"全篇骨架（给你看上下文，不是让你都写）：{' / '.join(name for name, _ in parts)}\n"
        f"**文件里只写「{current}」这一节的正文，不要写标题行**——一级标题由程序统一加，"
        "你写了反而会重复。\n"
        "只重新组织与解读，不做新的调研，不编造任何事实与数字。",
        f"# 共用硬规则\n\n{shared_rules()}",
        f"# 本模板骨架\n\n{template.body}",
        f"# 调研问题\n{data.get('research_question')}",
        f"# 本次研究的目标\n{objectives}",
        f"# 涉及的实体\n{'、'.join(data.get('entities') or [])}",
        f"# 信息源池（只能引这些角标，一个都不许多；第三栏是这条源的交叉验证结论）\n{pool}",
        f"# 确定性数据表（数字的唯一来源，一个数都不许改）\n```json\n{tables}\n```",
        f"# 工作稿\n\n{_work_view(data, report_text)}",
    ]
    if errors:
        parts.append("# 上一轮被打回的原因（必须改掉）\n" + "\n".join(f"- {e}" for e in errors))
    return "\n\n".join(parts)


def offpool_marks(markdown: str, pool: frozenset[int]) -> list[str]:
    """成稿里越出信息源池的角标，升序去重。与工作稿的 `_shard_stale_citations` 同思路。"""
    used = {int(n) for n in _MARK.findall(markdown)}
    return [f"S{n:02d}" for n in sorted(used - pool)]


def _ctx(path: Path, research_id: str) -> validation.Ctx:
    return validation.Ctx(
        output_path=path, output_format="markdown", research_id=research_id,
        goal_id=GOAL_ID, agent_id=AGENT_ID,
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None, source_domains=frozenset(), runs_root=path.parents[3],
    )


def _task(body: str, output_path: Path, research_id: str, model: str) -> EngineTask:
    return EngineTask(
        body=body, output_path=output_path, output_format="markdown",
        research_id=research_id, goal_id=GOAL_ID, agent_id=AGENT_ID,
        agent_kind=AGENT_KIND, validators=["file_exists"], model=model,
        runs_root=output_path.parents[3],
        capability=Capability(
            # 分节落盘要 Edit 追加，Edit 得先 Read 回自己刚写的那一段，故 read 也开在 exports/。
            profile="readonly-analyst", tools=("fs.write", "fs.read"),
            # 相对本次调研产物根（runs/<id>/）：只准动 goals/polished/，工作稿的
            # goal-1/2/3 与 _report_target() 一概碰不到。
            fs=FileSystemScope(read=(f"goals/{GOAL_ID}/**",), write=(f"goals/{GOAL_ID}/**",)),
        ),
    )


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
        from app.adapters.routing import RoutedAdapter

        adapter = RoutedAdapter()
    parts = section_paths(runs_root, research_id, skill.name, skill.sections)
    parts[0][1].parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    for name, path in parts:
        errors: tuple[str, ...] = ()
        for _ in range(MAX_ATTEMPTS):
            attempts += 1
            path.unlink(missing_ok=True)
            body = build_prompt(skill, data, report_text, path, errors, parts, name)
            result = await adapter.run(_task(body, path, research_id, skill.model),
                                       _ctx(path, research_id), on_event=on_event)
            # 判据落在产物上不落在返回码上：传输层报错但这一节落盘了就认（照 backfill 的
            # `_recover_transport_completion` 同思路）；返回 succeeded 但没落盘一样判没写。
            if not path.is_file() or path.stat().st_size < MIN_SECTION_BYTES:
                engine_error = (getattr(result, "engine_error", None)
                                or getattr(result, "conclusion_error", None))
                errors = (f"「{name}」这一节没写出来或写得过短。"
                          + (f"上一轮引擎报错：{engine_error}" if engine_error else ""),)
                continue
            offpool = offpool_marks(path.read_text(encoding="utf-8"), pool)
            if not offpool:
                break
            errors = (f"这一节引用了信息源池里没有的角标：{'、'.join(offpool)}。"
                      f"池内只有 {len(pool)} 个角标，把越池的那几处删掉或换成池内角标。",)
        else:
            return {"status": "failed", "template": skill.name, "path": str(md_path),
                    "draft_path": str(draft_path), "tables_path": str(tables_path),
                    "attempts": attempts, "failed_section": name,
                    "missing_sections": [n for n, p in parts if not p.is_file()],
                    "offpool": offpool_marks(path.read_text(encoding="utf-8"), pool)
                    if path.is_file() else [], "errors": list(errors)}
    markdown = assemble(parts)
    draft_path.write_text(markdown, encoding="utf-8")
    # 引擎只写得进 goals/polished/；exports/ 这一份由本模块搬，接口与登记都指它。
    md_path.write_text(markdown, encoding="utf-8")
    return {"status": "ok", "template": skill.name, "path": str(md_path),
            "draft_path": str(draft_path), "tables_path": str(tables_path),
            "attempts": attempts, "offpool": []}
