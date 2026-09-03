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


def _merge_shard_structures(
    texts: Sequence[str],
    citation_numbers: Mapping[str, int] | None,
) -> tuple[list[str], list[str], dict[str, tuple[str, str]], list[str]]:
    """节/片归并的共用零件：拆结构、按 permalink 去重信息源、按全局号重排角标。

    §D-031：片级合并（`merge_section_shards`）与节级合并
    （`merge_sectioned_markdown`）要的是同一套去重与重排，只是外层形状不同，
    所以抽出来共用——两处各写一份迟早会漂。
    """
    inventory: dict[str, tuple[str, str]] = {}  # 去重键 → (新角标, 条目行)
    plain_source_lines: list[str] = []  # 无角标条目按原文去重保留，不参与重排
    merged_sections: list[str] = []
    merged_conclusions: list[str] = []
    for index, text in enumerate(texts):
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
                    citation_numbers.get(key)
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
    return merged_sections, merged_conclusions, inventory, plain_source_lines


def section_conclusion_items(text: str) -> list[str]:
    """一节/一片正文里「结论」段的列表项原文（不含前导符号）。

    §D-031 片间衔接用：把前面几片已写过的结论条目摘给后续片，让它别重复。
    """
    _, conclusion, _ = _split_section_structures(text)
    return [
        line.strip().lstrip("-* ")
        for line in conclusion.splitlines()
        if line.strip().startswith(("-", "*"))
    ]


def merge_section_shards(
    shard_texts: Sequence[str],
    *,
    citation_numbers: Mapping[str, int] | None = None,
) -> str:
    """§D-031：把一节的 K 份片正文归并成**一节**的正文。

    与 `merge_sectioned_markdown` 的差别只在外层形状：片级产出的是节，不是整卷，
    所以既不写 `# 标题`（节标题由片正文自己带或不带），也不写 `## 缺失清单`
    （那份由章拼装器按账本另写权威的一份）。

    去重与重排共用 `_merge_shard_structures`：信息源按 permalink 去重——
    两片各列一遍同一条源，`source_citations` 会直接抛「信息源清单含重复
    permalink 或 citation_no」。角标本身不需要跨片编号：`_evidence_index`
    是先按全报告排序再编号、每节只挑子集，同一条证据在哪片都是同一个号。
    """
    merged_sections, merged_conclusions, inventory, plain_source_lines = (
        _merge_shard_structures(shard_texts, citation_numbers)
    )
    blocks: list[str] = []
    for part in merged_sections:
        blocks.extend([part, ""])
    if merged_conclusions:
        blocks.extend(["## 结论", "", *merged_conclusions, ""])
    if inventory or plain_source_lines:
        blocks.extend(["## 信息源", ""])
        blocks.extend(line for _, line in inventory.values())
        blocks.extend(plain_source_lines)
    return "\n".join(blocks).rstrip() + "\n"


def render_entity_section(entities: Sequence[Mapping[str, Any]]) -> list[str]:
    """§ENT-1 货 6：报告开头的「研究对象」节——这份报告说的到底是哪几个产品。

    `same_product=false` 的实体在这里就标出来（「与同名的海外/国内产品不是同一个
    产品」），交叉章据此只并列不交叉；读报告的人也不用自己猜抖音那节说的是不是
    TikTok。没有实体卡（历史报告、解析失败）时返回空，报告结构一个字不变。
    """
    if not entities:
        return []
    lines = ["## 研究对象", ""]
    for entity in entities:
        names = entity.get("names") if isinstance(entity.get("names"), Mapping) else {}
        alias = "、".join(
            str(item) for item in [names.get("zh"), names.get("en"), *(names.get("aliases") or [])]
            if str(item or "").strip()
        )
        canonical = str(entity.get("canonical") or entity.get("id") or "").strip()
        mark = "" if entity.get("same_product", True) else "（中外同名产品不是同一个，本报告只并列不交叉）"
        note = str(entity.get("note") or "").strip()
        lines.append(f"- **{canonical}**{mark}：{alias or canonical}。{note}".rstrip())
    lines.append("")
    return lines


def merge_sectioned_markdown(
    title: str,
    section_texts: Sequence[str],
    missing_lines: Sequence[str],
    *,
    citation_numbers: Mapping[str, int] | None = None,
    entities: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """把各节 Markdown 归并成单结论、单信息源的整卷报告。

    节按 goal 切且每节都带自己的「结论/信息源」，直接拼接会得到三份并列的
    小报告（worklog report-module §10.4）。这里做确定性归并：各节的结论合入
    章末唯一的「结论」，信息源按 permalink 去重合入唯一的「信息源」并全卷
    统一重排 [SNN] 角标（各节独立编号会互相撞号）。
    """
    merged_sections, merged_conclusions, inventory, plain_source_lines = (
        _merge_shard_structures(section_texts, citation_numbers)
    )
    blocks = [f"# {title}", ""]
    blocks.extend(render_entity_section(entities or []))
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


def report_citations(text: str) -> dict[str, int]:
    """从成稿正文提取角标，Markdown 与 JSON 两种产物都认。

    §SRC-1 货 7（D-022）：`source_citations` 是逐行扫 Markdown 标题的，
    而 JSON 成稿把正文以转义 `\n` 塞在字符串里——整份文件没有一行是标题，
    于是解析出 0 个角标，`replace_evidence_citations` 反手把全库
    `citation_no` 清成 NULL。D-013 那轮「全库 0 条非空」就是这么来的，
    与写手引不引国内源无关。
    """

    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return source_citations(text)
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return source_citations(text)
    merged: dict[str, int] = {}
    numbers: dict[int, str] = {}
    for block in _markdown_blocks(document):
        for permalink, number in source_citations(block).items():
            existing = merged.get(permalink)
            if existing is not None and existing != number:
                raise ValueError(
                    f"同一 permalink 在不同节拿到不同角标：{permalink}"
                )
            owner = numbers.get(number)
            if owner is not None and owner != permalink:
                raise ValueError(f"角标 {number} 在不同节指向不同 permalink")
            merged[permalink] = number
            numbers[number] = permalink
    return merged


def report_cites_but_lists_nothing(text: str) -> bool:
    """正文有 [Sxx] 角标、却一条『信息源』都解析不出来 —— 格式没读懂。

    §SRC-1 货 7（D-022）的护栏判据：`replace_evidence_citations` 会把未列出的
    行 `citation_no` 置 NULL，空映射等于全库清零。但「报告确实一条都没引」
    也会得到空映射，那时清零是对的。两者的区别就在正文有没有角标。
    """

    if report_citations(text):
        return False
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            blocks = _markdown_blocks(json.loads(text))
        except json.JSONDecodeError:
            blocks = [text]
    else:
        blocks = [text]
    return any(_MARK.search(block) for block in blocks)


def _markdown_blocks(document: Any) -> list[str]:
    """深搜出所有 `markdown` 字段值；节化产物是 sections[].markdown。"""

    blocks: list[str] = []
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            value = node.get("markdown")
            if isinstance(value, str) and value.strip():
                blocks.append(value)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return blocks


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
