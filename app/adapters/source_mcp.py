"""把 capability.sources 翻译为 Claude/Codex 都可调用的 source.* MCP 工具。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MCP_SERVER_NAME = "owli_sources"
SOURCE_MCP_PATH = Path(__file__).resolve()
_X_RUNTIME_ENV_NAMES = (
    "OWLI_X_API_BASE_URL",
    "OWLI_X_BEARER_TOKEN_ENV",
    "OWLI_X_WEEKLY_BUDGET_USD",
    "OWLI_X_BALANCE_USD",
    "OWLI_X_BILLING_CYCLE_CAP_USD",
    "OWLI_X_BILLING_CYCLE_SPENT_USD",
    "OWLI_X_PRICE_PER_READ_USD",
    "OWLI_X_USAGE_DB_PATH",
)


def exposed_tool_name(source_id: str) -> str:
    """Claude SDK 的 MCP 白名单名；逻辑工具名仍是 source.<id>。"""

    return f"mcp__{MCP_SERVER_NAME}__source.{source_id}"


def source_event_path(task: Any) -> Path:
    safe_agent = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in str(task.agent_id)
    )
    identity = "\0".join(
        (
            str(task.research_id),
            str(task.goal_id),
            str(task.agent_id),
            str(Path(task.output_path).resolve(strict=False)),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return (
        PROJECT_ROOT
        / "var"
        / "source-events"
        / f"{safe_agent or 'agent'}-{digest}.jsonl"
    )


def prepare_source_events(task: Any) -> None:
    """单任务启动前清理旧事件，避免重试轮重放。"""

    if tuple(getattr(getattr(task, "capability", None), "sources", ())):
        source_event_path(task).unlink(missing_ok=True)


async def replay_source_events(task: Any, on_event: Any = None) -> None:
    """MCP 子进程事件在引擎返回前重放到 Owli 事件管道。"""

    path = source_event_path(task)
    if not path.is_file():
        return
    try:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return
        for raw in lines:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if on_event is not None:
                result = on_event(event)
                if inspect.isawaitable(result):
                    await result
    finally:
        path.unlink(missing_ok=True)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class SourceToolAdapter:
    """source.* 统一调用面；工具发现与实现细节只留在适配层。"""

    def __init__(self, source_tools: Mapping[str, Any] | None = None) -> None:
        self._source_tools = dict(source_tools) if source_tools is not None else None

    def _entrypoint(self, tool_name: str) -> Any:
        if self._source_tools is None:
            from app.sources.registry import get_tool

            return get_tool(tool_name)
        try:
            return self._source_tools[tool_name]
        except KeyError as exc:
            raise KeyError(f"未注册的信息源工具：{tool_name}") from exc

    async def call(
        self,
        tool_name: str,
        query: str,
        window: str,
        *,
        research_id: str,
        goal_id: str,
        agent_id: str,
        capability: Any,
        on_event: Any = None,
        **kwargs: Any,
    ) -> Any:
        if not tool_name.startswith("source."):
            raise ValueError(f"信息源工具名必须以 source. 开头：{tool_name}")
        source_id = tool_name.removeprefix("source.")
        tools = tuple(getattr(capability, "tools", ()))
        sources = tuple(getattr(capability, "sources", ()))
        network = str(getattr(capability, "network", "none"))
        if (
            source_id not in sources
            or tool_name not in tools and "source.*" not in tools
            or network not in {"sources_only", "open"}
        ):
            raise PermissionError(
                f"capability 未同时授权 {tool_name}、sources={source_id} 与网络访问"
            )

        entrypoint = self._entrypoint(tool_name)
        buffered_events: list[Any] = []

        def capture(event: Any) -> None:
            if isinstance(event, Mapping):
                payload = dict(event)
                data = payload.get("data")
                if payload.get("type") == "card_update" and isinstance(data, Mapping):
                    card = data.get("card")
                    if isinstance(card, Mapping):
                        normalized = dict(card)
                        normalized.update(
                            research_id=research_id,
                            goal_id=goal_id,
                            agent_id=agent_id,
                        )
                        payload["data"] = {**dict(data), "card": normalized}
                buffered_events.append(payload)
            else:
                buffered_events.append(event)

        parameters = inspect.signature(entrypoint).parameters.values()
        accepts_events = any(
            parameter.name == "on_event"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        call_kwargs = dict(kwargs)
        if accepts_events:
            call_kwargs["on_event"] = capture
        try:
            if inspect.iscoroutinefunction(entrypoint):
                result = await entrypoint(query, window, **call_kwargs)
            else:
                result = await asyncio.to_thread(
                    entrypoint, query, window, **call_kwargs
                )
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            if on_event is not None:
                for event in buffered_events:
                    callback_result = on_event(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result


def stdio_server_config(
    source_ids: tuple[str, ...],
    *,
    event_path: str | Path | None = None,
    research_id: str = "mcp",
    goal_id: str = "mcp",
    agent_id: str = "mcp",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Claude SDK 可直接消费的 stdio MCP 配置。"""

    args = ["-m", "app.adapters.source_mcp"]
    for source_id in source_ids:
        args.extend(["--source", source_id])
    if event_path is not None:
        args.extend(["--event-path", str(event_path)])
    args.extend(
        [
            "--research-id",
            research_id,
            "--goal-id",
            goal_id,
            "--agent-id",
            agent_id,
        ]
    )
    parent_env = os.environ if environ is None else environ
    child_env = {"PYTHONPATH": str(PROJECT_ROOT)}
    child_env.update(
        {
            name: str(parent_env[name])
            for name in _X_RUNTIME_ENV_NAMES
            if str(parent_env.get(name, "")).strip()
        }
    )
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": args,
        "env": child_env,
    }


def codex_mcp_args(
    source_ids: tuple[str, ...],
    *,
    event_path: str | Path | None = None,
    research_id: str = "mcp",
    goal_id: str = "mcp",
    agent_id: str = "mcp",
) -> list[str]:
    """Codex CLI 单次任务 MCP 配置，不写入隔离 CODEX_HOME。"""

    config = stdio_server_config(
        source_ids,
        event_path=event_path,
        research_id=research_id,
        goal_id=goal_id,
        agent_id=agent_id,
    )
    env_toml = ",".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in config["env"].items()
    )
    return [
        "-c",
        f"mcp_servers.{MCP_SERVER_NAME}.command={json.dumps(config['command'])}",
        "-c",
        f"mcp_servers.{MCP_SERVER_NAME}.args={json.dumps(config['args'], ensure_ascii=False)}",
        "-c",
        f"mcp_servers.{MCP_SERVER_NAME}.env={{{env_toml}}}",
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Owli source.* stdio MCP server")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--event-path", type=Path)
    parser.add_argument("--research-id", default="mcp")
    parser.add_argument("--goal-id", default="mcp")
    parser.add_argument("--agent-id", default="mcp")
    return parser


async def _serve(
    source_ids: tuple[str, ...],
    *,
    event_path: Path | None = None,
    research_id: str = "mcp",
    goal_id: str = "mcp",
    agent_id: str = "mcp",
) -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp_types import CallToolResult, ListToolsResult, TextContent, Tool

    from app.adapters.capability import Capability

    adapter = SourceToolAdapter()
    tools = [
        Tool(
            name=f"source.{source_id}",
            description=f"调用 Owli 注册信息源 {source_id}",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "window": {"type": "string"},
                },
                "required": ["query", "window"],
                "additionalProperties": False,
            },
        )
        for source_id in source_ids
    ]

    async def list_tools(_ctx: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    async def call_tool(_ctx: Any, params: Any) -> CallToolResult:
        name = str(params.name)
        if name not in {tool.name for tool in tools}:
            return CallToolResult(
                content=[TextContent(text=f"工具未授权：{name}")], isError=True
            )
        arguments = params.arguments or {}
        source_id = name.removeprefix("source.")
        events: list[Any] = []
        error: Exception | None = None
        result: Any = None
        try:
            result = await adapter.call(
                name,
                str(arguments.get("query") or ""),
                str(arguments.get("window") or ""),
                research_id=research_id,
                goal_id=goal_id,
                agent_id=agent_id,
                capability=Capability(
                    tools=(name,), sources=(source_id,), network="sources_only"
                ),
                on_event=events.append,
            )
        except Exception as exc:
            error = exc
        finally:
            if event_path is not None and events:
                event_path.parent.mkdir(parents=True, exist_ok=True)
                with event_path.open("a", encoding="utf-8") as stream:
                    for event in events:
                        stream.write(
                            json.dumps(
                                _jsonable(event),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
        payload = {
            "result": _jsonable(result),
            "events": _jsonable(events),
            "error": (
                {"type": type(error).__name__, "message": str(error)}
                if error is not None
                else None
            ),
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return CallToolResult(
            content=[TextContent(text=text)],
            structuredContent=payload,
            isError=error is not None,
        )

    server = Server(
        MCP_SERVER_NAME,
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    source_ids = tuple(dict.fromkeys(str(item) for item in args.source))
    asyncio.run(
        _serve(
            source_ids,
            event_path=args.event_path,
            research_id=args.research_id,
            goal_id=args.goal_id,
            agent_id=args.agent_id,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "MCP_SERVER_NAME",
    "SourceToolAdapter",
    "codex_mcp_args",
    "exposed_tool_name",
    "prepare_source_events",
    "replay_source_events",
    "source_event_path",
    "stdio_server_config",
]
