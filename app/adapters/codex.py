"""Codex CLI 产品适配器：隔离环境、流式事件与双腿判定。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import signal
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping

from app.adapters import validation as artifact_validation
from app.adapters.capability import (
    CapabilityValidationError,
    CodexArgs,
    to_codex_args,
)
from app.adapters.claude import OwliResultError, parse_owli_result
from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
from app.adapters.events import ItemKind, NormalizedEvent, normalize_codex_event
from app.adapters.logging import (
    DEFAULT_LOG_ROOT,
    append_engine_error,
    append_outcome_event,
)
from app.adapters.ratelimit import route
from app.adapters.source_mcp import codex_mcp_args, source_event_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMMON_PROMPT_PATH = PROJECT_ROOT / "app" / "prompts" / "common" / "v1.md"
RESULT_SCHEMA_PATH = PROJECT_ROOT / "app" / "prompts" / "common" / "owli-result.schema.json"
DEFAULT_CODEX_HOME = Path("~/.owli/codex_home").expanduser()
_SANDBOXES = frozenset({"read-only", "workspace-write"})
_INFRASTRUCTURE_MARKERS = (
    "requires a newer version",
    "not supported when using codex",
    "authentication",
    "not logged in",
    "unauthorized",
    "invalid api key",
    "error sending request",
    "failed to connect",
    "service unavailable",
    "model_not_found",
    "unsupported model",
    "missing optional dependency",
)
_STREAM_LINE_LIMIT = 16 * 1024 * 1024

_NON_FATAL_WARNING_MARKERS = (
    "analytics", "telemetry", "opentelemetry", "analytics-events",
    "featured plugin ids cache", "featured plugin cache",
    "failed to load recommended plugins",
)
_RECOVERABLE_TRANSPORT_MARKERS = (
    "tls handshake eof",
    "stream disconnected",
    "failed to connect to websocket",
    "reconnecting",
)


class CodexAuthMode(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


CodexRunResult = EngineRunResult


@dataclass(frozen=True)
class CodexTask:
    """M1-b 旧调用兼容；新链路统一使用 EngineTask。"""

    body: str
    output_path: Path
    output_format: str
    research_id: str
    goal_id: str
    agent_id: str
    validators: list[str]
    tools: frozenset[str]
    model: str | None = None
    sandbox: str = "workspace-write"
    network: bool = False


TaskSpec = EngineTask | CodexTask


def resolve_codex_home(
    value: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    source = os.environ if environ is None else environ
    configured = value or source.get("OWLI_CODEX_HOME") or DEFAULT_CODEX_HOME
    path = Path(configured).expanduser().resolve(strict=False)
    if path == PROJECT_ROOT or PROJECT_ROOT in path.parents:
        raise ValueError("CODEX_HOME 必须位于 Owli 工作树之外")
    return path


def build_codex_env(
    auth_mode: CodexAuthMode | str,
    *,
    codex_home: str | Path | None = None,
    api_key: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    mode = CodexAuthMode(auth_mode)
    env = dict(os.environ if environ is None else environ)
    inherited_key = env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_HOME", None)
    env["CODEX_HOME"] = str(resolve_codex_home(codex_home, environ=env))
    env["NO_COLOR"] = "1"
    if mode is CodexAuthMode.API_KEY:
        selected_key = api_key or inherited_key
        if not selected_key:
            raise ValueError("API key 认证档未提供 OPENAI_API_KEY")
        env["OPENAI_API_KEY"] = selected_key
    return env


def compose_prompt(body: str) -> str:
    prefix = COMMON_PROMPT_PATH.read_text(encoding="utf-8").rstrip("\n")
    return f"{prefix}\n{body}"


def _workdir(task: TaskSpec) -> Path:
    return _resolve_output_path(task.output_path).parent


def _last_message_path(task: TaskSpec) -> Path:
    safe_agent = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in task.agent_id
    )
    return _workdir(task) / f".{safe_agent or 'agent'}-codex-last-message.json"


def _writable_directories(task: TaskSpec, roots: tuple[str, ...]) -> list[Path]:
    """把 capability 写 glob 收敛为 Codex 可挂载的目录根。"""

    directories: list[Path] = []
    research_root = (
        artifact_validation.RUNS_ROOT / task.research_id
    ).resolve(strict=False)
    goal_root = (research_root / "goals" / task.goal_id).resolve(strict=False)
    replacements = {
        "<current-goal>": task.goal_id,
    }
    for raw_root in roots:
        normalized = raw_root.replace("\\", "/")
        for placeholder, value in replacements.items():
            normalized = normalized.replace(placeholder, value)
        parts: list[str] = []
        for part in PurePosixPath(normalized).parts:
            if any(marker in part for marker in ("*", "?", "[", "<")):
                break
            parts.append(part)
        if not parts:
            continue
        directory = research_root.joinpath(*parts).resolve(strict=False)
        try:
            directory.relative_to(goal_root)
        except ValueError as exc:
            raise ValueError(
                f"Codex capability 写根必须位于当前 goal：{directory}"
            ) from exc
        if directory not in directories:
            directories.append(directory)
    return directories


def _require_capability_output(task: TaskSpec, translated: CodexArgs) -> None:
    """防止 -C 自带的可写权限绕过 capability 路径谓词。"""

    research_root = (
        artifact_validation.RUNS_ROOT / task.research_id
    ).resolve(strict=False)
    output_path = _resolve_output_path(task.output_path)
    try:
        relative = output_path.relative_to(research_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Codex 产物路径不在调研根目录：{output_path}") from exc
    if not translated.path_predicate(relative, "write"):
        raise ValueError(
            f"Codex 产物路径不在 capability.fs.write：{output_path}"
        )


def build_codex_command(
    task: TaskSpec,
    *,
    executable: str = "codex",
) -> list[str]:
    capability = getattr(task, "capability", None)
    translated: CodexArgs | None = (
        to_codex_args(capability) if capability is not None else None
    )
    if translated is not None:
        _require_capability_output(task, translated)
    sandbox = translated.sandbox if translated is not None else task.sandbox
    network = translated.network_enabled if translated is not None else task.network
    if sandbox not in _SANDBOXES:
        raise ValueError(f"不支持的 Codex 沙箱档位：{sandbox}")
    if network and sandbox != "workspace-write":
        raise ValueError("联网任务必须使用 workspace-write 沙箱档位")
    command = [
        executable,
        "exec",
        "--json",
        "-C",
        str(_workdir(task)),
        "-s",
        sandbox,
        "--skip-git-repo-check",
        "-o",
        str(_last_message_path(task)),
        "--output-schema",
        str(RESULT_SCHEMA_PATH),
    ]
    if task.model:
        command.extend(["-m", task.model])
    if translated is not None:
        for directory in _writable_directories(task, translated.writable_roots):
            command.extend(["--add-dir", str(directory)])
        if translated.injected_sources:
            command.extend(
                codex_mcp_args(
                    translated.injected_sources,
                    event_path=source_event_path(task),
                    research_id=task.research_id,
                    goal_id=task.goal_id,
                    agent_id=task.agent_id,
                )
            )
    if network:
        command.extend([
            "-c", "sandbox_workspace_write.network_access=true"
        ])
    command.append(compose_prompt(task.body))
    return command


def _resolve_output_path(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if value.is_absolute():
        return value.resolve(strict=False)
    return (PROJECT_ROOT / value).resolve(strict=False)


def _task_contract_failure(
    task: TaskSpec,
    ctx: artifact_validation.Ctx,
) -> artifact_validation.Result | None:
    actual_path = _resolve_output_path(task.output_path)
    ctx_path = _resolve_output_path(ctx.output_path)
    expected_root = (
        artifact_validation.RUNS_ROOT
        / task.research_id
        / "goals"
        / task.goal_id
    ).resolve(strict=False)
    differences: list[str] = []
    if actual_path != ctx_path:
        differences.append(f"output_path: task={actual_path}, ctx={ctx_path}")
    for name in ("research_id", "goal_id", "agent_id", "output_format"):
        task_value = getattr(task, name)
        ctx_value = getattr(ctx, name)
        if task_value != ctx_value:
            differences.append(f"{name}: task={task_value}, ctx={ctx_value}")
    try:
        relative_path = actual_path.relative_to(expected_root)
    except ValueError:
        differences.append(f"产物路径越界：{actual_path}")
    else:
        if relative_path == Path("."):
            differences.append("产物路径不能等于 goal 根目录")
    if not differences:
        return None
    message = "CodexTask 与校验上下文不一致，已在启动前拒绝"
    return artifact_validation.Result(
        artifact_validation.Verdict.FAIL,
        "codex_task_contract",
        message,
        differences,
        {"expected_root": str(expected_root)},
    )


def _infrastructure_error(events: list[NormalizedEvent]) -> str | None:
    """只按可识别的基础设施错误载荷分类，绝不按进程状态码分类。"""

    for index, event in enumerate(events):
        if not event.is_error and not isinstance(event.raw, str):
            continue
        text = event.text.lower()
        recovered = any(
            later.item_kind is ItemKind.DONE
            and (event.thread_id is None or later.thread_id == event.thread_id)
            for later in events[index + 1:]
        )
        if recovered and any(
            marker in text for marker in _RECOVERABLE_TRANSPORT_MARKERS
        ):
            continue
        if (
            not event.is_error
            and re.search(r"\bwarn(?:ing)?\b", text)
            and any(marker in text for marker in _NON_FATAL_WARNING_MARKERS)
        ):
            continue
        if any(marker in text for marker in _INFRASTRUCTURE_MARKERS):
            return event.text
    return None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_last_message(path: Path) -> OwliResult:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwliResultError(f"Codex 最终消息未落盘：{exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return parse_owli_result(text)
    wrapped = (
        "```json owli-result\n"
        f"{json.dumps(value, ensure_ascii=False)}\n"
        "```"
    )
    return parse_owli_result(wrapped)


def _unavailable_report(name: str, message: str, error: Exception) -> artifact_validation.ValidationReport:
    result = artifact_validation.Result(
        artifact_validation.Verdict.UNAVAILABLE,
        name,
        message,
        [],
        {"exception": type(error).__name__},
    )
    return artifact_validation.ValidationReport(
        artifact_validation.Verdict.UNAVAILABLE, [result]
    )


class CodexAdapter:
    """通过 codex exec --json 执行整任务；中断后只允许整任务重跑。"""

    def __init__(
        self,
        *,
        executable: str = "codex",
        codex_home: str | Path | None = None,
        auth_mode: CodexAuthMode | str = CodexAuthMode.SUBSCRIPTION,
        api_key: str | None = None,
        log_root: Path = DEFAULT_LOG_ROOT,
        timeout_seconds: float = 300.0,
        on_rate_limited: Any = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Codex 任务超时必须大于 0 秒")
        self._executable = executable
        self._codex_home = codex_home
        self._auth_mode = CodexAuthMode(auth_mode)
        self._api_key = api_key
        self._log_root = log_root
        self._timeout_seconds = timeout_seconds
        self._on_rate_limited = on_rate_limited
        self._process: asyncio.subprocess.Process | None = None
        self._processes: dict[object, asyncio.subprocess.Process] = {}
        self._interrupted_runs: set[object] = set()
        self._interrupted = False

    async def probe(self) -> bool:
        """以当前订阅认证发起 read-only 短请求，不用进程退出码判健康。"""

        command = [
            self._executable,
            "exec",
            "--json",
            "-C",
            str(PROJECT_ROOT),
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "这是 Owli 引擎恢复探测。不要调用工具，只输出 OWLI_HEALTHY。",
        ]
        process: asyncio.subprocess.Process | None = None
        try:
            env = build_codex_env(
                self._auth_mode,
                codex_home=self._codex_home,
                api_key=self._api_key,
            )
            Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=_STREAM_LINE_LIMIT,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            if process is not None:
                await self._terminate_process(process)
            return False
        except Exception:
            if process is not None:
                await self._terminate_process(process)
            return False

        healthy = False
        failed = False
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                raw: Any = json.loads(raw_line)
            except json.JSONDecodeError:
                raw = raw_line
            for event in normalize_codex_event(raw):
                failed = failed or event.is_error
                healthy = healthy or (
                    event.item_kind is ItemKind.OUTPUT
                    and event.text.strip() == "OWLI_HEALTHY"
                )
        return healthy and not failed

    async def _emit_outcome(
        self,
        events: list[NormalizedEvent],
        on_event: Any,
        *,
        outcome: str,
        message: str,
        detail: Any,
    ) -> None:
        last_event = events[-1] if events else None
        raw = {
            "type": "engine_run_outcome",
            "outcome": outcome,
            "message": message,
            "detail": detail,
        }
        event = NormalizedEvent(
            engine="Codex",
            thread_id=last_event.thread_id if last_event else None,
            turn_id=last_event.turn_id if last_event else None,
            item_kind=ItemKind.ERROR,
            text=message,
            is_error=True,
            raw=raw,
            outcome=outcome,
        )
        events.append(event)
        append_outcome_event(event, log_root=self._log_root)
        if on_event is not None:
            callback_result = on_event(event)
            if inspect.isawaitable(callback_result):
                await callback_result

    async def interrupt(self, *, run_token: object | None = None) -> None:
        process = (
            self._processes.get(run_token)
            if run_token is not None
            else self._process
        )
        if process is None:
            raise RuntimeError("当前没有运行中的 Codex 任务")
        token = run_token
        if token is None:
            token = next(
                (
                    candidate
                    for candidate, active in self._processes.items()
                    if active is process
                ),
                None,
            )
        if token is None:
            raise RuntimeError("Codex 运行中进程缺少 run_token")
        self._interrupted = True
        self._interrupted_runs.add(token)
        os.killpg(process.pid, signal.SIGINT)

    def _unavailable(
        self,
        error: Exception,
        events: list[NormalizedEvent],
    ) -> CodexRunResult:
        message = f"Codex CLI 不可用：{type(error).__name__}: {error}"
        return CodexRunResult(
            None,
            message,
            _unavailable_report("codex_cli", message, error),
            events,
            [],
            message,
        )

    async def _consume(
        self,
        stream: asyncio.StreamReader,
        events: list[NormalizedEvent],
        on_event: Any,
    ) -> None:
        thread_id: str | None = None
        turn_id: str | None = None
        turn_number = 0
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                raw: Any = json.loads(text)
            except json.JSONDecodeError:
                raw = text
            if isinstance(raw, dict) and raw.get("type") == "thread.started":
                thread_id = raw.get("thread_id") or thread_id
            if isinstance(raw, dict) and raw.get("type") == "turn.started":
                turn_number += 1
                turn_id = raw.get("turn_id") or f"{thread_id or 'codex'}:turn-{turn_number}"
            routing_events: list[NormalizedEvent] = []
            route(
                raw,
                engine="Codex",
                on_event=routing_events.append,
                on_rate_limited=self._on_rate_limited,
                log_root=self._log_root,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            for event in routing_events:
                events.append(event)
                if on_event is not None:
                    callback_result = on_event(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            for event in normalize_codex_event(
                raw, thread_id=thread_id, turn_id=turn_id
            ):
                events.append(event)
                if not routing_events:
                    append_engine_error(event, log_root=self._log_root)
                if on_event is not None:
                    callback_result = on_event(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result

    async def _consume_and_wait(
        self,
        process: asyncio.subprocess.Process,
        events: list[NormalizedEvent],
        on_event: Any,
    ) -> int | None:
        if process.stdout is None:
            raise RuntimeError("Codex 子进程未提供 JSONL 输出流")
        await self._consume(process.stdout, events, on_event)
        return await process.wait()

    async def _run_with_timeout(
        self,
        process: asyncio.subprocess.Process,
        events: list[NormalizedEvent],
        on_event: Any,
    ) -> int | None:
        consumer = asyncio.create_task(
            self._consume_and_wait(process, events, on_event)
        )
        done, _ = await asyncio.wait(
            {consumer}, timeout=self._timeout_seconds
        )
        if consumer in done:
            return consumer.result()
        await self._terminate_process(process)
        try:
            await asyncio.wait_for(consumer, timeout=5)
        except asyncio.TimeoutError:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        raise asyncio.TimeoutError

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        process_group_id = process.pid
        if process.returncode is None:
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        for _ in range(10):
            if not _process_group_exists(process_group_id):
                return
            await asyncio.sleep(0.02)
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        if process.returncode is None:
            await process.wait()
        for _ in range(25):
            if not _process_group_exists(process_group_id):
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("Codex 子进程组未能完整回收")

    def _timeout_result(
        self,
        task: TaskSpec,
        ctx: artifact_validation.Ctx,
        events: list[NormalizedEvent],
    ) -> CodexRunResult:
        message = f"Codex 任务超时（{self._timeout_seconds:g} 秒），已终止并要求整任务重跑"
        report = artifact_validation.validate(ctx, task.validators)
        timeout_failure = artifact_validation.Result(
            artifact_validation.Verdict.FAIL,
            "codex_timeout",
            message,
            [],
            {"timeout_seconds": self._timeout_seconds},
        )
        verdict = (
            artifact_validation.Verdict.UNAVAILABLE
            if report.verdict is artifact_validation.Verdict.UNAVAILABLE
            else artifact_validation.Verdict.FAIL
        )
        combined = artifact_validation.ValidationReport(
            verdict, [*report.results, timeout_failure]
        )
        return CodexRunResult(None, message, combined, events, [], None)

    def _interrupted_result(
        self,
        task: TaskSpec,
        ctx: artifact_validation.Ctx,
        events: list[NormalizedEvent],
    ) -> CodexRunResult:
        message = "Codex 任务已主动中断；不恢复进度，后续必须按 goal 整任务重跑"
        report = artifact_validation.validate(ctx, task.validators)
        interruption = artifact_validation.Result(
            artifact_validation.Verdict.FAIL,
            "codex_interrupted",
            message,
            [],
            None,
        )
        verdict = (
            artifact_validation.Verdict.UNAVAILABLE
            if report.verdict is artifact_validation.Verdict.UNAVAILABLE
            else artifact_validation.Verdict.FAIL
        )
        combined = artifact_validation.ValidationReport(
            verdict, [*report.results, interruption]
        )
        return CodexRunResult(None, message, combined, events, [], None)

    async def run(
        self,
        task: TaskSpec,
        ctx: artifact_validation.Ctx,
        on_event: Any = None,
        source_adapter: Any = None,
        run_token: object | None = None,
    ) -> CodexRunResult:
        del source_adapter
        events: list[NormalizedEvent] = []
        token = run_token if run_token is not None else object()
        process: asyncio.subprocess.Process | None = None
        contract_failure = _task_contract_failure(task, ctx)
        if contract_failure is not None:
            report = artifact_validation.ValidationReport(
                artifact_validation.Verdict.FAIL, [contract_failure]
            )
            result = CodexRunResult(
                None,
                contract_failure.message,
                report,
                events,
                [],
                None,
            )
            await self._emit_outcome(
                events,
                on_event,
                outcome="FAIL",
                message=contract_failure.message,
                detail=contract_failure.detail,
            )
            return result
        try:
            command = build_codex_command(task, executable=self._executable)
        except (CapabilityValidationError, ValueError) as exc:
            message = f"Codex capability 无法安全执行：{exc}"
            failure = artifact_validation.Result(
                artifact_validation.Verdict.FAIL,
                "codex_capability",
                message,
                [],
                {"exception": type(exc).__name__},
            )
            report = artifact_validation.ValidationReport(
                artifact_validation.Verdict.FAIL, [failure]
            )
            result = CodexRunResult(None, str(exc), report, events, [], None)
            await self._emit_outcome(
                events,
                on_event,
                outcome="FAIL",
                message=message,
                detail=failure.detail,
            )
            return result
        try:
            env = build_codex_env(
                self._auth_mode,
                codex_home=self._codex_home,
                api_key=self._api_key,
            )
            workdir = _workdir(task)
            codex_home = Path(env["CODEX_HOME"])
            workdir.mkdir(parents=True, exist_ok=True)
            codex_home.mkdir(parents=True, exist_ok=True)
            last_message = _last_message_path(task)
            if last_message.is_file():
                last_message.unlink()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workdir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                # 大产物会让 Codex 单行事件超过 asyncio 默认 64KB 行缓冲，
                # readline 抛 ValueError 被误判为引擎不可用（r-4878be30ff8c 实锤）。
                limit=_STREAM_LINE_LIMIT,
            )
            if token in self._processes:
                await self._terminate_process(process)
                raise RuntimeError("Codex run_token 正在使用")
            self._processes[token] = process
            self._process = process
            await self._run_with_timeout(
                process, events, on_event
            )
            if token in self._interrupted_runs:
                result = self._interrupted_result(task, ctx, events)
                await self._emit_outcome(
                    events,
                    on_event,
                    outcome="FAIL",
                    message=result.conclusion_error or "Codex 任务已中断",
                    detail={"interrupted": True},
                )
                return result
            infrastructure_error = _infrastructure_error(events)
            if infrastructure_error is not None:
                raise RuntimeError(infrastructure_error)
        except asyncio.TimeoutError:
            if process is not None:
                await self._terminate_process(process)
            result = self._timeout_result(task, ctx, events)
            await self._emit_outcome(
                events,
                on_event,
                outcome="FAIL",
                message=result.conclusion_error or "Codex 任务超时",
                detail={"timeout_seconds": self._timeout_seconds},
            )
            return result
        except Exception as exc:
            if process is not None:
                await self._terminate_process(process)
            result = self._unavailable(exc, events)
            await self._emit_outcome(
                events,
                on_event,
                outcome="UNAVAILABLE",
                message=result.engine_error or "Codex CLI 不可用",
                detail={"exception": type(exc).__name__, "message": str(exc)},
            )
            return result
        finally:
            if process is not None and self._processes.get(token) is process:
                self._processes.pop(token, None)
            self._interrupted_runs.discard(token)
            self._interrupted = bool(self._interrupted_runs)
            if self._process is process:
                self._process = next(reversed(self._processes.values()), None)

        conclusion: OwliResult | None = None
        conclusion_error: str | None = None
        path_failure: artifact_validation.Result | None = None
        try:
            conclusion = _parse_last_message(last_message)
            actual = _resolve_output_path(conclusion.output_path)
            expected = _resolve_output_path(task.output_path)
            if actual != expected:
                conclusion_error = (
                    "owli-result.output_path 与任务产物路径不一致："
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
        denials = conclusion.capability_denials if conclusion is not None else []
        result = CodexRunResult(
            conclusion,
            conclusion_error,
            report,
            events,
            denials,
        )
        if not result.succeeded:
            messages = [item.message for item in report.failures]
            if conclusion_error:
                messages.append(conclusion_error)
            message = "；".join(messages) or "产物与结构化结论未同时通过"
            await self._emit_outcome(
                events,
                on_event,
                outcome="FAIL",
                message=message,
                detail={
                    "validation_verdict": report.verdict.value,
                    "conclusion_error": conclusion_error,
                },
            )
        return result
