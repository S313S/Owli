"""§PREFLIGHT-1 八项体检的自验：**每一项都要在已知会红的案例上真跑出红**。

只跑绿的那一半不算过——本项目自造脚本连造过三次假数据
（记忆 `verification-ruler-needs-verifying`：量出异常先怀疑尺子）。
所以每项两条用例：红案例照 09-08 的真实形状造，绿案例是修好之后的当前状态。

⑥ 另有一份**真历史证据**：`73e0aca`（`8c1f9c3` 的前一个 commit，正是少传 codex
那版）上跑同一句 `preflight.py adapter` → `FAIL / ValueError: 缺少引擎适配器：codex`，
当前 base 上跑 → `PASS`。落在 `docs/worklog/2026-09-09-preflight1.md`。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts" / "preflight") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts" / "preflight"))

import preflight  # noqa: E402

from plan_factory import make_plan_dict  # noqa: E402

SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
RID = "r-01JXOWLI0000000000TEST00"


def _args(**kwargs) -> argparse.Namespace:
    """体检的入参袋子：没给的一律 None/空，跟命令行默认值一致。"""

    base = {
        "db": None, "sandbox_db": None, "research": None, "goal": None,
        "chapter": None, "section": [], "mode": None, "budget_minutes": None,
        "keep_section": [], "citation_numbers": None, "shards": None,
        "shard_wallclock": None, "scale": None, "report_text": None,
        "adapter": None, "process": None, "wait_seconds": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def _seed(database: Path, *, wall_clock: int | None = 1800,
          scores: str = "full", citation_nos: list[int] | None = None) -> None:
    """一份最小「跑过的研究」：计划快照（节墙钟烙在里面）+ 证据 + 章账本。"""

    from app.adapters.selfcheck import initialize_and_check
    from app.store.dao import Store

    initialize_and_check(database, SCHEMA_PATH)
    store = Store(database)
    snapshot = make_plan_dict()
    for goal in snapshot["goals"]:
        if wall_clock is None:
            goal["retry_policy"].pop("chapter_deadline_seconds", None)
        else:
            goal["retry_policy"]["chapter_deadline_seconds"] = wall_clock
    store.create_report(
        id=RID, title=snapshot["title"], research_question=snapshot["research_question"],
        use_case=snapshot["use_case"], status="completed",
        created_at="2026-09-08T00:00:00+00:00", plan_snapshot=snapshot,
    )
    nos = citation_nos if citation_nos is not None else []
    rows = []
    for index in range(max(3, len(nos))):
        row = {
            "id": f"ev-{index}", "report_id": RID, "goal_id": "goal-1",
            "platform": "xhs", "platform_item_id": f"item-{index}",
            "permalink": f"https://www.xiaohongshu.com/explore/{index}",
            "fetched_at": "2026-09-08T00:00:00+00:00",
        }
        if index < len(nos):
            row["citation_no"] = nos[index]
        rows.append(row)
    store.add_evidence_batch(rows)
    store.ensure_chapters(
        RID, [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
        updated_at="2026-09-08T00:00:00+00:00",
    )
    if scores == "full":
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE evidence SET rated_by = 'agent:test', score_authority = 2,"
            " score_freshness = 2, score_crossref = 2, score_completeness = 2,"
            " score_independence = 2, extra = ? WHERE report_id = ?",
            (json.dumps({"crossref_verdict": "supported", "crossref_n_clusters": 1,
                         "claim_ids": ["c-1"]}), RID),
        )
        connection.commit()
        connection.close()


# ① 会不会回填 —— 红案例：整跑要回填 20 条却没算进预算（D-043 那轮的形状）
def test_红_会回填却没算进预算(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, scores="none")
    result = preflight.check_backfill(_args(db=str(db), research=RID, mode="fullrun"))
    assert result.status == preflight.FAIL
    assert result.numbers["backfill_rows"] == 3 and result.numbers["batches"] == 1
    assert "未算进预算" in result.headline


def test_绿_算进预算了就过_且重放这条路根本不回填(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, scores="none")
    counted = preflight.check_backfill(
        _args(db=str(db), research=RID, mode="fullrun", budget_minutes=30))
    assert counted.status == preflight.PASS
    # 重放单节走 `runtime._run_task`，不走收尾 → 不回填。
    replay = preflight.check_backfill(
        _args(db=str(db), research=RID, mode="section-replay"))
    assert replay.status == preflight.PASS and "不走收尾" in replay.headline


# ② 未重写节撞号 —— 红案例：09-08 那份工作稿里 [S07] 指两条不同的源
def _keep_section(path: Path, marks: dict[int, str]) -> Path:
    body = ["## 小节", "正文引用 " + " ".join(f"[S{n:02d}]" for n in marks), "",
            "### 信息源"]
    # 照 `render_source_list` 的真实格式，别自造——格式一差，角标就读成 0 条。
    body += [f"- [S{n:02d}] [标题]({url}) · fetched_at=2026-09-08T00:00:00+00:00"
             for n, url in marks.items()]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def test_红_同一个角标在两节里指两条不同的源(tmp_path: Path) -> None:
    keep = _keep_section(tmp_path / "sec-3.md", {7: "https://a.example/1"})
    numbers = tmp_path / "nos.json"
    numbers.write_text(json.dumps({"https://b.example/2": 7}), encoding="utf-8")
    result = preflight.check_citation_collision(
        _args(keep_section=[str(keep)], citation_numbers=str(numbers)))
    assert result.status == preflight.FAIL
    assert result.numbers["clashes"][0]["mark"] == "[S07]"


def test_绿_同号同源不算撞(tmp_path: Path) -> None:
    keep = _keep_section(tmp_path / "sec-3.md", {7: "https://a.example/1"})
    numbers = tmp_path / "nos.json"
    numbers.write_text(json.dumps({"https://a.example/1": 7,
                                   "https://b.example/2": 8}), encoding="utf-8")
    result = preflight.check_citation_collision(
        _args(keep_section=[str(keep)], citation_numbers=str(numbers)))
    assert result.status == preflight.PASS


# ③ 时间预算乘法 —— 红案例：fast 档 330 s 的节预算装不下 4 片 × 300 s（09-08 sec-2）
def test_红_fast档节预算装不下四片(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, wall_clock=330)
    result = preflight.check_time_budget(_args(db=str(db), research=RID, goal="goal-1"))
    assert result.status == preflight.FAIL
    assert result.numbers["节预算 s"] == 330.0
    # 片墙钟 min(300, 330)=300；4×300+30=1230，差 900 s。
    assert result.numbers["片×数+并轨余量 s"] == 1230.0
    assert result.numbers["差额 s"] == 900.0


def test_绿_standard档1800装得下(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, wall_clock=1800)
    result = preflight.check_time_budget(_args(db=str(db), research=RID, goal="goal-1"))
    assert result.status == preflight.PASS and result.numbers["差额 s"] == -570.0


# ④ 开关通不通电 —— 红案例：09-08 15:5x「换 standard 档跑这一节」，节墙钟没跟着变
def test_红_换了档位但计划快照里还是老值(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, wall_clock=330)          # 建计划时是 fast，之后声称拧到了 standard
    result = preflight.check_switch_energized(
        _args(db=str(db), research=RID, goal="goal-1", scale="standard"))
    assert result.status == preflight.FAIL
    assert result.numbers["库里实际生效"] == 330 and "不通电" in result.headline


def test_绿_计划快照里就是这一档的值(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, wall_clock=1800)
    result = preflight.check_switch_energized(
        _args(db=str(db), research=RID, goal="goal-1", scale="standard"))
    assert result.status == preflight.PASS


# ⑤ 两处编号逐条对表 —— 红案例：min/max 相同但中间差一条（只比号段就假绿）
def _report_text(path: Path, marks: list[int]) -> Path:
    lines = ["# 稿", "## 结论", "- 一条结论 " + " ".join(f"[S{n:02d}]" for n in marks),
             "## 信息源"]
    lines += [f"- [S{n:02d}] [标题](https://www.xiaohongshu.com/explore/{n})"
              f" · fetched_at=2026-09-08T00:00:00+00:00" for n in marks]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_红_两处号段相同但逐条差一条(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, citation_nos=[1, 2, 4, 5])          # 库里没有 S03
    text = _report_text(tmp_path / "r.md", [1, 2, 3, 4, 5])
    result = preflight.check_citation_two_sites(
        _args(db=str(db), research=RID, report_text=str(text)))
    assert result.status == preflight.FAIL
    assert result.numbers["min/max 是否相同"] is True     # 号段一样
    assert result.numbers["文件有库没有"] == ["S03"]      # 逐条才看得见


def test_绿_两处逐条一致(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    _seed(db, citation_nos=[1, 2, 3])
    text = _report_text(tmp_path / "r.md", [1, 2, 3])
    result = preflight.check_citation_two_sites(
        _args(db=str(db), research=RID, report_text=str(text)))
    assert result.status == preflight.PASS


# ⑥ 适配器构造冒烟 —— 红案例照抄 73e0aca 的函数体（少传 codex 那版）
def test_红_适配器少传codex就构造不起来(monkeypatch) -> None:
    from datetime import datetime, timezone

    import app.replay.section as section

    def broken(store):
        from app.adapters.claude import ClaudeAdapter
        from app.adapters.routing import RoutedAdapter

        def factory():
            # 73e0aca 原样：adapters 里只有 claude。
            return RoutedAdapter(
                utc_clock=lambda: datetime.now(timezone.utc), source_store=store,
                adapters={"claude": ClaudeAdapter(timeout_seconds=900.0)})

        return factory

    monkeypatch.setattr(section, "_replay_adapter_factory", broken)
    result = preflight.check_adapter_smoke(_args(adapter="replay"))
    assert result.status == preflight.FAIL
    assert "缺少引擎适配器：codex" in result.headline


def test_绿_当前的重放与生产适配器都构造得起来() -> None:
    for which in ("replay", "production"):
        result = preflight.check_adapter_smoke(_args(adapter=which))
        assert result.status == preflight.PASS, (which, result.headline)
        assert result.numbers["engines"] == ["claude", "codex"]


# ⑦ 点名范围 vs 实际范围 —— 红案例：D-057，点名 sec-2 而库里 sec-3 也是 pending
def _seed_sections(database: Path, pending: list[str]) -> None:
    from app.adapters.selfcheck import initialize_and_check
    from app.store.dao import Store

    initialize_and_check(database, SCHEMA_PATH)
    store = Store(database)
    snapshot = make_plan_dict()
    store.create_report(
        id=RID, title=snapshot["title"], research_question=snapshot["research_question"],
        use_case=snapshot["use_case"], status="running",
        created_at="2026-09-08T00:00:00+00:00", plan_snapshot=snapshot)
    sections = ["ch-1/sec-1", "ch-1/sec-2", "ch-1/sec-3"]
    store.ensure_chapters(
        RID, [{"goal_id": "goal-1", "chapter_id": sid} for sid in sections],
        updated_at="2026-09-08T00:00:00+00:00")
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE chapter_progress SET status = 'done' WHERE research_id = ?", (RID,))
    for sid in pending:
        connection.execute(
            "UPDATE chapter_progress SET status = 'pending' WHERE research_id = ?"
            " AND chapter_id = ?", (RID, sid))
    connection.commit()
    connection.close()


def test_红_点名sec2但库里sec3也会被重写(tmp_path: Path) -> None:
    db = tmp_path / "sandbox.db"
    _seed_sections(db, pending=["ch-1/sec-2", "ch-1/sec-3"])
    result = preflight.check_replay_scope(
        _args(sandbox_db=str(db), research=RID, goal="goal-1", section=["sec-2"]))
    assert result.status == preflight.FAIL
    assert result.numbers["多出来会被静默重写的"] == ["sec-3"]


def test_红_点了名却没置pending这一轮不会重写它(tmp_path: Path) -> None:
    db = tmp_path / "sandbox.db"
    _seed_sections(db, pending=[])
    result = preflight.check_replay_scope(
        _args(sandbox_db=str(db), research=RID, goal="goal-1", section=["sec-2"]))
    assert result.status == preflight.FAIL
    assert result.numbers["点了名却没置 pending 的"] == ["sec-2"]


def test_绿_点名范围与库里pending逐项相同(tmp_path: Path) -> None:
    db = tmp_path / "sandbox.db"
    _seed_sections(db, pending=["ch-1/sec-2"])
    result = preflight.check_replay_scope(
        _args(sandbox_db=str(db), research=RID, goal="goal-1", section=["sec-2"]))
    assert result.status == preflight.PASS


# ⑧ 起跑后查存活 —— 红案例：09-08 起跑 106 s 即崩，调度据此转述「还在跑」
def test_红_起跑即死等完就不在了() -> None:
    result = preflight.check_liveness(
        _args(process="owli-preflight-不存在的进程-8f2a", wait_seconds=0.2))
    assert result.status == preflight.FAIL and result.numbers["退出码"] != 0


def test_红_把worktree名当进程名传是坑20(tmp_path: Path) -> None:
    # 进程命令行里没有 worktree 名（那是 cwd 不是参数），拿它做正则只会命中
    # zsh 包装层，那层做完就退 → 立刻误判「长跑结束」。
    result = preflight.check_liveness(_args(process="Owli-rpt1", wait_seconds=0.1))
    assert result.status == preflight.FAIL and "worktree" in result.headline


def test_绿_活着的进程查得到() -> None:
    import subprocess

    marker = "owli-preflight-存活哨-9c1d"
    child = subprocess.Popen(["/bin/sh", "-c", f": {marker}; sleep 5"])
    try:
        result = preflight.check_liveness(_args(process=marker, wait_seconds=0.5))
        assert result.status == preflight.PASS and result.numbers["pid"]
    finally:
        child.kill()
        child.wait()
