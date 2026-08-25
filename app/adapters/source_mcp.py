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
_SOURCE_RUNTIME_ENV_NAMES = (
    "OWLI_SOURCE_PAYLOAD_BYTE_LIMIT",
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


def _payload_archive_path(event_path: Path | None) -> Path | None:
    if event_path is None:
        return None
    return Path(f"{event_path}.payload.json")


def prepare_source_events(task: Any) -> None:
    """单任务启动前清理旧事件，避免重试轮重放。"""

    if tuple(getattr(getattr(task, "capability", None), "sources", ())):
        event_path = source_event_path(task)
        event_path.unlink(missing_ok=True)
        archive_path = _payload_archive_path(event_path)
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


async def replay_source_events(task: Any, on_event: Any = None) -> None:
    """MCP 子进程事件在引擎返回前重放到 Owli 事件管道。"""

    path = source_event_path(task)
    archive_path = _payload_archive_path(path)
    if not path.is_file():
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
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
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _event_is_error(event: Any) -> bool:
    normalized = _jsonable(event)
    if not isinstance(normalized, Mapping):
        return False
    if bool(normalized.get("is_error")):
        return True
    item_kind = str(normalized.get("item_kind") or "").casefold()
    if item_kind.rsplit(".", 1)[-1] == "error":
        return True
    event_type = str(normalized.get("type") or "").casefold()
    if (
        "error" in event_type
        or event_type.endswith(("failed", "failure", "unavailable"))
    ):
        return True
    data = normalized.get("data")
    return isinstance(data, Mapping) and bool(data.get("error"))


def _event_summary(
    events: list[Any], event_path: Path | None
) -> dict[str, Any]:
    """回灌仅保留可判定摘要；逐条原文已由 call_tool 落盘。"""

    return {
        "count": len(events),
        "error_count": sum(_event_is_error(event) for event in events),
        "path": str(event_path) if event_path is not None else None,
    }


def _json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def _payload_with_kept_items(
    payload: Mapping[str, Any], kept_items: int
) -> dict[str, Any]:
    bounded = dict(payload)
    result = payload.get("result")
    if isinstance(result, list):
        bounded["result"] = result[:kept_items]
        return bounded
    if isinstance(result, Mapping) and isinstance(result.get("evidence"), list):
        bounded_result = dict(result)
        bounded_result["evidence"] = result["evidence"][:kept_items]
        bounded["result"] = bounded_result
        return bounded
    return bounded


def _result_item_count(payload: Mapping[str, Any]) -> int | None:
    result = payload.get("result")
    if isinstance(result, list):
        return len(result)
    if isinstance(result, Mapping) and isinstance(result.get("evidence"), list):
        return len(result["evidence"])
    return None


def _bounded_payload(
    payload: Mapping[str, Any],
    *,
    full_payload_path: Path | None,
    byte_limit: int,
) -> tuple[dict[str, Any], str]:
    """超限时仅丢弃尾部完整 item，绝不切割序列化后的 JSON。"""

    text = _json_text(payload)
    if len(text.encode("utf-8")) <= byte_limit:
        return dict(payload), text

    path_text = str(full_payload_path) if full_payload_path is not None else "未配置"

    def mark_truncated(candidate: dict[str, Any], omitted: int) -> str:
        candidate["truncation"] = {
            "omitted_items": omitted,
            "full_payload_path": (
                str(full_payload_path) if full_payload_path is not None else None
            ),
            "message": f"已截断 {omitted} 条 / 全量见落盘文件 {path_text}",
        }
        return _json_text(candidate)

    item_count = _result_item_count(payload)
    if item_count is None:
        candidate = dict(payload)
        candidate["result"] = None
        error = payload.get("error")
        if isinstance(error, Mapping):
            candidate["error"] = {
                "type": str(error.get("type") or "Error"),
                "message": f"错误详情已省略；全量见落盘文件 {path_text}",
            }
        candidate_text = mark_truncated(candidate, 1)
        if len(candidate_text.encode("utf-8")) <= byte_limit:
            return candidate, candidate_text
        raise ValueError("OWLI_SOURCE_PAYLOAD_BYTE_LIMIT 小于回灌摘要所需字节数")

    best: tuple[dict[str, Any], str] | None = None
    low = 0
    high = item_count - 1
    while low <= high:
        kept = (low + high) // 2
        candidate = _payload_with_kept_items(payload, kept)
        omitted = item_count - kept
        candidate_text = mark_truncated(candidate, omitted)
        if len(candidate_text.encode("utf-8")) <= byte_limit:
            best = candidate, candidate_text
            low = kept + 1
        else:
            high = kept - 1
    if best is None:
        raise ValueError("OWLI_SOURCE_PAYLOAD_BYTE_LIMIT 小于回灌摘要所需字节数")
    return best


def _build_tool_payload(
    *,
    result: Any,
    events: list[Any],
    error: Exception | None,
    event_path: Path | None,
    byte_limit: int,
) -> tuple[dict[str, Any], str]:
    payload = {
        "result": _jsonable(result),
        "events": _event_summary(events, event_path),
        "error": (
            {"type": type(error).__name__, "message": str(error)}
            if error is not None
            else None
        ),
    }
    full_text = _json_text(payload)
    archive_path = _payload_archive_path(event_path)
    bounded_payload, bounded_text = _bounded_payload(
        payload, full_payload_path=archive_path, byte_limit=byte_limit
    )
    if "truncation" in bounded_payload and archive_path is not None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(full_text, encoding="utf-8")
    return bounded_payload, bounded_text


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
        item_limit: int | None = None,
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
        if item_limit is not None:
            if not isinstance(item_limit, int) or isinstance(item_limit, bool) or item_limit < 1:
                raise ValueError("item_limit 必须是正整数")
            parameter = {
                "hacker_news": "limit",
                "product_hunt": "limit",
                "web_search": "max_results",
                "x": "max_results",
            }.get(source_id)
            if parameter is not None:
                call_kwargs[parameter] = item_limit
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
    item_limit: int | None = None,
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
    if item_limit is not None:
        args.extend(["--item-limit", str(item_limit)])
    parent_env = os.environ if environ is None else environ
    child_env = {"PYTHONPATH": str(PROJECT_ROOT)}
    child_env.update(
        {
            name: str(parent_env[name])
            for name in _SOURCE_RUNTIME_ENV_NAMES
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
    item_limit: int | None = None,
) -> list[str]:
    """Codex CLI 单次任务 MCP 配置，不写入隔离 CODEX_HOME。"""

    config = stdio_server_config(
        source_ids,
        event_path=event_path,
        research_id=research_id,
        goal_id=goal_id,
        agent_id=agent_id,
        item_limit=item_limit,
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
        # codex-cli ≥0.149 把 MCP 工具调用挂在逐次审批门后，非交互 exec 的
        # 审批策略为 never 时一律拒绝；能注入本服务器的源已经过 capability
        # 层收敛，故显式放行。旧版 codex 忽略未知配置键，不受影响。
        "-c",
        f'mcp_servers.{MCP_SERVER_NAME}.default_tools_approval_mode="approve"',
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Owli source.* stdio MCP server")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--event-path", type=Path)
    parser.add_argument("--research-id", default="mcp")
    parser.add_argument("--goal-id", default="mcp")
    parser.add_argument("--agent-id", default="mcp")
    parser.add_argument("--item-limit", type=int)
    return parser


async def _serve(
    source_ids: tuple[str, ...],
    *,
    event_path: Path | None = None,
    research_id: str = "mcp",
    goal_id: str = "mcp",
    agent_id: str = "mcp",
    item_limit: int | None = None,
) -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp_types import CallToolResult, ListToolsResult, TextContent, Tool

    from app.adapters.capability import Capability
    from app.config import load_source_response_config

    adapter = SourceToolAdapter()
    response_config = load_source_response_config()
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
                item_limit=item_limit,
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
        payload, text = _build_tool_payload(
            result=result,
            events=events,
            error=error,
            event_path=event_path,
            byte_limit=response_config.payload_byte_limit,
        )
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
            item_limit=args.item_limit,
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
