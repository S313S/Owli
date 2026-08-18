from dataclasses import replace
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.capability import (
    PRESETS,
    READONLY_ANALYST,
    REPORT_WRITER,
    SANDBOXED_RUNNER,
    WEB_COLLECTOR,
    Capability,
    CapabilityValidationError,
    FileSystemScope,
    to_claude_options,
    to_codex_args,
    validate,
)


def test_四个预设档逐格保持权威表内容() -> None:
    assert PRESETS == {
        "readonly-analyst": READONLY_ANALYST,
        "web-collector": WEB_COLLECTOR,
        "sandboxed-runner": SANDBOXED_RUNNER,
        "report-writer": REPORT_WRITER,
    }
    assert READONLY_ANALYST == Capability(
        profile="readonly-analyst",
        tools=("fs.read", "db.read"),
        sources=(),
        fs=FileSystemScope(read=("**",), write=()),
        network="none",
        shell="none",
    )
    assert WEB_COLLECTOR == Capability(
        profile="web-collector",
        tools=("source.*", "fs.write", "db.write"),
        sources=(),
        fs=FileSystemScope(
            read=("goals/<upstream-goal>/**",),
            write=("goals/<current-goal>/**",),
        ),
        network="sources_only",
        shell="none",
    )
    assert SANDBOXED_RUNNER == Capability(
        profile="sandboxed-runner",
        tools=("shell.exec", "fs.read", "fs.write"),
        sources=(),
        fs=FileSystemScope(
            read=("goals/<current-goal>/**",),
            write=("goals/<current-goal>/**",),
        ),
        network="none",
        shell="workspace",
    )
    assert REPORT_WRITER == Capability(
        profile="report-writer",
        tools=("fs.read", "fs.write", "db.read", "db.write", "report.render"),
        sources=(),
        fs=FileSystemScope(read=("**",), write=("report/**",)),
        network="none",
        shell="none",
    )


@pytest.mark.parametrize(
    ("capability", "expected_disallowed"),
    [
        (
            READONLY_ANALYST,
            (
                "Bash", "Edit", "MultiEdit", "NotebookEdit", "Skill", "Task",
                "TodoWrite", "WebFetch", "WebSearch", "Write",
            ),
        ),
        (
            WEB_COLLECTOR,
            (
                "Bash", "Glob", "Grep", "Read", "Skill", "Task", "TodoWrite",
                "WebFetch", "WebSearch",
            ),
        ),
        (SANDBOXED_RUNNER, None),
        (
            REPORT_WRITER,
            ("Bash", "Skill", "Task", "TodoWrite", "WebFetch", "WebSearch"),
        ),
    ],
)
def test_四档都产生明确的_Claude_翻译结果(capability, expected_disallowed) -> None:
    if expected_disallowed is None:
        with pytest.raises(CapabilityValidationError, match="Claude.*内核"):
            to_claude_options(capability)
        return

    options = to_claude_options(capability)
    assert options.profile == capability.profile
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == ()
    assert options.allowed_tools == capability.tools
    assert options.disallowed_tools == expected_disallowed
    assert options.registered_sources == ()
    assert options.allow_bare_urls is False
    assert options.log_all_network_access is False
    assert options.path_predicate.read_globs == capability.fs.read
    assert options.path_predicate.write_globs == capability.fs.write


@pytest.mark.parametrize(
    ("capability", "sandbox", "roots", "network", "audit"),
    [
        (READONLY_ANALYST, "read-only", (), False, False),
        (
            WEB_COLLECTOR,
            "workspace-write",
            ("goals/<current-goal>/**",),
            True,
            True,
        ),
        (
            SANDBOXED_RUNNER,
            "workspace-write",
            ("goals/<current-goal>/**",),
            False,
            False,
        ),
        (REPORT_WRITER, "workspace-write", ("report/**",), False, False),
    ],
)
def test_四档都产生逐格一致的_Codex_翻译结果(
    capability, sandbox, roots, network, audit
) -> None:
    options = to_codex_args(capability)
    assert options.profile == capability.profile
    assert options.sandbox == sandbox
    assert options.cli_args == ("--sandbox", sandbox)
    assert options.shell_mode == capability.shell
    assert options.writable_roots == roots
    assert options.network_enabled is network
    assert options.injected_sources == ()
    assert options.outbound_audit_required is audit
    assert options.log_all_network_access is False
    assert options.allowed_tools == capability.tools
    assert options.path_predicate.write_globs == capability.fs.write


def test_Claude_按白名单与网络档位生成排除表和路径谓词() -> None:
    cap = replace(
        WEB_COLLECTOR,
        tools=("source.hacker_news", "fs.read", "fs.write", "db.write"),
        sources=("hacker_news",),
        fs=FileSystemScope(
            read=("goals/goal-1/**",), write=("goals/goal-2/**",)
        ),
    )

    options = to_claude_options(cap)

    assert options.registered_sources == ("hacker_news",)
    assert options.allow_bare_urls is False
    assert options.log_all_network_access is False
    assert {"WebFetch", "WebSearch", "Bash"} <= set(options.disallowed_tools)
    assert {"Read", "Glob", "Grep", "Write", "Edit"}.isdisjoint(
        options.disallowed_tools
    )
    assert options.path_predicate("goals/goal-1/input/a.json", "read")
    assert not options.path_predicate("goals/goal-2/output.json", "read")
    assert options.path_predicate("goals/goal-2/output.json", "write")
    assert not options.path_predicate("goals/goal-3/output.json", "write")
    assert not options.path_predicate("../outside.txt", "write")


@pytest.mark.parametrize(
    ("network", "web_fetch_allowed", "sources_registered", "log_all"),
    [
        ("none", False, False, False),
        ("sources_only", False, True, False),
        ("open", True, True, True),
    ],
)
def test_Claude_网络三档逐行翻译(
    network, web_fetch_allowed, sources_registered, log_all
) -> None:
    cap = Capability(
        profile="custom",
        tools=("source.web_search", "web.fetch"),
        sources=("web_search",),
        fs=FileSystemScope(),
        network=network,
        shell="none",
        justification="需要访问任意公开网页" if network == "open" else None,
    )

    options = to_claude_options(cap)

    assert ("WebFetch" not in options.disallowed_tools) is web_fetch_allowed
    assert "WebSearch" in options.disallowed_tools
    assert bool(options.registered_sources) is sources_registered
    assert options.allow_bare_urls is web_fetch_allowed
    assert options.log_all_network_access is log_all


def test_Claude_open_不能越过_tools_白名单放出网页工具() -> None:
    cap = Capability(network="open", justification="仅供已声明工具按需出网")

    options = to_claude_options(cap)

    assert {"WebFetch", "WebSearch"} <= set(options.disallowed_tools)
    assert options.allow_bare_urls is False


def test_Codex_sources_only_开网并登记待接的出站审计接口() -> None:
    cap = replace(
        WEB_COLLECTOR,
        tools=("source.hacker_news", "fs.write", "db.write"),
        sources=("hacker_news",),
        fs=FileSystemScope(write=("goals/goal-2/**",)),
    )

    options = to_codex_args(cap)

    assert options.network_enabled is True
    assert options.outbound_audit_required is True
    assert options.outbound_audit is None
    assert options.injected_sources == ("hacker_news",)


def test_Codex_出站审计回调只登记不在本包执行() -> None:
    callback = lambda url, sources: url.startswith("https://") and bool(sources)
    cap = replace(
        WEB_COLLECTOR,
        tools=("source.hacker_news",),
        sources=("hacker_news",),
        fs=FileSystemScope(),
    )

    options = to_codex_args(cap, outbound_audit=callback)

    assert options.outbound_audit is callback


def test_Codex_断网时不向隔离_HOME_注入信息源() -> None:
    cap = Capability(
        tools=("source.hacker_news",),
        sources=("hacker_news",),
        network="none",
    )

    options = to_codex_args(cap)

    assert options.network_enabled is False
    assert options.injected_sources == ()


def test_Codex_保留_shell_readonly_供适配层拦截写盘命令() -> None:
    cap = Capability(
        tools=("shell.exec", "fs.write"),
        fs=FileSystemScope(write=("report/**",)),
        shell="readonly",
    )

    options = to_codex_args(cap)

    assert options.sandbox == "workspace-write"
    assert options.shell_mode == "readonly"


def test_Codex_open_开网并全量记录但不要求域名收敛审计() -> None:
    cap = Capability(
        profile="custom",
        tools=("web.fetch",),
        sources=(),
        fs=FileSystemScope(),
        network="open",
        shell="none",
        justification="需要访问用户给出的任意公开链接",
    )

    options = to_codex_args(cap)

    assert options.network_enabled is True
    assert options.outbound_audit_required is False
    assert options.log_all_network_access is True


@pytest.mark.parametrize(
    ("capability", "engine", "reason"),
    [
        (
            Capability(
                "custom",
                ("source.hacker_news",),
                (),
                FileSystemScope(),
                "sources_only",
                "none",
            ),
            "codex",
            "hacker_news",
        ),
        (
            Capability(
                "custom",
                (),
                (),
                FileSystemScope(write=("goals/goal-1/**",)),
                "none",
                "none",
            ),
            "codex",
            "fs.write",
        ),
        (
            Capability(
                "custom",
                ("fs.read",),
                (),
                FileSystemScope(read=("/etc/passwd",)),
                "none",
                "none",
            ),
            "codex",
            "绝对路径",
        ),
        (
            Capability(
                "custom",
                ("fs.read",),
                (),
                FileSystemScope(read=("goals/goal-1/../goal-2/**",)),
                "none",
                "none",
            ),
            "codex",
            "..",
        ),
        (
            SANDBOXED_RUNNER,
            "claude",
            "内核",
        ),
        (
            Capability(
                "custom", (), (), FileSystemScope(), "open", "none"
            ),
            "codex",
            "justification",
        ),
    ],
)
def test_违规矩阵返回可复用的中文原因(capability, engine, reason) -> None:
    violations = validate(capability, engine)

    assert violations
    assert any(reason in violation for violation in violations)
    assert all(any("\u4e00" <= char <= "\u9fff" for char in item) for item in violations)


def test_shell_workspace_在_Claude_校验和翻译两处都硬拒绝() -> None:
    violations = validate(SANDBOXED_RUNNER, "claude")

    assert any("shell=workspace" in item and "Claude" in item for item in violations)
    with pytest.raises(CapabilityValidationError) as raised:
        to_claude_options(SANDBOXED_RUNNER)
    assert raised.value.violations == violations


def test_shell_workspace_可按验收示例最小构造并拒绝_Claude() -> None:
    violations = validate(Capability(shell="workspace"), engine="claude")

    assert any("只能使用 Codex" in item and "Claude" in item for item in violations)


@pytest.mark.parametrize("engine", [None, "other"])
def test_shell_非_none_在未指定或非_Codex_引擎时拒绝(engine) -> None:
    violations = validate(Capability(shell="readonly"), engine=engine)

    assert any("只能使用 Codex" in item for item in violations)


def test_路径谓词的单层星号不会跨目录放大权限() -> None:
    options = to_claude_options(
        Capability(
            tools=("fs.read",),
            fs=FileSystemScope(read=("report/*.md",)),
        )
    )

    assert options.path_predicate("report/summary.md", "read")
    assert not options.path_predicate("report/private/secret.md", "read")


def test_任何_Codex_翻译结果都不出现最高危沙箱档位() -> None:
    forbidden = "danger-full-access"

    for capability in PRESETS.values():
        assert forbidden not in repr(to_codex_args(capability))


def test_结构化对象接收字典形式的_fs_并归一化列表() -> None:
    cap = Capability(
        profile="custom",
        tools=["fs.read"],
        sources=[],
        fs={"read": ["goals/**"], "write": []},
        network="none",
        shell="none",
    )

    assert cap.tools == ("fs.read",)
    assert cap.sources == ()
    assert cap.fs == FileSystemScope(read=("goals/**",), write=())
    assert validate(cap, "claude") == []
