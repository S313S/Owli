"""报告 Markdown 的信息源清单渲染。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.reliability.scoring import SCORE_FIELDS, rating_notes_problem


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
    """只读调研根下 JSON 数组，汇总可用于清单的数据；不接触裸 SQL。"""

    root = Path(research_root)
    by_url: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return []
    for path in sorted(root.glob("goals/**/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, list):
            continue
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            item = _report_ready(raw)
            if item is None:
                continue
            url = str(item["permalink"])
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
