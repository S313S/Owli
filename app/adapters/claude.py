"""Claude Agent SDK 适配器与 owli-result 结构化结论解析。"""

from __future__ import annotations

import json
import re
import inspect
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping

from jsonschema import SchemaError, ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

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
OWLI_RESULT_SCHEMA_PATH = (
    PROJECT_ROOT / "app" / "prompts" / "common" / "owli-result.schema.json"
)
CLAUDE_TOOL_UNIVERSE = frozenset(
    {
        "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
        "Bash", "WebFetch", "WebSearch", "Task", "TodoWrite", "Skill",
    }
)
CLAUDE_PROTOCOL_TOOLS = frozenset({"StructuredOutput"})


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


def _owli_result_schema() -> dict[str, Any]:
    return json.loads(OWLI_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _schema_for_claude_cli(schema: Mapping[str, Any]) -> dict[str, Any]:
    """复制 schema 并剥离 Claude CLI 不接受的顶层元数据。"""

    unsupported_meta_keys = {"$schema", "$id", "$defs"}
    return {
        key: value for key, value in schema.items()
        if key not in unsupported_meta_keys
    }


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
        if tool_name in CLAUDE_PROTOCOL_TOOLS:
            return sdk.PermissionResultAllow()
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
        "output_format": {
            "type": "json_schema",
            "schema": _schema_for_claude_cli(_owli_result_schema()),
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


def _planning_json_payload(text: str) -> str:
    """只按 JSON 结构剥离围栏或前后说明，不读取业务措辞。"""

    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        stripped = stripped[first_newline + 1:] if first_newline >= 0 else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            stripped = stripped[start:end + 1]
    return stripped


def _merge_planning_continuation(prefix: str, suffix: str) -> str:
    """按字符级最长首尾重叠还原已收文本，供产物判定使用。"""

    if not prefix:
        return suffix
    if not suffix:
        return prefix
    for size in range(min(len(prefix), len(suffix)), 0, -1):
        if prefix[-size:] == suffix[:size]:
            return prefix + suffix[size:]
    return prefix + suffix


def _planning_product(
    candidates: list[Any], output_schema: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """按候选产物顺序解析 object，并在有 schema 时做协议校验。"""

    errors: list[str] = []
    for candidate in candidates:
        try:
            if isinstance(candidate, Mapping):
                value = dict(candidate)
            elif isinstance(candidate, str) and candidate.strip():
                value = json.loads(_planning_json_payload(candidate))
            else:
                raise ValueError("规划段未收到 JSON object 候选")
            if not isinstance(value, dict):
                raise ValueError("规划段 JSON 顶层必须是 object")
            if output_schema is not None:
                validate_json_schema(instance=value, schema=dict(output_schema))
            return value, None
        except (
            json.JSONDecodeError,
            ValueError,
            JsonSchemaValidationError,
            SchemaError,
        ) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return None, "；".join(errors) or "规划段产物不可用"


def _planning_error_event(request: PlanningSegmentRequest, message: Any) -> NormalizedEvent:
    return NormalizedEvent(
        engine="Claude",
        thread_id=request.research_id,
        turn_id=request.segment_name,
        item_kind=ItemKind.ERROR,
        text=str(message),
        is_error=True,
        raw=message,
        cause="planning_segment",
    )


def _engine_error_from_events(events: list[NormalizedEvent]) -> str | None:
    """汇总正常收尾消息携带的错误原文，不依赖 SDK 是否抛异常。"""

    samples: list[str] = []
    for event in events:
        if not event.is_error:
            continue
        raw = event.raw

        def field(name: str, default: Any = None) -> Any:
            if isinstance(raw, dict):
                return raw.get(name, default)
            return getattr(raw, name, default)

        api_error_status = field("api_error_status")
        subtype = field("subtype")
        raw_result = field("result")
        raw_errors = field("errors")
        if not (
            raw_result
            or subtype
            or api_error_status is not None
            or raw_errors
        ):
            continue
        sample = json.dumps(
            {
                "is_error": bool(field("is_error", event.is_error)),
                "api_error_status": api_error_status,
                "subtype": subtype,
                "result": raw_result or event.text,
                "errors": raw_errors,
            },
            ensure_ascii=False,
            default=str,
        )
        if sample not in samples:
            samples.append(sample)
    return "\n".join(samples) or None


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

    async def generate_plan_segment(
        self,
        request: PlanningSegmentRequest,
        *,
        on_text: Any = None,
    ) -> PlanningSegmentResult:
        """通过 Agent SDK 无工具短流生成单段；续写仍使用 user 请求。"""

        try:
            sdk = self._sdk or _load_sdk()
            option_values: dict[str, Any] = {
                "cwd": str(PROJECT_ROOT),
                "setting_sources": [],
                "tools": [],
                "allowed_tools": [],
                "disallowed_tools": sorted(CLAUDE_TOOL_UNIVERSE),
                "permission_mode": "dontAsk",
                "include_partial_messages": True,
            }
            if request.output_schema is not None:
                option_values["output_format"] = {
                    "type": "json_schema",
                    "schema": request.output_schema,
                }
            options = sdk.ClaudeAgentOptions(
                **option_values,
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
        error_text: str | None = None
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
                    plain_text = "".join(chunks)
                    structured = getattr(message, "structured_output", None)
                    if structured is not None:
                        text = json.dumps(structured, ensure_ascii=False)
                        if on_text is not None and not saw_stream_delta:
                            callback_result = on_text(text)
                            if inspect.isawaitable(callback_result):
                                await callback_result
                    api_status = getattr(message, "api_error_status", None)
                    is_error = bool(getattr(message, "is_error", False))
                    if is_error or api_status is not None:
                        append_engine_error(
                            _planning_error_event(request, message),
                            log_root=self._log_root,
                        )
                    merged_text = _merge_planning_continuation(
                        request.continuation, plain_text
                    )
                    product, product_error = _planning_product(
                        [structured, merged_text, plain_text],
                        request.output_schema,
                    )
                    if product is not None:
                        completed = True
                        failed = False
                        cause = None
                        error_text = None
                        if structured is not None:
                            chunks[:] = [json.dumps(product, ensure_ascii=False)]
                        continue

                    failed = True
                    message_cause = str(getattr(message, "cause", "") or "").casefold()
                    subtype = getattr(message, "subtype", None)
                    stop_reason = getattr(message, "stop_reason", None)
                    normal_stop_sequence = (
                        subtype == "success"
                        and stop_reason == "stop_sequence"
                        and api_status is None
                    )
                    if api_status == 429:
                        cause = "rate_limit"
                    elif api_status in {500, 529}:
                        cause = "service"
                    elif normal_stop_sequence:
                        cause = "stop_sequence"
                    elif message_cause:
                        cause = message_cause
                    elif is_error:
                        message_text = str(message)
                        cause = (
                            "transport"
                            if classify_transport_error(message_text)
                            else "engine_error"
                        )
                    else:
                        cause = "artifact_invalid"
                    error_text = (
                        f"规划段产物不可用：{product_error}; is_error={is_error}; "
                        f"api_error_status={api_status}; subtype={subtype}; "
                        f"stop_reason={stop_reason}"
                    )
                    completed = False
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
            completed and bool(chunks or request.continuation),
            error=error_text if failed else None,
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
        structured_output: Any = None
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
                if isinstance(message, sdk.ResultMessage):
                    candidate = getattr(message, "structured_output", None)
                    if candidate is not None:
                        structured_output = candidate
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
            conclusion = (
                _parse_owli_result_value(structured_output)
                if structured_output is not None
                else parse_owli_result("\n".join(output_text))
            )
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

        if conclusion is not None:
            from dataclasses import replace

            ctx = replace(ctx, missing_reason=conclusion.reason)
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
            _engine_error_from_events(events),
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
    return _parse_owli_result_value(value)


def _parse_owli_result_value(value: Any) -> OwliResult:
    if not isinstance(value, dict):
        raise OwliResultError("owli-result 顶层必须是对象")
    required = {
        "status", "output_path", "summary", "assumptions", "unmet",
        "capability_denials", "reason",
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
    reason = value["reason"]
    allowed_reasons = {
        None, "empty_result", "tool_unavailable", "quota_exhausted",
        "retry_exhausted",
    }
    if reason not in allowed_reasons:
        raise OwliResultError("owli-result.reason 不在缺失原因闭集")
    if value["status"] == "done" and reason is not None:
        raise OwliResultError("done 状态的 owli-result.reason 必须为 null")
    if reason is not None and value["status"] not in {"partial", "blocked"}:
        raise OwliResultError("非空 owli-result.reason 只允许 partial 或 blocked")
    if value["status"] in {"partial", "blocked"} and not unmet:
        raise OwliResultError("partial 或 blocked 状态必须填写 unmet")
    return OwliResult(
        value["status"], value["output_path"], value["summary"], assumptions,
        unmet, denials, reason,
    )
