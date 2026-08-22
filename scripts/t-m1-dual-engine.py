#!/usr/bin/env python3
"""M1 双引擎同 spec 真实验收；M7 回归继续复用。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from dataclasses import fields
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def _ensure_runtime() -> None:
    """允许验收命令直接写 python，同时复用仓库已安装依赖。"""

    sdk_missing = importlib.util.find_spec("claude_agent_sdk") is None
    in_project_venv = Path(sys.prefix).absolute() == (ROOT / ".venv").absolute()
    if sdk_missing and VENV_PYTHON.is_file() and not in_project_venv:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])


_ensure_runtime()
sys.path.insert(0, str(ROOT))

from app.adapters import validation  # noqa: E402
from app.adapters.capability import Capability, FileSystemScope  # noqa: E402
from app.adapters.claude import ClaudeAdapter  # noqa: E402
from app.adapters.codex import CodexAdapter  # noqa: E402
from app.adapters.contracts import EngineTask  # noqa: E402
from app.adapters.events import NormalizedEvent  # noqa: E402


RESEARCH_ID = "t-m1-dual-engine"
GOAL_ID = "goal-1"
OUTPUT_PATH = (
    validation.RUNS_ROOT / RESEARCH_ID / "goals" / GOAL_ID / "result.txt"
)


def _ctx() -> validation.Ctx:
    return validation.Ctx(
        output_path=OUTPUT_PATH,
        output_format="text",
        research_id=RESEARCH_ID,
        goal_id=GOAL_ID,
        agent_id="dual-engine-writer",
        read_text=lambda: OUTPUT_PATH.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(OUTPUT_PATH.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
    )


def _task() -> EngineTask:
    body = (
        "这是 M1 双引擎同 spec 验收。不得向用户提问。\n"
        f"请把且只把『M1 双引擎串线通过』写入 {OUTPUT_PATH}。\n"
        "写完后自检文件存在且非空。最终必须输出 json owli-result 代码块，"
        "status=done，output_path 必须逐字填写上述绝对路径，summary=双引擎最小任务完成，"
        "assumptions、unmet、capability_denials 均为空数组，reason=null。"
    )
    return EngineTask(
        body=body,
        output_path=OUTPUT_PATH,
        output_format="text",
        research_id=RESEARCH_ID,
        goal_id=GOAL_ID,
        agent_id="dual-engine-writer",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            profile="custom",
            tools=("fs.write",),
            fs=FileSystemScope(write=(f"goals/{GOAL_ID}/**",)),
            network="none",
            shell="none",
        ),
    )


async def _run_one(name: str, adapter, task: EngineTask) -> dict[str, object]:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.unlink(missing_ok=True)
    streamed: list[NormalizedEvent] = []
    started = time.perf_counter()
    result = await adapter.run(task, _ctx(), on_event=streamed.append)
    elapsed = time.perf_counter() - started

    if result.engine_error is not None:
        raise AssertionError(f"{name} 引擎不可用：{result.engine_error}")
    if result.validation.verdict is not validation.Verdict.PASS:
        details = "；".join(item.message for item in result.validation.results)
        raise AssertionError(f"{name} 产物校验不是 PASS：{details}")
    if not result.succeeded:
        raise AssertionError(
            f"{name} 双腿判定失败：{result.conclusion_error or result.engine_error}"
        )
    if result.conclusion is None:
        raise AssertionError(f"{name} 缺少 owli-result 结论")
    if not all(isinstance(event, NormalizedEvent) for event in result.events):
        raise AssertionError(f"{name} result.events 含非 NormalizedEvent")
    if not all(isinstance(event, NormalizedEvent) for event in streamed):
        raise AssertionError(f"{name} 回调事件含非 NormalizedEvent")

    conclusion_fields = {item.name for item in fields(result.conclusion)}
    print(
        f"{name}：PASS｜耗时 {elapsed:.2f}s｜结果事件 {len(result.events)} 条｜"
        f"流式事件 {len(streamed)} 条"
    )
    return {
        "name": name,
        "elapsed": elapsed,
        "events": len(result.events),
        "streamed": len(streamed),
        "fields": conclusion_fields,
    }


async def _main() -> int:
    task = _task()
    task_identity = id(task)
    rows = []
    adapters = (
        ("Claude", ClaudeAdapter()),
        ("Codex", CodexAdapter()),
    )
    errors = []
    for name, adapter in adapters:
        if id(task) != task_identity:
            raise AssertionError("双引擎收到的不是同一个任务 spec 对象")
        try:
            rows.append(await _run_one(name, adapter, task))
        except AssertionError as error:
            errors.append(str(error))
            print(f"{name}：FAIL｜{error}")

    if errors:
        raise AssertionError("；".join(errors))
    if rows[0]["fields"] != rows[1]["fields"]:
        raise AssertionError(
            f"两侧 owli-result 字段集合不同：{rows[0]['fields']} / {rows[1]['fields']}"
        )
    fields_text = ", ".join(sorted(rows[0]["fields"]))
    print(f"结论字段集合一致：{fields_text}")
    print(f"同一任务 spec 对象：是（对象标识 {task_identity}）")
    print(f"最终产物：{OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except (AssertionError, RuntimeError, ValueError) as error:
        print(f"M1 双引擎验收失败：{error}", file=sys.stderr)
        raise SystemExit(1)
