"""§SHARD-1：正式稿「关键发现」按摘要条数分片；开跑前清全部旧分节。

零引擎——全部用真稿夹具（`tests/fixtures/rpt1/`）与打桩适配器，一次都不调模型。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rpt1"
SUMMARY = (FIXTURES / "01-执行摘要.md").read_text(encoding="utf-8")
FINDINGS_SECTION = (FIXTURES / "02-关键发现.md").read_text(encoding="utf-8")


class _Store:
    """`collect_inputs` 要的最小库；读者身份留空 = 「不明」，建议节会被改名。"""

    def get_report(self, rid):
        return {"id": rid, "title": "T", "research_question": "q", "plan_snapshot": {},
                "extra": {"claims": []}}

    def list_evidence(self, rid):
        return [{"id": "ev-1", "platform": "xhs", "kind": "post", "citation_no": 1,
                 "title": "帖", "content_excerpt": "豆包好用", "grade": "A",
                 "published_at": None, "extra": "{}"}]


WORK = "# 工作稿\n\n## 信息源\n\n- [S01] [帖](https://e.com/a)\n"


def _parts_dir(runs: Path, rid: str = "r-t", template: str = "consulting") -> Path:
    path = runs / rid / "goals" / "polished" / f"{template}-parts"
    path.mkdir(parents=True, exist_ok=True)
    return path


# —— 货 1：开跑前清全部旧分节/旧分片（D-041/D-042 销账，分片的硬前置）——————

def test_clear_stale_parts_清掉分节与分片但不碰旁产物(tmp_path):
    parts = _parts_dir(tmp_path / "runs")
    for name in ("01-执行摘要.md", "02-关键发现.md", "02-关键发现.shard-1.md",
                 "02-关键发现.shard-2.md", "04-建议.md"):
        (parts / name).write_text("旧", encoding="utf-8")
    keep = parts / ".report-polisher-codex-last-message.json"
    keep.write_text("{}", encoding="utf-8")

    from app.report.polish.run import clear_stale_parts

    removed = clear_stale_parts(tmp_path / "runs", "r-t", "consulting")
    assert removed == ["01-执行摘要.md", "02-关键发现.md", "02-关键发现.shard-1.md",
                       "02-关键发现.shard-2.md", "04-建议.md"]
    assert list(parts.iterdir()) == [keep], "旁产物被误删"


def test_目录不存在时清理不炸(tmp_path):
    from app.report.polish.run import clear_stale_parts

    assert clear_stale_parts(tmp_path / "runs", "r-nope", "consulting") == []


def _stub_adapter(body: str = "正文[S01]。"):
    class _Adapter:
        calls: list[str] = []

        async def run(self, task, ctx, on_event=None):
            self.calls.append(task.output_path.name)
            task.output_path.write_text(body * 40, encoding="utf-8")
            return type("R", (), {"succeeded": True, "engine_error": None})()

    adapter = _Adapter()
    adapter.calls = []
    return adapter


def test_polish_开跑前清掉不在本轮清单里的旧分节(tmp_path):
    """读者「不明」时建议节改名，上一轮那份 `04-建议.md` 落在本轮清单之外。

    只清「当前这一节」的老做法留不住它：它既不会被本轮任何一次尝试碰到，
    又躺在同一个目录里，`missing_sections` 与后来的合并器都会把它算进去。
    """
    from app.report.polish.run import polish, section_paths, sections_for
    from app.report.polish.skills import get_template

    runs = tmp_path / "runs"
    parts_dir = _parts_dir(runs)
    stale = parts_dir / "04-建议.md"
    stale.write_text("上一轮的旧建议[S99]。", encoding="utf-8")
    (parts_dir / "02-关键发现.shard-7.md").write_text("上一轮的旧片[S99]。", encoding="utf-8")

    outcome = asyncio.run(polish(_Store(), "r-t", runs, WORK,
                                 template="consulting", adapter=_stub_adapter()))
    assert outcome["status"] == "ok"
    assert set(outcome["cleared"]) == {"04-建议.md", "02-关键发现.shard-7.md"}
    assert not stale.is_file(), "改名后落在清单外的旧分节没被清掉"
    skill = get_template("consulting")
    written = {p.name for _, p in section_paths(runs, "r-t", skill.name,
                                                sections_for(skill, {}))}
    assert {p.name for p in parts_dir.glob("[0-9][0-9]-*.md")} == written
    assert "S99" not in Path(outcome["path"]).read_text(encoding="utf-8"), "旧片混进了正文"
