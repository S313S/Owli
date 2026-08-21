"""经 RoutedAdapter 执行的 authority / independence 闭集判定。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineTask
from app.reliability.scoring import (
    AUTHORITY_SCORES,
    INTEREST_SCORES,
    PLATFORM_BASELINES,
    SCORE_FIELDS,
    grade_for_total,
    rating_notes_problem,
    score_evidence,
)


MAX_ATTEMPTS = 3
AUTHORITY_KINDS = frozenset(AUTHORITY_SCORES)
INTEREST_RELATIONS = frozenset(INTEREST_SCORES)


def _prompt(evidence: Sequence[Mapping[str, Any]], errors: Sequence[str]) -> str:
    retry = ""
    if errors:
        retry = "\n上一轮闭集错误（逐条修正，不得创造近义词）：\n" + "\n".join(errors)
    return (
        "目标：逐条判定来源权威性与利益无关性，只返回闭集标签。\n"
        "方法要点：authority_kind 的闭集与判据："
        "first_party_official=被讨论主体自己的官方域名；"
        "verified_principal=平台官方认证且主体是议题当事方；"
        "institutional_primary=具名机构的一手披露；"
        "named_secondary=具名二手报道、分析或评测且作者历史可查；"
        "community_high_signal=社区热度达批内P75及以上或作者历史贡献可查；"
        "anonymous_or_unverifiable=作者匿名或不可核验；"
        "content_farm=SEO聚合、批量生成或正文不支撑标题。"
        "interest_relation 的闭集与判据："
        "arms_length=作者与被评价对象无可见利益关系；"
        "disclosed_interest=有利益关系但已披露；"
        "undisclosed_interest=利益关系明显但未披露。"
        "判的是披露状态，不猜测动机。\n"
        "产物结构：写入任务指定 JSON 文件；顶层数组长度必须等于输入，顺序不变；"
        "每项只能含 authority_kind、interest_relation 两个字符串字段。\n"
        "边界与降级：只用输入字段判定；信息不足也必须在闭集内选最保守标签；"
        "不得输出解释性近义词。\n"
        f"输入证据：{json.dumps(list(evidence), ensure_ascii=False, separators=(',', ':'))}"
        f"{retry}"
    )


def _ctx(path: Path, research_id: str, goal_id: str, agent_id: str) -> validation.Ctx:
    runs_root = path.parents[3] if len(path.parents) >= 4 else validation.RUNS_ROOT
    return validation.Ctx(
        output_path=path,
        output_format="json",
        research_id=research_id,
        goal_id=goal_id,
        agent_id=agent_id,
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
        runs_root=runs_root,
    )


def _closed_set_errors(value: Any, expected: int) -> list[str]:
    if not isinstance(value, list):
        return ["判定产物顶层必须是数组"]
    errors: list[str] = []
    if len(value) != expected:
        errors.append(f"判定条数应为 {expected}，实际 {len(value)}")
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"items[{index}] 必须是 object")
            continue
        authority = item.get("authority_kind")
        relation = item.get("interest_relation")
        if authority not in AUTHORITY_KINDS:
            errors.append(f"items[{index}].authority_kind 越界：{authority!r}")
        if relation not in INTEREST_RELATIONS:
            errors.append(f"items[{index}].interest_relation 越界：{relation!r}")
    return errors


def degrade_after_closed_set_retry(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        platform = str(item.get("platform", "web_search"))
        scores = dict(PLATFORM_BASELINES.get(platform, PLATFORM_BASELINES["web_search"]))
        notes = " · ".join(
            f"{label}{scores[field]}:闭集失败取基线"
            for label, field in zip(("权威", "时效", "交叉", "完整", "无关"), SCORE_FIELDS)
        ) + " ⚠️闭集判定降级"
        extra = dict(item.get("extra") or {})
        extra.pop("authority_kind", None)
        extra.pop("interest_relation", None)
        extra["reliability_degraded"] = {
            "reason": "closed_set_retry_exhausted",
            "attempts": MAX_ATTEMPTS,
        }
        item.update(scores)
        item.update(
            score_total=sum(scores.values()),
            grade=grade_for_total(sum(scores.values())),
            rating_notes=notes,
            rated_by=f"baseline:{platform}@v1:degraded",
            extra=extra,
        )
        problem = rating_notes_problem(notes, item)
        if problem is not None:
            raise AssertionError(problem)
        result.append(item)
    return result


async def classify_and_score(
    evidence: Sequence[Mapping[str, Any]],
    *,
    adapter: Any,
    output_path: str | Path,
    research_id: str,
    goal_id: str,
    agent_id: str,
    on_event: Any = None,
) -> list[dict[str, Any]]:
    """闭集越界最多重试三次；耗尽后以平台基线落盘并显式 degraded。"""

    items = [dict(item) for item in evidence]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        path.unlink(missing_ok=True)
        task = EngineTask(
            body=_prompt(items, errors),
            output_path=path,
            output_format="json",
            research_id=research_id,
            goal_id=goal_id,
            agent_id=agent_id,
            agent_kind="reliability_audit",
            validators=["file_exists"],
            capability=Capability(
                profile="readonly-analyst",
                tools=("fs.write",),
                fs=FileSystemScope(write=(f"goals/{goal_id}/**",)),
            ),
        )
        result = await adapter.run(task, _ctx(path, research_id, goal_id, agent_id), on_event=on_event)
        if not bool(getattr(result, "succeeded", False)):
            errors = ["适配器双腿判定未通过"]
            continue
        try:
            labels = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors = [f"判定产物无法解析：{type(exc).__name__}"]
            continue
        errors = _closed_set_errors(labels, len(items))
        if errors:
            continue
        scored: list[dict[str, Any]] = []
        for item, label in zip(items, labels):
            extra = dict(item.get("extra") or {})
            extra.update(
                authority_kind=label["authority_kind"],
                interest_relation=label["interest_relation"],
            )
            enriched = {**item, "extra": extra}
            enriched.update(score_evidence(enriched))
            enriched["rated_by"] = f"agent:{agent_id}"
            scored.append(enriched)
        path.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
        return scored
    degraded = degrade_after_closed_set_retry(items)
    path.write_text(json.dumps(degraded, ensure_ascii=False, indent=2), encoding="utf-8")
    return degraded


__all__ = [
    "AUTHORITY_KINDS",
    "INTEREST_RELATIONS",
    "MAX_ATTEMPTS",
    "classify_and_score",
    "degrade_after_closed_set_retry",
]
