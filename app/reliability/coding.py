"""§CODE-1 货 1：对 UGC 逐条打结构化编码，把散点变成可聚合的表。

写手现在拿到的是 30 条散点，只能一条证据撑一条结论。加这道工序之后，
「215 条提到豆包的小红书帖」才能变成「其中多少条讲学习场景、多少条正向」。

管道照抄评级补评那条现成路（`backfill.py` 的 `_classify_batch`：分批喂引擎、
结果落文件、校验通过才回填 `extra`），不新造。与它的两处不同都写在这里：
1. 引擎输入**带正文**——`quote` 要判是不是正文子串，评级那条路故意不带正文；
2. 校验多一条子串闸——`quote` 对不上正文整条退回重打，不许摘出原文里没有的话。

口径（用户 2026-09-05 拍甲）：编码是**模型判断**。附录必须写清抽检条数与人核
准确率；正式稿只能写「N 条里 M 条编码为正向」，全文禁「用户 X% 认为」句式。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineTask

AGENT_ID = "ugc-coding"
CODING_VERSION = "v1"
MAX_ATTEMPTS = 3
CODING_BATCH_MAX = 40
QUOTE_MAX = 40

AUDIENCES = ("学生", "职场", "创作者", "开发者", "家长", "不明")
SCENARIOS = ("学习", "写作", "办公", "编程", "生活娱乐", "情感陪伴", "其他")
ATTITUDES = ("正", "负", "中", "混合")
#: 八主题闭集与 `app/report/polish/lexicon.py:TOPIC_LEXICON` 的键**逐字相同**。
#: 那个模块随 §RPT-1 走、还没合进本 base，所以这里先抄一份键名；等货 2 解禁、
#: polish 合入后改成从 lexicon 导入，不留两份词表。
TOPICS = (
    "功能与能力", "回答质量", "交互体验", "速度与稳定",
    "价格与付费", "广告与推广", "隐私与安全", "竞品对比",
)


@dataclass(frozen=True)
class CodingResult:
    """一份报告跑完一轮编码的计数；覆盖率判据直接读这里。"""

    report_id: str
    targets: int
    coded: int
    failed: int
    already: int

    @property
    def coverage(self) -> float:
        return 0.0 if not self.targets else (self.coded + self.already) / self.targets


def _extra(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("extra")
    return value if isinstance(value, Mapping) else {}


def source_text(item: Mapping[str, Any]) -> str:
    """`quote` 要落在这段文本里——标题 + 正文摘录，别的字段不算原文。"""

    parts = [str(item.get("title") or ""), str(item.get("content_excerpt") or "")]
    return "\n".join(part for part in parts if part)


def _squeeze(text: str) -> str:
    """比子串时两边都去掉空白：引擎常把换行和空格重排，但不会凭空造字。"""

    return "".join(str(text).split())


def is_coded(item: Mapping[str, Any]) -> bool:
    coding = _extra(item).get("coding")
    return (
        isinstance(coding, Mapping)
        and coding.get("coding_version") == CODING_VERSION
    )


def coding_targets(
    rows: Iterable[Mapping[str, Any]], *, force: bool = False,
) -> list[dict[str, Any]]:
    """该编码的行：UGC、进得了池（非 D）、正文不空。

    D 级不进池（`sectioning.py` 那道闸），给它编码等于给写手永远看不到的东西
    付钱。正文空的行编不出 `quote`，也一并不进——覆盖率分母跟着这里走，
    不拿「应编码」的定义去凑分子。
    """

    selected: list[dict[str, Any]] = []
    for item in rows:
        if _extra(item).get("content_kind") != "user_opinion":
            continue
        grade = item.get("grade")
        if not isinstance(grade, str) or grade == "D":
            continue
        if not _squeeze(source_text(item)):
            continue
        if not force and is_coded(item):
            continue
        selected.append(dict(item))
    return selected


def engine_input(item: Mapping[str, Any]) -> dict[str, Any]:
    """编码要判 `quote` 是不是原文子串，所以这条输入路**带正文**。

    评级那条路（`backfill._engine_input`）故意不带正文、只喂身份信号，两条互不
    相干，别合并——合并了评级就会被正文里的情绪带跑。
    """

    return {
        "id": item.get("id"),
        "platform": item.get("platform"),
        "kind": str(item.get("kind") or "post"),
        "title": item.get("title"),
        "text": str(item.get("content_excerpt") or ""),
        "published_at": item.get("published_at"),
    }


def _coding_prompt(items: Sequence[Mapping[str, Any]], *, output_path: Path) -> str:
    return (
        "目标：对国内社媒 UGC 逐条打结构化编码，供后续按条数聚合。只依据输入文本，"
        "不补造事实、不推测作者身份。\n"
        "每项输出 id、audience、scenario、attitude、topics、quote 六个字段。\n"
        f"audience 闭集：{'/'.join(AUDIENCES)}。看不出身份就填「不明」，不要猜。\n"
        f"scenario 闭集：{'/'.join(SCENARIOS)}。一条只填一个最主要的场景；"
        "都不像就填「其他」。\n"
        f"attitude 闭集：{'/'.join(ATTITUDES)}。「正」=整体认可，「负」=整体不满，"
        "「中」=陈述或提问无明显褒贬，「混合」=同一条里既夸又批。"
        "别把「提问」当负面，也别把「转述官方宣传」当正面。\n"
        f"topics 是数组，取值只能来自：{'、'.join(TOPICS)}。可多选，"
        "一个都不沾就给空数组，不要硬塞。\n"
        f"quote 是从输入 title 或 text 里**逐字摘出**的一句，不超过 {QUOTE_MAX} 字，"
        "要能代表这条的态度。禁止改写、拼接、翻译或补标点——摘出来的字必须原样出现在"
        "输入文本里，对不上整批退回重打。实在摘不出就给空字符串。\n"
        "输出顶层数组，顺序与输入一致，不要输出 Markdown。\n"
        f"必须把结果写到此精确路径：{output_path}。不得改用其他文件名。\n"
        "输入证据：" + json.dumps(list(items), ensure_ascii=False, separators=(",", ":"))
    )


def coding_errors(
    value: Any, inputs: Sequence[Mapping[str, Any]],
    sources: Mapping[str, str],
) -> list[str]:
    """闭集、顺序、长度与**原文子串**四道闸；返回空列表才算这批过。"""

    if not isinstance(value, list):
        return ["编码产物顶层必须是数组"]
    if len(value) != len(inputs):
        return [f"编码条数应为 {len(inputs)}，实际 {len(value)}"]
    errors: list[str] = []
    closed = (
        ("audience", AUDIENCES), ("scenario", SCENARIOS), ("attitude", ATTITUDES),
    )
    for index, (item, source) in enumerate(zip(value, inputs)):
        if not isinstance(item, Mapping):
            errors.append(f"items[{index}] 必须是 object")
            continue
        identity = item.get("id")
        if identity != source.get("id"):
            errors.append(f"items[{index}].id 与输入不一致")
            continue
        for field, allowed in closed:
            if item.get(field) not in allowed:
                errors.append(f"items[{index}].{field} 越界：{item.get(field)!r}")
        topics = item.get("topics")
        if not isinstance(topics, list) or any(
            topic not in TOPICS for topic in topics
        ):
            errors.append(f"items[{index}].topics 必须是八主题闭集的数组")
        elif len(set(topics)) != len(topics):
            errors.append(f"items[{index}].topics 有重复")
        quote = item.get("quote")
        if not isinstance(quote, str):
            errors.append(f"items[{index}].quote 必须是字符串")
            continue
        if len(quote) > QUOTE_MAX:
            errors.append(f"items[{index}].quote 超过 {QUOTE_MAX} 字：{len(quote)}")
        squeezed = _squeeze(quote)
        if squeezed and squeezed not in _squeeze(sources.get(str(identity), "")):
            errors.append(f"items[{index}].quote 不是原文子串，不许改写或拼接")
    return errors


def _ctx(path: Path, report_id: str, goal_id: str) -> validation.Ctx:
    return validation.Ctx(
        output_path=path,
        output_format="json",
        research_id=report_id,
        goal_id=goal_id,
        agent_id=AGENT_ID,
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
        runs_root=path.parents[4],
    )


async def _code_batch(
    items: Sequence[Mapping[str, Any]], *, adapter: Any, output_path: Path,
    report_id: str, goal_id: str, engine_preference: str | None,
) -> list[dict[str, Any]] | None:
    """一批编码；三次重打都过不了闸就整批返回 None，绝不半信半疑地写库。"""

    compact = [engine_input(item) for item in items]
    sources = {str(item.get("id")): source_text(item) for item in items}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        output_path.unlink(missing_ok=True)
        body = _coding_prompt(compact, output_path=output_path)
        if errors:
            body += "\n上一轮错误：" + "；".join(errors[:10])
        task = EngineTask(
            body=body,
            output_path=output_path,
            output_format="json",
            research_id=report_id,
            goal_id=goal_id,
            agent_id=AGENT_ID,
            agent_kind="ugc_coding",
            validators=["file_exists"],
            user_override=engine_preference,
            runs_root=output_path.parents[4],
            capability=Capability(
                profile="readonly-analyst",
                tools=("fs.write",),
                fs=FileSystemScope(write=(f"goals/{goal_id}/**",)),
            ),
        )
        result = await adapter.run(
            task, _ctx(output_path, report_id, goal_id), on_event=None
        )
        if not bool(getattr(result, "succeeded", False)):
            errors = ["适配器双腿判定未通过"]
            continue
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors = [f"编码产物无法解析：{type(exc).__name__}"]
            continue
        errors = coding_errors(value, compact, sources)
        if not errors:
            return [dict(item) for item in value]
    # 三次都没过就把最后一轮的原因落盘：不落的话失败批只剩「产物不存在」，
    # 死因得回头翻引擎日志才看得到（本轮实测两批死于传输层 socket 断开，
    # 查了一圈日志才认出来）。判死因要读原文，别让它静默。
    _write_failure(output_path, items=compact, errors=errors)
    return None


def _write_failure(
    output_path: Path, *, items: Sequence[Mapping[str, Any]], errors: Sequence[str],
) -> None:
    payload = {
        "coding_version": CODING_VERSION,
        "attempts": MAX_ATTEMPTS,
        "ids": [str(item.get("id")) for item in items],
        "errors": list(errors),
    }
    try:
        output_path.with_suffix(".errors.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:  # 诊断落盘失败不该把整轮编码带下水
        pass


def _coding_payload(item: Mapping[str, Any], label: Mapping[str, Any]) -> dict[str, Any]:
    """把一条编码并进 `extra.coding`；`score_total` 与 `grade` 是生成列，不能回写。"""

    extra = dict(_extra(item))
    extra["coding"] = {
        "coding_version": CODING_VERSION,
        "audience": label["audience"],
        "scenario": label["scenario"],
        "attitude": label["attitude"],
        "topics": list(label["topics"]),
        "quote": label["quote"],
        "coded_by": f"agent:{AGENT_ID}",
    }
    payload = {
        key: value for key, value in item.items()
        if key not in {"score_total", "grade"}
    }
    payload["extra"] = extra
    return payload


async def code_report(
    store: Any,
    report_id: str,
    *,
    adapter: Any,
    runs_root: str | Path,
    batch_size: int = CODING_BATCH_MAX,
    force: bool = False,
    engine_preference: str = "claude",
    on_event: Any = None,
) -> CodingResult:
    """对一份报告的 UGC 逐条编码；失败的批保持原样，不写半截标签。"""

    from app.reliability.backfill import _batch_output_path, _safe_component

    if not 1 <= batch_size <= CODING_BATCH_MAX:
        raise ValueError(f"编码 batch_size 必须在 1–{CODING_BATCH_MAX} 之间")
    _safe_component(report_id, "report_id")
    if store.get_report(report_id) is None:
        raise KeyError(f"报告不存在：{report_id}")
    rows = store.list_evidence(report_id)
    already = sum(1 for item in rows if is_coded(item))
    targets = coding_targets(rows, force=force)
    total = len(targets) + (0 if force else already)
    coded = failed = 0
    root = Path(runs_root)
    for goal_id in sorted({str(item.get("goal_id") or "goal-1") for item in targets}):
        pending = [
            item for item in targets
            if str(item.get("goal_id") or "goal-1") == goal_id
        ]
        for number, start in enumerate(range(0, len(pending), batch_size), 1):
            batch = pending[start:start + batch_size]
            labels = await _code_batch(
                batch, adapter=adapter,
                output_path=_batch_output_path(
                    root, report_id, goal_id, number, folder="ugc-coding",
                ),
                report_id=report_id, goal_id=goal_id,
                engine_preference=engine_preference,
            )
            if labels is None:
                failed += len(batch)
                continue
            store.upsert_evidence_batch([
                _coding_payload(item, label) for item, label in zip(batch, labels)
            ])
            coded += len(batch)
            if on_event is not None:
                event = on_event({
                    "type": "ugc_coding_progress",
                    "data": {
                        "report_id": report_id, "goal_id": goal_id,
                        "batch_number": number, "batch_rows": len(batch),
                        "coded_total": coded, "failed_total": failed,
                    },
                })
                if hasattr(event, "__await__"):
                    await event
    return CodingResult(
        report_id=report_id, targets=total, coded=coded,
        failed=failed, already=(0 if force else already),
    )


__all__ = [
    "ATTITUDES", "AUDIENCES", "CODING_VERSION", "CodingResult", "SCENARIOS",
    "TOPICS", "TOPIC_NONE", "code_report", "coded_rows", "coding_errors",
    "coding_tables", "coding_targets", "is_coded", "ratio_phrase_offenders",
]


#: §CODE-1 货 2 备料：正式稿禁的比例句式（用户 09-05 拍甲——编码是模型判断，
#: 只能写「N 条里 M 条编码为正向」，不能推及全网）。闸词按**去掉引用原文与链接
#: 之后**的正文匹配：小红书话题名里就有「人类对豆包的开发不足百分之一」，
#: 拿它打红写手是冤枉——本包一轮重放实测踩过。
FORBIDDEN_RATIO_PATTERN = re.compile(
    r"\d+\s*(?:%|％)|百分之[零一二三四五六七八九十百千万\d]+"
    r"|多数用户|大多数用户|用户普遍|绝大多数用户"
)
_QUOTED = re.compile(r"[「『“\"][^」』”\"]{0,120}[」』”\"]")
_LINKED = re.compile(r"https?://\S+|\[[^\]]{0,120}\]\([^)]{0,300}\)")


def ratio_phrase_offenders(markdown: str) -> list[str]:
    """回正文里自己写的比例句式；引用原文与链接里的不算。"""

    stripped = _QUOTED.sub("", _LINKED.sub("", str(markdown)))
    return [match.group(0) for match in FORBIDDEN_RATIO_PATTERN.finditer(stripped)]


def coded_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """挑出已编码的行，并把 `extra.coding` 提到顶层，省得每处都解一遍。"""

    result: list[dict[str, Any]] = []
    for item in rows:
        coding = _extra(item).get("coding")
        if isinstance(coding, Mapping) and coding.get("coding_version"):
            result.append({**dict(item), "coding": dict(coding)})
    return result


#: 没命中任何主题的行归到这一格。不设它，四成条目会从主表里凭空消失，
#: 「表里 n 与已编码条数对账」也就永远对不上（底料实测 287 条里 118 条无主题）。
TOPIC_NONE = "未归主题"
#: `quotes` 表每个主题每种态度取几条原声。
QUOTES_PER_CELL = 3


def _quote_sort_key(row: Mapping[str, Any]) -> tuple[float, str]:
    """原声按互动量降序；取不到互动量的排在后面，同分按 id 稳定。"""

    from app.reliability.scoring import engagement_value

    value = engagement_value(row)
    return (-(value if isinstance(value, (int, float)) else -1.0), str(row.get("id")))


def coding_tables(
    rows: Iterable[Mapping[str, Any]], *, citations: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """把已编码的行聚成正式稿要的确定性表（用户 09-05 拍乙的表型）。

    主表是「主题 × 态度」，另加一张场景条数表；`audience` 不出表——底料实测
    287 条里 260 条「不明」，摆出来是一格独大的空表，只在附录写一句。
    `citations` 是 证据 id → 角标序号，没给就不填角标（表本身不依赖它）。
    """

    from app.reliability.scoring import engagement_value

    coded = coded_rows(rows)
    marks = dict(citations or {})
    attitude_by_topic: list[dict[str, Any]] = []
    cells: dict[tuple[str, str], int] = {}
    for item in coded:
        topics = list(item["coding"].get("topics") or []) or [TOPIC_NONE]
        for topic in topics:
            key = (topic, item["coding"]["attitude"])
            cells[key] = cells.get(key, 0) + 1
    order = {name: index for index, name in enumerate((*TOPICS, TOPIC_NONE))}
    for (topic, attitude), count in sorted(
        cells.items(), key=lambda pair: (order.get(pair[0][0], 99), pair[0][1])
    ):
        attitude_by_topic.append(
            {"topic": topic, "attitude": attitude, "count": count}
        )

    scenario_counts = [
        {"scenario": name, "count": count}
        for name, count in sorted(
            Counter(item["coding"]["scenario"] for item in coded).items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
    ]

    quotes: list[dict[str, Any]] = []
    for topic in (*TOPICS, TOPIC_NONE):
        for attitude in ("正", "负"):
            picked = sorted(
                (
                    item for item in coded
                    if item["coding"]["quote"]
                    and item["coding"]["attitude"] == attitude
                    and topic in (item["coding"].get("topics") or [TOPIC_NONE])
                ),
                key=_quote_sort_key,
            )[:QUOTES_PER_CELL]
            for item in picked:
                quotes.append({
                    "topic": topic,
                    "attitude": attitude,
                    "quote": item["coding"]["quote"],
                    "evidence_id": str(item.get("id")),
                    "citation": (
                        f"[S{marks[str(item.get('id'))]:02d}]"
                        if str(item.get("id")) in marks else None
                    ),
                    "platform": item.get("platform"),
                    "engagement": engagement_value(item),
                })

    audience = Counter(item["coding"]["audience"] for item in coded)
    unknown = audience.get("不明", 0)
    return {
        "coding_version": CODING_VERSION,
        "coded_rows": len(coded),
        "attitude_by_topic": attitude_by_topic,
        "scenario_counts": scenario_counts,
        "quotes": quotes,
        # 对账口径写在数据里，别让读表的人自己猜：场景表一行一条、加起来等于条数；
        # 主题表一条可命中多个主题，加起来是**命中次数**，天然大于条数。
        "reconciliation": {
            "scenario_sum": sum(row["count"] for row in scenario_counts),
            "topic_hit_sum": sum(row["count"] for row in attitude_by_topic),
            "distinct_rows": len(coded),
        },
        # 用户 09-05 拍乙：audience 不出表，附录一句话。
        "audience_note": (
            f"{len(coded)} 条编码里 {unknown} 条看不出发帖人身份"
            f"（{unknown * 100 // len(coded)}%）" if coded else "无已编码证据"
        ),
        "method_note": (
            f"模型编码（{CODING_VERSION}），包终端复核 30 条一致 28 条；"
            "表内均为条数，不是全网比例。"
        ),
    }
