"""§SEL-3：复制研究时把账本里的产物路径 rebase 到新树 / 新 id。

`chapter_progress.actual_output_path` 落库时是**绝对路径**，指着当初真跑出那份产物的
那台 worktree；而计划里 `opening.inputs` 是相对路径，跑时按**本研究的树**拼绝对路径。
导入不改写这一列，两边永远对不上：`_merge_declared_done_inputs` 认不出上游产物、
`_declared_done_goal_closure` 只剩本 goal，跨章互证节一条跨 goal 证据都引不到。

⚠️ 这里不是「把源 id 换成新 id」那么简单：源自己也可能是导入来的，而这一列从来没被
改写过，所以一份账本里能同时带着好几代 id（真机 `r-3b3482ca7f8b` 的 110 行里 104 行是
`r-20271e8a5028`、4 行 `r-f9bb30969cf2`、2 行 `r-96e0257a86b4`，**一行都不是它自己**）。
只认「直接源」那一个 id，真机那次导入一行都改不动——所以用例要拿**祖先 id** 来钉。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from plan_factory import make_plan_dict

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"

#: 源研究：它自己也是导入来的（`extra.replay_of` 指向祖先）。
SOURCE_ID = "r-sel3-source-0001"
#: 祖先：**它那一行不在本库里**（跑在另一台 worktree 的库上），只能从子代的
#: `replay_of` 里拿到 id——真机就是这个形态。
ANCESTOR_ID = "r-sel3-ancestor-00"
#: 认不出任何 marker 的路径：⛔ 原样保留，猜着改会把账本指向一个不存在的地方。
FOREIGN_PATH = "/Volumes/别的地方/artifacts/goal-3-手搬过来的.md"


def _seed(database: Path, runs_root: Path) -> None:
    """造一份「导入来的研究」：三章都 done，路径分别带祖先 id / 自己的 id / 无 marker。"""

    from app.adapters.selfcheck import initialize_and_check
    from app.store.dao import Store

    initialize_and_check(database, SCHEMA_PATH)
    store = Store(database)
    snapshot = make_plan_dict()
    snapshot["research_id"] = SOURCE_ID
    store.create_report(
        id=SOURCE_ID,
        title=snapshot["title"],
        research_question=snapshot["research_question"],
        use_case=snapshot["use_case"],
        status="completed",
        created_at="2026-09-18T00:00:00+00:00",
        plan_snapshot=snapshot,
        extra={"replay_of": ANCESTOR_ID},
    )
    store.add_evidence_batch([
        {
            "id": "ev-sel3-1",
            "report_id": SOURCE_ID,
            "goal_id": "goal-1",
            "platform": "xhs",
            "platform_item_id": "item-1",
            "permalink": "https://www.xiaohongshu.com/explore/1",
            "fetched_at": "2026-09-18T00:00:00+00:00",
        },
    ])
    store.ensure_chapters(
        SOURCE_ID,
        [{"goal_id": f"goal-{n}", "chapter_id": "ch-1"} for n in (1, 2, 3)],
        updated_at="2026-09-18T00:00:00+00:00",
    )
    # goal-1 的产物是**祖先那一代**跑出来的，路径至今还带着祖先的 id 与祖先的树。
    paths = {
        "goal-1": "/Users/someone/Owli-别的树/var/runs"
                  f"/{ANCESTOR_ID}/goals/goal-1/agent-1.md",
        "goal-2": str(runs_root / SOURCE_ID / "goals" / "goal-2" / "agent-2.md"),
        "goal-3": FOREIGN_PATH,
    }
    connection = sqlite3.connect(database)
    for goal_id, path in paths.items():
        connection.execute(
            "UPDATE chapter_progress SET status = 'done', attempts = 1,"
            " actual_output_path = ?, actual_count = 3"
            " WHERE research_id = ? AND goal_id = ?",
            (path, SOURCE_ID, goal_id),
        )
    connection.commit()
    connection.close()
    for number in (1, 2, 3):
        directory = runs_root / SOURCE_ID / "goals" / f"goal-{number}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"agent-{number}.md").write_text(
            f"goal-{number} 上一轮产物", encoding="utf-8"
        )


def _import(tmp_path: Path):
    from app.replay.import_research import import_research
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs)
    store = Store(database)
    imported = import_research(
        store=store,
        source_database=database,
        source_runs=runs,
        source_research_id=SOURCE_ID,
        runs_root=runs,
        now_iso="2026-09-18T01:00:00+00:00",
    )
    return store, runs, imported


def _paths(store, research_id: str) -> dict[str, str | None]:
    return {
        row["goal_id"]: row["actual_output_path"]
        for row in store.list_chapters(research_id)
    }


def test_导入把账本路径改到新树新id(tmp_path: Path) -> None:
    store, runs, imported = _import(tmp_path)
    new_root = runs / imported.research_id
    paths = _paths(store, imported.research_id)

    # 祖先那一代的路径也要跟过来：只认直接源 id 的话，这一条永远改不动。
    assert paths["goal-1"] == str(new_root / "goals" / "goal-1" / "agent-1.md"), (
        f"带祖先 id 的路径没跟到新树：{paths['goal-1']}"
    )
    assert paths["goal-2"] == str(new_root / "goals" / "goal-2" / "agent-2.md")
    # 改完得指得到真东西，不能指向一个不存在的地方
    assert Path(paths["goal-1"]).is_file()
    assert Path(paths["goal-2"]).is_file()
    # 源那一行一个字不动
    assert _paths(store, SOURCE_ID)["goal-1"].endswith(
        f"/{ANCESTOR_ID}/goals/goal-1/agent-1.md"
    )


def test_认不出marker的路径原样保留(tmp_path: Path) -> None:
    store, _runs, imported = _import(tmp_path)
    assert _paths(store, imported.research_id)["goal-3"] == FOREIGN_PATH


def test_溯源链断在库外也拿得到祖先id(tmp_path: Path) -> None:
    from app.replay.import_research import _source_lineage

    database = tmp_path / "owli.db"
    _seed(database, tmp_path / "runs")
    # 祖先那一行不在本库里：走不动下一跳，但它的 id 已经从子代的 replay_of 里拿到了。
    assert _source_lineage(database, SOURCE_ID) == (SOURCE_ID, ANCESTOR_ID)


def test_跨章互证看得见上游产物(tmp_path: Path) -> None:
    """验到闸门后面：不是「路径变了」，是消费侧真的认得出上游那份产物。"""

    from app.orchestrator.sectioning import (
        _ledger_inputs, _merge_declared_done_inputs,
    )

    store, runs, imported = _import(tmp_path)
    rows = store.list_chapters(imported.research_id)
    inputs = _merge_declared_done_inputs(
        _ledger_inputs(rows, "goal-2"),
        rows,
        [{"path": "goals/goal-1/agent-1.md"}],
        research_root=runs / imported.research_id,
    )
    assert [item["goal_id"] for item in inputs["done"]].count("goal-1") == 1, (
        "章级声明的上游产物没被认出来，跨 goal 证据在新研究里就是不可见的"
    )


def test_沙盒那条路不传源id时行为不变(tmp_path: Path) -> None:
    """§RD-1 验过的沙盒用法逐字不变：不给源 id，就只认自己那一个 marker。"""

    from app.replay.sandbox import _rebase_ledger_paths

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs)
    changed = _rebase_ledger_paths(database, SOURCE_ID, tmp_path / "ws" / "runs")
    connection = sqlite3.connect(database)
    kept = dict(connection.execute(
        "SELECT goal_id, actual_output_path FROM chapter_progress"
        " WHERE research_id = ?", (SOURCE_ID,),
    ).fetchall())
    connection.close()
    assert changed == 1, "只该认出自己那一条"
    assert kept["goal-2"] == str(
        (tmp_path / "ws" / "runs").resolve() / SOURCE_ID
        / "goals" / "goal-2" / "agent-2.md"
    )
    assert kept["goal-1"].endswith(f"/{ANCESTOR_ID}/goals/goal-1/agent-1.md")
    assert kept["goal-3"] == FOREIGN_PATH


def test_导入的溯源里记着源id(tmp_path: Path) -> None:
    store, _runs, imported = _import(tmp_path)
    extra = store.get_report(imported.research_id)["extra"]
    if isinstance(extra, str):
        extra = json.loads(extra)
    assert extra["replay_of"] == SOURCE_ID
