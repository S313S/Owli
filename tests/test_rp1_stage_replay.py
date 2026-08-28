"""§RP-1 阶段重放：以跑过的研究为底，只重跑出问题的那一段。

两件事分开验：

- **沙盒**（脚本级重放用）：库与产物各复制一份再跑，底料原件全程只读。
  隔离做在文件层不做在主键层，`reports.id` / `evidence.report_id` 一个字不改。
- **导入**（运行时级入口用）：源那一行是要对照的基线，且它可能已经在
  `_schedulers` 里，就地起跑会被 `_claim_execution` 挡下；所以重放**新建一个
  research**，源 research 一个字不动，「旧那套谁来停」的答案是不停也不换。

`evidence` 两个唯一键 `UNIQUE(report_id, permalink)` 与
`UNIQUE(report_id, platform, platform_item_id)` **都带 report_id 作用域**，
换了 report_id 就撞不上源那批行——D-015「upsert 只覆盖一个唯一键」在这里不成立，
但这条必须有用例钉住，否则将来谁把唯一键改成不带 report_id 就会静默改写底料。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from functools import wraps
from pathlib import Path

import httpx

from plan_factory import make_plan_dict

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
SOURCE_ID = "r-01JXOWLI0000000000TEST00"


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _seed(database: Path, runs_root: Path, *, statuses: dict[str, str]) -> None:
    """造一份「跑过的研究」：计划快照 + 证据 + 章账本 + 产物目录。"""

    from app.store.dao import Store
    from app.adapters.selfcheck import initialize_and_check

    initialize_and_check(database, SCHEMA_PATH)
    store = Store(database)
    snapshot = make_plan_dict()
    store.create_report(
        id=SOURCE_ID,
        title=snapshot["title"],
        research_question=snapshot["research_question"],
        use_case=snapshot["use_case"],
        status="completed",
        created_at="2026-08-28T00:00:00+00:00",
        plan_snapshot=snapshot,
    )
    store.add_evidence_batch([
        {
            "id": f"ev-seed-{index}",
            "report_id": SOURCE_ID,
            "goal_id": "goal-1",
            "platform": "xhs",
            "platform_item_id": f"item-{index}",
            "permalink": f"https://www.xiaohongshu.com/explore/{index}",
            "fetched_at": "2026-08-28T00:00:00+00:00",
        }
        for index in range(3)
    ])
    store.ensure_chapters(
        SOURCE_ID,
        [{"goal_id": f"goal-{n}", "chapter_id": "ch-1"} for n in (1, 2, 3)],
        updated_at="2026-08-28T00:00:00+00:00",
    )
    connection = sqlite3.connect(database)
    for goal_id, status in statuses.items():
        connection.execute(
            "UPDATE chapter_progress SET status = ?, attempts = 2, reason = ?"
            " WHERE research_id = ? AND goal_id = ?",
            (status, None if status == "done" else "timeout", SOURCE_ID, goal_id),
        )
    connection.commit()
    connection.close()
    for number in (1, 2, 3):
        directory = runs_root / SOURCE_ID / "goals" / f"goal-{number}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"agent-{number}.md").write_text(
            f"goal-{number} 上一轮产物", encoding="utf-8"
        )


def _fingerprint(database: Path, runs: Path):
    from app.replay.sandbox import fingerprint

    return fingerprint(database, runs)


def test_沙盒里怎么写底料原件都不变(tmp_path: Path) -> None:
    from app.replay.sandbox import open_sandbox

    database = tmp_path / "source.db"
    runs = tmp_path / "runs"
    _seed(database, runs, statuses={"goal-1": "done", "goal-2": "missing"})
    sandbox = open_sandbox(
        source_database=database,
        source_runs=runs,
        research_id=SOURCE_ID,
        workspace=tmp_path / "ws",
    )

    # 在沙盒里大改一通：库改状态、产物改内容、再加一个文件。
    connection = sqlite3.connect(sandbox.database)
    connection.execute("UPDATE reports SET status = 'running'")
    connection.commit()
    connection.close()
    artifact = sandbox.runs_root / SOURCE_ID / "goals" / "goal-1" / "agent-1.md"
    artifact.write_text("沙盒里改过了", encoding="utf-8")
    (sandbox.runs_root / SOURCE_ID / "新文件.md").write_text("x", encoding="utf-8")

    assert sandbox.verify_source_untouched() == sandbox.source_fingerprint
    assert _fingerprint(database, runs / SOURCE_ID) == sandbox.source_fingerprint
    # 沙盒确实拿到了底料，不是空目录里的平凡真
    assert artifact.read_text(encoding="utf-8") == "沙盒里改过了"
    assert (runs / SOURCE_ID / "goals" / "goal-1" / "agent-1.md").read_text(
        encoding="utf-8"
    ) == "goal-1 上一轮产物"


def test_导入建的是新研究源那一行一个字不动(tmp_path: Path) -> None:
    from app.replay.import_research import import_research
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs, statuses={"goal-1": "done", "goal-2": "missing"})
    store = Store(database)
    before = _fingerprint(database, runs / SOURCE_ID)

    imported = import_research(
        store=store,
        source_database=database,
        source_runs=runs,
        source_research_id=SOURCE_ID,
        runs_root=runs,
        now_iso="2026-08-29T00:00:00+00:00",
        from_goal="goal-2",
    )

    assert imported.research_id != SOURCE_ID
    source = store.get_report(SOURCE_ID)
    assert source["status"] == "completed", "源那一行被改了状态"
    assert source["plan_snapshot"]["research_id"] == SOURCE_ID
    new = store.get_report(imported.research_id)
    assert new["status"] == "running"
    assert new["plan_snapshot"]["research_id"] == imported.research_id
    assert new["extra"]["replay_of"] == SOURCE_ID
    assert new["extra"]["replay_from_goal"] == "goal-2"
    # 产物目录整份复制过来，源目录还在
    assert (imported.runs_dir / "goals" / "goal-1" / "agent-1.md").is_file()
    # 源产物目录逐字节不变；库文件本身当然变了（新研究的行就写在同一个库里），
    # 所以这里量的是**源的那一份**，不是整个库文件。
    assert _fingerprint(database, runs / SOURCE_ID).runs == before.runs


def test_同库导入证据换新主键且撞不上源那批唯一键(tmp_path: Path) -> None:
    """`evidence` 两个唯一键都带 `report_id` 作用域——换 report_id 就不冲突。

    这条钉的是 D-015 的教训：谁要是把唯一键改成不带 report_id，
    同库导入会静默 upsert 到源那批行上，底料当场被改写。
    """

    from app.replay.import_research import import_research
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs, statuses={"goal-1": "done"})
    store = Store(database)

    imported = import_research(
        store=store,
        source_database=database,
        source_runs=runs,
        source_research_id=SOURCE_ID,
        runs_root=runs,
        now_iso="2026-08-29T00:00:00+00:00",
    )

    source_rows = store.list_evidence(SOURCE_ID)
    new_rows = store.list_evidence(imported.research_id)
    assert len(source_rows) == 3 and len(new_rows) == 3, "证据没被完整复制"
    assert imported.evidence_copied == 3
    assert {row["id"] for row in source_rows} == {f"ev-seed-{i}" for i in range(3)}
    assert not ({row["id"] for row in new_rows} & {row["id"] for row in source_rows})
    assert {row["permalink"] for row in new_rows} == {
        row["permalink"] for row in source_rows
    }, "复制过来的 permalink 应当一模一样，靠 report_id 分家"


def test_从指定goal起跑只复位那一段(tmp_path: Path) -> None:
    from app.replay.import_research import import_research
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(
        database, runs,
        statuses={"goal-1": "done", "goal-2": "done", "goal-3": "missing"},
    )
    store = Store(database)

    imported = import_research(
        store=store,
        source_database=database,
        source_runs=runs,
        source_research_id=SOURCE_ID,
        runs_root=runs,
        now_iso="2026-08-29T00:00:00+00:00",
        from_goal="goal-2",
    )

    after = {
        row["goal_id"]: row for row in store.list_chapters(imported.research_id)
    }
    assert after["goal-1"]["status"] == "done", "起跑点之前的章不该被动"
    assert after["goal-1"]["attempts"] == 2, "之前那段的 attempts 要原样搬过来"
    assert after["goal-2"]["status"] == "done", (
        "默认只补没做完的：goal-2 已经 done，不该被复位"
    )
    assert after["goal-3"]["status"] == "pending", "goal-3 没做完，必须复位重跑"
    assert after["goal-3"]["attempts"] == 0 and after["goal-3"]["reason"] is None
    assert imported.chapters_reset == ("goal-3/ch-1",)
    # 复位的章要连上一轮产物一起删掉，否则 file_exists 会被占位正文骗过去
    assert not (imported.runs_dir / "goals" / "goal-3" / "agent-3.md").exists()
    assert (imported.runs_dir / "goals" / "goal-2" / "agent-2.md").is_file()


def test_reset_done_把整段重做(tmp_path: Path) -> None:
    """反向护栏：默认只补没做完的；要整段重做得显式说。"""

    from app.replay.import_research import import_research
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs, statuses={"goal-1": "done", "goal-2": "done"})
    store = Store(database)

    imported = import_research(
        store=store,
        source_database=database,
        source_runs=runs,
        source_research_id=SOURCE_ID,
        runs_root=runs,
        now_iso="2026-08-29T00:00:00+00:00",
        from_goal="goal-2",
        reset_done=True,
    )

    after = {row["goal_id"]: row for row in store.list_chapters(imported.research_id)}
    assert after["goal-1"]["status"] == "done"
    assert after["goal-2"]["status"] == "pending", "reset_done 下 done 也要复位"
    assert set(imported.chapters_reset) == {"goal-2/ch-1", "goal-3/ch-1"}


def test_起跑点不在计划里要报错不要建半份研究(tmp_path: Path) -> None:
    from app.replay.import_research import ReplayImportError, import_research
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs, statuses={"goal-1": "done"})
    store = Store(database)

    raised = False
    try:
        import_research(
            store=store,
            source_database=database,
            source_runs=runs,
            source_research_id=SOURCE_ID,
            runs_root=runs,
            now_iso="2026-08-29T00:00:00+00:00",
            from_goal="goal-9",
        )
    except ReplayImportError as error:
        raised = True
        assert "goal-9" in str(error)
    assert raised, "起跑点不存在必须当场报错"
    connection = sqlite3.connect(database)
    count = connection.execute("SELECT count(*) FROM reports").fetchone()[0]
    connection.close()
    assert count == 1, "校验没过就不许留下半份研究"


async def _api(tmp_path: Path):
    from app.api.main import create_app

    application = create_app(
        tmp_path / "owli.db",
        SCHEMA_PATH,
        enable_test_routes=False,
        engine_probe=lambda: {},
        runs_root=tmp_path / "runs",
    )
    return application


def _fake_schedulers(runtime) -> list:
    built: list = []

    class FakeScheduler:
        def __init__(self) -> None:
            self.status = "running"
            self.goal_statuses = {}
            self.agent_statuses = {}
            self.started = 0

        async def start(self) -> None:
            self.started += 1

    def build(plan):
        scheduler = FakeScheduler()
        built.append(scheduler)
        return scheduler

    async def finalize(research_id: str) -> None:
        return None

    runtime._build_scheduler = build  # type: ignore[method-assign]
    runtime._finalize_if_terminal = finalize  # type: ignore[method-assign]
    return built


@async_test
async def test_重放入口走认领闸不产生第二套执行器(tmp_path: Path) -> None:
    """本项目第三条启动路径也必须走 `runtime.start_research` 那道闸。

    绕过去直接写 `_schedulers[rid]` 正是 §D-021 的病根，刚花一整包修掉。
    """

    application = await _api(tmp_path)
    async with application.router.lifespan_context(application):
        runtime = application.state.runtime
        built = _fake_schedulers(runtime)
        _seed(
            Path(runtime.store._database_path),
            runtime.runs_root,
            statuses={"goal-1": "done", "goal-2": "missing"},
        )
        # 只数「起跑入口被调用了几次」还不够——绕过闸直接写 _schedulers 时
        # 造出来的执行器也是一套，计数一样。真正分得开的是 start_research
        # 自己做的那件事：把研究状态推成 running 并发一条 research_update。
        started: list[str] = []
        real_start = runtime.start_research

        async def spy(plan):
            started.append(plan.research_id)
            await real_start(plan)

        runtime.start_research = spy  # type: ignore[method-assign]
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/researches/replay",
                json={"source_research_id": SOURCE_ID, "from_goal": "goal-2"},
                headers={"X-Request-ID": "rp1-replay-1"},
            )
            assert response.status_code == 200, response.text
            for _ in range(20):
                await asyncio.sleep(0)
        data = response.json()["data"]
        new_id = data["research_id"]

        assert new_id != SOURCE_ID
        assert data["replay_of"] == SOURCE_ID
        assert data["evidence_copied"] == 3, "证据没复用，重放就白做了"
        assert data["chapters_reset"] == ["goal-2/ch-1", "goal-3/ch-1"]
        assert started == [new_id], "重放起跑没走 runtime.start_research"
        assert runtime.researches[new_id]["status"] == "running", (
            "状态没被推成 running —— 说明起跑绕过了 start_research 那条路"
        )
        assert len(built) == 1 and built[0].started == 1
        assert runtime.scheduler_for(new_id) is built[0]
        assert runtime.scheduler_for(SOURCE_ID) is None, "源研究不许被起跑"

        # 闸真的在：同一个重放研究再起跑一次，不会再造一套。
        from app.plan.store import load_plan

        await real_start(load_plan(runtime.store, new_id))
        assert len(built) == 1, "重放起跑绕过了 _claim_execution"
