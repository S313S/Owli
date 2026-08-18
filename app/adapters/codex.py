"""Codex CLI 产品适配器：隔离环境、流式事件与双腿判定。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from app.adapters import validation as artifact_validation
from app.adapters.claude import OwliResult, OwliResultError, parse_owli_result
from app.adapters.events import NormalizedEvent, normalize_codex_event
from app.adapters.logging import DEFAULT_LOG_ROOT, append_engine_error


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
)


class CodexAuthMode(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


@dataclass(frozen=True)
class CodexTask:
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


@dataclass(frozen=True)
class CodexRunResult:
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


def _workdir(task: CodexTask) -> Path:
    return _resolve_output_path(task.output_path).parent


def _last_message_path(task: CodexTask) -> Path:
    safe_agent = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in task.agent_id
    )
    return _workdir(task) / f".{safe_agent or 'agent'}-codex-last-message.json"


def build_codex_command(
    task: CodexTask,
    *,
    executable: str = "codex",
) -> list[str]:
    if task.sandbox not in _SANDBOXES:
        raise ValueError(f"不支持的 Codex 沙箱档位：{task.sandbox}")
    if task.network and task.sandbox != "workspace-write":
        raise ValueError("联网任务必须使用 workspace-write 沙箱档位")
    command = [
        executable,
        "exec",
        "--json",
        "-C",
        str(_workdir(task)),
        "-s",
        task.sandbox,
        "--skip-git-repo-check",
        "-o",
        str(_last_message_path(task)),
        "--output-schema",
        str(RESULT_SCHEMA_PATH),
    ]
    if task.model:
        command.extend(["-m", task.model])
    if task.network:
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
    task: CodexTask,
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


def _infrastructure_error(
    events: list[NormalizedEvent],
    process_status: int | None,
) -> str | None:
    event_text = "\n".join(event.text for event in events if event.text)
    if process_status:
        return event_text or f"Codex CLI 进程异常结束：status={process_status}"
    for event in events:
        if not event.is_error:
            continue
        text = event.text.lower()
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
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Codex 任务超时必须大于 0 秒")
        self._executable = executable
        self._codex_home = codex_home
        self._auth_mode = CodexAuthMode(auth_mode)
        self._api_key = api_key
        self._log_root = log_root
        self._timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._interrupted = False

    async def interrupt(self) -> None:
        if self._process is None:
            raise RuntimeError("当前没有运行中的 Codex 任务")
        self._interrupted = True
        os.killpg(self._process.pid, signal.SIGINT)

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
            for event in normalize_codex_event(
                raw, thread_id=thread_id, turn_id=turn_id
            ):
                events.append(event)
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
        task: CodexTask,
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
        task: CodexTask,
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
        task: CodexTask,
        ctx: artifact_validation.Ctx,
        on_event: Any = None,
    ) -> CodexRunResult:
        events: list[NormalizedEvent] = []
        self._interrupted = False
        contract_failure = _task_contract_failure(task, ctx)
        if contract_failure is not None:
            report = artifact_validation.ValidationReport(
                artifact_validation.Verdict.FAIL, [contract_failure]
            )
            return CodexRunResult(
                None,
                contract_failure.message,
                report,
                events,
                [],
                None,
            )
        try:
            command = build_codex_command(task, executable=self._executable)
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
            )
            self._process = process
            process_status = await self._run_with_timeout(
                process, events, on_event
            )
            if self._interrupted:
                return self._interrupted_result(task, ctx, events)
            infrastructure_error = _infrastructure_error(
                events, process_status
            )
            if infrastructure_error is not None:
                raise RuntimeError(infrastructure_error)
        except asyncio.TimeoutError:
            if self._process is not None:
                await self._terminate_process(self._process)
            return self._timeout_result(task, ctx, events)
        except Exception as exc:
            if self._process is not None:
                await self._terminate_process(self._process)
            return self._unavailable(exc, events)
        finally:
            self._process = None

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
        return CodexRunResult(
            conclusion,
            conclusion_error,
            report,
            events,
            denials,
        )
