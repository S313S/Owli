#!/usr/bin/env python3
"""§RPT-1 验收：对底料库里的一份成稿整理一次正式稿。

    python3 scripts/acceptance/rpt1/rpt1_polish.py --db var/rpt1-8956.db \
        --runs var/runs --id r-3e04f808dffd --template consulting

`--engine codex` 可以把这一轮出稿**全程**钉在某一家引擎上（不传＝沿用路由默认，
`report_writing` 默认是 claude）：

    python3 scripts/acceptance/rpt1/rpt1_polish.py ... --engine codex

只读库与工作稿，只写 `runs/<id>/exports/`。零采集、零评级，只付一次撰写调用。

退出码：0＝出稿且全程在指定引擎上；1＝没出稿；2＝出稿了但中途被改派到别家。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import default_adapter, polish  # noqa: E402


#: JSON 冻结列：读回来要还原成 Python 值，与 `store/dao.py` 的 `get_report` /
#: `list_evidence` **逐字同名单**。⛔ 少解一个字段就会出现「同一个库两条读法、
#: 量出两个世界」——§D-059 实测：`raw_metrics` 留成字符串，`engagement_value`
#: 只认 dict，于是 S66 明明有 176 个赞，正式稿原声表却写「该平台未提供互动数」，
#: 「原声按互动量排序」那条判据在生产里整个空转，而验收走真 `Store` 一路全绿。
_REPORT_JSON_FIELDS = (
    "plan_snapshot", "decision_balance", "engines_used", "attachments", "extra")
_EVIDENCE_JSON_FIELDS = ("author_meta", "raw_metrics", "norm_context", "extra")


def _decode(row, fields: tuple[str, ...]) -> dict:
    item = dict(row)
    for field in fields:
        value = item.get(field)
        if isinstance(value, str):
            try:
                item[field] = json.loads(value)
            except json.JSONDecodeError:
                pass          # 存进去就不是 JSON 的，原样留着，别让整份稿子读不出来
    return item


class ReadOnlyStore:
    """够 `collect_inputs` 用的最小 store：只有 get_report 与 list_evidence 两个读方法。

    ⚠️ 「最小」只省方法，⛔ 不省**语义**：这两个方法回的行必须和真 `Store` 回的
    一模一样（JSON 列已还原）。语义省掉一半，下游每个读 `raw_metrics` 的地方
    都会静默读空，而两边读数各自都是绿的（§D-059 货 2）。
    """

    def __init__(self, database: Path) -> None:
        self.conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def get_report(self, research_id: str):
        row = self.conn.execute("select * from reports where id=?", (research_id,)).fetchone()
        return _decode(row, _REPORT_JSON_FIELDS) if row else None

    def list_evidence(self, research_id: str):
        return [_decode(r, _EVIDENCE_JSON_FIELDS) for r in
                self.conn.execute(
                    "select * from evidence where report_id=? order by id", (research_id,))]


#: 允许 `--engine` 指定的引擎。⛔ 不自己另开一套名字：与 `app/adapters/routing.py`
#: 的 `_ENGINES` 同名同值，改了那边这边的用例会当场红（见 test_rptcodex_engine_override）。
ENGINES = ("claude", "codex")


class ForcedEngineAdapter:
    """把 `--engine` 拧进 `EngineTask.user_override`，其余一概透传。

    ⛔ **不新开一条路由**：走的是现成那条——`EngineTask.user_override`
    （`app/adapters/contracts.py`）→ `RoutedAdapter.run` 里的
    `pick_engine(task.agent_kind, task.user_override)`（`app/adapters/routing.py`），
    覆盖命中时 `origin="user"`。正式稿的 `agent_kind` 是 `report_writing`，
    **不在** `_PLANNING_KINDS` 里，所以「规划期固定走 claude」那条分支拦不住它。

    ⚠️ 它保证的是**默认值这一步**选谁，**不保证整轮每一次都落在这一家**：
    `RoutedAdapter.run` 在选完之后还有两个会把引擎改回去的口子——限流让路写的
    `_route_overrides[research_id]`，和 D-074 的容量改派（`_note_capacity`）。
    所以「真的全程都在 codex」必须另外**量事件里的引擎**（`EngineTally`），
    ⛔ 不能拿「参数传进去了」当读数。
    """

    def __init__(self, inner: Any, engine: str) -> None:
        if engine not in ENGINES:
            raise ValueError(f"不支持的引擎：{engine}")
        self._inner = inner
        self._engine = engine

    def __getattr__(self, name: str) -> Any:        # timeout_seconds 等一律透传
        return getattr(self._inner, name)

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> Any:
        return await self._inner.run(
            replace(task, user_override=self._engine), ctx, on_event=on_event)


class EngineTally:
    """数「事件是哪一家引擎发的」——判据 2 的读数落在这里。

    ⛔ 不读日志措辞、⛔ 不读「参数传没传进去」。`NormalizedEvent.engine` 是
    **叶子适配器自己盖的章**（`app/adapters/codex.py` 写 `engine="Codex"`、
    `app/adapters/claude.py` 写 `engine="Claude"`），所以它量的是真跑起来的那一家：
    只要有一条事件署名不是指定引擎，这一轮就**没有全程走指定引擎**。
    """

    def __init__(self, expected: str | None = None) -> None:
        self.expected = expected
        self.counts: Counter[str] = Counter()

    async def __call__(self, event: Any) -> None:
        engine = getattr(event, "engine", None)
        if isinstance(engine, str) and engine.strip():
            self.counts[engine.strip().casefold()] += 1

    @property
    def strays(self) -> dict[str, int]:
        """署名不是 `expected` 的那些事件。空 = 全程都在指定引擎上。"""
        if self.expected is None:
            return {}
        return {k: v for k, v in self.counts.items() if k != self.expected}


def work_draft_path(runs_root: Path, research_id: str, report_path: str) -> Path:
    """库里的 report_path 可能是指向别的 worktree 的绝对路径，按 runs_root 重新拼。"""
    raw = Path(report_path)
    return runs_root / research_id / "goals" / raw.parent.name / raw.name


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--template", default="consulting")
    # ⛔ 默认 None——不传这个参数时下面走的是和以前**逐字相同**的那一条
    # （`adapter=None` + `on_event=None`），一个分支都不多走。
    parser.add_argument("--engine", choices=ENGINES, default=None,
                        help="强制这一轮出稿全程走指定引擎；不传＝沿用路由默认（report_writing→claude）")
    args = parser.parse_args()
    runs_root = Path(args.runs).resolve()
    store = ReadOnlyStore(Path(args.db).resolve())
    report = store.get_report(args.id)
    if report is None:
        raise SystemExit(f"× 库里没有 {args.id}")
    text = work_draft_path(runs_root, args.id, report["report_path"]).read_text("utf-8")
    adapter = None if args.engine is None else ForcedEngineAdapter(
        default_adapter(), args.engine)
    tally = None if args.engine is None else EngineTally(args.engine)
    started = time.time()
    outcome = await polish(store, args.id, runs_root, text, template=args.template,
                           adapter=adapter, on_event=tally)
    outcome["elapsed_seconds"] = round(time.time() - started, 1)
    if tally is not None:
        outcome["engine_override"] = args.engine
        outcome["engines_observed"] = dict(sorted(tally.counts.items()))
        outcome["engine_strays"] = tally.strays
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    if outcome["status"] != "ok":
        return 1
    # 出了稿但中途被改派到别家：稿还在（上面的 JSON 已经把路径打出来了），
    # 但「全程走指定引擎」这条判据没过，⛔ 不许报 0 混过去。
    return 2 if tally is not None and tally.strays else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
