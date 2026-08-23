"""Owli 启动时的 schema 自检。"""

from __future__ import annotations

import re
import subprocess
import tempfile
from inspect import signature
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Callable

from app.adapters.codex import CodexAdapter, CodexAuthMode, build_codex_env
from app.config import ResearchScaleConfig

from app.store.schema import (
    initialize_database_if_empty,
    read_database_snapshot,
    read_expected_snapshot,
)


class SchemaCheckError(RuntimeError):
    """SQLite 实际结构与权威 schema 不一致。"""


class RuntimeConfigCheckError(RuntimeError):
    """部署级运行配置违反跨模块安全约束。"""


def _adapter_timeout_default(adapter_type: Any) -> float:
    return float(signature(adapter_type).parameters["timeout_seconds"].default)


def validate_runtime_config(
    scale_config: ResearchScaleConfig,
    *,
    codex_timeout_seconds: float | None = None,
    claude_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """拒绝章墙钟不大于任一引擎单次任务超时的配置（D-008 期望 b：claude 同样校验）。"""

    timeout = codex_timeout_seconds
    if timeout is None:
        timeout = _adapter_timeout_default(CodexAdapter)
    claude_timeout = claude_timeout_seconds
    if claude_timeout is None:
        from app.adapters.claude import ClaudeAdapter

        claude_timeout = _adapter_timeout_default(ClaudeAdapter)
    engine_timeouts = {"Codex": float(timeout), "Claude": float(claude_timeout)}
    for scale in ("standard", "fast"):
        wall_clock = scale_config.profile(scale).chapter_wall_clock_seconds
        if wall_clock is None:
            continue
        for engine, engine_timeout in engine_timeouts.items():
            if wall_clock <= engine_timeout:
                raise RuntimeConfigCheckError(
                    f"{scale} 档 chapter_wall_clock_seconds={wall_clock} 必须严格大于 "
                    f"{engine} 引擎超时 {engine_timeout:g} 秒"
                )
    return {
        "ok": True,
        "codex_timeout_seconds": timeout,
        "claude_timeout_seconds": claude_timeout,
        "chapter_wall_clock_seconds": {
            scale: scale_config.profile(scale).chapter_wall_clock_seconds
            for scale in ("standard", "fast")
        },
    }


def _engine_result(
    status: str,
    *,
    version: str | None = None,
    sandbox: str | None = None,
    detail: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "status_label": "可用" if status == "available" else "引擎不可用",
        "version": version,
        "sandbox": sandbox,
        "detail": detail,
    }


def probe_claude_sdk(
    *,
    version_reader: Callable[[str], str] = package_version,
) -> dict[str, Any]:
    try:
        version = version_reader("claude-agent-sdk")
    except Exception as exc:
        return _engine_result(
            "unavailable",
            detail=f"Claude Agent SDK 版本探测失败：{type(exc).__name__}: {exc}",
        )
    return _engine_result(
        "available",
        version=version,
        detail="Claude Agent SDK 版本探测通过",
    )


def _combined_output(completed: Any) -> str:
    return "\n".join(
        value.strip()
        for value in (getattr(completed, "stdout", ""), getattr(completed, "stderr", ""))
        if isinstance(value, str) and value.strip()
    )


def probe_codex_cli(
    *,
    executable: str = "codex",
    codex_home: str | Path | None = None,
    auth_mode: CodexAuthMode | str = CodexAuthMode.SUBSCRIPTION,
    api_key: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    try:
        env = build_codex_env(
            auth_mode,
            codex_home=codex_home,
            api_key=api_key,
        )
        Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
        version_run = runner(
            [executable, "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        version = _combined_output(version_run).splitlines()[0].strip()
        if not version:
            raise RuntimeError("codex --version 未返回版本文本")

        with tempfile.TemporaryDirectory(prefix="owli-codex-selfcheck-") as temp_dir:
            command = [
                executable,
                "exec",
                "-C",
                temp_dir,
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "这是启动自检：只输出“自检完成”，不要调用工具。",
            ]
            dry_run = runner(
                command,
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            )
        output = _combined_output(dry_run)
        match = re.search(r"sandbox:\s*([a-z-]+)", output, re.IGNORECASE)
        actual_sandbox = match.group(1).lower() if match else None
        if actual_sandbox != "read-only":
            shown = actual_sandbox or "未回显"
            raise RuntimeError(f"Codex 沙箱档位不符：期望 read-only，实际 {shown}")
    except Exception as exc:
        return _engine_result(
            "unavailable",
            detail=f"Codex CLI 探测失败：{type(exc).__name__}: {exc}",
        )
    return _engine_result(
        "available",
        version=version,
        sandbox=actual_sandbox,
        detail="Codex CLI 版本与沙箱干跑探测通过",
    )


def probe_engines(
    *,
    claude_probe: Callable[[], dict[str, Any]] = probe_claude_sdk,
    codex_probe: Callable[[], dict[str, Any]] = probe_codex_cli,
) -> dict[str, dict[str, Any]]:
    return {"claude": claude_probe(), "codex": codex_probe()}


def initialize_and_check(
    database_path: str | Path, schema_path: str | Path
) -> dict[str, Any]:
    initialize_database_if_empty(database_path, schema_path)
    actual = read_database_snapshot(database_path)
    expected = read_expected_snapshot(schema_path)
    differences = _compare_snapshots(actual, expected)
    if differences:
        detail = "\n".join(f"- {difference}" for difference in differences)
        raise SchemaCheckError(f"Schema 自检失败：\n{detail}")

    business_tables = sorted(actual["tables"] - actual["virtual_tables"])
    return {
        "ok": True,
        "schema_version": actual["schema_version"],
        "journal_mode": actual["journal_mode"],
        "tables": business_tables,
        "virtual_tables": sorted(actual["virtual_tables"]),
    }


def _compare_snapshots(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    differences: list[str] = []
    missing_tables = expected["tables"] - actual["tables"]
    unexpected_tables = actual["tables"] - expected["tables"]
    differences.extend(f"缺少表 {name}" for name in sorted(missing_tables))
    differences.extend(f"多出表 {name}" for name in sorted(unexpected_tables))

    for table in sorted(actual["tables"] & expected["tables"]):
        missing_columns = expected["columns"][table] - actual["columns"][table]
        unexpected_columns = actual["columns"][table] - expected["columns"][table]
        differences.extend(
            f"表 {table} 缺少列 {name}" for name in sorted(missing_columns)
        )
        differences.extend(
            f"表 {table} 多出列 {name}" for name in sorted(unexpected_columns)
        )
        if actual["strict"].get(table) != expected["strict"].get(table):
            differences.append(
                f"表 {table} STRICT 标记不一致："
                f"预期 {expected['strict'].get(table)}，"
                f"实际 {actual['strict'].get(table)}"
            )

    if actual["schema_version"] != expected["schema_version"]:
        differences.append(
            f"user_version 不一致：预期 {expected['schema_version']}，"
            f"实际 {actual['schema_version']}"
        )
    if actual["journal_mode"].lower() != "wal":
        differences.append(
            f"journal_mode 不一致：预期 wal，实际 {actual['journal_mode']}"
        )
    return differences
