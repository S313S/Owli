"""编排层与双引擎适配器之间唯一共享的任务与结果契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.adapters import validation as artifact_validation
from app.adapters.capability import Capability
from app.adapters.events import NormalizedEvent


@dataclass(frozen=True)
class OwliResult:
    """两侧逐字段一致的结构化结论。"""

    status: str
    output_path: str
    summary: str
    assumptions: list[dict[str, str]]
    unmet: list[str]
    capability_denials: list[str]
    reason: str | None = None


@dataclass(frozen=True)
class EngineTask:
    """不含引擎分支的统一任务 spec。"""

    body: str
    output_path: Path
    output_format: str
    research_id: str
    goal_id: str
    agent_id: str
    agent_kind: str
    validators: list[str]
    capability: Capability
    model: str | None = None
    user_override: str | None = None
    source_item_limit: int | None = None


@dataclass(frozen=True)
class EngineRunResult:
    """适配器执行后的统一双腿判定结果。"""

    conclusion: OwliResult | None
    conclusion_error: str | None
    validation: artifact_validation.ValidationReport
    events: list[NormalizedEvent]
    permission_denials: list[str]
    engine_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.engine_error is None
            and self.conclusion_error is None
            and self.conclusion is not None
            and self.conclusion.status == "done"
            and self.validation.verdict is artifact_validation.Verdict.PASS
        )


@dataclass(frozen=True)
class PlanningSegmentRequest:
    """规划短流请求；continuation 是上轮已确认落盘的原始前缀。"""

    research_id: str
    segment_name: str
    prompt: str
    continuation: str = ""
    output_path: Path | None = None

    @property
    def body(self) -> str:
        """兼容测试替身与事件观测中的任务正文命名。"""

        return self.prompt


@dataclass(frozen=True)
class PlanningSegmentResult:
    """规划短流结果；成功由协议完成信号和后续 JSON 校验共同判定。"""

    text: str
    completed: bool
    transport_interrupted: bool = False
    error: str | None = None
    cause: str | None = None
