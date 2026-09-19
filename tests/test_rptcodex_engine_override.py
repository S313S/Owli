"""§RPT-CODEX 货 1：验收脚本的 `--engine` 必须把出稿**真的**钉在指定引擎上。

背景（09-19 09:28 那轮，零成稿）：`MAX_ATTEMPTS = 2` 两次机会，第 1 次派给 Claude
——30 分钟一个字没写、被 `SECTION_TIMEOUT_SECONDS` 掐掉，白烧掉一次；第 2 次派给
Codex，**2 分钟就写完了**，只是被门禁正常打回。瓶颈在「派给谁」，不在门禁。

⛔ 这里**不验「参数传进去了」**。本项目坑：「量出没差别先怀疑开关有没有真的拨动」，
反过来也成立——传进去了不等于路由认它。所以每条判据都落在
**真 `pick_engine` 回的那个引擎名**上，且每条都配一个**反向对照**：
同一个 task 不带覆盖时路由回的是 `claude`。没有这个对照，
就算 `report_writing` 的默认值哪天被改成 codex，用例照样全绿、却什么都没证明。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "acceptance" / "rpt1"))

from app.adapters.routing import _ENGINES, _PLANNING_KINDS, pick_engine  # noqa: E402
from app.report.polish.run import AGENT_KIND  # noqa: E402
from rpt1_polish import ENGINES, EngineTally, ForcedEngineAdapter  # noqa: E402


def async_test(function):           # 本仓既有写法（tests/test_normalized_events.py:16）
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class _CapturingInner:
    """冒充 `RoutedAdapter`：只把收到的 task 扣下来，不跑引擎。"""

    def __init__(self) -> None:
        self.seen: list[Any] = []
        self.timeout_seconds = 1800.0

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> str:
        self.seen.append(task)
        return "ran"


@dataclass(frozen=True)
class _Event:
    engine: str


def _task(**over: Any) -> Any:
    """真 `EngineTask`，agent_kind 取生产常量——⛔ 不写死字符串。"""
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineTask

    base = dict(
        body="x", output_path=Path("/tmp/x.md"), output_format="markdown",
        research_id="r-1", goal_id="polished", agent_id="report-polisher",
        agent_kind=AGENT_KIND, validators=["file_exists"],
        capability=Capability(profile="readonly-analyst", tools=("fs.write",),
                              fs=FileSystemScope(read=(), write=())))
    base.update(over)
    return EngineTask(**base)


# ── 反向对照：不覆盖的时候，这条链路默认派给的是 Claude ────────────────────────

def test_不覆盖时报告撰写默认派给claude():
    """⭐ 整组用例的对照组。这条要是变了，下面「拧到 codex」就不再证明任何事。"""
    assert pick_engine(AGENT_KIND, None).engine == "claude"


def test_报告撰写不在规划期名单里否则覆盖会被静默忽略():
    """`RoutedAdapter.run` 对 `_PLANNING_KINDS` 固定走 claude、**根本不看** user_override。"""
    assert AGENT_KIND not in _PLANNING_KINDS


# ── 开关真的拨动了：task 到路由手里，路由回的是 codex ──────────────────────────

@async_test
async def test_拧到codex之后路由回的是codex且署名是用户覆盖():
    inner = _CapturingInner()
    adapter = ForcedEngineAdapter(inner, "codex")

    assert await adapter.run(_task(), ctx=None) == "ran"

    (seen,) = inner.seen
    # 判据落在真 pick_engine 的回值上，不落在 user_override 这个字段本身。
    selection = pick_engine(seen.agent_kind, seen.user_override)
    assert selection.engine == "codex"
    assert selection.origin == "user", "origin 不是 user，说明覆盖没生效、是默认值撞对了"


@async_test
async def test_不加壳的同一个task路由回的是claude():
    """同一个 task 走 `_CapturingInner` 拿到手，不加壳就是 claude——差别出在壳上。"""
    inner = _CapturingInner()

    await _CapturingInner.run(inner, _task(), ctx=None)

    (seen,) = inner.seen
    assert seen.user_override is None
    assert pick_engine(seen.agent_kind, seen.user_override).engine == "claude"


@async_test
async def test_壳只改引擎不动别的字段():
    """回归面：除了 user_override，task 一个字段都不许被顺手改掉。"""
    inner = _CapturingInner()
    original = _task(model="claude-opus-4")

    await ForcedEngineAdapter(inner, "codex").run(original, ctx=None)

    (seen,) = inner.seen
    from dataclasses import fields
    changed = [f.name for f in fields(original)
               if getattr(original, f.name) != getattr(seen, f.name)]
    assert changed == ["user_override"], f"壳顺手改了别的字段：{changed}"


def test_壳透传内层属性():
    assert ForcedEngineAdapter(_CapturingInner(), "codex").timeout_seconds == 1800.0


def test_引擎名单与路由同源():
    """⛔ 验收脚本不许自己另开一套引擎名字。"""
    assert set(ENGINES) == set(_ENGINES)


def test_不认识的引擎当场拒绝():
    with pytest.raises(ValueError):
        ForcedEngineAdapter(_CapturingInner(), "gpt")


# ── 判据 2 的读数：事件署名 ───────────────────────────────────────────────────

@async_test
async def test_事件署名全是codex时没有跑偏():
    tally = EngineTally("codex")
    for _ in range(3):
        await tally(_Event(engine="Codex"))      # 叶子适配器盖的章是大写 Codex

    assert tally.counts == {"codex": 3}
    assert tally.strays == {}


@async_test
async def test_中途改派到claude会被读数抓到():
    """⭐ 这条是整个判据 2 的意义所在：`user_override` **不**封死限流让路与容量改派
    （`RoutedAdapter._route_overrides` / D-074 `_note_capacity`），
    所以「拧了开关」和「全程真在那一家」是两件事，必须分开量。"""
    tally = EngineTally("codex")
    await tally(_Event(engine="Codex"))
    await tally(_Event(engine="Claude"))

    assert tally.strays == {"claude": 1}, "被改派到 claude 却量不出来，判据 2 就是空的"


@async_test
async def test_没指定引擎时不判跑偏():
    tally = EngineTally(None)
    await tally(_Event(engine="Claude"))

    assert tally.counts == {"claude": 1}
    assert tally.strays == {}


# ── 判据 1 的回归面：不传 `--engine` 时，交给 `polish` 的东西一个字不变 ──────────

def _run_main(module: Any, argv: list[str], tmp_path: Path) -> dict[str, Any]:
    """真跑一遍 `main()`，把它交给 `polish` 的位置参数与关键字参数原样扣下来。

    ⛔ 不读源码文本比对——那量的是字符串不是行为。这里换掉的只有 `polish` 本身。
    """
    import sqlite3

    database = tmp_path / "t.db"
    conn = sqlite3.connect(database)
    conn.execute("create table reports (id text primary key, report_path text, "
                 "plan_snapshot text, decision_balance text, engines_used text, "
                 "attachments text, extra text)")
    conn.execute("insert into reports values (?,?,?,?,?,?,?)",
                 ("r-1", "goals/goal-1/report.md", None, None, None, None, None))
    conn.execute("create table evidence (id text primary key, report_id text)")
    conn.commit(); conn.close()
    draft = tmp_path / "runs" / "r-1" / "goals" / "goal-1" / "report.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("# 稿子", encoding="utf-8")

    captured: dict[str, Any] = {}

    async def fake_polish(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    original_polish, original_argv = module.polish, sys.argv
    module.polish = fake_polish
    sys.argv = ["rpt1_polish.py", "--db", str(database), "--runs",
                str(tmp_path / "runs"), "--id", "r-1", *argv]
    try:
        asyncio.run(module.main())
    finally:
        module.polish, sys.argv = original_polish, original_argv
    return captured


def test_不传engine时交给polish的关键字与老行为逐字相同(tmp_path: Path):
    """老代码调的是 `polish(store, id, runs_root, text, template=...)`——
    也就是 `adapter` / `on_event` 两个形参都吃默认值 `None`。
    新代码显式传了这两个，**值必须还是 None**，否则「不传参数就照旧」这句话是空的。"""
    import rpt1_polish

    captured = _run_main(rpt1_polish, [], tmp_path)

    assert captured["kwargs"] == {"template": "consulting", "adapter": None,
                                  "on_event": None}


def test_传了engine才会挂上壳和读数(tmp_path: Path):
    """反向对照：同一条 `main()`，加一个参数就必须量得出差别。
    量不出差别 = 开关没接上（本项目坑：「量出没差别先怀疑开关有没有真的拨动」）。"""
    import rpt1_polish

    captured = _run_main(rpt1_polish, ["--engine", "codex"], tmp_path)

    adapter = captured["kwargs"]["adapter"]
    tally = captured["kwargs"]["on_event"]
    assert isinstance(adapter, rpt1_polish.ForcedEngineAdapter)
    assert adapter._engine == "codex"
    assert isinstance(tally, rpt1_polish.EngineTally) and tally.expected == "codex"
