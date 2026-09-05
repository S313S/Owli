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
from dataclasses import dataclass
from pathlib import Path
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
    return None


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
    "TOPICS", "code_report", "coding_errors", "coding_targets", "is_coded",
]
