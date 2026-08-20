import asyncio
import json
import os
import signal
import stat
import sys
import time
from dataclasses import fields
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_非结构化_WARN_遥测失败不覆盖已完成任务() -> None:
    from app.adapters.codex import _infrastructure_error
    from app.adapters.events import ItemKind, NormalizedEvent

    warning = NormalizedEvent(
        engine="Codex", thread_id="thread-1", turn_id="turn-1",
        item_kind=ItemKind.THINKING,
        text="WARN analytics: error sending request for url (https://example.invalid/events)",
        is_error=False,
        raw="WARN analytics: error sending request for url (https://example.invalid/events)",
    )
    assert _infrastructure_error([warning]) is None

    authentication = NormalizedEvent(
        engine="Codex", thread_id="thread-1", turn_id="turn-1",
        item_kind=ItemKind.THINKING,
        text="WARN authentication failed: not logged in",
        is_error=False,
        raw="WARN authentication failed: not logged in",
    )
    assert _infrastructure_error([authentication]) == authentication.text


def test_推荐插件目录_WARN_不覆盖已落盘的成功产物() -> None:
    from app.adapters.codex import _infrastructure_error
    from app.adapters.events import ItemKind, NormalizedEvent

    text = (
        "WARN codex_core_plugins::manager: failed to load recommended plugins "
        "error=failed to send remote plugin catalog request to "
        "https://chatgpt.com/backend-api/ps/plugins/suggested?scope=GLOBAL: "
        "error sending request for url"
    )
    warning = NormalizedEvent(
        engine="Codex",
        thread_id="thread-real-regression",
        turn_id="turn-1",
        item_kind=ItemKind.THINKING,
        text=text,
        is_error=False,
        raw=text,
    )

    assert _infrastructure_error([warning]) is None


def test_传输错误后有turn_completed则保留错误但不覆盖最终成功() -> None:
    from app.adapters.codex import _infrastructure_error
    from app.adapters.events import ItemKind, NormalizedEvent

    error_text = (
        "ERROR responses_websocket: failed to connect to websocket: "
        "IO error: tls handshake eof"
    )
    transport_error = NormalizedEvent(
        "Codex", "thread-1", "turn-1", ItemKind.THINKING,
        error_text, False, error_text,
    )
    completed = NormalizedEvent(
        "Codex", "thread-1", "turn-1", ItemKind.DONE,
        "", False, {"type": "turn.completed"},
    )

    assert _infrastructure_error([transport_error]) == error_text
    assert _infrastructure_error([transport_error, completed]) is None


def _write_fake_codex(path: Path, *, create_artifact: bool, error_event: bool = False) -> None:
    source = f'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
prompt = args[-1]
output_path = pathlib.Path(prompt.split("ARTIFACT=", 1)[1].splitlines()[0])
capture_path = pathlib.Path(prompt.split("CAPTURE=", 1)[1].splitlines()[0])
last_message = pathlib.Path(args[args.index("-o") + 1])
capture_path.write_text(json.dumps({{"args": args, "env": dict(os.environ)}}), encoding="utf-8")
if {create_artifact!r}:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("Codex 产物", encoding="utf-8")
result = {{
    "status": "done",
    "output_path": str(output_path),
    "summary": "假子进程完成",
    "assumptions": [],
    "unmet": [],
    "capability_denials": [],
}}
last_message.parent.mkdir(parents=True, exist_ok=True)
last_message.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
events = [
    {{"type": "thread.started", "thread_id": "thread-fake"}},
    {{"type": "turn.started"}},
    {{"type": "item.completed", "item": {{"id": "item-1", "type": "agent_message", "text": "处理中"}}}},
    {{"type": "future.event", "message": "新版未知事件"}},
]
if {error_event!r}:
    events.append({{"type": "turn.failed", "error": {{"message": "patch rejected: writing is blocked by read-only sandbox"}}}})
else:
    events.append({{"type": "turn.completed", "usage": {{"input_tokens": 12, "output_tokens": 4}}}})
for event in events:
    print(json.dumps(event, ensure_ascii=False), flush=True)
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_infrastructure_failure(path: Path) -> None:
    path.write_text(
        '''#!/usr/bin/env python3
import json
import sys

print(json.dumps({"type": "thread.started", "thread_id": "thread-infra"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({
    "type": "turn.failed",
    "error": {"message": "The selected model requires a newer version of Codex"},
}), flush=True)
sys.exit(1)
''',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_hanging_codex(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_stderr_infrastructure_failure(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('Missing optional dependency @openai/codex-darwin-arm64')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_child_hanging_codex(path: Path) -> None:
    path.write_text(
        '''#!/usr/bin/env python3
import pathlib
import subprocess
import sys
import time

prompt = sys.argv[-1]
capture = pathlib.Path(prompt.split("CAPTURE=", 1)[1].splitlines()[0])
ready = capture.with_suffix(".ready")
child = subprocess.Popen([
    sys.executable,
    "-c",
    (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(30)"
    ),
])
for _ in range(100):
    if ready.exists():
        break
    time.sleep(0.01)
capture.write_text(str(child.pid), encoding="utf-8")
time.sleep(30)
''',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _ctx(validation, output_path: Path):
    return validation.Ctx(
        output_path=output_path,
        output_format="markdown",
        research_id="research-1",
        goal_id="goal-1",
        agent_id="agent-1",
        read_text=lambda: output_path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(output_path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
    )


def _task(codex, output_path: Path, capture_path: Path, **overrides):
    values = {
        "body": f"写入最小产物。\nARTIFACT={output_path}\nCAPTURE={capture_path}",
        "output_path": output_path,
        "output_format": "markdown",
        "research_id": "research-1",
        "goal_id": "goal-1",
        "agent_id": "agent-1",
        "validators": ["file_exists"],
        "tools": frozenset(),
        "sandbox": "workspace-write",
        "network": True,
    }
    values.update(overrides)
    return codex.CodexTask(**values)


def test_正常通路按双腿判定并与_claude_结论字段完全一致(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters.claude import OwliResult as ClaudeOwliResult
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    capture_path = tmp_path / "capture.json"
    executable = tmp_path / "fake-codex"
    _write_fake_codex(executable, create_artifact=True)
    codex_home = tmp_path / "runtime-home"
    adapter = codex.CodexAdapter(executable=str(executable), codex_home=codex_home)

    result = asyncio.run(adapter.run(_task(codex, output_path, capture_path), _ctx(validation, output_path)))

    assert result.succeeded is True
    assert result.validation.verdict is validation.Verdict.PASS
    assert result.conclusion is not None
    assert {field.name for field in fields(result.conclusion)} == {
        field.name for field in fields(ClaudeOwliResult)
    } == {
        "status", "output_path", "summary", "assumptions", "unmet", "capability_denials"
    }
    assert {event.thread_id for event in result.events} == {"thread-fake"}
    assert result.events[-2].item_kind.value == "thinking"
    assert result.events[-1].item_kind.value == "done"

    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    args = capture["args"]
    assert args[:2] == ["exec", "--json"]
    assert args[args.index("-C") + 1] == str(output_path.parent)
    assert args[args.index("-s") + 1] == "workspace-write"
    assert "--skip-git-repo-check" in args
    assert "--output-schema" in args
    assert args[args.index("-c") + 1] == "sandbox_workspace_write.network_access=true"
    assert args[-1].startswith(codex.COMMON_PROMPT_PATH.read_text(encoding="utf-8"))
    assert capture["env"]["CODEX_HOME"] == str(codex_home.resolve())


def test_退出码零但产物缺失仍为_fail(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "missing.md"
    executable = tmp_path / "fake-codex"
    _write_fake_codex(executable, create_artifact=False)

    result = asyncio.run(codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    ).run(_task(codex, output_path, tmp_path / "capture.json"), _ctx(validation, output_path)))

    assert result.succeeded is False
    assert result.engine_error is None
    assert result.validation.verdict is validation.Verdict.FAIL
    assert "不存在" in result.validation.failures[0].message


def test_越权文案样本判_fail_且错误事件原样落盘(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "denied.md"
    executable = tmp_path / "fake-codex"
    _write_fake_codex(executable, create_artifact=False, error_event=True)
    log_root = tmp_path / "engine-errors"

    result = asyncio.run(codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home", log_root=log_root
    ).run(_task(codex, output_path, tmp_path / "capture.json"), _ctx(validation, output_path)))

    assert result.succeeded is False
    assert result.validation.verdict is validation.Verdict.FAIL
    logged = next(log_root.glob("codex-*.jsonl"))
    raw = json.loads(logged.read_text(encoding="utf-8"))
    assert raw == {
        "type": "turn.failed",
        "error": {"message": "patch rejected: writing is blocked by read-only sandbox"},
    }


def test_codex_不存在时为_unavailable_且不烧任务重试(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    result = asyncio.run(codex.CodexAdapter(
        executable=str(tmp_path / "不存在-codex"), codex_home=tmp_path / "runtime-home"
    ).run(_task(codex, output_path, tmp_path / "capture.json"), _ctx(validation, output_path)))

    assert result.succeeded is False
    assert result.engine_error is not None
    assert result.validation.verdict is validation.Verdict.UNAVAILABLE


def test_子进程环境隔离全局_home_且认证两档可切(tmp_path, monkeypatch):
    from app.adapters.codex import CodexAuthMode, build_codex_env

    monkeypatch.setenv("CODEX_HOME", str(ROOT / ".codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "开发机不应被订阅档继承")
    home = tmp_path / "outside-runtime-home"

    subscription = build_codex_env(CodexAuthMode.SUBSCRIPTION, codex_home=home)
    api_key = build_codex_env(
        CodexAuthMode.API_KEY, codex_home=home, api_key="测试专用-key"
    )

    assert subscription["CODEX_HOME"] == str(home.resolve())
    assert "OPENAI_API_KEY" not in subscription
    assert api_key["CODEX_HOME"] == str(home.resolve())
    assert api_key["OPENAI_API_KEY"] == "测试专用-key"
    assert not Path(subscription["CODEX_HOME"]).is_relative_to(ROOT)


def test_只允许两个沙箱档位且只读任务不附加联网开关(tmp_path):
    from app.adapters import codex

    output_path = tmp_path / "runs" / "research-1" / "goals" / "goal-1" / "result.md"
    task = _task(
        codex, output_path, tmp_path / "capture.json", sandbox="read-only", network=False
    )
    command = codex.build_codex_command(task, executable="codex")

    assert command[command.index("-s") + 1] == "read-only"
    assert "sandbox_workspace_write.network_access=true" not in command

    invalid = _task(codex, output_path, tmp_path / "capture.json", sandbox="unsafe")
    try:
        codex.build_codex_command(invalid, executable="codex")
    except ValueError as error:
        assert "沙箱档位" in str(error)
    else:
        raise AssertionError("非法沙箱档位必须拒绝")


def test_越界路径和_task_ctx_失配均在启动子进程前拒绝(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    outside = tmp_path / "outside" / "result.md"
    executable = tmp_path / "fake-codex"
    capture = tmp_path / "capture.json"
    _write_fake_codex(executable, create_artifact=True)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )

    outside_result = asyncio.run(adapter.run(
        _task(codex, outside, capture), _ctx(validation, outside)
    ))
    legal = runs_root / "research-1" / "goals" / "goal-1" / "legal.md"
    other = legal.with_name("other.md")
    mismatch_result = asyncio.run(adapter.run(
        _task(codex, legal, capture), _ctx(validation, other)
    ))
    goal_root = runs_root / "research-1" / "goals" / "goal-1"
    root_result = asyncio.run(adapter.run(
        _task(codex, goal_root, capture), _ctx(validation, goal_root)
    ))

    assert outside_result.validation.verdict is validation.Verdict.FAIL
    assert mismatch_result.validation.verdict is validation.Verdict.FAIL
    assert root_result.validation.verdict is validation.Verdict.FAIL
    assert not outside.parent.exists()
    assert not capture.exists()


def test_已启动进程的版本不兼容错误归为_unavailable(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    executable = tmp_path / "fake-codex"
    _write_infrastructure_failure(executable)

    result = asyncio.run(codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    ).run(_task(codex, output_path, tmp_path / "capture.json"), _ctx(validation, output_path)))

    assert result.engine_error is not None
    assert result.validation.verdict is validation.Verdict.UNAVAILABLE
    assert "newer version" in " ".join(event.text for event in result.events)


def test_无人值守卡死由_timeout_终止并判任务_fail(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    executable = tmp_path / "fake-codex"
    _write_hanging_codex(executable)
    adapter = codex.CodexAdapter(
        executable=str(executable),
        codex_home=tmp_path / "runtime-home",
        timeout_seconds=0.05,
    )

    started_at = time.monotonic()
    result = asyncio.run(adapter.run(
        _task(codex, output_path, tmp_path / "capture.json"), _ctx(validation, output_path)
    ))
    elapsed = time.monotonic() - started_at

    assert result.engine_error is None
    assert result.validation.verdict is validation.Verdict.FAIL
    assert "超时" in (result.conclusion_error or "")
    assert elapsed < 1.0


def test_非零退出且纯_stderr_启动故障归为_unavailable(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    executable = tmp_path / "fake-codex"
    _write_stderr_infrastructure_failure(executable)

    result = asyncio.run(codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    ).run(_task(codex, output_path, tmp_path / "capture.json"), _ctx(validation, output_path)))

    assert result.engine_error is not None
    assert result.validation.verdict is validation.Verdict.UNAVAILABLE
    assert "Missing optional dependency" in " ".join(event.text for event in result.events)


def test_主动_interrupt_归任务_fail_且要求整任务重跑(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    executable = tmp_path / "fake-codex"
    _write_hanging_codex(executable)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )

    async def run_and_interrupt():
        running = asyncio.create_task(adapter.run(
            _task(codex, output_path, tmp_path / "capture.json"),
            _ctx(validation, output_path),
        ))
        await asyncio.sleep(0.05)
        await adapter.interrupt()
        return await running

    result = asyncio.run(run_and_interrupt())
    assert result.engine_error is None
    assert result.validation.verdict is validation.Verdict.FAIL
    assert "整任务重跑" in (result.conclusion_error or "")


def test_timeout_回收忽略_sigterm_的同组子进程(tmp_path, monkeypatch):
    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-1" / "goals" / "goal-1" / "result.md"
    capture = tmp_path / "child.pid"
    executable = tmp_path / "fake-codex"
    _write_child_hanging_codex(executable)
    adapter = codex.CodexAdapter(
        executable=str(executable),
        codex_home=tmp_path / "runtime-home",
        timeout_seconds=2.0,
    )

    started_at = time.monotonic()
    result = asyncio.run(adapter.run(
        _task(codex, output_path, capture), _ctx(validation, output_path)
    ))
    elapsed = time.monotonic() - started_at
    child_pid = int(capture.read_text(encoding="utf-8"))
    try:
        assert result.validation.verdict is validation.Verdict.FAIL
        assert not _pid_exists(child_pid)
        assert elapsed < 3.5
    finally:
        if _pid_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
