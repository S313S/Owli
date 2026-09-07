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


# —— 货 2：片数从摘要读出来；合并是按片序拼接 ————————————————

def test_真摘要抽出四条发现各带自己的角标():
    """09-07 15:45 真写成的那份摘要，4 条【B】发现。片数由稿子定，代码里不写死。"""
    from app.report.polish.sharding import parse_findings, should_shard

    findings = parse_findings(SUMMARY)
    assert [f.index for f in findings] == [1, 2, 3, 4]
    assert should_shard(findings)
    assert findings[0].marks == ("S52", "S58")
    assert findings[3].marks == ("S75", "S76", "S83", "S90")
    assert all(f.text.startswith("【B】") for f in findings)
    # 摘要里的 SCQA 段与末尾那句把握度都不带编号，不该被当成发现。
    assert not any("把握度" in f.text for f in findings)


@pytest.mark.parametrize("count", [3, 5])
def test_三条的摘要出三片_五条出五片_都不产生空片(count):
    """写死成 5 会在 3 条的稿上造出两个空片——这条用例就是钉住这一点。"""
    from app.report.polish.sharding import parse_findings

    body = "背景一段话。\n\n关键发现：\n\n" + "".join(
        f"{i}. 【B】第 {i} 条结论[S0{i}]。\n" for i in range(1, count + 1))
    findings = parse_findings(body)
    assert len(findings) == count
    assert all(f.text.strip() and f.marks for f in findings)


def test_真稿按二级标题切四片再合回来逐字节相同(tmp_path):
    """判据是**逐字节**，不是「看着差不多」——合并要是多吞一个空行，正文就变形了。"""
    from app.report.polish.run import offpool_marks
    from app.report.polish.sharding import merge_shards, shard_paths

    original = FINDINGS_SECTION.strip()
    chunks = [c for c in re.split(r"(?m)^(?=## )", original) if c.strip()]
    assert len(chunks) == 4, "夹具应当是 4 个二级标题"

    section = tmp_path / "02-关键发现.md"
    paths = shard_paths(section, len(chunks))
    for path, chunk in zip(paths, chunks):
        path.write_text(chunk.strip() + "\n", encoding="utf-8")
    merged = merge_shards(paths)

    assert merged == original
    pool = frozenset(range(1, 100))
    assert offpool_marks(merged, pool) == offpool_marks(original, pool)
    marks = lambda t: sorted(set(re.findall(r"\[S\d{2,}\]", t)))
    assert marks(merged) == marks(original), "角标集合变了"


def test_片名与分节同目录_所以清理那一网也捞得到(tmp_path):
    from app.report.polish.sharding import shard_paths

    section = tmp_path / "02-关键发现.md"
    names = [p.name for p in shard_paths(section, 3)]
    assert names == ["02-关键发现.shard-1.md", "02-关键发现.shard-2.md",
                     "02-关键发现.shard-3.md"]
    assert all(re.match(r"^[0-9][0-9]-.*\.md$", n) for n in names)


def test_合并只认给定的片路径_旧片不会被扫进来(tmp_path):
    """合并按片序取**清单**，不 glob 目录——旧片就算没被清掉也拼不进来。

    双保险：货 1 那一网清在前，这里的清单取法兜在后。D-041/D-042 的内容错误
    要两道都失守才发生。
    """
    from app.report.polish.sharding import merge_shards, shard_paths

    section = tmp_path / "02-关键发现.md"
    paths = shard_paths(section, 2)
    paths[0].write_text("## 一\n\n本轮[S01]。", encoding="utf-8")
    paths[1].write_text("## 二\n\n本轮[S02]。", encoding="utf-8")
    (tmp_path / "02-关键发现.shard-9.md").write_text("## 旧\n\n上一轮[S99]。", encoding="utf-8")
    merged = merge_shards(paths)
    assert "S99" not in merged and merged.count("## ") == 2
