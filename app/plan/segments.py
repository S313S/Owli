"""规划段的即时落盘、段级重试与确定性断点拼接。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

from app.adapters.contracts import PlanningSegmentRequest
from app.config import ResilienceConfig


_SEGMENT_NAME = re.compile(
    r"(?:skeleton|goal-[1-9][0-9]*(?:-ch-[1-9][0-9]*)?)"
)


class PlanSegmentError(RuntimeError):
    """规划段耗尽配置重试预算后仍未形成合法 JSON。"""


def _json_payload(text: str) -> str:
    """剥离 Markdown 代码围栏，只认结构不认措辞。

    接受：```json / ``` 开头；闭合围栏可有可无（6b 实跑取证：
    r-99fdccf53cae goal-3-ch-2 重写轮只给开头围栏不给闭合，旧实现要求
    首尾成对导致三连 JSONDecodeError）。围栏外若有前后缀文字，退化为
    取首个 ``{`` 到末个 ``}`` 之间的片段。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        stripped = stripped[first_newline + 1:] if first_newline >= 0 else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
        stripped = stripped.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            stripped = stripped[start:end + 1]
    return stripped


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

    def __init__(
        self,
        research_root: Path,
        config: ResilienceConfig,
        *,
        retry_sleep: Any = asyncio.sleep,
    ) -> None:
        self.root = Path(research_root) / "plan-segments"
        self.config = config
        self._retry_sleep = retry_sleep
        self._attempts: dict[str, int] = {}

    def reset_attempts(self, name: str) -> None:
        """清零某段的尝试计数：章节预算按「lint 轮」独立计，不跨轮累加
        （r-072721cddbb0 取证：第 1 轮语义退回用掉 2 次，第 2 轮整份重生成
        时剩 1 次即「预算耗尽」）。"""
        self._attempts.pop(name, None)

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
        output_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        formal = self.formal_path(name)
        partial = self.partial_path(name)
        self.root.mkdir(parents=True, exist_ok=True)
        counted_attempts = self._attempts.get(name, 0)
        if counted_attempts >= self.config.plan_segment_retries:
            raise PlanSegmentError(
                f"规划段 {name} 总尝试预算 "
                f"{self.config.plan_segment_retries} 次已耗尽"
            )
        continuation = (
            partial.read_text(encoding="utf-8") if partial.is_file() else ""
        )
        last_error = "规划短流未完成"
        current_prompt = prompt

        transport_failures = 0
        while counted_attempts < self.config.plan_segment_retries:
            attempt = counted_attempts + 1
            # 每轮起跑先移除旧 partial；已收前缀已进内存并随请求续写，
            # 防止 Agent Write/Edit 因残留文件进入覆盖死锁。
            partial.unlink(missing_ok=True)
            received = ""
            assembled = continuation

            async def on_text(chunk: str) -> None:
                nonlocal assembled, received
                # 同一次 SDK 流的 delta 是互不重叠的增量，只能原样追加；
                # 最长重叠只用于上一轮 partial 与本轮完整响应之间。
                received += str(chunk)
                assembled = merge_continuation(continuation, received)
                partial.write_text(assembled, encoding="utf-8")

            request = PlanningSegmentRequest(
                research_id=self.root.parent.name,
                segment_name=name,
                prompt=current_prompt,
                continuation=continuation,
                output_path=formal,
                output_schema=output_schema,
            )
            result = adapter.run_planning_segment(request, on_text=on_text)
            if inspect.isawaitable(result):
                result = await result
            generated = str(result.text or "") or received
            assembled = merge_continuation(continuation, generated)
            partial.write_text(assembled, encoding="utf-8")
            cause = str(getattr(result, "cause", "") or "").casefold()
            effective_cause = (
                "transport" if result.transport_interrupted else cause
            )
            does_not_consume_budget = effective_cause in {
                "rate_limit", "transport", "service",
            }
            if does_not_consume_budget:
                transport_failures += 1
            else:
                counted_attempts += 1
                self._attempts[name] = counted_attempts
            if result.completed:
                try:
                    value = json.loads(_json_payload(assembled))
                    if not isinstance(value, dict):
                        raise ValueError("规划段 JSON 顶层必须是 object")
                except (json.JSONDecodeError, ValueError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, json.JSONDecodeError):
                        # 回灌必须自带出错文本：只给行列号模型无从自纠
                        # （6b 实跑取证：字符串内嵌未转义英文引号连拒三轮，
                        # 2026-08-21 r-d7857eb04e56）。
                        start = max(0, exc.pos - 60)
                        end = min(len(assembled), exc.pos + 60)
                        snippet = assembled[start:end]
                        last_error += (
                            f"；出错位置附近原文：…{snippet}…；"
                            "若字符串值内出现未转义的英文双引号，"
                            "请改用中文引号「」或转义"
                        )
                else:
                    formal.write_text(
                        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    partial.unlink(missing_ok=True)
                    return value
            else:
                last_error = result.error or "规划短流未收到完成信号"
            continuation = (
                assembled
                if effective_cause == "transport"
                else ""
            )
            current_prompt = (
                f"{prompt}\n\n上一轮本段失败原文：{last_error}。"
                + (
                    "请保持原结构契约并从已有前缀继续。"
                    if continuation
                    else "请保持原结构契约并重新输出完整 JSON。"
                )
            )
            if does_not_consume_budget:
                if transport_failures >= self.config.plan_transport_retries:
                    raise PlanSegmentError(
                        f"规划段 {name} 传输类失败连续 {transport_failures} 次："
                        f"{last_error}"
                    )
                await self._retry_sleep(
                    self.config.backoff_seconds(transport_failures - 1)
                )
            if counted_attempts < self.config.plan_segment_retries:
                if on_retry is not None:
                    callback_result = on_retry(counted_attempts + 1, last_error)
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
