"""正式稿的确定性数据表：从 evidence / claims / plan 算，写手只解读不改数。

每张表固定五件东西：`n`（样本量）、`basis`（口径一句话）、`coverage`（该口径能覆盖
多少条，覆盖低于一半的表写手不得拿来下强结论）、`columns`、`rows`；行上带 `marks`
（本行背后的信息源角标 `S01` 形式），让写手引数时直接抄角标，不必自己找出处。
表只读工作稿的库与成稿，从不回写。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence

from app.report.polish.lexicon import LEXICON_VERSION, hit_polarity, hit_topics

#: 竞品对比稿的维度词表；`evidence.extra.dimensions` 实测只覆盖约 3.5%，故以词表兜底。
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "产品定位": ("定位", "面向", "主打", "场景", "用户群"),
    "能力侧重": ("能力", "模型", "参数", "多模态", "推理", "长文"),
    "国内用户口碑": ("口碑", "评价", "吐槽", "好评", "差评", "体验"),
    "价格与门槛": ("免费", "付费", "价格", "会员", "额度"),
    "渠道与生态": ("App", "小程序", "网页", "插件", "接入", "生态"),
}


def _mark(number: int) -> str:
    return f"S{number:02d}"


def _marks(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """行集合背后的角标，去重升序；未被引用的证据不产生角标。"""
    return [_mark(n) for n in sorted({int(r["citation_no"]) for r in rows
                                      if r.get("citation_no") is not None})]


def _table(name: str, title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]],
           *, n: int, basis: str, coverage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "title": title, "columns": list(columns), "rows": list(rows),
            "n": n, "basis": basis, "coverage": dict(coverage or {})}


def _text_of(row: Mapping[str, Any]) -> str:
    """一条证据参与词表匹配的全部文本：标题 + 摘要 + 评论正文（评论二跳带来的）。"""
    extra = row.get("_extra") or {}
    chunks = [row.get("title") or "", row.get("content_excerpt") or "",
              str(extra.get("content_summary") or ""), str(extra.get("text") or "")]
    comments = extra.get("comment_texts")
    if isinstance(comments, list):
        chunks.extend(str(c) for c in comments[:20])
    return "\n".join(chunks)


def _with_extra(evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把 `extra` 解开挂到 `_extra`，后续所有表共用，避免逐表重复解析 JSON。"""
    out: list[dict[str, Any]] = []
    for row in evidence:
        item = dict(row)
        raw = item.get("extra")
        if isinstance(raw, str):
            try:
                item["_extra"] = json.loads(raw)
            except json.JSONDecodeError:
                item["_extra"] = {}
        else:
            item["_extra"] = dict(raw or {})
        out.append(item)
    return out


def _platform_mix(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_platform: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_platform[str(row.get("platform") or "unknown")].append(row)
    out = []
    for platform, group in sorted(by_platform.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cited = [r for r in group if r.get("citation_no") is not None]
        out.append({
            "平台": platform, "采集条数": len(group),
            "其中评论": sum(1 for r in group if str(r.get("kind") or "post") == "comment"),
            "被引条数": len(cited),
            "被引占比": round(len(cited) / len(group), 4) if group else 0.0,
            "marks": _marks(cited),
        })
    return _table("platform_mix", "各平台采集量与被引量对照", 
                  ("平台", "采集条数", "其中评论", "被引条数", "被引占比"), out,
                  n=len(rows), basis="按 evidence.platform 分组计数；被引 = citation_no 非空。",
                  coverage={"评级覆盖": sum(1 for r in rows if r.get("grade")), "总条数": len(rows)})


def _grade_mix(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    meaning = {"A": "多维皆强，可独立支撑结论", "B": "较可靠，宜与他源同现",
               "C": "只作旁证，不得单独支撑结论", "D": "线索级，正文不引",
               "?": "未评级，正文不引"}
    total = Counter(str(r.get("grade") or "?") for r in rows)
    cited_rows = [r for r in rows if r.get("citation_no") is not None]
    cited = Counter(str(r.get("grade") or "?") for r in cited_rows)
    out = [{"等级": g, "被引条数": cited.get(g, 0), "全库条数": total.get(g, 0),
            "含义": meaning[g],
            "marks": _marks([r for r in cited_rows if str(r.get("grade") or "?") == g])}
           for g in ("A", "B", "C", "D", "?") if total.get(g)]
    return _table("grade_mix", "被引证据的可靠度等级分布",
                  ("等级", "被引条数", "全库条数", "含义"), out,
                  n=len(cited_rows),
                  basis="grade 是库内生成列（五维合计 ≥8 为 A、≥6 为 B、≥4 为 C，其余 D）。",
                  coverage={"被引条数": len(cited_rows), "全库条数": len(rows)})


def _crossref_mix(claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    meaning = {"PASS": "多源互证通过", "SINGLE": "单源孤证", "WEAK": "证据偏弱",
               "CONFLICT": "多源互相冲突"}
    counts = Counter(str(c.get("verdict") or "?") for c in claims)
    out = [{"交叉验证结论": v, "主张数": counts[v],
            "占比": round(counts[v] / len(claims), 4) if claims else 0.0,
            "含义": meaning.get(v, "未登记"), "marks": []}
           for v in sorted(counts, key=lambda k: -counts[k])]
    return _table("crossref_mix", "主张的交叉验证结论分布",
                  ("交叉验证结论", "主张数", "占比", "含义"), out,
                  n=len(claims),
                  basis="按 reports.extra.claims[].verdict 计数；SINGLE 占多数说明多数结论只有一个来源。",
                  coverage={"主张总数": len(claims),
                            "带多源证据的主张": sum(1 for c in claims
                                             if len(c.get("evidence_ids") or []) > 1)})


def _entity_aliases(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    """canonical → 全部叫法。`豆包` 与 `Doubao` 在计划里是两条，按 canonical 合并成一个实体。"""
    merged: dict[str, set[str]] = defaultdict(set)
    for item in plan.get("entities") or []:
        if not isinstance(item, Mapping):
            continue
        canonical = str(item.get("canonical") or item.get("id") or "").strip()
        if not canonical:
            continue
        names = item.get("names") if isinstance(item.get("names"), Mapping) else {}
        merged[canonical].update(
            str(v) for v in (item.get("id"), canonical, names.get("zh"), names.get("en")) if v)
        merged[canonical].update(str(a) for a in (names.get("aliases") or []) if a)
    # subjects 里常有已经是某实体别名的叫法（如 canonical=豆包 的 `Doubao`），
    # 直接 setdefault 会把同一个产品拆成两行 —— 只有谁的别名都不是时才新建实体。
    known = {name for names in merged.values() for name in names}
    for subject in plan.get("subjects") or []:
        if str(subject) not in known:
            merged[str(subject)].add(str(subject))
            known.add(str(subject))
    return {k: sorted(v, key=lambda s: (-len(s), s)) for k, v in merged.items()}


def _entity_mentions(rows: Sequence[Mapping[str, Any]], claims: Sequence[Mapping[str, Any]],
                     plan: Mapping[str, Any]) -> dict[str, Any]:
    aliases = _entity_aliases(plan)
    texts = [(row, _text_of(row)) for row in rows]
    out = []
    for canonical, names in aliases.items():
        hit = [row for row, text in texts if any(name in text for name in names)]
        cited = [row for row in hit if row.get("citation_no") is not None]
        out.append({
            "实体": canonical, "叫法": "/".join(names[:4]), "提及条数": len(hit),
            "其中被引": len(cited),
            "主张命中": sum(1 for c in claims
                        if any(name in str(c.get("text") or "") for name in names)),
            "marks": _marks(cited),
        })
    out.sort(key=lambda r: (-r["提及条数"], r["实体"]))
    return _table("entity_mentions", "各实体的提及量与被引量对照",
                  ("实体", "叫法", "提及条数", "其中被引", "主张命中"), out,
                  n=len(rows),
                  basis="在证据标题+摘要+评论正文上做叫法字符串命中；同条证据命中多个实体则各计一次。",
                  coverage={"参与匹配的证据": len(rows), "实体数": len(aliases)})


def _topic_polarity(rows: Sequence[Mapping[str, Any]], top_n: int = 8) -> dict[str, Any]:
    stat: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "pos": 0, "neg": 0, "both": 0, "rows": []})
    for row in rows:
        text = _text_of(row)
        positive, negative = hit_polarity(text)
        for topic in hit_topics(text):
            bucket = stat[topic]
            bucket["n"] += 1
            bucket["pos"] += int(positive)
            bucket["neg"] += int(negative)
            bucket["both"] += int(positive and negative)
            bucket["rows"].append(row)
    out = [{"主题": topic, "提及条数": b["n"], "含正向词": b["pos"], "含负向词": b["neg"],
            "正负同现": b["both"], "marks": _marks(b["rows"])}
           for topic, b in sorted(stat.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:top_n]]
    return _table("topic_polarity", f"主题提及量与极性词命中（Top {top_n}）",
                  ("主题", "提及条数", "含正向词", "含负向词", "正负同现"), out,
                  n=sum(r["提及条数"] for r in out),
                  basis=f"固定词表 {LEXICON_VERSION} 命中计数，不是情感判断；只能说「提及…的条数」，"
                        "不得说「X% 用户认为」。词表见 app/report/polish/lexicon.py。",
                  coverage={"命中任一主题的证据": sum(1 for r in rows if hit_topics(_text_of(r))),
                            "总条数": len(rows)})


def _timeline(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """证据发布月份分布；`published_at` 覆盖不足 30% 则整表不出（宁缺勿误导）。"""
    dated = [r for r in rows if str(r.get("published_at") or "")[:7].count("-") == 1]
    if not rows or len(dated) / len(rows) < 0.30:
        return None
    by_month: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in dated:
        by_month[str(row["published_at"])[:7]].append(row)
    # 选图法禁则 6：类别 >8 就取 Top N + 「其他」。时间轴取最近 12 个有数据的月份，
    # 更早的合并成一行「更早」，否则 22 根柱子既画不出也读不懂。
    months = sorted(by_month)
    recent, earlier = months[-12:], months[:-12]
    out = [{"月份": month, "证据条数": len(by_month[month]),
            "其中被引": sum(1 for r in by_month[month] if r.get("citation_no") is not None),
            "marks": _marks(by_month[month])}
           for month in recent]
    if earlier:
        old_rows = [r for month in earlier for r in by_month[month]]
        out.insert(0, {"月份": f"更早（{earlier[0]}–{earlier[-1]}）", "证据条数": len(old_rows),
                       "其中被引": sum(1 for r in old_rows if r.get("citation_no") is not None),
                       "marks": _marks(old_rows)})
    return _table("timeline", "证据发布时间分布（按月）",
                  ("月份", "证据条数", "其中被引"), out, n=len(dated),
                  basis="按 evidence.published_at 前 7 位分月，只列最近 12 个有数据的月份，"
                        "更早的合并成一行；索引源常拿不到发布时间，未标注的不计入。",
                  coverage={"有发布时间": len(dated), "总条数": len(rows)})


def _entity_dimension(rows: Sequence[Mapping[str, Any]], plan: Mapping[str, Any]) -> dict[str, Any]:
    """实体 × 维度矩阵：先认 `extra.dimensions`，没有再用 `DIMENSIONS` 词表兜底。"""
    aliases = _entity_aliases(plan)
    grid: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        text = _text_of(row)
        declared = [str(d) for d in (row.get("_extra") or {}).get("dimensions") or []]
        dims = declared or [d for d, words in DIMENSIONS.items() if any(w in text for w in words)]
        for canonical, names in aliases.items():
            if not any(name in text for name in names):
                continue
            for dim in dims:
                grid[(canonical, dim)].append(row)
    columns = list(DIMENSIONS) + sorted({d for _, d in grid} - set(DIMENSIONS))
    out = []
    for canonical in aliases:
        row_marks: list[str] = []
        cells: dict[str, Any] = {"实体": canonical}
        for dim in columns:
            group = grid.get((canonical, dim), [])
            cells[dim] = len(group)
            row_marks.extend(_marks(group))
        if any(cells[dim] for dim in columns):
            out.append({**cells, "marks": sorted(set(row_marks))})
    out.sort(key=lambda r: (-sum(r[d] for d in columns), r["实体"]))
    return _table("entity_dimension", "实体 × 维度的证据条数矩阵", ["实体", *columns], out,
                  n=sum(len(v) for v in grid.values()),
                  basis="格内是同时命中该实体叫法与该维度的证据条数；维度优先取 evidence.extra.dimensions，"
                        "缺失时用固定词表兜底（词表见 tables.DIMENSIONS）。",
                  coverage={"带 dimensions 字段的证据":
                            sum(1 for r in rows if (r.get("_extra") or {}).get("dimensions")),
                            "总条数": len(rows)})


#: 全部可用表名；SKILL.md 的 `tables:` 只能从这里挑（加载器会校验）。
TABLE_NAMES: tuple[str, ...] = (
    "platform_mix", "grade_mix", "crossref_mix", "entity_mentions",
    "topic_polarity", "timeline", "entity_dimension",
)


def build_tables(*, report: Mapping[str, Any], plan: Mapping[str, Any],
                 evidence: Sequence[Mapping[str, Any]], claims: Sequence[Mapping[str, Any]],
                 view: Mapping[str, Any]) -> dict[str, Any]:
    """算出全部确定性表 + 写手要用的元信息。`timeline` 数据不够时不出现在结果里。"""
    rows = _with_extra(evidence)
    tables = {
        "platform_mix": _platform_mix(rows),
        "grade_mix": _grade_mix(rows),
        "crossref_mix": _crossref_mix(claims),
        "entity_mentions": _entity_mentions(rows, claims, plan),
        "topic_polarity": _topic_polarity(rows),
        "entity_dimension": _entity_dimension(rows, plan),
    }
    timeline = _timeline(rows)
    if timeline is not None:
        tables["timeline"] = timeline
    cited = [r for r in rows if r.get("citation_no") is not None]
    grade_by_mark = {int(r["citation_no"]): r.get("grade") for r in cited}
    return {
        "research_id": report.get("id"),
        "research_question": plan.get("research_question") or report.get("research_question"),
        "title": view.get("title") or report.get("title"),
        "objectives": [{"goal_id": g.get("goal_id"), "objective": g.get("objective")}
                       for g in (plan.get("goals") or []) if isinstance(g, Mapping)],
        "entities": sorted(_entity_aliases(plan)),
        "lexicon_version": LEXICON_VERSION,
        "counts": {"evidence": len(rows), "cited": len(cited), "claims": len(claims),
                   "sources": len(view.get("sources") or [])},
        # 工作稿的信息源行不带 grade（等级藏在 raw_line 里），按角标号回查证据补上——
        # 写手的「C 级只作旁证」规则要靠每条源的等级才执行得了。
        "sources": [{"mark": _mark(int(s["citation_no"])), "title": s.get("title"),
                     "url": s.get("url") or s.get("permalink"),
                     "grade": grade_by_mark.get(int(s["citation_no"])) or s.get("grade")}
                    for s in (view.get("sources") or []) if s.get("citation_no") is not None],
        "tables": tables,
    }


def collect_inputs(store: Any, research_id: str, report_text: str) -> dict[str, Any]:
    """从库与工作稿正文取全部料。只读：不写库、不碰工作稿产物。"""
    from app.report.render import parse_report

    report = store.get_report(research_id)
    if report is None:
        raise KeyError(f"报告不存在：{research_id}")
    extra = report.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra or "{}")
    plan = report.get("plan_snapshot") or {}
    if isinstance(plan, str):
        plan = json.loads(plan or "{}")
    claims = [c for c in (extra.get("claims") or []) if isinstance(c, Mapping)]
    return build_tables(report=report, plan=plan, evidence=store.list_evidence(research_id),
                        claims=claims, view=parse_report(report_text))
