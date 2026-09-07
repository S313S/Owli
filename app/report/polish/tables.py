"""正式稿的确定性数据表：从 evidence / claims / plan 算，写手只解读不改数。

每张表固定五件东西：`n`（样本量）、`basis`（口径一句话）、`coverage`（该口径能覆盖
多少条，覆盖低于一半的表写手不得拿来下强结论）、`columns`、`rows`；行上带 `marks`
（本行背后的信息源角标 `S01` 形式），让写手引数时直接抄角标，不必自己找出处。
表只读工作稿的库与成稿，从不回写。
"""

from __future__ import annotations

import json
import re
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


#: §RULE-1 货 5（评审 #7）：原声必须是**用户/作者的评价句**。
#: 实测舆情简报「正面的说法」引的是云服务商上架广告
#: （S79「Doubao Seed Character now available on Atlas」），
#: 「负面的说法」引的是 Reddit 帖子标题——都不是「说法」。
#: 标题与公告有两条硬特征：它与证据自己的标题一字不差；或者它在做发布/推广的宣告。
ANNOUNCEMENT_MARKERS = (
    "现已上线", "正式上线", "正式发布", "重磅发布", "全新发布", "官方宣布", "官宣",
    "立即体验", "点击链接", "扫码", "限时优惠", "欢迎试用", "诚邀",
    "now available", "is now live", "introducing ", "sign up", "try it now",
)
#: 比较标题时忽略的东西：空白、标点、大小写。短句撞车没意义，只比 8 个字符以上的。
_QUOTE_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)
MIN_TITLE_OVERLAP = 8


def normalize_quote(text: object) -> str:
    return _QUOTE_NOISE.sub("", str(text or "")).lower()


def is_speech_quote(text: object, titles: Iterable[object] = ()) -> bool:
    """这句话能不能当原声。

    两条否决：① 它就是某条证据的标题（帖子标题不是人说的话，是编辑写的招牌）；
    ② 它在做发布或推广的宣告（产品公告、服务商广告）。
    两条都不命中才是「人在说自己怎么看」——原声要的是这个。
    """
    body = str(text or "").strip()
    if not body:
        return False
    lowered = body.lower()
    if any(marker in lowered for marker in ANNOUNCEMENT_MARKERS):
        return False
    normalized = normalize_quote(body)
    for title in titles:
        other = normalize_quote(title)
        if not other:
            continue
        if normalized == other:
            return False        # 一字不差就是标题，多短都算
        # 包含关系只在两边都够长时才算——短句撞车是巧合，不是证据。
        if (min(len(normalized), len(other)) >= MIN_TITLE_OVERLAP
                and (other in normalized or normalized in other)):
            return False
    return True


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


#: §RULE-1 货 7：矩阵一行最多给几个角标。给多少，写手就往每个格子里抄多少。
MATRIX_MARKS_PER_ROW = 3


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
            # §RULE-1 货 7（评审 #12）：这一行的角标写手会往每一格里抄，
            # 实测每格塞了 9–13 个、格子整个读不了。这里就只给前 3 个——
            # 矩阵是用来横着比大小的，角标的完整清单在文末信息源清单里。
            out.append({**cells, "marks": sorted(set(row_marks))[:MATRIX_MARKS_PER_ROW]})
    out.sort(key=lambda r: (-sum(r[d] for d in columns), r["实体"]))
    return _table("entity_dimension", "实体 × 维度的证据条数矩阵", ["实体", *columns], out,
                  n=sum(len(v) for v in grid.values()),
                  basis="格内是同时命中该实体叫法与该维度的证据条数；维度优先取 evidence.extra.dimensions，"
                        "缺失时用固定词表兜底（词表见 tables.DIMENSIONS）。"
                        f"每行角标最多列 {MATRIX_MARKS_PER_ROW} 个，且写在表下那一行，不进格子。",
                  coverage={"带 dimensions 字段的证据":
                            sum(1 for r in rows if (r.get("_extra") or {}).get("dimensions")),
                            "总条数": len(rows)})


#: 交叉验证结论的强弱序：只要有一条多源互证的主张撑着，这个角标就不算孤证。
_VERDICT_RANK = ("PASS", "CONFLICT", "WEAK", "SINGLE")


def _crossref_by_mark(cited: Sequence[Mapping[str, Any]],
                      claims: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """角标 → 引用它的那些主张里最强的交叉验证结论。没有主张引它就不出现在结果里。"""
    mark_of = {str(row["id"]): int(row["citation_no"]) for row in cited if row.get("id")}
    found: dict[int, set[str]] = defaultdict(set)
    for claim in claims:
        verdict = str(claim.get("verdict") or "")
        for evidence_id in claim.get("evidence_ids") or []:
            mark = mark_of.get(str(evidence_id))
            if mark is not None and verdict:
                found[mark].add(verdict)
    return {mark: next((v for v in _VERDICT_RANK if v in verdicts), "SINGLE")
            for mark, verdicts in found.items()}


#: 全部可用表名；SKILL.md 的 `tables:` 只能从这里挑（加载器会校验）。
#: 模板 frontmatter 只能声明这里有的表名（`skills._load_one` 会校验），
#: 而 `build_prompt` 又只投喂「模板点名过的表」——**两处都对上，写手才看得见一张表**。
#: 加表的人要同时改这里和三份 SKILL.md 的 `tables:` 行，只改一处是静默漏投、不报错。
TABLE_NAMES: tuple[str, ...] = (
    "platform_mix", "grade_mix", "crossref_mix", "entity_mentions",
    "topic_polarity", "timeline", "entity_dimension",
    # §CODE-1：UGC 逐条编码聚出来的三张表。词表命中（topic_polarity）只数触发词，
    # 编码是逐条模型判断，两者口径不同、都留着：编码表作主表，词表表作附录对照。
    "attitude_by_topic", "scenario_counts", "quotes",
    # §RPT-2 货 3：场景 × 态度是口碑节的主表（scenario_counts 只答「在哪谈」，
    # 这张答「在那儿是夸还是骂」）；人群 × 态度只在身份不明不过半时才出现。
    "scenario_attitude", "audience_attitude",
    # §RPT-2 货 4③：只在有行标出触发事件时才出现。
    "trigger_counts",
)


#: 读者身份没答时的占位。§RPT-2 货 1 的 q-1 闭集里「不明」排第一，两边要一致。
AUDIENCE_UNKNOWN = "不明"


def _audience(plan: Mapping[str, Any]) -> dict[str, str]:
    """取读者身份。§RPT-2 货 1 的 q-1 把它写进 plan，本包先按两个位置读：
    plan 顶层（规范位）与 `plan["audience"]` 子对象（兜底）。两处都没有就是「不明」——
    没答不是错，正式稿照写，只是建议节不按读者身份分行。"""
    nested = plan.get("audience")
    nested = nested if isinstance(nested, Mapping) else {}
    role = plan.get("audience_role") or nested.get("role") or nested.get("audience_role")
    stake = plan.get("audience_stake") or nested.get("stake") or nested.get("audience_stake")
    return {"audience_role": str(role or AUDIENCE_UNKNOWN).strip() or AUDIENCE_UNKNOWN,
            "audience_stake": str(stake or "").strip()}


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
    # §CODE-1：三张 UGC 编码表。挂进 `tables` 有两个作用——尺子 ④「数字有出处」的
    # 白名单从这里递归收数，SKILL 的 `tables:` 行也只能从这里选表；挂到别处等于
    # 既判红又漏投。没有已编码的 UGC 时整块不出，不摆空表。
    # 延迟 import：`polish/` 这层原本只依赖轻量的 lexicon，不把 reliability 那串
    # 依赖拉进模块顶层，也免了将来谁给 coding 加个 polish import 就成环。
    from app.reliability.coding import polish_tables

    coding = polish_tables(
        evidence,
        citations={str(r.get("id")): int(r["citation_no"]) for r in rows
                   if r.get("citation_no") is not None},
        # §CODE-2：原声必须点名被评实体。叫法从**这里**取，不在 coding 里另抽一份——
        # `_entity_aliases` 已经把「豆包」与「Doubao」两张卡按 canonical 并成一个实体
        # （骨架把它们当两实体的坑还在），另抽一份必然对不齐，而对不齐是静默的。
        entity_names=sorted({name for names in _entity_aliases(plan).values()
                             for name in names}),
    )
    # §RULE-1 货 5：原声候选先过一道「这是不是人说的话」。挡在这里而不是挡在写手那边——
    # 摆出来的候选写手就会用，规则拦不住一张摆在眼前的表（评审 #7 实测）。
    dropped = _drop_non_speech_quotes(coding, rows)
    # 逐表判空，整块判不够——编码非 0 但原声筛完可能是 0（只留引得动的）。
    # 空表比缺表坏得多：写手会把 n=0 读成「这个维度没人讨论」，把**没数据**写成
    # **没人谈**，等于往报告里塞一个假结论，还一路绿到用户眼前。比照 timeline
    # 数据不够就不出现的既有做法，不出表，并把原因记进元信息让人看得见。
    coded_n = coding["scenario_counts"]["n"]
    omitted_tables: dict[str, str] = {}
    for name, table in coding.items():
        if table["rows"]:
            tables[name] = table
        elif not coded_n:
            omitted_tables[name] = (
                "本轮没有已编码的 UGC（编码工序没跑，或结果没落进这个库），故不出表。"
                "这不等于没人讨论，只是这一轮没有可聚合的数据。")
        else:
            # 原因写中性的：这个分支管的是所有表，各表筛空的道理不同
            # （原声要既摘得出又进了引用池，触发事件要帖子交代了为什么开始用），
            # 套一个具体解释上去，等于给读的人一个错误的排查方向。
            omitted_tables[name] = (
                f"已编码 {coded_n} 条 UGC，但按这张表自己的口径筛完没有一行，"
                "故不出表。看该表 basis 里写的口径。")
    if dropped and "quotes" in omitted_tables:
        # 「筛完没有一行」有两种，读的人要分得清：本来就没摘出原声，
        # 还是摘出来了但全是标题/公告。给错了排查方向比不给更费事。
        omitted_tables["quotes"] += f"（其中 {dropped} 条候选是帖子标题或公告/推广，已剔除）"
    timeline = _timeline(rows)
    if timeline is not None:
        tables["timeline"] = timeline
    cited = [r for r in rows if r.get("citation_no") is not None]
    grade_by_mark = {int(r["citation_no"]): r.get("grade") for r in cited}
    # §RULE-1 货 1：抓取时间只在信息源清单那一列露面（`run.sources_table` 渲染），
    # 正文一次都不写。工作稿的信息源行不带这个字段，按角标号回查证据补上。
    fetched_by_mark = {int(r["citation_no"]): r.get("fetched_at") for r in cited}
    crossref_by_mark = _crossref_by_mark(cited, claims)
    return {
        "research_id": report.get("id"),
        "research_question": plan.get("research_question") or report.get("research_question"),
        "title": view.get("title") or report.get("title"),
        "objectives": [{"goal_id": g.get("goal_id"), "objective": g.get("objective")}
                       for g in (plan.get("goals") or []) if isinstance(g, Mapping)],
        "entities": sorted(_entity_aliases(plan)),
        # §RPT-2 货 1 ②：读者是谁、他要拿这份报告做什么决定。q-2/q-3 可跳过，
        # 跳过就是「不明」——写手见「不明」要把建议节写成「对不同读者的含义」。
        # 形状以 `_audience` 的平铺键为准（main 上先落的那一版）；
        # `run.sections_for` 平铺与嵌套两种都认，老产物不会因此读不出读者身份。
        **_audience(plan),
        "lexicon_version": LEXICON_VERSION,
        # 哪些表没出、为什么。空着是好事，非空就是这一轮少了东西——要让人看见，
        # 而不是让写手对着一张空表自己编解释。
        "omitted_tables": omitted_tables,
        # 附录要交底「为什么不给百分比」，用的就是后两个数——它们必须是算出来的，
        # 不能写死在规则文本里，否则写手照抄就成了没出处的数字（尺子 ④ 会判红，且判得对）。
        "counts": {"evidence": len(rows), "cited": len(cited), "claims": len(claims),
                   "sources": len(view.get("sources") or []),
                   "带情感标注的证据": sum(1 for r in rows
                                    if (r.get("_extra") or {}).get("sentiment_hint")),
                   "带立场标注的主张": sum(1 for c in claims if c.get("stance"))},
        # 工作稿的信息源行不带 grade（等级藏在 raw_line 里），按角标号回查证据补上——
        # 写手的「C 级只作旁证」规则要靠每条源的等级才执行得了。
        "sources": [{"mark": _mark(int(s["citation_no"])), "title": s.get("title"),
                     "url": s.get("url") or s.get("permalink"),
                     "grade": grade_by_mark.get(int(s["citation_no"])) or s.get("grade"),
                     "fetched_at": fetched_by_mark.get(int(s["citation_no"])),
                     # 建议段门禁要按角标核：这条源背后的主张里最强的那个交叉验证结论。
                     "crossref": crossref_by_mark.get(int(s["citation_no"]))}
                    for s in (view.get("sources") or []) if s.get("citation_no") is not None],
        "tables": tables,
    }


def _drop_non_speech_quotes(coding: dict[str, Any],
                            rows: Sequence[Mapping[str, Any]]) -> int:
    """把不是「人说的话」的原声从候选里去掉，返回去掉了几条。

    去掉之后这张表可能一行不剩——那就走既有的「不出表」分支，不摆空表
    （空表会被写手读成「这个维度没人谈」）。
    """
    table = coding.get("quotes")
    if not table or not table.get("rows"):
        return 0
    titles = [r.get("title") for r in rows]
    kept = [row for row in table["rows"] if is_speech_quote(row.get("原声"), titles)]
    dropped = len(table["rows"]) - len(kept)
    table["rows"] = kept
    if dropped:
        # 让人看得见少了什么：不写出来，「原声怎么变少了」只能靠猜。
        table["basis"] += f"另有 {dropped} 条候选是帖子标题或产品公告/推广，不是人说的话，已剔除。"
    return dropped


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
