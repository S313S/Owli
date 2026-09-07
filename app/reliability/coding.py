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
from app.plan.entities import mentions
from app.report.polish.lexicon import TOPIC_LEXICON

AGENT_ID = "ugc-coding"
#: v2 = 闭集加了 trigger / alternatives（§RPT-2 货 4③）。两个字段都可空，
#: 所以 v1 那批老行照样能用——`coded_rows` 只看这个字段非空，不比对具体值。
CODING_VERSION = "v2"
MAX_ATTEMPTS = 3
CODING_BATCH_MAX = 40
QUOTE_MAX = 40

AUDIENCES = ("学生", "职场", "创作者", "开发者", "家长", "不明")
SCENARIOS = ("学习", "写作", "办公", "编程", "生活娱乐", "情感陪伴", "其他")
ATTITUDES = ("正", "负", "中", "混合")
#: §RPT-2 货 4③：为什么开始用/换（借 customer-research 的「触发事件」）。
#: 与 `alternatives` 一样**可空**——底料里多数帖子根本不交代这个，
#: 逼写手填等于逼它猜，那比空着糟。
TRIGGERS = ("推荐", "热点", "工作要求", "试新", "不明")
#: 同帖提到的其他工具，自由文本，最多三个。多了多半是在抄榜单不是在比较。
ALTERNATIVES_MAX = 3
#: 八主题闭集**就是**词表的键，不另存一份——两份迟早分叉，而分叉是静默的：
#: 词表改了键名，库里已编码的老数据会一声不响地落在闭集外。
#: 词表是 §RPT-1 地界（「改词表即改口径」），本包只读它。
TOPICS = tuple(TOPIC_LEXICON)


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
    """`extra` 两种形状都认：`Store.list_evidence` 给的是解好的 dict，裸 sqlite
    读出来的是 JSON 字符串。

    只认 dict 的后果特别坏：裸读的调用方会**静默**拿到 0 条编码，一路绿到三张表
    整块不出，而没有任何一处报错——看起来像「编码没做」，其实是类型没对上。
    `polish/tables.py:_with_extra` 早就是这么处理的，两处口径统一。
    """

    value = item.get("extra")
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
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


def _plan_entity_names(report: Mapping[str, Any] | None) -> list[str]:
    """从报告的计划快照取被评实体的全部叫法，取不到就空着（编码照跑，只是不加这条约束）。

    沿用 `_entity_aliases`——它按 canonical 把「豆包」「Doubao」两张卡并成一个实体。
    延迟 import：`polish` 那层会反过来 import 本模块，放模块顶层就成环。
    """

    from app.report.polish.tables import _entity_aliases

    plan = (report or {}).get("plan_snapshot")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError:
            return []
    if not isinstance(plan, Mapping):
        return []
    return sorted({name for names in _entity_aliases(plan).values() for name in names
                   if len(str(name).strip()) >= 2})


def _coding_prompt(items: Sequence[Mapping[str, Any]], *, output_path: Path,
                   entity_names: Sequence[str] = ()) -> str:
    # §CODE-2 货 3：防**新**数据再出「引反人」。它不替代出表时那道程序闸——
    # 已经编码好的行不会因为提示词变了就重编，重编要真金白银付引擎钱。
    naming = (
        f"quote 必须点名被评实体（{'、'.join(entity_names[:12])}）——"
        "同一条里如果有既点了名、又能代表这条态度的句子，**必须选那句**；"
        "只有整条都没点名时才退而摘最能代表态度的一句。"
        "**别摘夸别的产品的话**：「但是 X 不会觉得自己是你的对立面」这种句子夸的是 X，"
        "拿它当被评实体的正面原声就是引反了人。\n"
    ) if entity_names else ""
    return (
        "目标：对国内社媒 UGC 逐条打结构化编码，供后续按条数聚合。只依据输入文本，"
        "不补造事实、不推测作者身份。\n"
        "每项输出 id、audience、scenario、attitude、topics、quote、trigger、"
        "alternatives 八个字段。\n"
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
        + naming +
        f"trigger 闭集：{'/'.join(TRIGGERS)}，答的是「这个人为什么开始用或换用」。"
        "帖子没交代就填「不明」——**不许从场景倒推**，说在办公场景用不等于是工作要求。\n"
        f"alternatives 是数组，最多 {ALTERNATIVES_MAX} 个，填**同一条里提到的其他工具名**，"
        "每个名字必须逐字出现在输入文本里；没提到别的工具就给空数组，不要补全竞品清单。\n"
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
        text = _squeeze(sources.get(str(identity), ""))
        if squeezed and squeezed not in text:
            errors.append(f"items[{index}].quote 不是原文子串，不许改写或拼接")
        # §RPT-2 货 4③：两个字段**可空**，缺字段不算错；填了才守规矩。
        # 老版本（v1）编码的行没有它们，不能因此判整批失败。
        trigger = item.get("trigger")
        if trigger not in (None, "") and trigger not in TRIGGERS:
            errors.append(f"items[{index}].trigger 越界：{trigger!r}")
        alternatives = item.get("alternatives")
        if alternatives not in (None, []):
            if not isinstance(alternatives, list) or len(alternatives) > ALTERNATIVES_MAX:
                errors.append(
                    f"items[{index}].alternatives 必须是不超过 {ALTERNATIVES_MAX} 个的数组")
            else:
                for name in alternatives:
                    if not isinstance(name, str) or not name.strip():
                        errors.append(f"items[{index}].alternatives 里有空项")
                    # 和 quote 同一道闸：不查子串，模型会把常见竞品名补全成一张榜单。
                    elif _squeeze(name) not in text:
                        errors.append(
                            f"items[{index}].alternatives 的 {name!r} 不在原文里")
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
    entity_names: Sequence[str] = (),
) -> list[dict[str, Any]] | None:
    """一批编码；三次重打都过不了闸就整批返回 None，绝不半信半疑地写库。"""

    compact = [engine_input(item) for item in items]
    sources = {str(item.get("id")): source_text(item) for item in items}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        output_path.unlink(missing_ok=True)
        body = _coding_prompt(compact, output_path=output_path,
                              entity_names=entity_names)
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
    report = store.get_report(report_id)
    if report is None:
        raise KeyError(f"报告不存在：{report_id}")
    # §CODE-2 货 3：把被评实体的叫法带进提示词，让模型挑句子时就避开「夸别人的话」。
    entity_names = _plan_entity_names(report)
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
                entity_names=entity_names,
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
#: 无条件违规：这几个词本身就是「推及全网」，跟有没有数字无关。
FORBIDDEN_CROWD_PATTERN = re.compile(r"多数用户|大多数用户|用户普遍|绝大多数用户")
#: 比例写法。**不无条件禁**——表里的「被引占比 15%」「287 条里 260 条（90%）」都是
#: 合法的数据引用，一刀切会把它们打成假红（本包三轮重放里假红比真红还多）。
#: 只有当同一句里还出现人群主语时，它才是用户拍甲禁的那种「用户 X% 认为」。
FORBIDDEN_RATIO_PATTERN = re.compile(
    r"\d+\s*(?:%|％)|百分之[零一二三四五六七八九十百千万\d]+"
)
CROWD_SUBJECT = re.compile(r"用户|网友|受访者|消费者|的人|人们|大家")
_SENTENCE = re.compile(r"[^。！？；\n]+")
_QUOTED = re.compile(r"[「『“\"][^」』”\"]{0,120}[」』”\"]")
_LINKED = re.compile(r"https?://\S+|\[[^\]]{0,120}\]\([^)]{0,300}\)")


def ratio_phrase_offenders(markdown: str) -> list[str]:
    """回正文里自己写的比例句式；引用原文与链接里的不算。

    编码是**模型判断**，正式稿只能写「N 条里 M 条编码为正向」，不能写
    「用户 X% 认为」（用户 2026-09-05 拍甲）。所以判的是**推及全网**，
    不是判百分号：占比数据照写，写成人群断言才红。
    """

    stripped = _QUOTED.sub("", _LINKED.sub("", str(markdown)))
    offenders = [m.group(0) for m in FORBIDDEN_CROWD_PATTERN.finditer(stripped)]
    for sentence in _SENTENCE.findall(stripped):
        if CROWD_SUBJECT.search(sentence):
            offenders.extend(m.group(0) for m in FORBIDDEN_RATIO_PATTERN.finditer(sentence))
    return offenders


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


#: 丢弃样本每类留几条——留数是为了**能抽查这道闸判得对不对**，不是为了出表。
#: 全量留会把几百条正文塞进报告数据块，一条不留就只剩一个没法复核的总数。
DROPPED_SAMPLES = 8


_PROPER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,}|[「『《]([^」』》]{2,12})[」』》]")


def _looks_like_other_name(quote: str, accepted: Sequence[str]) -> bool:
    """这句话里像不像点了**别人**的名字。粗筛，用来分堆，不用来判对错。

    认两种形状：拉丁词（`WorkBuddy`、`Claude`、`K3`）和书名号/引号里的短名
    （「通义千问」）。中文裸写的竞品名（通义千问不加引号）认不出来，会落进
    「谁都没点」那堆——**所以这两堆的边界是软的**，它只是让人抽查时知道先看哪堆，
    真要下结论得读样本原文。样本就在旁边，别只信这个标。
    """

    for match in _PROPER_NAME.finditer(quote):
        text = (match.group(1) or match.group(0)).strip()
        if len(text) >= 2 and not any(mentions(text, name) for name in accepted):
            return True
    return False


def _dropped_quotes(
    coded: Sequence[Mapping[str, Any]], accepted: Sequence[str],
    marks: Mapping[str, int],
) -> dict[str, Any]:
    """被原声闸丢掉的行：分两堆、各留样本、留计数。

    §CODE-2 判据 3：丢弃**必须数得出**。丢得多不一定是闸判错了——这份底料 74%
    的原声压根没点名被评实体（点的是别的产品，或者干脆是「短发yyds」这种跑题
    内容）——但**丢得多也可能是叫法表不全**，那会把国内用户的声音又删掉一批。
    两者从总数上分不出来，只能靠人读样本，所以这里留样本。

    分母写两个：`已编码带原声` 是语料面，`本可入表` 是这张表面（只算进了引用池、
    本来就够格出表的那些）。表注要用的是后者——读者看见的是那张表，不是全库。
    """

    if not accepted:
        return {"设闸": False, "丢弃": 0}
    with_quote = [item for item in coded if item["coding"].get("quote")]
    dropped = [item for item in with_quote
               if not _names_the_entity(item["coding"]["quote"], accepted)]
    eligible = [item for item in with_quote
                if not marks or str(item.get("id")) in marks]
    dropped_eligible = [item for item in eligible
                        if not _names_the_entity(item["coding"]["quote"], accepted)]

    def sample(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{"evidence_id": str(item.get("id")), "platform": item.get("platform"),
                 "quote": item["coding"]["quote"]}
                for item in items[:DROPPED_SAMPLES]]

    others = [item for item in dropped
              if _looks_like_other_name(item["coding"]["quote"], accepted)]
    none = [item for item in dropped if item not in others]
    return {
        "设闸": True,
        "已编码带原声": len(with_quote),
        "点名被评实体": len(with_quote) - len(dropped),
        "丢弃": len(dropped),
        "本可入表": len(eligible),
        "本可入表被丢": len(dropped_eligible),
        "点了别的名": {"条数": len(others), "样本": sample(others)},
        "谁都没点": {"条数": len(none), "样本": sample(none)},
    }


def _names_the_entity(quote: str, accepted: Sequence[str]) -> bool:
    """这句原声点没点被评实体的名。`accepted` 为空 = 不设闸，行为与加闸前一字不差。

    §CODE-2：加闸前程序只校验「是正文子串」——**逐字摘对了，但摘的可能是在夸
    别人**。评审第二轮坐实过一条：「但是workbuddy不会觉得自己是你的对立面」被
    当成豆包的正向原声出表，而这句夸的是 WorkBuddy、语境在暗踩豆包，等于当着
    客户的面把贬他的话说成夸他的话。子串闸拦不住这种错，因为它确实是子串。

    不设闸时不过滤，是留给备料与离线核数（那两处要看全量），和 `citations`
    为空时不筛角标是同一个道理。
    """

    return not accepted or any(mentions(quote, name) for name in accepted)


def _quote_sort_key(row: Mapping[str, Any]) -> tuple[float, str]:
    """原声按互动量降序；取不到互动量的排在后面，同分按 id 稳定。"""

    from app.reliability.scoring import engagement_value

    value = engagement_value(row)
    return (-(value if isinstance(value, (int, float)) else -1.0), str(row.get("id")))


def coding_tables(
    rows: Iterable[Mapping[str, Any]], *, citations: Mapping[str, int] | None = None,
    entity_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把已编码的行聚成正式稿要的确定性表（用户 09-05 拍乙的表型）。

    主表是「主题 × 态度」，另加一张场景条数表；`audience` 不出表——底料实测
    287 条里 260 条「不明」，摆出来是一格独大的空表，只在附录写一句。
    `citations` 是 证据 id → 角标序号，没给就不填角标（表本身不依赖它）。

    `entity_names` 是被评实体的全部叫法（中文名/英文名/别名，已按 canonical 归一）。
    §CODE-2：给了就只出**点名了被评实体**的原声，其余丢弃并计数；不给不设闸。
    调用方从计划的实体卡取名，别在这儿另抽一份——名字空间对不齐是静默的。
    """

    from app.reliability.scoring import engagement_value

    coded = coded_rows(rows)
    marks = dict(citations or {})
    # 一个字的叫法（"X"）拿去做包含匹配满篇都是，和 `plan/lint.py` 同一条规矩。
    accepted = [str(name).strip() for name in (entity_names or [])
                if len(str(name).strip()) >= 2]
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
    # §CODE-2 货 2：同一句原声跨格去重。一条 UGC 可命中多个主题，原样出表同一句
    # 会在两格各占一行（评审第二轮实测 S58 出现两行）；读者数不出这是一个人说的
    # 还是两个人说的，等于把 1 条声音读成 2 条。按**去空白后的句子**认，不按证据
    # id 认：转发同一句话的两条证据，对读者也是同一句。
    seen_quotes: set[str] = set()
    for topic in (*TOPICS, TOPIC_NONE):
        for attitude in ("正", "负"):
            candidates = sorted(
                (
                    item for item in coded
                    if item["coding"]["quote"]
                    and item["coding"]["attitude"] == attitude
                    and topic in (item["coding"].get("topics") or [TOPIC_NONE])
                    # 给了角标表就只挑**引得动**的：没角标的原声写手用不了——
                    # 弃用是浪费，裸引会被尺子③判红，摆出来只会诱导它裸引。
                    # 不给角标表时（备料、离线核数）不过滤，行为不变。
                    and (not marks or str(item.get("id")) in marks)
                    and _names_the_entity(item["coding"]["quote"], accepted)
                ),
                key=_quote_sort_key,
            )
            # 边挑边记 seen，不是先过滤再截断：**跨格重复与格内重复是同一件事**，
            # 只挡跨格的话，两条证据摘出同一句话、又落在同一格，照样出两行。
            # 记在截断之前，所以去掉重复不会让这一格空一位——后面的候选补得上来。
            picked: list[Mapping[str, Any]] = []
            for item in candidates:
                if len(picked) >= QUOTES_PER_CELL:
                    break
                key = _squeeze(item["coding"]["quote"])
                if key in seen_quotes:
                    continue
                seen_quotes.add(key)
                picked.append(item)
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

    dropped = _dropped_quotes(coded, accepted, marks)
    audience = Counter(item["coding"]["audience"] for item in coded)
    unknown = audience.get("不明", 0)
    return {
        "coding_version": CODING_VERSION,
        "coded_rows": len(coded),
        "attitude_by_topic": attitude_by_topic,
        "scenario_counts": scenario_counts,
        "quotes": quotes,
        # §CODE-2 货 1：被闸丢掉的原声要**数得出、抽得到**，不是静默跳过。
        # 丢得异常多（实测这份底料 74%）说明的不是闸坏了，可能是语料跑题、
        # 也可能是叫法表不全——两者都得有人看见才判得出，所以留样本不留总数。
        "quotes_dropped": dropped,
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


def _quotes_footnote(dropped: Mapping[str, Any]) -> str:
    """表注：这张表筛掉了多少、以及**不该**从行数少里读出什么。

    §CODE-2 判据 4 的变体，调度 09-07 晚补的：闸加上之后这张表会从 24 行缩到 4 行，
    而 4 行全在境外平台。读者看见这个形状，会顺手读出两个都不成立的结论——
    「国内没人评这个产品」和「我们没采到国内的声音」。实测这份底料两个都是假的：
    国内两家平台上点名评被评实体的原声有 64 条，占全部点名原声的九成，
    只是没走到这张表里。空表会被读成「没人这么说」，**一张筛短了的表同样会**，
    所以行数少的时候必须自己交代是筛短的。措辞不出字段名与表名：读表的是人。
    """

    if not dropped.get("设闸") or not dropped.get("丢弃"):
        return ""
    return (
        f"本表只收**点名了研究对象**的原声：另有 {dropped['本可入表被丢']} 条原本够格"
        f"进表的原声通篇没提到研究对象（多半在说别的产品，或与研究对象无关），已排除；"
        f"全部已编码原声里同样没点名的共 {dropped['丢弃']} 条。"
        f"所以**行数少是筛选后的结果**——既不代表没人讨论这个产品，"
        f"也不代表没有采到某个平台的声音。"
    )


def _shell(name: str, title: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]],
           *, n: int, basis: str, coverage: Mapping[str, Any]) -> dict[str, Any]:
    """正式稿的标准表壳，字段与 `app/report/polish/tables.py:_table` 一字不差。

    `marks` 不是壳字段，是**行内一列**——另六张表都这么摆，形态不一写手会读岔。
    """

    return {"name": name, "title": title, "columns": list(columns), "rows": list(rows),
            "n": n, "basis": basis, "coverage": dict(coverage)}


def _row_marks(items: Iterable[Mapping[str, Any]], marks: Mapping[str, int]) -> list[str]:
    """这一格背后的角标，去重升序；没被引用的证据不产生角标。"""

    return [f"S{no:02d}" for no in sorted(
        {marks[str(item.get("id"))] for item in items if str(item.get("id")) in marks}
    )]


def polish_tables(
    rows: Iterable[Mapping[str, Any]], *, citations: Mapping[str, int] | None = None,
    total_evidence: int | None = None, entity_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把 `coding_tables` 的聚合结果包成正式稿要的三张标准壳表。

    聚合语义一份、呈现形态一份，不重算——重算两遍迟早对不上账（§RATE-4 踩过：
    两条打分路 447 行不一致，把被测改动整个掩掉了）。
    """

    rows = list(rows)
    marks = dict(citations or {})
    data = coding_tables(rows, citations=citations, entity_names=entity_names)
    coded = coded_rows(rows)
    n = len(coded)
    total = len(rows) if total_evidence is None else total_evidence
    # 分母写进 coverage 与 basis：表里的 n 是**已编码的 UGC 条数**，不是全库条数。
    # 写手看不见机器表名，只看得见 title 与 basis，防误读只能靠这两处。
    coverage = {"已编码 UGC 条数": n, "全库证据条数": total,
                "身份不明条数": sum(
                    1 for item in coded if item["coding"]["audience"] == "不明")}
    by_cell: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    by_scenario: dict[str, list[Mapping[str, Any]]] = {}
    # §RPT-2 货 3：人群 × 态度、场景 × 态度。两张都从同一批 `coded` 里聚，
    # 不另起一条计算路——§RATE-4 踩过两条路 447 行不一致、把被测改动整个掩掉。
    by_audience: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    by_scene_cell: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in coded:
        for topic in (item["coding"].get("topics") or [TOPIC_NONE]):
            by_cell.setdefault((topic, item["coding"]["attitude"]), []).append(item)
        by_scenario.setdefault(item["coding"]["scenario"], []).append(item)
        by_audience.setdefault(
            (item["coding"]["audience"], item["coding"]["attitude"]), []).append(item)
        by_scene_cell.setdefault(
            (item["coding"]["scenario"], item["coding"]["attitude"]), []).append(item)
    unknown_audience = coverage["身份不明条数"]
    # §RPT-2 货 4③：trigger 可空，且 v1 那批老行根本没有这个字段。
    # 覆盖率写进 coverage，写手才知道这张表代表多少条、不至于拿它当全量。
    by_trigger: dict[str, list[Mapping[str, Any]]] = {}
    for item in coded:
        value = item["coding"].get("trigger")
        if value:
            by_trigger.setdefault(str(value), []).append(item)
    triggered = sum(len(v) for v in by_trigger.values())
    coverage = {**coverage, "带触发事件条数": triggered}
    return {
        "attitude_by_topic": _shell(
            "attitude_by_topic", "UGC 逐条编码：主题 × 态度条数",
            ("主题", "态度", "条数"),
            [{"主题": row["topic"], "态度": row["attitude"], "条数": row["count"],
              "marks": _row_marks(by_cell.get((row["topic"], row["attitude"]), []), marks)}
             for row in data["attitude_by_topic"]],
            n=n,
            basis=(
                f"对 {n} 条 UGC 逐条模型编码后计数（{data['method_note']}）。"
                f"一条可命中多个主题，故各格相加是**命中次数** "
                f"{data['reconciliation']['topic_hit_sum']}，大于条数 {n}；"
                f"没命中任何主题的归入「{TOPIC_NONE}」，不设它这一格四成条目会凭空消失。"
                f"条数覆盖全部 {n} 条已编码 UGC；角标只标其中**进了引用池**的那些，"
                f"所以有的格有条数没角标——那是没进池，不是数据可疑。"
            ),
            coverage=coverage),
        "scenario_counts": _shell(
            "scenario_counts", "UGC 逐条编码：使用场景条数",
            ("场景", "条数"),
            [{"场景": row["scenario"], "条数": row["count"],
              "marks": _row_marks(by_scenario.get(row["scenario"], []), marks)}
             for row in data["scenario_counts"]],
            n=n,
            basis=(
                f"每条 UGC 归一个场景，各行相加 = {data['reconciliation']['scenario_sum']}"
                f" = 已编码条数 {n}。分母是已编码的 UGC，不是全库 {total} 条证据。"
                f"{data['audience_note']}，故人群不单独出表。"
            ),
            coverage=coverage),
        "quotes": _shell(
            "quotes", "UGC 代表原声（每格按互动量取前 3）",
            ("主题", "态度", "原声", "平台", "互动量"),
            # 呈现层**永远**只出引得动的：一条角标都没有的原声，写手弃用是浪费、
            # 裸引会被尺子③判红。`coding_tables` 在没给角标表时不过滤（备料、
            # 离线核数要看全量），但走到这里就是要喂给写手了，没有回退。
            [row for row in (
                {"主题": q["topic"], "态度": q["attitude"], "原声": q["quote"],
                 "平台": q["platform"], "互动量": q["engagement"],
                 "marks": _row_marks([{"id": q["evidence_id"]}], marks)}
                for q in data["quotes"]) if row["marks"]],
            n=len(data["quotes"]),
            basis=(
                "从原文逐字摘出、程序校验过是正文子串的原声；每个主题的正/负各取"
                "互动量最高的 3 条。原声是**例子不是分布**，读它不能替代读上面的条数表。"
                + _quotes_footnote(data["quotes_dropped"])
            ),
            coverage=coverage),
        # §RPT-2 货 3：人群 × 态度。「不明」占到一半就整张不出——底料实测 287 条里
        # 260 条不明，摆出来是一格独大的表，读者会把「没标出身份」读成「这类人最多」。
        # 不出表不是缺一块内容：那个数在 coverage 的「身份不明条数」里，附录写一句。
        **({} if unknown_audience * 2 >= n else {"audience_attitude": _shell(
            "audience_attitude", "UGC 逐条编码：人群 × 态度条数",
            ("人群", "态度", "条数"),
            [{"人群": who, "态度": attitude, "条数": len(items),
              "marks": _row_marks(items, marks)}
             for (who, attitude), items in sorted(
                 by_audience.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
            n=n,
            basis=(
                f"每条 UGC 归一个人群、一个态度，各行相加 = 已编码条数 {n}；"
                f"其中身份不明 {unknown_audience} 条，占比不到一半才出这张表。"
            ),
            coverage=coverage)}),
        # §RPT-2 货 4③：触发事件条数。一条都没标就整张不出——v1 编码的行没这个字段，
        # 摆一张全空的表会被读成「没人是因为推荐来的」，那是把缺字段读成了结论。
        **({} if not triggered else {"trigger_counts": _shell(
            "trigger_counts", "UGC 逐条编码：为什么开始用/换",
            ("触发事件", "条数"),
            [{"触发事件": name, "条数": len(items), "marks": _row_marks(items, marks)}
             for name, items in sorted(by_trigger.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
            n=triggered,
            basis=(
                f"分母是**标出了触发事件的** {triggered} 条，不是已编码的 {n} 条，"
                f"更不是全库 {total} 条——多数帖子不交代为什么开始用，"
                f"没标出来的不计入，也不能当成「不明」那一格的人。"
            ),
            coverage=coverage)}),
        # §RPT-2 货 3：场景 × 态度。`scenario_counts` 只答「在什么场景下被谈」，
        # 这张才答「在那个场景下是夸还是骂」——口碑节要的是后者。
        "scenario_attitude": _shell(
            "scenario_attitude", "UGC 逐条编码：场景 × 态度条数",
            ("场景", "态度", "条数"),
            [{"场景": scene, "态度": attitude, "条数": len(items),
              "marks": _row_marks(items, marks)}
             for (scene, attitude), items in sorted(
                 by_scene_cell.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
            n=n,
            basis=(
                f"每条 UGC 归一个场景、一个态度，各行相加 = 已编码条数 {n}。"
                f"分母是已编码的 UGC，不是全库 {total} 条证据。"
                f"角标只标其中进了引用池的那些，有条数没角标是没进池、不是数据可疑。"
            ),
            coverage=coverage),
    }
