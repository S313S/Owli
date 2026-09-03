"""引擎无关的 capability 声明、校验与双引擎参数翻译。"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
import re
from typing import Callable, Literal, Mapping, Sequence


AccessKind = Literal["read", "write"]
OutboundAuditCallback = Callable[[str, tuple[str, ...]], bool]

_PROFILES = {
    "custom",
    "readonly-analyst",
    "web-collector",
    "sandboxed-runner",
    "report-writer",
}
_NETWORK_MODES = {"none", "sources_only", "open"}
_SHELL_MODES = {"none", "readonly", "workspace"}
#: §CMT-1 货 5：采集卡的评论二跳开关，默认开；计划编辑页可关。
_COMMENT_MODES = {"on", "off"}
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})")


def _tuple(value: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(value or ())


@dataclass(frozen=True)
class FileSystemScope:
    """相对于本次调研产物根目录的读写 glob。"""

    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "read", _tuple(self.read))
        object.__setattr__(self, "write", _tuple(self.write))


@dataclass(frozen=True)
class Capability:
    """§4.1 定义的五维能力声明与预设档引用。"""

    profile: str = "custom"
    tools: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    fs: FileSystemScope = FileSystemScope()
    network: str = "none"
    shell: str = "none"
    justification: str | None = None
    #: 采集卡是否发评论二跳（§CMT-1 货 5）。只对有评论端点的源有意义，
    #: 其余源忽略。老计划快照没有这一键 → 默认 "on"，与拍板口径一致。
    comments: str = "on"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _tuple(self.tools))
        object.__setattr__(self, "sources", _tuple(self.sources))
        if self.comments not in _COMMENT_MODES:
            raise ValueError(f"capability.comments 只能是 on/off：{self.comments!r}")
        if isinstance(self.fs, Mapping):
            object.__setattr__(
                self,
                "fs",
                FileSystemScope(
                    read=_tuple(self.fs.get("read")),
                    write=_tuple(self.fs.get("write")),
                ),
            )


READONLY_ANALYST = Capability(
    profile="readonly-analyst",
    tools=("fs.read", "db.read"),
    sources=(),
    fs=FileSystemScope(read=("**",)),
    network="none",
    shell="none",
)

WEB_COLLECTOR = Capability(
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

SANDBOXED_RUNNER = Capability(
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

REPORT_WRITER = Capability(
    profile="report-writer",
    tools=("fs.read", "fs.write", "db.read", "db.write", "report.render"),
    sources=(),
    fs=FileSystemScope(read=("**",), write=("report/**",)),
    network="none",
    shell="none",
)

PRESETS: dict[str, Capability] = {
    capability.profile: capability
    for capability in (
        READONLY_ANALYST,
        WEB_COLLECTOR,
        SANDBOXED_RUNNER,
        REPORT_WRITER,
    )
}


class CapabilityValidationError(ValueError):
    """能力声明不能安全地翻译到目标引擎。"""

    def __init__(self, violations: Sequence[str]):
        self.violations = list(violations)
        super().__init__("能力声明校验失败：" + "；".join(self.violations))


def _path_violation(glob: str) -> str | None:
    normalized = glob.replace("\\", "/")
    if PurePosixPath(normalized).is_absolute() or _WINDOWS_ABSOLUTE.match(glob):
        return "使用了绝对路径"
    if ".." in PurePosixPath(normalized).parts:
        return "含有 .. 路径段"
    return None


def validate(cap: Capability, engine: str | None = None) -> list[str]:
    """返回 §4.5 违规清单；空列表表示通过。"""

    violations: list[str] = []
    if cap.profile not in _PROFILES:
        violations.append(f"未知 capability profile：{cap.profile}")
    if cap.network not in _NETWORK_MODES:
        violations.append(f"未知 network 档位：{cap.network}")
    if cap.shell not in _SHELL_MODES:
        violations.append(f"未知 shell 档位：{cap.shell}")

    for tool in cap.tools:
        if tool.startswith("source."):
            source_id = tool.removeprefix("source.")
            if source_id != "*" and source_id not in cap.sources:
                violations.append(
                    f"工具 {tool} 要求信息源 {source_id}，但 sources 未声明该信息源"
                )

    if cap.fs.write and "fs.write" not in cap.tools:
        violations.append("fs.write 声明了可写路径，但 tools 未授权 fs.write")

    if cap.shell != "none" and engine != "codex":
        engine_label = "未指定引擎" if engine is None else engine
        reason = f"shell={cap.shell} 只能使用 Codex，当前为 {engine_label}"
        reason += {"claude": "；Claude 没有内核级权限边界"}.get(engine or "", "")
        violations.append(reason)

    if cap.network == "open" and not (cap.justification or "").strip():
        violations.append("network=open 必须填写非空 justification 说明开放原因")

    for access, globs in (("read", cap.fs.read), ("write", cap.fs.write)):
        for glob in globs:
            reason = _path_violation(glob)
            if reason:
                violations.append(f"fs.{access} 路径 {glob!r} 非法：{reason}，必须相对产物根目录")
    return violations


def _require_valid(cap: Capability, engine: str) -> None:
    violations = validate(cap, engine)
    if violations:
        raise CapabilityValidationError(violations)


def _matches(path: str, glob: str) -> bool:
    path_parts = tuple(PurePosixPath(path.replace("\\", "/")).parts)
    glob_parts = tuple(PurePosixPath(glob.replace("\\", "/")).parts)
    pending = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    while pending:
        path_index, glob_index = pending.pop()
        if (path_index, glob_index) in visited:
            continue
        visited.add((path_index, glob_index))
        if glob_index == len(glob_parts):
            if path_index == len(path_parts):
                return True
            continue
        pattern = glob_parts[glob_index]
        if pattern == "**":
            pending.append((path_index, glob_index + 1))
            if path_index < len(path_parts):
                pending.append((path_index + 1, glob_index))
            continue
        if path_index < len(path_parts) and fnmatchcase(path_parts[path_index], pattern):
            pending.append((path_index + 1, glob_index + 1))
    return False


@dataclass(frozen=True)
class PathGlobPredicate:
    """供适配器运行期 can_use_tool 调用的相对路径判定器。"""

    read_globs: tuple[str, ...]
    write_globs: tuple[str, ...]

    def __call__(self, path: str, access: AccessKind = "read") -> bool:
        if access not in ("read", "write") or _path_violation(path):
            return False
        globs = self.read_globs if access == "read" else self.write_globs
        normalized = str(PurePosixPath(path.replace("\\", "/")))
        return any(_matches(normalized, glob) for glob in globs)

    def allows_read(self, path: str) -> bool:
        return self(path, "read")

    def allows_write(self, path: str) -> bool:
        return self(path, "write")


_CLAUDE_TOOL_UNIVERSE = frozenset(
    {
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Glob",
        "Grep",
        "Bash",
        "WebFetch",
        "WebSearch",
        "Task",
        "TodoWrite",
        "Skill",
    }
)
_CLAUDE_TOOL_TRANSLATION = {
    "fs.read": frozenset({"Read", "Glob", "Grep"}),
    "fs.write": frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"}),
}


@dataclass(frozen=True)
class ClaudeOptions:
    """M1-e 可直接挂载到 Claude SDK 选项与回调的结构化参数。"""

    profile: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    permission_mode: str
    setting_sources: tuple[str, ...]
    registered_sources: tuple[str, ...]
    path_predicate: PathGlobPredicate
    allow_bare_urls: bool
    log_all_network_access: bool


def to_claude_options(cap: Capability) -> ClaudeOptions:
    """按 §4.3 翻译；需要 shell 时直接拒绝 Claude。"""

    _require_valid(cap, "claude")
    allowed_builtins: set[str] = set()
    for tool in cap.tools:
        allowed_builtins.update(_CLAUDE_TOOL_TRANSLATION.get(tool, ()))

    network_open = cap.network == "open"
    bare_url_allowed = network_open and "web.fetch" in cap.tools
    if bare_url_allowed:
        allowed_builtins.add("WebFetch")
    registered_sources = cap.sources if cap.network != "none" else ()
    return ClaudeOptions(
        profile=cap.profile,
        allowed_tools=cap.tools,
        disallowed_tools=tuple(sorted(_CLAUDE_TOOL_UNIVERSE - allowed_builtins)),
        permission_mode="dontAsk",
        setting_sources=(),
        registered_sources=registered_sources,
        path_predicate=PathGlobPredicate(cap.fs.read, cap.fs.write),
        allow_bare_urls=bare_url_allowed,
        log_all_network_access=network_open,
    )


@dataclass(frozen=True)
class CodexArgs:
    """M1-e 可据此组装 CLI、隔离 CODEX_HOME 与适配层拦截器。"""

    profile: str
    cli_args: tuple[str, ...]
    sandbox: str
    shell_mode: str
    writable_roots: tuple[str, ...]
    network_enabled: bool
    injected_sources: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    path_predicate: PathGlobPredicate
    outbound_audit_required: bool
    outbound_audit: OutboundAuditCallback | None
    log_all_network_access: bool


def to_codex_args(
    cap: Capability,
    *,
    outbound_audit: OutboundAuditCallback | None = None,
) -> CodexArgs:
    """按 §4.4 翻译；网络只表达开关，来源收敛留给审计回调。"""

    _require_valid(cap, "codex")
    workspace_write = cap.shell == "workspace" or bool(cap.fs.write)
    sandbox = "workspace-write" if workspace_write else "read-only"
    network_enabled = cap.network != "none"
    return CodexArgs(
        profile=cap.profile,
        cli_args=("--sandbox", sandbox),
        sandbox=sandbox,
        shell_mode=cap.shell,
        writable_roots=cap.fs.write,
        network_enabled=network_enabled,
        injected_sources=cap.sources if network_enabled else (),
        allowed_tools=cap.tools,
        path_predicate=PathGlobPredicate(cap.fs.read, cap.fs.write),
        outbound_audit_required=cap.network == "sources_only",
        outbound_audit=outbound_audit,
        log_all_network_access=cap.network == "open",
    )
