"""成稿（.md / .json）→ 交付面结构化视图（§DLV-1 货 1）。

只读、确定性；角标与信息源行的识别全部复用 `app/report/markdown.py`
的正则与解析器，不另写一份。输出给 `GET /api/researches/{id}/report`
与 Excel / 飞书导出共用。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from app.report.markdown import (
    _HEADING,
    _MARK,
    _SOURCE_LINE,
    _subtree_spans,
    report_citations,
)

_PLACEHOLDER = re.compile(r"^\s*-\s*此处缺失：(?P<where>[^；]+)；原因：(?P<reason>\S+)\s*$")
_MISSING_TEXT = re.compile(r"此处缺失：(?P<where>[^；]+)；原因：(?P<reason>[A-Za-z_]+)")
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def _bullets(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        matched = _BULLET.match(line)
        if matched:
            items.append(matched.group(1))
    return items


def _parse_source_line(line: str) -> dict[str, Any] | None:
    matched = _SOURCE_LINE.match(line)
    if matched is None:
        return None
    number = int(matched.group("number"))
    return {
        "citation_no": number,
        "mark": f"S{number:02d}",
        "title": matched.group("title"),
        "permalink": matched.group("url"),
        "raw_line": line.strip(),
    }


def _missing_from_text(text: str) -> dict[str, Any] | None:
    matched = _MISSING_TEXT.search(text)
    if matched is None:
        return None
    where = matched.group("where").strip()
    goal_id, _, chapter_id = where.partition("/")
    return {
        "goal_id": goal_id,
        "chapter_id": chapter_id or None,
        "reason": matched.group("reason"),
        "text": text.strip().lstrip("- ").strip(),
    }


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def split_markdown(text: str) -> dict[str, Any]:
    """一段 Markdown → 正文 / 结论条 / 信息源条 / 缺失条；标题层级按子树切。

    §RD-1：写手会把「<!-- q-1：两者兼顾 -->」这类追问批注当 HTML 注释吐进正文与结论，
    前端 Markdown 渲染不认原生 HTML、会把注释原样当文字显示给读者——视图层一律剥掉。
    """
    lines = _HTML_COMMENT.sub("", text).splitlines()
    removed: set[int] = set()
    conclusions: list[str] = []
    sources: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    title: str | None = None
    for start, end, heading in _subtree_spans(lines):
        if start in removed:
            continue
        level = len(_HEADING.match(lines[start]).group(1))
        if level == 1 and title is None:
            title = heading
            removed.add(start)
            continue
        if heading == "结论":
            conclusions.extend(_bullets("\n".join(lines[start + 1:end])))
        elif "信息源" in heading:
            for line in lines[start + 1:end]:
                parsed = _parse_source_line(line)
                if parsed is not None:
                    sources.append(parsed)
        elif "缺失清单" in heading:
            for line in lines[start + 1:end]:
                item = _missing_from_text(line)
                if item is not None:
                    missing.append(item)
        else:
            continue
        removed.update(range(start, end))
    body = "\n".join(line for i, line in enumerate(lines) if i not in removed).strip()
    return {
        "title": title,
        "body": body,
        "conclusions": conclusions,
        "sources": sources,
        "missing": missing,
    }


def _section_view(section: Mapping[str, Any]) -> dict[str, Any]:
    markdown = str(section.get("markdown") or "")
    parts = split_markdown(markdown)
    # 占位节：去掉标题行后只剩「- 此处缺失：…；原因：…」一行（sectioning.py:831-843）
    content = [
        line for line in parts["body"].splitlines()
        if line.strip() and not _HEADING.match(line)
    ]
    placeholder = _PLACEHOLDER.match(content[0]) if len(content) == 1 else None
    return {
        "section_id": section.get("section_id"),
        "goal_id": section.get("goal_id"),
        "title": section.get("title") or parts["title"],
        "markdown": parts["body"],
        "placeholder": placeholder is not None,
        "missing_reason": placeholder.group("reason") if placeholder else None,
        "_parts": parts,
    }


def _merge(sections: list[dict[str, Any]], *, title: str | None,
           extra_missing: list[Mapping[str, Any]], notes: Any, fmt: str) -> dict[str, Any]:
    conclusions: list[str] = []
    sources: dict[int, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    seen_missing: set[tuple[str | None, str | None]] = set()
    for item in extra_missing:
        if not isinstance(item, Mapping):
            continue
        key = (item.get("goal_id"), item.get("chapter_id"))
        seen_missing.add(key)
        missing.append({
            "goal_id": item.get("goal_id"), "chapter_id": item.get("chapter_id"),
            "reason": item.get("reason"), "text": item.get("text"),
        })
    for section in sections:
        parts = section.pop("_parts")
        conclusions.extend(parts["conclusions"])
        for source in parts["sources"]:
            sources.setdefault(source["citation_no"], source)
        for item in parts["missing"]:
            key = (item["goal_id"], item["chapter_id"])
            if key not in seen_missing:
                seen_missing.add(key)
                missing.append(item)
    body = "\n\n".join(s["markdown"] for s in sections if s["markdown"])
    cited = sorted({int(m[2:4]) for m in _MARK.findall(body + "\n".join(conclusions))})
    listed = set(sources)
    return {
        "format": fmt,
        "title": title,
        "sections": sections,
        "conclusions": conclusions,
        "sources": [sources[n] for n in sorted(sources)],
        "missing": missing,
        "citations": {
            "cited": cited,
            "listed": sorted(listed),
            "dangling": [n for n in cited if n not in listed],
        },
        "notes": notes,
    }


_ENTITY_LINE = re.compile(r"^\s*-\s+\*\*(?P<name>[^*]+)\*\*(?P<mark>（[^）]*）)?：(?P<rest>.+)$")


def report_entity_lines(text: str) -> list[dict[str, str]]:
    """§ENT-1 货 6：从成稿的「研究对象」节确定性摘出实体行，供 Excel 与导出复用。

    只读成稿，不重算：报告里写的是什么，附件里就是什么（`verification-ruler`
    的教训——尺子另写一份解析，量出来的数就不是报告里的数）。
    """
    lines = _HTML_COMMENT.sub("", text).splitlines()
    picked: list[dict[str, str]] = []
    for start, end, heading in _subtree_spans(lines):
        if heading.strip() != "研究对象":
            continue
        for line in lines[start + 1:end]:
            matched = _ENTITY_LINE.match(line)
            if matched is None:
                continue
            picked.append({
                "name": matched.group("name").strip(),
                "same_product": "不是同一个" not in (matched.group("mark") or ""),
                "text": matched.group("rest").strip(),
            })
    return picked


def parse_report(text: str) -> dict[str, Any]:
    """成稿文本 → 结构化视图。JSON（节化成稿）与 Markdown 两种产物都认。"""
    stripped = text.lstrip()
    document: Any = None
    if stripped.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            document = None
    entities = report_entity_lines(text if not stripped.startswith("{") else "")
    if isinstance(document, Mapping) and isinstance(document.get("sections"), list):
        sections = [_section_view(s) for s in document["sections"] if isinstance(s, Mapping)]
        raw_missing = document.get("缺失清单")
        view = _merge(
            sections,
            title=document.get("title"),
            extra_missing=raw_missing if isinstance(raw_missing, list) else [],
            notes=document.get("收尾注释"),
            fmt="json",
        )
        # JSON 成稿的「研究对象」节在合并后的 body 里，摘取跟 Markdown 走同一把尺子。
        view["entities"] = report_entity_lines(
            "\n".join(str(item.get("markdown") or "") for item in sections)
        )
        return view
    parts = split_markdown(text)
    section = {
        "section_id": None, "goal_id": None, "title": parts["title"],
        "markdown": parts["body"], "placeholder": False, "missing_reason": None,
        "_parts": parts,
    }
    view = _merge([section], title=parts["title"], extra_missing=[], notes=None, fmt="markdown")
    view["entities"] = entities
    return view


def report_citation_map(text: str) -> dict[str, int]:
    """permalink → citation_no（归一化），与收尾回填同一把尺子。"""
    return report_citations(text)
