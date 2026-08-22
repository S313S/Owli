"""Claude Agent SDK 适配器与 owli-result 结构化结论解析。"""

from __future__ import annotations

import json
import re
import inspect
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from app.adapters.capability import (
    CapabilityValidationError,
    ClaudeOptions,
    to_claude_options,
)
from app.adapters.events import ItemKind, NormalizedEvent, normalize_claude_event
from app.adapters.logging import DEFAULT_LOG_ROOT, append_engine_error
from app.adapters.ratelimit import classify_transport_error, route
from app.adapters import validation as artifact_validation
from app.adapters.contracts import (
    EngineRunResult,
    EngineTask,
    OwliResult,
    PlanningSegmentRequest,
    PlanningSegmentResult,
)
from app.adapters.source_mcp import (
    exposed_tool_name,
    source_event_path,
    stdio_server_config,
)


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


ClaudeRunResult = EngineRunResult


@dataclass(frozen=True)
class ClaudeTask:
    """M0-c 旧调用兼容；新链路统一使用 EngineTask。"""

    body: str
    output_path: Path
    output_format: str
    research_id: str
    goal_id: str
    agent_id: str
    validators: list[str]
    tools: frozenset[str]
    model: str | None = None


TaskSpec = EngineTask | ClaudeTask


def _load_sdk():
    try:
        return import_module("claude_agent_sdk")
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 claude-agent-sdk，Claude 引擎不可用") from exc


def compose_prompt(body: str) -> str:
    prefix = COMMON_PROMPT_PATH.read_text(encoding="utf-8").rstrip("\n")
    return f"{prefix}\n{body}"


def _goal_root(task: TaskSpec) -> Path:
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


def _capability_path(task: TaskSpec, raw_path: str) -> str | None:
    actual = _resolve_tool_path(raw_path)
    research_root = (
        artifact_validation.RUNS_ROOT / task.research_id
    ).resolve(strict=False)
    try:
        return actual.relative_to(research_root).as_posix()
    except ValueError:
        return None


def _claude_capability(task: TaskSpec) -> ClaudeOptions | None:
    capability = getattr(task, "capability", None)
    if capability is None:
        return None
    return to_claude_options(capability)


def make_permission_callback(task: TaskSpec, denials: list[str], *, sdk=None):
    sdk = sdk or _load_sdk()
    write_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    read_tools = {"Read", "Glob", "Grep"}
    translated = _claude_capability(task)
    source_tools = {
        exposed_tool_name(source_id)
        for source_id in (translated.registered_sources if translated is not None else ())
    }
    allowed_tools = (
        (CLAUDE_TOOL_UNIVERSE - set(translated.disallowed_tools)) | source_tools
        if translated is not None
        else task.tools
    )

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any):
        del context
        if tool_name not in allowed_tools:
            denials.append(tool_name)
            return sdk.PermissionResultDeny(message=f"工具不在 capability 白名单：{tool_name}")
        access = "write" if tool_name in write_tools else "read"
        if tool_name in write_tools | read_tools:
            raw_path = _tool_path(input_data)
            if raw_path is None:
                denials.append(f"{tool_name}:未提供路径")
                return sdk.PermissionResultDeny(message=f"{tool_name} 未提供可校验的写入路径")
            actual = _resolve_tool_path(raw_path)
            if translated is not None:
                relative = _capability_path(task, raw_path)
                allowed = relative is not None and translated.path_predicate(
                    relative, access
                )
                if not allowed:
                    denials.append(str(actual))
                    return sdk.PermissionResultDeny(
                        message=f"路径不在 capability 路径范围：{actual}"
                    )
        if tool_name in write_tools:
            try:
                actual.relative_to(_goal_root(task))
            except ValueError:
                denials.append(str(actual))
                return sdk.PermissionResultDeny(message=f"写入路径越界：{actual}")
        return sdk.PermissionResultAllow()

    return can_use_tool


def make_pre_tool_hook(permission_callback, *, sdk=None):
    """即使工具已预批准，也在执行前强制复核 capability 与写入路径。"""
    sdk = sdk or _load_sdk()

    async def enforce(input_data: dict[str, Any], tool_use_id: str | None, context: Any):
        del tool_use_id
        decision = await permission_callback(
            input_data.get("tool_name", ""),
            input_data.get("tool_input", {}),
            context,
        )
        denied = isinstance(decision, sdk.PermissionResultDeny)
        reason = getattr(decision, "message", "") or "工具调用被 capability 边界拒绝"
        hook_output = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if denied else "allow",
        }
        if denied:
            hook_output["permissionDecisionReason"] = reason
        return {
            "hookSpecificOutput": hook_output,
        }

    return sdk.HookMatcher(matcher=None, hooks=[enforce])


def build_claude_options(task: TaskSpec, permission_callback, *, sdk=None):
    sdk = sdk or _load_sdk()
    translated = _claude_capability(task)
    if translated is None:
        unknown = sorted(task.tools - CLAUDE_TOOL_UNIVERSE)
        if unknown:
            raise ValueError(f"Claude 工具白名单包含未知工具：{','.join(unknown)}")
        declared_tools = sorted(task.tools)
        disallowed_tools = sorted(CLAUDE_TOOL_UNIVERSE - task.tools)
        setting_sources: list[str] = []
        permission_mode = "dontAsk"
    else:
        disallowed_tools = list(translated.disallowed_tools)
        declared_tools = sorted(
            (CLAUDE_TOOL_UNIVERSE - set(disallowed_tools))
            | {exposed_tool_name(item) for item in translated.registered_sources}
        )
        setting_sources = list(translated.setting_sources)
        permission_mode = translated.permission_mode
    values: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "setting_sources": setting_sources,
        "tools": declared_tools,
        "allowed_tools": declared_tools,
        "disallowed_tools": disallowed_tools,
        "permission_mode": permission_mode,
        "can_use_tool": permission_callback,
        "hooks": {
            "PreToolUse": [make_pre_tool_hook(permission_callback, sdk=sdk)],
        },
    }
    if translated is not None and translated.registered_sources:
        values["mcp_servers"] = {
            "owli_sources": stdio_server_config(
                translated.registered_sources,
                event_path=source_event_path(task),
                research_id=task.research_id,
                goal_id=task.goal_id,
                agent_id=task.agent_id,
                item_limit=getattr(task, "source_item_limit", None),
            )
        }
        values["strict_mcp_config"] = True
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
    events: list[NormalizedEvent],
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

    def __init__(
        self,
        *,
        sdk=None,
        log_root: Path = DEFAULT_LOG_ROOT,
        on_rate_limited=None,
    ):
        self._sdk = sdk
        self._log_root = log_root
        self._on_rate_limited = on_rate_limited
        self._client = None
        self._clients: dict[object, Any] = {}

    async def interrupt(self, *, run_token: object | None = None) -> None:
        client = (
            self._clients.get(run_token)
            if run_token is not None
            else self._client
        )
        if client is None:
            raise RuntimeError("当前没有运行中的 Claude 任务")
        await client.interrupt()

    async def probe(self) -> bool:
        """发起不带工具的真实短请求；只认模型返回的结构化健康标记。"""

        try:
            sdk = self._sdk or _load_sdk()
            options = sdk.ClaudeAgentOptions(
                cwd=str(PROJECT_ROOT),
                setting_sources=[],
                tools=[],
                allowed_tools=[],
                disallowed_tools=sorted(CLAUDE_TOOL_UNIVERSE),
                permission_mode="dontAsk",
            )
            client = sdk.ClaudeSDKClient(options)
        except Exception:
            return False
        healthy = False
        failed = False
        try:
            await client.connect(_prompt_stream(
                "这是 Owli 引擎恢复探测。不要调用工具，只输出 OWLI_HEALTHY。"
            ))
            async for message in client.receive_response():
                if (
                    bool(getattr(message, "is_error", False))
                    or getattr(message, "api_error_status", None) is not None
                ):
                    failed = True
                if any(
                    text.strip() == "OWLI_HEALTHY"
                    for text in _assistant_text(message, sdk)
                ):
                    healthy = True
        except Exception:
            return False
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        return healthy and not failed

    async def generate_plan_segment(
        self,
        request: PlanningSegmentRequest,
        *,
        on_text: Any = None,
    ) -> PlanningSegmentResult:
        """通过 Agent SDK 无工具短流生成单段；续写仍使用 user 请求。"""

        try:
            sdk = self._sdk or _load_sdk()
            options = sdk.ClaudeAgentOptions(
                cwd=str(PROJECT_ROOT),
                setting_sources=[],
                tools=[],
                allowed_tools=[],
                disallowed_tools=sorted(CLAUDE_TOOL_UNIVERSE),
                permission_mode="dontAsk",
                include_partial_messages=True,
            )
            client = sdk.ClaudeSDKClient(options)
        except Exception as exc:
            return PlanningSegmentResult("", False, error=str(exc))

        prompt = request.prompt
        if request.continuation:
            prompt = (
                f"{request.prompt}\n\n"
                "上次响应在传输中断前已收到以下精确前缀：\n"
                f"{request.continuation}\n"
                "请从断点继续，只输出尚未收到的 JSON 后缀；不要重写说明文字。"
            )
        chunks: list[str] = []
        saw_stream_delta = False
        completed = False
        failed = False
        cause: str | None = None
        try:
            await client.connect(_prompt_stream(prompt))
            async for message in client.receive_response():
                stream_type = getattr(sdk, "StreamEvent", ())
                if stream_type and isinstance(message, stream_type):
                    event = getattr(message, "event", None)
                    delta = event.get("delta", {}) if isinstance(event, dict) else {}
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "content_block_delta"
                        and isinstance(delta, dict)
                        and delta.get("type") == "text_delta"
                        and delta.get("text")
                    ):
                        text = str(delta["text"])
                        saw_stream_delta = True
                        chunks.append(text)
                        if on_text is not None:
                            callback_result = on_text(text)
                            if inspect.isawaitable(callback_result):
                                await callback_result
                elif isinstance(message, sdk.AssistantMessage) and not saw_stream_delta:
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock) and block.text:
                            chunks.append(block.text)
                            if on_text is not None:
                                callback_result = on_text(block.text)
                                if inspect.isawaitable(callback_result):
                                    await callback_result
                if isinstance(message, sdk.ResultMessage):
                    api_status = getattr(message, "api_error_status", None)
                    if api_status == 429:
                        cause = "rate_limit"
                    elif api_status in {500, 529}:
                        cause = "service"
                    elif bool(getattr(message, "is_error", False)):
                        message_text = str(message)
                        cause = (
                            "transport"
                            if classify_transport_error(message_text)
                            else "engine_error"
                        )
                    failed = (
                        bool(getattr(message, "is_error", False))
                        or api_status is not None
                    )
                    completed = not failed
        except Exception as exc:
            message = str(exc)
            interrupted = classify_transport_error(message)
            return PlanningSegmentResult(
                "".join(chunks),
                False,
                transport_interrupted=interrupted,
                error=message,
                cause="transport" if interrupted else "engine_error",
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        return PlanningSegmentResult(
            "".join(chunks),
            completed and bool(chunks),
            error="规划短流返回错误" if failed else None,
            cause=cause,
        )

    async def run(
        self,
        task: TaskSpec,
        ctx: artifact_validation.Ctx,
        on_event=None,
        source_adapter=None,
        run_token: object | None = None,
    ) -> ClaudeRunResult:
        del source_adapter
        denials: list[str] = []
        events: list[NormalizedEvent] = []
        output_text: list[str] = []
        try:
            sdk = self._sdk or _load_sdk()
        except Exception as exc:
            return _unavailable_run(exc, events, denials)
        try:
            callback = make_permission_callback(task, denials, sdk=sdk)
            options = build_claude_options(task, callback, sdk=sdk)
        except (CapabilityValidationError, ValueError) as exc:
            failure = artifact_validation.Result(
                artifact_validation.Verdict.FAIL,
                "claude_capability",
                f"Claude capability 无法安全执行：{exc}",
                [],
                {"exception": type(exc).__name__},
            )
            report = artifact_validation.ValidationReport(
                artifact_validation.Verdict.FAIL, [failure]
            )
            return ClaudeRunResult(None, str(exc), report, events, denials)
        token = run_token if run_token is not None else object()
        if token in self._clients:
            return _unavailable_run(
                RuntimeError("Claude run_token 正在使用"), events, denials
            )
        try:
            client = sdk.ClaudeSDKClient(options)
        except Exception as exc:
            return _unavailable_run(exc, events, denials)
        self._clients[token] = client
        self._client = client
        fallback_thread_id = f"{task.research_id}:{task.goal_id}:{task.agent_id}"
        run_turn_id = f"{fallback_thread_id}:turn-1"
        try:
            await client.connect(_prompt_stream(compose_prompt(task.body)))
            async for message in client.receive_response():
                output_text.extend(_assistant_text(message, sdk))
                routing_events: list[NormalizedEvent] = []
                route(
                    message,
                    engine="Claude",
                    on_event=routing_events.append,
                    on_rate_limited=self._on_rate_limited,
                    log_root=self._log_root,
                )
                for event in routing_events:
                    events.append(event)
                    if on_event is not None:
                        callback_result = on_event(event)
                        if inspect.isawaitable(callback_result):
                            await callback_result
                normalized = normalize_claude_event(
                    message,
                    sdk=sdk,
                    thread_id=fallback_thread_id,
                    turn_id=run_turn_id,
                )
                for event in normalized:
                    events.append(event)
                    if not routing_events:
                        append_engine_error(event, log_root=self._log_root)
                    if on_event is not None:
                        callback_result = on_event(event)
                        if inspect.isawaitable(callback_result):
                            await callback_result
        except Exception as exc:
            message = str(exc)
            if classify_transport_error(message):
                event = NormalizedEvent(
                    engine="Claude",
                    thread_id=fallback_thread_id,
                    turn_id=run_turn_id,
                    item_kind=ItemKind.ERROR,
                    text=message,
                    is_error=True,
                    raw={
                        "exception": type(exc).__name__,
                        "message": message,
                    },
                    route_state="BACKOFF",
                    suspend_new_tasks=True,
                    cause="transport",
                )
                events.append(event)
                append_engine_error(event, log_root=self._log_root)
                if on_event is not None:
                    callback_result = on_event(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            return _unavailable_run(exc, events, denials)
        finally:
            self._clients.pop(token, None)
            if self._client is client:
                self._client = next(reversed(self._clients.values()), None)
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
