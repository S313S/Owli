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
#: §RPT-2 货 5 ②：`conclusion_invalid` 这类词读者看不懂，也不该看懂。
#: 每条都写「发生了什么 + 你现在能做什么」，不写内部原因码。
MISSING_TEXT: dict[str, str] = {
    "conclusion_invalid": "本节未成稿：写手引用了证据池外的来源，已作废；"
                          "可在运行面板「重跑这节」。",
    "timeout": "本节未成稿：写到一半超出了本节的时间上限；可在运行面板「重跑这节」。",
    "empty_result": "本节未成稿：这一轮没采到可用的材料，没有东西可写。",
    "tool_unavailable": "本节未成稿：这一节要用的采集工具当时不可用。",
    "quota_exhausted": "本节未成稿：这一节撞上了额度上限。",
    "retry_exhausted": "本节未成稿：重试次数用完了仍没写成；可在运行面板「重跑这节」。",
}
_DEFAULT_MISSING_TEXT = "本节未成稿；可在运行面板「重跑这节」。"


def missing_text(reason: str | None) -> str:
    """原因码 → 读者能看懂的一句话。认不出的码不许原样漏到页面上。"""
    return MISSING_TEXT.get(str(reason or "").strip(), _DEFAULT_MISSING_TEXT)


#: 节首「证据缺口」那行裸 JSON。工作稿可以粗，但读者能切页签看到。
_GAP_LINE = re.compile(r"^(?P<lead>\s*[-*]\s*)(?P<kind>gap|假设)=(?P<json>\{.*\})\s*$")
#: JSON 里的键 → 中文行名。证据缺口摊成「维度 / 状态 / 说明」，假设摊成「假设 / 理由」。
_GAP_LABELS: dict[str, tuple[tuple[str, str], ...]] = {
    "gap": (("dimension", "维度"), ("dimensions", "维度"), ("status", "状态"),
            ("detail", "说明"), ("handling", "说明")),
    "假设": (("item", "假设"), ("assumption", "假设"), ("reason", "理由"),
             ("detail", "理由")),
}
#: 写手常写的几个英文状态值。认不出就原样留着，不猜。
_GAP_STATUS: dict[str, str] = {
    "insufficient": "证据不足",
    "insufficient_visible_evidence": "可见证据不足",
    "missing": "完全没采到",
    "partial": "只覆盖了一部分",
    "covered": "已覆盖",
    "deferred": "本轮不补，留到后续",
}


def humanize_gap_lines(text: str) -> str:
    """把「- gap={...}」这类裸 JSON 摊成人话行；解析不了就原样留着，不吞内容。"""
    out: list[str] = []
    for line in text.splitlines():
        matched = _GAP_LINE.match(line)
        if matched is None:
            out.append(line)
            continue
        try:
            data = json.loads(matched.group("json"))
        except (TypeError, ValueError):
            out.append(line)
            continue
        if not isinstance(data, Mapping):
            out.append(line)
            continue
        lead = matched.group("lead")
        indent = " " * (len(lead) - len(lead.lstrip()))
        labels = _GAP_LABELS[matched.group("kind")]
        rendered = []
        for key, label in labels:
            if key not in data or any(name == label for name, _ in rendered):
                continue
            value = data[key]
            if isinstance(value, (list, tuple)):
                value = "、".join(str(item) for item in value)
            text_value = str(value).strip()
            if label == "状态":
                text_value = _GAP_STATUS.get(text_value.casefold(), text_value)
            if text_value:
                rendered.append((label, text_value))
        if not rendered:
            out.append(line)
            continue
        out.extend(f"{indent}- {label}：{value}" for label, value in rendered)
        leftover = {k: v for k, v in data.items()
                    if k not in {key for key, _ in labels} and str(v).strip()}
        out.extend(f"{indent}- {k}：{v}" for k, v in leftover.items())
    return "\n".join(out)


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
    # §RPT-2 货 5 ③：节首「证据缺口」那行裸 JSON 在读者眼里就是乱码，摊成人话行。
    # 货 5 ①：同名的研究对象行合成一行。
    markdown = dedupe_entity_lines(humanize_gap_lines(str(section.get("markdown") or "")))
    parts = split_markdown(markdown)
    # 占位节：去掉标题行后只剩「- 此处缺失：…；原因：…」一行（sectioning.py:831-843）
    content = [
        line for line in parts["body"].splitlines()
        if line.strip() and not _HEADING.match(line)
    ]
    placeholder = _PLACEHOLDER.match(content[0]) if len(content) == 1 else None
    reason = placeholder.group("reason") if placeholder else None
    body = parts["body"]
    if placeholder is not None:
        # 占位行本身带着 `goal-1/ch-6/sec-1；原因：conclusion_invalid`，
        # 留在正文里前端一旦走了 Markdown 分支就原样漏给读者。渲染层直接换成人话。
        body = missing_text(reason)
    return {
        "section_id": section.get("section_id"),
        "goal_id": section.get("goal_id"),
        "title": section.get("title") or parts["title"],
        "markdown": body,
        "placeholder": placeholder is not None,
        "missing_reason": reason,
        "missing_text": missing_text(reason) if placeholder else None,
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


def dedupe_entity_lines(text: str) -> str:
    """§RPT-2 货 5 ①：「研究对象」节里同名的行合成一行。

    骨架侧 `merge_entity_cards` 后来补上了合并（§ENT-2），但节的 markdown 是写盘时
    烘进 JSON 的——历史报告里「豆包」还是两行，读者切到工作稿一眼就看得见。
    合并规则：按加粗的正式名归组，别名并集去重，说明取最长的那句。
    """
    lines = text.splitlines()
    seen: dict[str, int] = {}
    out: list[str] = []
    for line in lines:
        matched = _ENTITY_LINE.match(line)
        if matched is None:
            out.append(line)
            continue
        name = matched.group("name").strip()
        alias_text, _, note = matched.group("rest").partition("。")
        aliases = [a.strip() for a in alias_text.split("、") if a.strip()]
        if name not in seen:
            seen[name] = len(out)
            out.append(line)
            continue
        prior = _ENTITY_LINE.match(out[seen[name]])
        prior_alias, _, prior_note = prior.group("rest").partition("。")
        merged = list(dict.fromkeys([*(a.strip() for a in prior_alias.split("、") if a.strip()),
                                     *aliases]))
        note = max((prior_note.strip(), note.strip()), key=len)
        mark = prior.group("mark") or matched.group("mark") or ""
        out[seen[name]] = f"- **{name}**{mark}：{'、'.join(merged)}。{note}".rstrip("。 ") + "。"
    return "\n".join(out)


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
