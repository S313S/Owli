"""Claude Agent SDK 适配器与 owli-result 结构化结论解析。"""

from __future__ import annotations

import json
import re
import inspect
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from app.adapters import validation as artifact_validation


_RESULT_BLOCK = re.compile(
    r"```json[ \t]+owli-result[ \t]*\r?\n(.*?)[ \t]*```",
    re.DOTALL,
)
_STATUSES = {"done", "partial", "blocked"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMMON_PROMPT_PATH = PROJECT_ROOT / "app" / "prompts" / "common" / "v1.md"
CLAUDE_TOOL_UNIVERSE = frozenset(
    {
        "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
        "Bash", "WebFetch", "WebSearch", "Task", "TodoWrite", "Skill",
    }
)


class OwliResultError(ValueError):
    """Claude 最终输出不含可解析的 owli-result 结论块。"""


@dataclass(frozen=True)
class OwliResult:
    status: str
    output_path: str
    summary: str
    assumptions: list[dict[str, str]]
    unmet: list[str]
    capability_denials: list[str]


@dataclass(frozen=True)
class ClaudeTask:
    body: str
    output_path: Path
    output_format: str
    research_id: str
    goal_id: str
    agent_id: str
    validators: list[str]
    tools: frozenset[str]
    model: str | None = None


@dataclass(frozen=True)
class ClaudeEvent:
    kind: str
    text: str
    raw: Any


@dataclass(frozen=True)
class ClaudeRunResult:
    conclusion: OwliResult | None
    conclusion_error: str | None
    validation: artifact_validation.ValidationReport
    events: list[ClaudeEvent]
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


def _load_sdk():
    try:
        return import_module("claude_agent_sdk")
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 claude-agent-sdk，Claude 引擎不可用") from exc


def compose_prompt(body: str) -> str:
    prefix = COMMON_PROMPT_PATH.read_text(encoding="utf-8").rstrip("\n")
    return f"{prefix}\n{body}"


def _goal_root(task: ClaudeTask) -> Path:
    return (
        artifact_validation.RUNS_ROOT
        / task.research_id
        / "goals"
        / task.goal_id
    ).resolve(strict=False)


def _tool_path(input_data: dict[str, Any]) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        value = input_data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_tool_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve(strict=False)
    return (PROJECT_ROOT / path).resolve(strict=False)


def make_permission_callback(task: ClaudeTask, denials: list[str], *, sdk=None):
    sdk = sdk or _load_sdk()
    write_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any):
        del context
        if tool_name not in task.tools:
            denials.append(tool_name)
            return sdk.PermissionResultDeny(message=f"工具不在 capability 白名单：{tool_name}")
        if tool_name in write_tools:
            raw_path = _tool_path(input_data)
            if raw_path is None:
                denials.append(f"{tool_name}:未提供路径")
                return sdk.PermissionResultDeny(message=f"{tool_name} 未提供可校验的写入路径")
            actual = _resolve_tool_path(raw_path)
            try:
                actual.relative_to(_goal_root(task))
            except ValueError:
                denials.append(str(actual))
                return sdk.PermissionResultDeny(message=f"写入路径越界：{actual}")
        return sdk.PermissionResultAllow()

    return can_use_tool


def build_claude_options(task: ClaudeTask, permission_callback, *, sdk=None):
    sdk = sdk or _load_sdk()
    unknown = sorted(task.tools - CLAUDE_TOOL_UNIVERSE)
    if unknown:
        raise ValueError(f"Claude 工具白名单包含未知工具：{','.join(unknown)}")
    values: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "setting_sources": [],
        "disallowed_tools": sorted(CLAUDE_TOOL_UNIVERSE - task.tools),
        "permission_mode": "dontAsk",
        "can_use_tool": permission_callback,
    }
    if task.model:
        values["model"] = task.model
    return sdk.ClaudeAgentOptions(**values)


async def _prompt_stream(prompt: str):
    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": "owli",
    }


def _message_event(message: Any, sdk: Any) -> ClaudeEvent:
    if isinstance(message, sdk.AssistantMessage):
        for block in message.content:
            if isinstance(block, sdk.ToolUseBlock):
                payload = json.dumps(getattr(block, "input", {}), ensure_ascii=False)
                return ClaudeEvent("tool_call", f"[{block.name}] {payload}", message)
            if isinstance(block, sdk.TextBlock) and block.text.strip():
                return ClaudeEvent("output", block.text, message)
        return ClaudeEvent("thinking", "[assistant]", message)
    if isinstance(message, sdk.UserMessage):
        return ClaudeEvent("tool_call", "[tool_result] 工具返回", message)
    if isinstance(message, sdk.ResultMessage):
        kind = "error" if getattr(message, "is_error", False) else "done"
        return ClaudeEvent(kind, str(getattr(message, "result", "") or ""), message)
    if isinstance(message, sdk.SystemMessage):
        subtype = getattr(message, "subtype", "")
        return ClaudeEvent("thinking", f"[session] {subtype}", message)
    return ClaudeEvent("thinking", f"[{type(message).__name__}]", message)


def _assistant_text(message: Any, sdk: Any) -> list[str]:
    texts = []
    if isinstance(message, sdk.AssistantMessage):
        texts.extend(
            block.text
            for block in message.content
            if isinstance(block, sdk.TextBlock) and block.text.strip()
        )
    if isinstance(message, sdk.ResultMessage):
        result = getattr(message, "result", "")
        if isinstance(result, str) and result.strip():
            texts.append(result)
    return texts


def _unavailable_run(
    error: Exception,
    events: list[ClaudeEvent],
    denials: list[str],
) -> ClaudeRunResult:
    message = f"Claude Agent SDK 不可用：{type(error).__name__}: {error}"
    result = artifact_validation.Result(
        artifact_validation.Verdict.UNAVAILABLE,
        "claude_sdk",
        message,
        [],
        {"exception": type(error).__name__},
    )
    report = artifact_validation.ValidationReport(
        artifact_validation.Verdict.UNAVAILABLE, [result]
    )
    return ClaudeRunResult(None, message, report, events, denials, message)


class ClaudeAdapter:
    """用 ClaudeSDKClient 流式执行一个 agent，并做双保险判定。"""

    def __init__(self, *, sdk=None):
        self._sdk = sdk
        self._client = None

    async def interrupt(self) -> None:
        if self._client is None:
            raise RuntimeError("当前没有运行中的 Claude 任务")
        await self._client.interrupt()

    async def run(
        self,
        task: ClaudeTask,
        ctx: artifact_validation.Ctx,
        on_event=None,
    ) -> ClaudeRunResult:
        sdk = self._sdk or _load_sdk()
        denials: list[str] = []
        events: list[ClaudeEvent] = []
        output_text: list[str] = []
        callback = make_permission_callback(task, denials, sdk=sdk)
        options = build_claude_options(task, callback, sdk=sdk)
        client = sdk.ClaudeSDKClient(options)
        self._client = client
        try:
            await client.connect(_prompt_stream(compose_prompt(task.body)))
            async for message in client.receive_response():
                event = _message_event(message, sdk)
                events.append(event)
                output_text.extend(_assistant_text(message, sdk))
                if on_event is not None:
                    callback_result = on_event(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result
        except Exception as exc:
            return _unavailable_run(exc, events, denials)
        finally:
            self._client = None
            try:
                await client.disconnect()
            except Exception:
                pass

        conclusion = None
        conclusion_error = None
        path_failure = None
        try:
            conclusion = parse_owli_result("\n".join(output_text))
            actual = _resolve_tool_path(conclusion.output_path)
            expected = _resolve_tool_path(str(task.output_path))
            if actual != expected:
                conclusion_error = (
                    f"owli-result.output_path 与任务产物路径不一致："
                    f"期望 {expected}，实际 {actual}"
                )
                path_failure = artifact_validation.Result(
                    artifact_validation.Verdict.FAIL,
                    "owli_result_output_path",
                    conclusion_error,
                    [str(actual)],
                    {"expected_path": str(expected), "actual_path": str(actual)},
                )
        except OwliResultError as exc:
            conclusion_error = str(exc)

        report = artifact_validation.validate(ctx, task.validators)
        if path_failure is not None:
            verdict = (
                artifact_validation.Verdict.UNAVAILABLE
                if report.verdict is artifact_validation.Verdict.UNAVAILABLE
                else artifact_validation.Verdict.FAIL
            )
            report = artifact_validation.ValidationReport(
                verdict, [*report.results, path_failure]
            )
        return ClaudeRunResult(
            conclusion,
            conclusion_error,
            report,
            events,
            denials,
        )


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OwliResultError(f"owli-result.{field} 必须是字符串数组")
    return value


def parse_owli_result(text: str) -> OwliResult:
    matches = _RESULT_BLOCK.findall(text)
    if not matches:
        raise OwliResultError("未找到 json owli-result 结构化结论块")
    try:
        value = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise OwliResultError(f"owli-result JSON 无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise OwliResultError("owli-result 顶层必须是对象")
    required = {
        "status", "output_path", "summary", "assumptions", "unmet",
        "capability_denials",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise OwliResultError(f"owli-result 缺少字段：{','.join(missing)}")
    if value["status"] not in _STATUSES:
        raise OwliResultError("owli-result.status 只能是 done、partial 或 blocked")
    if not isinstance(value["output_path"], str) or not value["output_path"]:
        raise OwliResultError("owli-result.output_path 必须是非空字符串")
    if not isinstance(value["summary"], str) or len(value["summary"]) > 200:
        raise OwliResultError("owli-result.summary 必须是 200 字以内字符串")
    assumptions = value["assumptions"]
    if not isinstance(assumptions, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("item"), str)
        and isinstance(item.get("reason"), str)
        for item in assumptions
    ):
        raise OwliResultError("owli-result.assumptions 必须含 item 与 reason")
    unmet = _string_list(value["unmet"], "unmet")
    denials = _string_list(value["capability_denials"], "capability_denials")
    if value["status"] in {"partial", "blocked"} and not unmet:
        raise OwliResultError("partial 或 blocked 状态必须填写 unmet")
    return OwliResult(
        value["status"], value["output_path"], value["summary"], assumptions,
        unmet, denials,
    )
