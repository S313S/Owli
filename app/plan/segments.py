"""规划段的即时落盘、段级重试与确定性断点拼接。"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

from app.adapters.contracts import PlanningSegmentRequest
from app.config import ResilienceConfig


_SEGMENT_NAME = re.compile(r"(?:skeleton|goal-[1-9][0-9]*)")


class PlanSegmentError(RuntimeError):
    """规划段耗尽配置重试预算后仍未形成合法 JSON。"""


def merge_continuation(prefix: str, suffix: str) -> str:
    """按字符级最长首尾重叠拼接，覆盖重复 token 与 token 中间断流。"""

    if not prefix:
        return suffix
    if not suffix:
        return prefix
    maximum = min(len(prefix), len(suffix))
    for size in range(maximum, 0, -1):
        if prefix[-size:] == suffix[:size]:
            return prefix + suffix[size:]
    return prefix + suffix


class PlanSegmentWorkspace:
    """管理单个 research 下 plan-segments 的正式文件与 partial。"""

    def __init__(self, research_root: Path, config: ResilienceConfig) -> None:
        self.root = Path(research_root) / "plan-segments"
        self.config = config

    @staticmethod
    def _checked_name(name: str) -> str:
        if _SEGMENT_NAME.fullmatch(name) is None:
            raise ValueError(f"非法规划段名称：{name}")
        return name

    def formal_path(self, name: str) -> Path:
        return self.root / f"{self._checked_name(name)}.json"

    def partial_path(self, name: str) -> Path:
        return self.root / f"{self._checked_name(name)}.json.partial"

    async def generate(
        self,
        name: str,
        prompt: str,
        adapter: Any,
        *,
        on_retry: Any = None,
    ) -> dict[str, Any]:
        formal = self.formal_path(name)
        partial = self.partial_path(name)
        self.root.mkdir(parents=True, exist_ok=True)
        continuation = (
            partial.read_text(encoding="utf-8") if partial.is_file() else ""
        )
        last_error = "规划短流未完成"
        current_prompt = prompt

        for attempt in range(1, self.config.plan_segment_retries + 1):
            # 每轮起跑先移除旧 partial；已收前缀已进内存并随请求续写，
            # 防止 Agent Write/Edit 因残留文件进入覆盖死锁。
            partial.unlink(missing_ok=True)
            assembled = continuation

            async def on_text(chunk: str) -> None:
                nonlocal assembled
                assembled = merge_continuation(assembled, str(chunk))
                partial.write_text(assembled, encoding="utf-8")

            request = PlanningSegmentRequest(
                research_id=self.root.parent.name,
                segment_name=name,
                prompt=current_prompt,
                continuation=continuation,
                output_path=formal,
            )
            result = adapter.run_planning_segment(request, on_text=on_text)
            if inspect.isawaitable(result):
                result = await result
            assembled = merge_continuation(assembled, str(result.text or ""))
            partial.write_text(assembled, encoding="utf-8")
            if result.completed:
                try:
                    value = json.loads(assembled)
                    if not isinstance(value, dict):
                        raise ValueError("规划段 JSON 顶层必须是 object")
                except (json.JSONDecodeError, ValueError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    formal.write_text(
                        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    partial.unlink(missing_ok=True)
                    return value
            else:
                last_error = result.error or "规划短流未收到完成信号"
            continuation = assembled
            current_prompt = (
                f"{prompt}\n\n上一轮本段失败原文：{last_error}。"
                "请保持原结构契约并从已有前缀继续。"
            )
            if attempt < self.config.plan_segment_retries and on_retry is not None:
                callback_result = on_retry(attempt + 1, last_error)
                if inspect.isawaitable(callback_result):
                    await callback_result

        raise PlanSegmentError(
            f"规划段 {name} 连续 {self.config.plan_segment_retries} 次失败："
            f"{last_error}"
        )


__all__ = [
    "PlanSegmentError",
    "PlanSegmentWorkspace",
    "merge_continuation",
]
