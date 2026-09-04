"""§OBS-4：分批章的日志合并、失败章的终态行、进程行不许漏 JSON 信封。

货 1 的读数（夜跑库 17 个 agent 里 4 个评级章两栏全空）就是这三条的由来：
评级章按 `<章>.part.<N>.json` 平铺落盘 → 一章 N 份 transcript → 面板按章名找不到。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.adapters.transcript import TRANSCRIPT_SUFFIX, TranscriptWriter, read_transcript
from app.observability.section_log import (
    PART_STRIDE,
    TERMINAL_SEQ_BASE,
    part_files,
    read_section,
    terminal_progress,
    terminal_records,
    terminal_rows,
)

ROOT = Path(__file__).resolve().parents[1]


class _Task:
    """RATE-3 的评级批次：产物是 goal 根下平铺的 `<章>.part.<N>.json`。"""

    def __init__(self, runs_root: Path, output: Path) -> None:
        self.research_id = "r-obs4"
        self.goal_id = "goal-1"
        self.agent_id = "reliability-audit"
        self.output_path = output
        self.runs_root = runs_root


def _write_batches(tmp_path: Path, count: int, per_batch: int = 3) -> Path:
    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs4" / "goals" / "goal-1"
    for number in range(1, count + 1):
        writer = TranscriptWriter(
            _Task(runs_root, goal / f"reliability-audit.part.{number}.json"),
            engine="Claude",
        )
        for index in range(per_batch):
            writer.append({"batch": number, "index": index})
    return goal / f"reliability-audit{TRANSCRIPT_SUFFIX}"


def test_评级批次各写各的_章名带上了_part_编号(tmp_path: Path) -> None:
    """先钉住现象本身：一章写出 N 份，直连文件根本不存在。"""

    direct = _write_batches(tmp_path, 3)
    assert not direct.is_file()
    assert [path.name for path in part_files(direct)] == [
        f"reliability-audit.part.{n}{TRANSCRIPT_SUFFIX}" for n in (1, 2, 3)
    ]


def test_合并后按批次号排_seq_单调无重复(tmp_path: Path) -> None:
    direct = _write_batches(tmp_path, 12)  # 12 批：文件名排序会把 part.10 排到 part.2 前
    merged = read_section(direct, tail=2000, after_seq=None)
    seqs = [line["seq"] for line in merged["lines"]]
    assert len(seqs) == 36
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert [line["event"]["batch"] for line in merged["lines"]][:4] == [1, 1, 1, 2]
    assert merged["last_seq"] == 12 * PART_STRIDE + 3


def test_tail_尾部优先_after_seq_只回更新的(tmp_path: Path) -> None:
    direct = _write_batches(tmp_path, 5)
    full = read_section(direct, tail=2000, after_seq=None)
    tail = read_section(direct, tail=4, after_seq=None)
    assert len(tail["lines"]) == 4
    assert tail["lines"][-1]["seq"] == full["lines"][-1]["seq"]  # 取的是尾巴不是头
    cut = full["lines"][7]["seq"]
    incremental = read_section(direct, tail=2000, after_seq=cut)
    assert [line["seq"] for line in incremental["lines"]] == [
        line["seq"] for line in full["lines"] if line["seq"] > cut
    ]


def test_同前缀的别的章不许被吃进来(tmp_path: Path) -> None:
    direct = _write_batches(tmp_path, 2)
    goal = direct.parent
    (goal / f"reliability-audit-2.part.1{TRANSCRIPT_SUFFIX}").write_text(
        json.dumps({"ts": 1.0, "seq": 1, "event": "别的章"}) + "\n", encoding="utf-8"
    )
    merged = read_section(direct, tail=2000, after_seq=None)
    assert all(line["event"] != "别的章" for line in merged["lines"])


def test_直连文件在就走原路_与_OBS2_口径逐字节相同(tmp_path: Path) -> None:
    """单文件那一支不许有任何行为变化——它是 §OBS-2 判据锁住的那条。"""

    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs4" / "goals" / "goal-1"
    writer = TranscriptWriter(_Task(runs_root, goal / "cross-validation.json"), engine="Claude")
    for index in range(5):
        writer.append({"index": index})
    direct = goal / f"cross-validation{TRANSCRIPT_SUFFIX}"
    for tail, after in ((2000, None), (2, None), (2000, 3)):
        assert read_section(direct, tail=tail, after_seq=after) == read_transcript(
            direct, tail=tail, after_seq=after
        )


_LEDGER = [
    {"goal_id": "goal-1", "chapter_id": "ch-5", "status": "done", "reason": None,
     "engine": "claude", "attempts": 1, "engine_error": None, "conclusion_error": None,
     "actual_output_path": "", "updated_at": "2026-09-03T16:50:58.679435+00:00"},
    {"goal_id": "goal-1", "chapter_id": "ch-5/sec-1", "status": "missing", "reason": "timeout",
     "engine": "claude", "attempts": 2, "engine_error": "socket closed", "conclusion_error": None,
     "actual_output_path": "", "updated_at": "2026-09-03T19:54:32.348117+00:00"},
    {"goal_id": "goal-1", "chapter_id": "ch-50", "status": "missing", "reason": "timeout",
     "engine": "claude", "attempts": 1, "engine_error": None, "conclusion_error": None,
     "actual_output_path": "", "updated_at": "2026-09-03T19:54:32.348117+00:00"},
    {"goal_id": "goal-2", "chapter_id": "ch-5", "status": "missing", "reason": "empty_result",
     "engine": "claude", "attempts": 1, "engine_error": None, "conclusion_error": None,
     "actual_output_path": "", "updated_at": "2026-09-03T19:54:32.348117+00:00"},
]


def test_终态行只挑本章与章下的节_别的_goal_与前缀相同的章不算() -> None:
    picked = terminal_rows(_LEDGER, goal_id="goal-1", chapter_id="ch-5")
    # 本章 done 不出行；`ch-50` 只是前缀像，不是 `ch-5` 的节；goal-2 同名章更不算
    assert [row["chapter_id"] for row in picked] == ["ch-5/sec-1"]


def test_日志栏的终态行带库行原文_进程栏是人话加原文() -> None:
    rows = terminal_rows(_LEDGER, goal_id="goal-1", chapter_id="ch-5")
    record = terminal_records(rows)[0]
    assert record["seq"] == TERMINAL_SEQ_BASE and record["ts"] > 0
    assert record["event"] == {
        "owli_terminal": True, "chapter_id": "ch-5/sec-1", "status": "missing",
        "reason": "timeout", "attempts": 2, "engine_error": "socket closed",
        "conclusion_error": None,
    }
    line = terminal_progress(rows)[0]
    assert line.kind == "error" and line.stage == "失败"
    assert "本节失败（ch-5/sec-1）" in line.text and "超时" in line.text
    assert "原文：socket closed" in line.text  # 失败行才给原文（OBS-3 货 11）


def test_健康章一行终态都不补() -> None:
    assert terminal_rows(_LEDGER, goal_id="goal-2", chapter_id="ch-9") == []
    assert terminal_records([]) == [] and terminal_progress([]) == []


def test_没有_reason_时也要出行_不许因为没死因就沉默() -> None:
    rows = terminal_rows(_LEDGER, goal_id="goal-1", chapter_id="ch-50")
    assert terminal_progress(rows)[0].text == "本节失败（ch-50）：超时"


def _endpoint(app, suffix: str):
    path = "/api/researches/{research_id}/sections/{goal_id}/{chapter}/" + suffix
    return next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == path)


def _serve(tmp_path: Path):
    """起一份真服务，库里放一份带分批评级章 + missing 终态的计划与账本。"""

    import asyncio

    from app.api.main import create_app
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    direct = _write_batches(tmp_path, 4)
    runs_root = tmp_path / "runs"
    database = tmp_path / "owli.db"
    schema = ROOT / "app" / "store" / "schema.sql"
    initialize_database_if_empty(database, schema)
    store = Store(database)
    from tests.plan_factory import make_agent, make_plan_dict

    plan = make_plan_dict()
    plan["research_id"] = "r-obs4"
    agent = make_agent("reliability-audit", "goal-1")
    agent["output"]["path"] = "goals/goal-1/reliability-audit.json"
    agent["chapter"]["chapter_id"] = "ch-2"
    plan["goals"] = [{**plan["goals"][0], "goal_id": "goal-1", "agents": [agent]}]
    now = "2026-09-04T00:00:00+00:00"
    store.create_report(
        id="r-obs4", title="夜跑复现", research_question="问题",
        created_at=now, status="completed", plan_snapshot=plan,
    )
    store.ensure_chapters(
        "r-obs4", [{"goal_id": "goal-1", "chapter_id": "ch-2"}], updated_at=now
    )
    store.finish_chapter(
        "r-obs4", "goal-1", "ch-2", status="missing", reason="timeout",
        actual_output_path=None, actual_count=None,
        engine_error="socket closed", updated_at=now,
    )
    app = create_app(database, schema, runs_root=runs_root,
                     engine_probe=lambda: {"claude": {"status": "available"}})

    def call(suffix: str, **kwargs):
        async def run():
            async with app.router.lifespan_context(app):
                return await _endpoint(app, suffix)(
                    "r-obs4", "goal-1", "reliability-audit", **kwargs
                )
        return asyncio.run(run())

    assert not direct.is_file()
    return call


def test_分批评级章在面板两栏都拿得到行(tmp_path: Path) -> None:
    """货 1 现象的端到端锁：面板拿 agent_id 来问，直连文件不存在也要出得来。"""

    call = _serve(tmp_path)
    log = call("transcript", tail=2000, after_seq=None)["data"]["lines"]
    progress = call("progress", tail=2000, after_seq=None)["data"]["lines"]
    assert len(log) == 4 * 3 + 1  # 12 行原始事件 + 1 行终态
    assert progress, "分批评级章的进程栏不许为空"


def test_失败章两栏各有一行终态_原文进日志_人话进进程(tmp_path: Path) -> None:
    call = _serve(tmp_path)
    log = call("transcript", tail=2000, after_seq=None)["data"]["lines"]
    terminal = [line for line in log
                if isinstance(line["event"], dict) and line["event"].get("owli_terminal")]
    assert len(terminal) == 1
    assert terminal[0]["event"]["engine_error"] == "socket closed"
    assert terminal[0]["seq"] > log[-2]["seq"]  # 排在所有真实行之后
    errors = [line for line in call("progress", tail=2000, after_seq=None)["data"]["lines"]
              if line["kind"] == "error"]
    assert len(errors) == 1 and "超时" in errors[0]["text"]
    assert "原文：socket closed" in errors[0]["text"]


def test_终态行不计入_last_seq_否则章还在跑时会占掉后续号段(tmp_path: Path) -> None:
    call = _serve(tmp_path)
    body = call("transcript", tail=2000, after_seq=None)["data"]
    assert body["last_seq"] == 4 * PART_STRIDE + 3 < TERMINAL_SEQ_BASE


def test_人话后面跟着围栏信封时只留人话() -> None:
    """真机评级章 20 批里 22 行是这个形态；货 2 之前这些行面板根本看不到。"""

    from app.observability.narrate import narrate_lines

    envelope = (
        "已完成第 1/20 批共 15 条评级。\n\n"
        '```json owli-result\n{"status": "done", "output_path": "/a/b.json"}\n```'
    )
    lines = narrate_lines([
        {"ts": 1.0, "seq": 1, "engine": "Claude", "agent": "reliability-audit",
         "output": "reliability-audit.part.1.json",
         "event": {"content": [{"text": envelope}]}},
    ])
    assert [line.text for line in lines] == ["已完成第 1/20 批共 15 条评级。"]


def test_面板拉全量而不是只拉尾巴() -> None:
    """货 4 的契约：不许再出现「只保留尾部 N 行」的切片。"""

    source = (ROOT / "web" / "src" / "RunPanel.tsx").read_text(encoding="utf-8")
    assert "TAIL_LINES" not in source
    assert "const MAX_LINES = 2000" in source
    assert "slice(-" not in source
    assert "mergeBySeq" in source  # 增量按 seq 去重合并，不靠切尾巴收敛


def test_视图标签必须自己指定字体() -> None:
    """货 5 的契约：原生 button 不继承页面字体，浏览器默认给 Arial（没有中文字形）。"""

    css = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    block = css[css.index(".run-panel-view {"):]
    assert "font-family: inherit;" in block[: block.index("}")]


def test_两栏都要有一条不断的高度链才滚得动() -> None:
    """货 4 的契约：少写一层，内容就撑出去被 overflow:hidden 切掉、没有滚轮。"""

    css = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    for selector in (".ant-tabs-body-holder", ".ant-tabs-body", ".ant-tabs-content"):
        assert selector in css, selector
    for block_start in (".run-panel-lines {", ".run-panel-progress {", ".run-panel-col {"):
        block = css[css.index(block_start):]
        block = block[: block.index("}")]
        assert "min-height: 0;" in block, block_start
