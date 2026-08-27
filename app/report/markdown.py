"""报告 Markdown 的信息源清单渲染。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.reliability.scoring import SCORE_FIELDS, rating_notes_problem
from app.store.dao import normalize_permalink


_LABELS = ("权威", "时效", "交叉", "完整", "无关")
_SOURCE_LINE = re.compile(
    r"^(?P<prefix>\s*-\s*)\[S(?P<number>\d{2})\]\s*"
    r"\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\).*$"
)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _citation(value: Any, fallback: int) -> str:
    number = value if isinstance(value, int) and not isinstance(value, bool) else fallback
    if number < 1 or number > 99:
        raise ValueError("citation_no 必须是 1–99 整数")
    return f"S{number:02d}"


def _title(value: Any) -> str:
    text = str(value or "未命名来源").replace("[", "［").replace("]", "］")
    return " ".join(text.split())


def render_source_list(evidence: Sequence[Mapping[str, Any]]) -> str:
    """按 §4.3 数据要求渲染可点击、可正则复核的 Markdown 清单。"""

    lines: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(evidence, start=1):
        mark = _citation(item.get("citation_no"), index)
        if mark in seen:
            raise ValueError(f"重复信息源角标：[{mark}]")
        seen.add(mark)
        permalink = str(item.get("permalink") or "").strip()
        fetched_at = str(item.get("fetched_at") or "").strip()
        if not permalink.startswith(("https://", "http://")):
            raise ValueError(f"[{mark}] permalink 必须是可点击 HTTP(S) 链接")
        if not fetched_at:
            raise ValueError(f"[{mark}] fetched_at 不能为空")
        scores = []
        for label, field in zip(_LABELS, SCORE_FIELDS):
            value = item.get(field)
            if value is None:
                scores.append(f"{label}?")
                continue
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2:
                raise ValueError(f"[{mark}] {field} 必须是 0–2 整数")
            scores.append(f"{label}{value}")
        notes = item.get("rating_notes")
        problem = rating_notes_problem(notes, item)
        if problem is not None:
            raise ValueError(f"[{mark}] rating_notes 非法：{problem}")
        lines.append(
            f"- [{mark}] [{_title(item.get('title'))}]({permalink})"
            f" · fetched_at={fetched_at}"
            f" · 五维={'/'.join(scores)}"
            f" · rating_notes={notes}"
        )
    if not lines:
        raise ValueError("信息源清单至少需要 1 条证据")
    return "\n".join(lines)


def render_report(
    conclusions: Sequence[str], evidence: Sequence[Mapping[str, Any]]
) -> str:
    """渲染最小报告；双向一致性仍由既有四件套校验。"""

    if not conclusions:
        raise ValueError("报告至少需要 1 条结论")
    body = "\n".join(f"- {str(item).strip()}" for item in conclusions)
    return f"# 结论\n\n{body}\n\n# 信息源\n\n{render_source_list(evidence)}\n"


def enrich_source_section(
    markdown: str, evidence: Sequence[Mapping[str, Any]]
) -> str:
    """按 permalink 把 agent 已写的 [SNN] 清单确定性升级为五维数据行。"""

    by_url = {
        str(item.get("permalink") or "").strip(): dict(item)
        for item in evidence
        if str(item.get("permalink") or "").strip()
    }
    lines = markdown.splitlines()
    in_sources = False
    changed = False
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading:
            in_sources = "信息源" in heading.group(2)
            continue
        if not in_sources:
            continue
        matched = _SOURCE_LINE.match(line)
        if matched is None:
            continue
        item = by_url.get(matched.group("url"))
        if item is None:
            continue
        item["citation_no"] = int(matched.group("number"))
        item["title"] = matched.group("title")
        lines[index] = render_source_list([item])
        changed = True
    if not changed:
        return markdown
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join(lines) + suffix


_MARK = re.compile(r"\[S\d{2}\]")
_LINK_URL = re.compile(r"\((https?://[^)\s]+)\)")


def _subtree_spans(lines: Sequence[str]) -> list[tuple[int, int, str]]:
    """(起始行, 结束行, 标题)；子树含标题行，延伸到下一个同级或更高级标题。"""
    headings = [
        (index, len(matched.group(1)), matched.group(2).strip())
        for index, line in enumerate(lines)
        if (matched := _HEADING.match(line))
    ]
    spans: list[tuple[int, int, str]] = []
    for position, (start, level, title) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        spans.append((start, end, title))
    return spans


def _split_section_structures(text: str) -> tuple[str, str, list[str]]:
    """摘出一节里的「结论 / 信息源 / 缺失清单」子树。

    返回 (剩余正文, 结论正文, 信息源条目行)。节内缺失清单只摘不留 ——
    拼装器按账本另写权威的一份，节内那份与之重复（worklog report-module §10.4）。
    """
    lines = text.splitlines()
    removed: set[int] = set()
    conclusion_parts: list[str] = []
    source_lines: list[str] = []
    for start, end, title in _subtree_spans(lines):
        if start in removed:
            continue
        if title == "结论":
            conclusion_parts.append("\n".join(lines[start + 1:end]).strip())
        elif "信息源" in title:
            source_lines.extend(
                line for line in lines[start + 1:end] if line.strip()
            )
        elif "缺失清单" in title:
            pass
        else:
            continue
        removed.update(range(start, end))
    remainder = "\n".join(
        line for index, line in enumerate(lines) if index not in removed
    ).strip()
    return remainder, "\n\n".join(part for part in conclusion_parts if part), source_lines


def merge_sectioned_markdown(
    title: str,
    section_texts: Sequence[str],
    missing_lines: Sequence[str],
    *,
    citation_numbers: Mapping[str, int] | None = None,
) -> str:
    """把各节 Markdown 归并成单结论、单信息源的整卷报告。

    节按 goal 切且每节都带自己的「结论/信息源」，直接拼接会得到三份并列的
    小报告（worklog report-module §10.4）。这里做确定性归并：各节的结论合入
    章末唯一的「结论」，信息源按 permalink 去重合入唯一的「信息源」并全卷
    统一重排 [SNN] 角标（各节独立编号会互相撞号）。
    """
    inventory: dict[str, tuple[str, str]] = {}  # 去重键 → (新角标, 条目行)
    plain_source_lines: list[str] = []  # 无角标条目按原文去重保留，不参与重排
    merged_sections: list[str] = []
    merged_conclusions: list[str] = []
    for index, text in enumerate(section_texts):
        remainder, conclusion, source_lines = _split_section_structures(text)
        mapping: dict[str, str] = {}
        for line in source_lines:
            mark_match = _MARK.search(line)
            if mark_match is None:
                if line not in plain_source_lines:
                    plain_source_lines.append(line)
                continue
            old_mark = mark_match.group(0)
            url_match = _LINK_URL.search(line)
            key = url_match.group(1) if url_match else f"sec-{index}:{old_mark}"
            if key not in inventory:
                number = (
                    citation_numbers.get(normalize_permalink(key))
                    if citation_numbers is not None and url_match is not None
                    else None
                )
                new_mark = (
                    f"[S{number:02d}]"
                    if number is not None
                    else f"[S{len(inventory) + 1:02d}]"
                )
                inventory[key] = (new_mark, line.replace(old_mark, new_mark, 1))
            mapping[old_mark] = inventory[key][0]

        def renumber(value: str) -> str:
            return _MARK.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)

        if remainder:
            merged_sections.append(renumber(remainder))
        if conclusion:
            merged_conclusions.append(renumber(conclusion))
    blocks = [f"# {title}", ""]
    for part in merged_sections:
        blocks.extend([part, ""])
    if merged_conclusions:
        blocks.extend(["## 结论", "", *merged_conclusions, ""])
    if inventory or plain_source_lines:
        blocks.extend(["## 信息源", ""])
        blocks.extend(line for _, line in inventory.values())
        blocks.extend(plain_source_lines)
        blocks.append("")
    blocks.append("## 缺失清单")
    if missing_lines:
        blocks.extend(f"- {line}" for line in missing_lines)
    else:
        blocks.append("- 无。")
    return "\n".join(blocks).rstrip() + "\n"


def source_citations(markdown: str) -> dict[str, int]:
    """从成稿信息源章节提取归一化 permalink 与角标编号。"""

    citations: dict[str, int] = {}
    seen_numbers: set[int] = set()
    in_sources = False
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            in_sources = "信息源" in heading.group(2)
            continue
        if not in_sources:
            continue
        matched = _SOURCE_LINE.match(line)
        if matched is None:
            continue
        permalink = normalize_permalink(matched.group("url"))
        number = int(matched.group("number"))
        if permalink in citations or number in seen_numbers:
            raise ValueError("信息源清单含重复 permalink 或 citation_no")
        citations[permalink] = number
        seen_numbers.add(number)
    return citations


def _report_ready(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not item.get("permalink") or not item.get("fetched_at"):
        return None
    normalized = dict(item)
    problem = rating_notes_problem(normalized.get("rating_notes"), normalized)
    if problem is None:
        return normalized
    scores: list[int] = []
    for field in SCORE_FIELDS:
        value = normalized.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2:
            return None
        scores.append(value)
    normalized["rating_notes"] = " · ".join(
        f"{label}{score}:历史字段回填"
        for label, score in zip(_LABELS, scores)
    ) + " ⚠️旧产物补全"
    if rating_notes_problem(normalized["rating_notes"], normalized) is not None:
        return None
    return normalized


def load_evidence_artifacts(research_root: str | Path) -> list[dict[str, Any]]:
    """只读调研根下 JSON 证据数组/三件套对象，汇总清单候选。"""

    root = Path(research_root)
    by_url: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return []
    for path in sorted(root.glob("goals/**/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, list):
            items = value
        elif isinstance(value, Mapping) and isinstance(value.get("evidence"), list):
            items = value["evidence"]
        else:
            continue
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            item = _report_ready(raw)
            if item is None:
                continue
            try:
                url = normalize_permalink(str(item["permalink"]))
            except ValueError:
                continue
            item["permalink"] = url
            current = by_url.get(url)
            def priority(value: Mapping[str, Any]) -> int:
                rated_by = str(value.get("rated_by") or "")
                if rated_by.startswith("agent:"):
                    return 3
                if rated_by.endswith(":degraded"):
                    return 2
                if "旧产物补全" not in str(value.get("rating_notes") or ""):
                    return 1
                return 0

            # 同 permalink 时，可靠度审计产物优先于采集期基线分。
            if current is None or priority(item) > priority(current):
                by_url[url] = item
    return list(by_url.values())
