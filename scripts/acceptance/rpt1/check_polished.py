#!/usr/bin/env python3
"""§RPT-1 货 2 尺子：核一份正式稿是否合规。

    python3 scripts/acceptance/rpt1/check_polished.py <正式稿.md> <tables.json> <工作稿>

五条判据（全过才 PASS）：
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

from app.report.polish.skills import load_templates  # noqa: E402

#: 内部词：读者不知道也不需要知道研究是怎么切块的。
FORBIDDEN = (r"goal-\d", r"sec-\d", r"ch-\d", "本片", "本节样本", "本章样本", "上游目标",
             "采集章", "撰写章")
#: ⑤ 的程度词：没数字时至少要有一个判断的力度。
DEGREE_WORDS = ("最", "更", "近半", "过半", "多数", "少数", "普遍", "集中", "远", "几乎",
                "全部", "唯一", "首", "领先", "落后", "不足", "超过", "翻倍", "零")
MARK = re.compile(r"\[S(\d{2,})\]")
#: 附录的信息源清单里角标是裸写的（`S01｜A 级｜…`），也得当角标认，
#: 否则 ④ 会把 `S01` 里的 `01` 当成一个没出处的数字。
MARK_ANY = re.compile(r"\[?S(\d{2,})\]?")
NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
#: 日期、版本号、列表序号这些不是「论据数字」，不纳入 ④。
SKIP_NUMBER_CONTEXT = re.compile(r"^\s*\d+[.)、]\s|20\d\d[-年]\d|v\d")


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


def _template_for(markdown: str, tables_path: Path):
    """模板名从 tables.json 的文件名里认：<id>.polished.<模板>.tables.json。"""
    name = tables_path.name.split(".polished.", 1)[-1].removesuffix(".tables.json")
    for template in load_templates():
        if template.name == name:
            return template
    raise SystemExit(f"× 认不出模板 {name!r}（文件名要形如 <id>.polished.<模板>.tables.json）")


def check_no_internal_words(markdown: str) -> list[str]:
    hits = []
    for pattern in FORBIDDEN:
        for match in re.finditer(pattern, markdown):
            line = markdown[:match.start()].count("\n") + 1
            hits.append(f"第 {line} 行命中内部词 {match.group(0)!r}")
    return hits


def check_sections(markdown: str, template) -> list[str]:
    found = set(_sections_of(markdown, 1))
    return [f"缺一级标题 {s!r}" for s in template.sections if s not in found]


def check_marks_in_pool(markdown: str, pool: set[int]) -> list[str]:
    used = {int(n) for n in MARK_ANY.findall(markdown)}
    offpool = sorted(used - pool)
    problems = [f"角标 S{n:02d} 不在信息源池里" for n in offpool]
    if not used:
        problems.append("全文一个角标都没有")
    return problems


def check_numbers(markdown: str, allowed: set[str]) -> list[str]:
    problems = []
    for index, line in enumerate(markdown.splitlines(), start=1):
        if line.startswith("|") or line.startswith(">") or SKIP_NUMBER_CONTEXT.search(line):
            continue  # 表格行、原文引用、列表序号不纳入
        naked = MARK_ANY.sub("", line)
        for number in NUMBER.findall(naked):
            if number in allowed or number.rstrip("%") in allowed:
                continue
            if MARK_ANY.search(line):
                continue  # 同句带角标 = 有出处
            problems.append(f"第 {index} 行的数字 {number!r} 在数据表里找不到、同句也没角标")
    return problems


def check_action_titles(markdown: str, template) -> list[str]:
    problems = []
    for title in _sections_of(markdown, 2):
        if title in template.sections:
            continue
        if any(char.isdigit() for char in title) or any(w in title for w in DEGREE_WORDS):
            continue
        problems.append(f"二级标题 {title!r} 不是行动式标题：既没量级也没程度词")
    return problems


CHECKS = ("① 无内部词", "② 一级标题齐", "③ 角标不越池", "④ 数字有出处", "⑤ 行动式标题")


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
    findings = {
        CHECKS[0]: check_no_internal_words(markdown),
        CHECKS[1]: check_sections(markdown, template),
        CHECKS[2]: check_marks_in_pool(markdown, pool),
        CHECKS[3]: check_numbers(markdown, allowed),
        CHECKS[4]: check_action_titles(markdown, template),
    }
    if pool != work_marks:
        findings[CHECKS[2]].append(
            f"信息源池与工作稿角标对不上：池 {len(pool)} 个、工作稿 {len(work_marks)} 个")
    return findings


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
    failed = [name for name in CHECKS if findings[name]]
    print(("× 未过：" + "、".join(failed)) if failed else "√ 五条判据全过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
