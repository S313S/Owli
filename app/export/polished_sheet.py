"""Excel `00_正式稿`：把正式稿的结论与确定性表写进工作簿（§RPT-1 货 5）。

排版按 `docs/design/report-attachment-spec.md` §4：行动式标题写在**单元格**里
（图表对象自身标题留空，这样校验脚本读得到、也能复制进飞书），副标题固定一行
`单位 · 时间范围 · 样本量 n=xx`，图统一 24×11 cm。选图按
`docs/design/chart-selection-guide.md` 三步法——先有结论句才画图，一张图一种比较类型。
没整理过正式稿时本 sheet 只写一行说明，**sheet 本身始终存在**（清单是死契约）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

SHEET_NAME = "00_正式稿"
EMPTY_HINT = "这份研究还没整理过正式稿。在报告页点「整理成正式稿」后重新导出即可。"
#: 一级标题 → 取几行正文。执行摘要要全，其余只取前若干行，免得整张 sheet 变成第二份正文。
_SUMMARY_HEAD = "执行摘要"
_H1 = re.compile(r"^# +(.+)$")
_LIST = re.compile(r"^\s*\d+\.\s+")


def find_polished(runs_root: Path, research_id: str) -> tuple[Path, Path] | None:
    """挑最近整理的一份正式稿；返回 (markdown, tables.json)，都在才算数。"""
    exports = Path(runs_root) / research_id / "exports"
    if not exports.is_dir():
        return None
    candidates = sorted(exports.glob(f"{research_id}.polished.*.md"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for md_path in candidates:
        tables_path = md_path.with_name(md_path.name[:-len(".md")] + ".tables.json")
        if tables_path.is_file():
            return md_path, tables_path
    return None


def load_polished(runs_root: Path, research_id: str) -> dict[str, Any] | None:
    """读出 `00` 需要的三件：模板名、正文分节、确定性表。"""
    found = find_polished(runs_root, research_id)
    if found is None:
        return None
    md_path, tables_path = found
    try:
        data = json.loads(tables_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    template = md_path.name[len(f"{research_id}.polished."):-len(".md")]
    markdown = md_path.read_text(encoding="utf-8")
    return {"template": template, "markdown": markdown, "sections": _split_sections(markdown),
            "tables": data.get("tables") or {}, "counts": data.get("counts") or {},
            "research_question": data.get("research_question") or ""}


def _split_sections(markdown: str) -> dict[str, list[str]]:
    """一级标题 → 该节的非空正文行（表格行与图片行不要，Excel 里另有确定性表）。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        matched = _H1.match(line)
        if matched:
            current = matched.group(1).strip()
            sections.setdefault(current, [])
            continue
        text = line.strip()
        if current is None or not text or text.startswith(("|", "```", ">", "---")):
            continue
        sections[current].append(text.lstrip("# ").strip())
    return sections


def _write_block(ws, row: int, table: Mapping[str, Any], fonts: Mapping[str, Any]) -> int:
    """一张确定性表：行动式标题 / 副标题（n 与口径）/ 表头 / 数据行 / 空行。"""
    ws.cell(row=row, column=1, value=table.get("title") or table.get("name")).font = fonts["title"]
    coverage = table.get("coverage") or {}
    subtitle = f"条 · 本次采集 · 样本量 n={table.get('n', 0)}"
    if coverage:
        subtitle += " · 覆盖 " + " / ".join(f"{k} {v}" for k, v in coverage.items())
    ws.cell(row=row + 1, column=1, value=subtitle).font = fonts["sub"]
    columns = list(table.get("columns") or [])
    for index, name in enumerate(columns, start=1):
        ws.cell(row=row + 2, column=index, value=name).font = fonts["head"]
    ws.cell(row=row + 2, column=len(columns) + 1, value="来源角标").font = fonts["head"]
    cursor = row + 3
    for item in table.get("rows") or []:
        for index, name in enumerate(columns, start=1):
            ws.cell(row=cursor, column=index, value=item.get(name)).font = fonts["body"]
        ws.cell(row=cursor, column=len(columns) + 1,
                value="、".join(item.get("marks") or []) or "—").font = fonts["body"]
        cursor += 1
    ws.cell(row=cursor, column=1, value="口径：" + str(table.get("basis") or "")).font = fonts["sub"]
    return cursor + 2


def write_sheet(ws, data: Mapping[str, Any] | None, fonts: Mapping[str, Any]) -> int:
    """写 `00_正式稿`，返回下一个可用行号。`data` 为空只留一行说明。"""
    ws.cell(row=1, column=1, value="正式稿").font = fonts["title"]
    if data is None:
        ws.cell(row=2, column=1, value=EMPTY_HINT).font = fonts["body"]
        return 3
    ws.cell(row=1, column=1,
            value=f"正式稿 · {data['template']} · {data.get('research_question') or ''}").font = fonts["title"]
    counts = data.get("counts") or {}
    ws.cell(row=2, column=1, value=(
        f"条 · 本次调研 · 证据 {counts.get('evidence', 0)} 条 / 被引 {counts.get('cited', 0)} 条"
        f" / 主张 {counts.get('claims', 0)} 条")).font = fonts["sub"]
    row = 4
    sections = data.get("sections") or {}
    for name, lines in sections.items():
        if not lines:
            continue
        ws.cell(row=row, column=1, value=name).font = fonts["title"]
        row += 1
        # 执行摘要与关键发现是给人读的，整段进；其余节只留结论行（以序号开头的那些）。
        keep = lines if name == _SUMMARY_HEAD else [x for x in lines if _LIST.match(x)]
        for line in keep[:20]:
            ws.cell(row=row, column=1, value=line).font = fonts["tldr" if row == 5 else "body"]
            row += 1
        row += 1
    for table in (data.get("tables") or {}).values():
        if table.get("rows"):
            row = _write_block(ws, row, table, fonts)
    return row


#: 选图三步法的落地：结论句 → 比较类型 → 图。一表一图一种比较，至多三张（spec §4.5）。
CHART_PLAN: tuple[tuple[str, str, str, str], ...] = (
    ("platform_mix", "平台", "采集条数", "bar"),      # 排名/大小 → 降序条形
    ("grade_mix", "等级", "被引条数", "pie"),         # 占比/构成，≤5 片 → 饼图
    ("timeline", "月份", "证据条数", "line"),         # 趋势，期数 >6 → 折线
)


def _conclusion_line(name: str, table: Mapping[str, Any]) -> str:
    """图上方那句行动式标题：主语 + 判断 + 量级，量级从表里取，不新造数。"""
    rows = list(table.get("rows") or [])
    if name == "platform_mix":
        top = max(rows, key=lambda r: r.get("采集条数") or 0)
        return (f"{top['平台']} 贡献了最多证据（{top['采集条数']} 条），"
                f"其中被引 {top.get('被引条数', 0)} 条")
    if name == "grade_mix":
        best = max(rows, key=lambda r: r.get("被引条数") or 0)
        return f"被引证据以 {best['等级']} 级最多（{best['被引条数']} 条）"
    span = f"{rows[0]['月份']} 起共 {len(rows)} 个时段" if rows else ""
    peak = max(rows, key=lambda r: r.get("证据条数") or 0) if rows else None
    return f"证据发布集中在 {peak['月份']}（{peak['证据条数']} 条），{span}" if peak else "证据发布时间分布"


def add_charts(ws, data_ws, data: Mapping[str, Any] | None, row: int, fonts: Mapping[str, Any]) -> int:
    """按选图法画至多三张原生图；数据落隐藏 sheet，标题写单元格、图对象自身不带标题。"""
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.chart.label import DataLabelList

    if data is None:
        return row
    tables = data.get("tables") or {}
    anchor_col = data_ws.max_column + 2 if data_ws.max_column > 1 else 1
    for name, label_key, value_key, kind in CHART_PLAN:
        table = tables.get(name)
        rows = list(table.get("rows") or []) if table else []
        if not rows:
            continue  # 没数据不画图（选图法第 1 步：写不出结论句就别画）
        ws.cell(row=row, column=1, value=_conclusion_line(name, table)).font = fonts["title"]
        ws.cell(row=row + 1, column=1,
                value=f"条 · 本次调研 · 样本量 n={table.get('n', 0)}").font = fonts["sub"]
        data_ws.cell(row=1, column=anchor_col, value=label_key)
        data_ws.cell(row=1, column=anchor_col + 1, value=value_key)
        for offset, item in enumerate(rows, start=2):
            data_ws.cell(row=offset, column=anchor_col, value=item.get(label_key))
            data_ws.cell(row=offset, column=anchor_col + 1, value=item.get(value_key))
        last = len(rows) + 1
        labels = Reference(data_ws, min_col=anchor_col, min_row=2, max_row=last)
        values = Reference(data_ws, min_col=anchor_col + 1, min_row=1, max_row=last)
        chart = {"bar": BarChart, "pie": PieChart, "line": LineChart}[kind]()
        if kind == "bar":
            chart.type = "bar"          # 横向条形：类别名长，竖着放不下
        chart.add_data(values, titles_from_data=True)
        chart.set_categories(labels)
        chart.title = None              # 标题在单元格里，图对象自身留空（spec §4.3）
        chart.width, chart.height = 24, 11
        chart.dataLabels = DataLabelList()
        chart.dataLabels.showVal = True
        chart.dataLabels.showSerName = chart.dataLabels.showCatName = False
        chart.dataLabels.showLegendKey = False
        ws.add_chart(chart, f"A{row + 2}")
        anchor_col += 3
        row += 28                        # 每图一个 28 行区块（spec §4.5）
    return row
