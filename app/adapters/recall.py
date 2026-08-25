"""主引擎历史判重：无工具、JSON Schema 约束、失败可明确退化。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.adapters.contracts import PlanningSegmentRequest
from app.store.recall import DuplicateDecision, RecallCandidate


class PrimaryEngineUnavailable(RuntimeError):
    """主引擎传输或服务不可用。"""


class PrimaryEngineInvalidOutput(ValueError):
    """主引擎返回了不可解析的判重产物。"""


def _output_schema(report_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "judgements": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "report_id": {"type": "string", "enum": list(report_ids)},
                        "same_item": {"type": "boolean"},
                        "confidence": {
                            "type": "string",
                            "enum": ["高", "中", "低"],
                        },
                        "reason": {"type": "string", "minLength": 1},
                        "reusable_elements": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": ["信息源组合", "采集方式", "报告骨架"],
                            },
                        },
                    },
                    "required": [
                        "report_id",
                        "same_item",
                        "confidence",
                        "reason",
                        "reusable_elements",
                    ],
                },
            }
        },
        "required": ["judgements"],
    }


def _prompt(query: str, candidates: Sequence[RecallCandidate]) -> str:
    payload = [
        {
            "report_id": item.report_id,
            "title": item.title,
            "research_question": item.research_question,
            "summary_line": item.summary_line,
            "tags": list(item.tags),
            "sources": list(item.sources),
            "completed_at": item.completed_at,
        }
        for item in candidates
    ]
    return (
        "你是 Owli 的历史调研判重器。判断新需求与各候选是否属于同一件事，"
        "重点看研究对象、决策目标，以及信息源组合、采集方式、报告骨架能否复用；"
        "不要只按字面相似度判断。最多返回 3 条最有参考价值的候选，同一件事和不同的"
        "候选都可返回。理由必须是一句话且能被用户直接阅读。不得向用户提问；歧义自行"
        "假设并降低 confidence。只返回符合给定 JSON Schema 的对象。\n\n"
        f"新需求：{query.strip()}\n"
        "历史候选：\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


class PrimaryEngineRecallJudge:
    """经适配层现有主引擎短流执行一次结构化判重。"""

    def __init__(self, routed_adapter: Any) -> None:
        self._routed_adapter = routed_adapter

    async def __call__(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
    ) -> tuple[DuplicateDecision, ...]:
        if not candidates:
            return ()
        digest = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:12]
        request = PlanningSegmentRequest(
            research_id=f"r-recall-{digest}",
            segment_name="duplicate-judge",
            prompt=_prompt(query, candidates),
            output_schema=_output_schema([item.report_id for item in candidates]),
        )
        try:
            result = await self._routed_adapter.run_planning_segment(request)
        except Exception as exc:
            raise PrimaryEngineUnavailable(
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not result.completed:
            cause = result.cause or (
                "transport" if result.transport_interrupted else "engine_error"
            )
            detail = result.error or "主引擎未返回完整结构化结论"
            raise PrimaryEngineUnavailable(f"{cause}: {detail}")
        try:
            value = json.loads(result.text)
            if not isinstance(value, Mapping):
                raise TypeError("判重结果顶层必须是 object")
            raw_judgements = value.get("judgements")
            if not isinstance(raw_judgements, list):
                raise TypeError("judgements 必须是 array")
            decisions = []
            for raw in raw_judgements:
                if not isinstance(raw, Mapping):
                    raise TypeError("judgements[] 必须是 object")
                decisions.append(DuplicateDecision(
                    report_id=str(raw["report_id"]),
                    same_item=raw["same_item"],
                    confidence=str(raw["confidence"]),
                    reason=str(raw["reason"]),
                    reusable_elements=tuple(raw["reusable_elements"]),
                ))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PrimaryEngineInvalidOutput(
                f"主引擎判重产物不可解析：{type(exc).__name__}: {exc}"
            ) from exc
        return tuple(decisions)
