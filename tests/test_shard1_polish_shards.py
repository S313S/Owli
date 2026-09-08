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


# —— 货 3：polish() 的片循环（端到端，引擎打桩）——————————————

#: 背景那段要撑过 MIN_SECTION_BYTES，但**编号列表只能有一份**——
#: 整段重复三遍就成了 12 条发现，正好会撞上 MAX_SHARDS 的封顶（第一次写就踩了）。
SUMMARY_4 = ("背景一段话[S01]。" * 30 + "\n\n关键发现：\n\n"
             + "".join(f"{i}. 【B】第 {i} 条结论句[S01]。\n" for i in range(1, 5))
             + "\n> 本报告结论的把握度为**低**，主要因为样本薄。\n")


class _Scripted:
    """按落点决定写什么：摘要写编号列表，片写带二级标题的正文，其余整节写。"""

    def __init__(self, summary: str = SUMMARY_4, fail_shard: int | None = None,
                 shard_body: str | None = None):
        self.summary, self.fail_shard, self.shard_body = summary, fail_shard, shard_body
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def run(self, task, ctx, on_event=None):
        name = task.output_path.name
        self.calls.append(name)
        self.prompts.append(task.body)
        result = type("R", (), {"succeeded": True, "engine_error": None})()
        if ".shard-" in name:
            index = int(name.rsplit(".shard-", 1)[1].split(".")[0])
            if index == self.fail_shard:
                return result                       # 返回成功但不落盘：假绿那一族
            body = self.shard_body or f"## 第 {index} 条的行动式标题\n\n解读[S01]。"
            task.output_path.write_text(body + "补白。" * 80, encoding="utf-8")
            return result
        text = self.summary if name.startswith("01-") else "正文[S01]。" * 40
        task.output_path.write_text(text, encoding="utf-8")
        return result


def _polish(tmp_path, adapter):
    runs = tmp_path / "runs"
    return asyncio.run(polish_fn()(_Store(), "r-t", runs, WORK,
                                   template="consulting", adapter=adapter)), runs


def polish_fn():
    from app.report.polish.run import polish

    return lambda *a, **k: polish(*a, **k)


def test_摘要四条发现就切四片_合并进正文(tmp_path):
    adapter = _Scripted()
    outcome, runs = _polish(tmp_path, adapter)
    assert outcome["status"] == "ok"
    assert outcome["shards"]["关键发现"] == 4
    assert outcome["shards"]["执行摘要"] == 0, "大纲节不许切"
    shard_calls = [c for c in adapter.calls if ".shard-" in c]
    assert shard_calls == [f"02-关键发现.shard-{i}.md" for i in range(1, 5)]

    section = runs / "r-t" / "goals" / "polished" / "consulting-parts" / "02-关键发现.md"
    assert section.is_file()
    assert section.read_text(encoding="utf-8").count("## 第") == 4
    final = Path(outcome["path"]).read_text(encoding="utf-8")
    for i in range(1, 5):
        assert f"## 第 {i} 条的行动式标题" in final, f"第 {i} 片没进正文"


def test_一片没写成整节就不判done_半份稿不落exports(tmp_path):
    """D-051 降到片级：第 3 片没落盘，整节作废，残缺的合并稿一个字都不许往下走。"""
    outcome, runs = _polish(tmp_path, _Scripted(fail_shard=3))
    assert outcome["status"] == "failed"
    assert outcome["failed_section"] == "关键发现"
    parts_dir = runs / "r-t" / "goals" / "polished" / "consulting-parts"
    assert not (parts_dir / "02-关键发现.md").is_file(), "半份合并稿落盘了"
    assert not Path(outcome["path"]).is_file(), "半份稿进了 exports/"
    assert "关键发现" in outcome["missing_sections"]
    # 前两片写成了、第 4 片根本没起——失败即停，不白烧后面的片。
    assert (parts_dir / "02-关键发现.shard-1.md").is_file()
    assert not (parts_dir / "02-关键发现.shard-4.md").is_file()


def test_摘要读不出编号列表就退回整节写一次(tmp_path):
    """解析失灵最坏回到 09-07 之前的老行为，不是新的失败路径。"""
    adapter = _Scripted(summary="通篇散文没有编号列表[S01]。" * 30)
    outcome, _ = _polish(tmp_path, adapter)
    assert outcome["status"] == "ok"
    assert outcome["shards"]["关键发现"] == 0
    assert not [c for c in adapter.calls if ".shard-" in c]
    assert adapter.calls.count("02-关键发现.md") == 1


def test_片提示词只加边界不加活(tmp_path):
    """§七 第 2 条：每片的活只有「展开这一条」，共用硬规则原样带、一条都不许加。

    兄弟片只给**标题行**不给正文——给了正文提示词按片翻倍，
    正好把分片省下的那点又还回去。
    """
    from app.report.polish.skills import shared_rules

    adapter = _Scripted()
    _polish(tmp_path, adapter)
    shard_prompt = next(b for c, b in zip(adapter.calls, adapter.prompts) if ".shard-1." in c)
    section_prompt = next(b for c, b in zip(adapter.calls, adapter.prompts)
                          if c.startswith("04-"))
    for chunk in (shared_rules(), "# 本模板骨架", "# 信息源池", "# 确定性数据表", "# 工作稿"):
        assert chunk in shard_prompt, "共用区在片提示词里缺了一块"
    assert "**本轮你只写第 1 条**" in shard_prompt
    assert "只展开你这一条，别复述别条" in shard_prompt
    for i in range(1, 5):                      # 四条的标题行都在，作边界
        assert f"{i}. 【B】第 {i} 条结论句[S01]。" in shard_prompt
    # 不加活的量化判据：片提示词不该比整节提示词长出一截。
    assert len(shard_prompt) < len(section_prompt) + 1200


def test_片没引到自带角标就重写一次(tmp_path):
    """片级引用契约（D-052 思路降级）：这条发现自带的角标至少要引到一个。"""
    adapter = _Scripted(shard_body="## 标题\n\n通篇不引角标的解读。")
    outcome, _ = _polish(tmp_path, adapter)
    assert outcome["status"] == "failed"
    assert any("一个自带角标都没引到" in e for e in outcome["errors"])
    # 只赔这一片两次尝试，不是整节重来。
    assert adapter.calls.count("02-关键发现.shard-1.md") == 2
    assert "02-关键发现.shard-2.md" not in adapter.calls


def test_落盘回读闸留在合并之后_中途不误判(tmp_path, monkeypatch):
    """§七 第 5 条：RPT-2 那道「落盘后当场回读 exports/」的闸必须在**合并之后**。

    留在片与片之间会在「片写完、整节还没合并落盘」时误判 failed。这条用例两头都钉：
    ① 分片顺利跑完时，闸照常在收尾开火（把 exports 写掉就该判红）；
    ② 中途那些片写完的时刻不触发它——`fail_shard` 那条用例给的错误是「没引到角标 /
       没写出来」，而不是「正式稿没落到 exports/」。
    """
    real = Path.write_text

    def _skip_exports(self, *args, **kwargs):
        if self.parent.name == "exports" and self.suffix == ".md":
            return 0
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _skip_exports)
    outcome, runs = _polish(tmp_path, _Scripted())
    assert outcome["status"] == "failed"
    assert any("exports" in e for e in outcome["errors"])
    # 四片与合并后的整节都写成了，红只红在搬运这一步。
    assert outcome["shards"]["关键发现"] == 4
    parts_dir = runs / "r-t" / "goals" / "polished" / "consulting-parts"
    assert (parts_dir / "02-关键发现.md").is_file()


def test_中途片失败时不报exports那条错(tmp_path):
    outcome, _ = _polish(tmp_path, _Scripted(fail_shard=2))
    assert outcome["status"] == "failed"
    assert not any("没落到 exports/" in e for e in outcome["errors"]), \
        "闸被挪到了片与片之间，中途误判"


# —— 货 5：节级总上限（沿用 sectioning，片墙钟一个字不改）——————————

class _SlowShards:
    """每片各睡 `per_shard` 秒；用来量节上限，不量适配器自己那份片墙钟。"""

    def __init__(self, per_shard: float, summary: str = SUMMARY_4,
                 per_section: float = 0.0):
        self.per_shard, self.summary = per_shard, summary
        self.per_section = per_section
        self.calls: list[str] = []

    async def run(self, task, ctx, on_event=None):
        name = task.output_path.name
        self.calls.append(name)
        if ".shard-" not in name and self.per_section:
            await asyncio.sleep(self.per_section)
        if ".shard-" in name:
            await asyncio.sleep(self.per_shard)
            index = int(name.rsplit(".shard-", 1)[1].split(".")[0])
            task.output_path.write_text(f"## 第 {index} 条\n\n解读[S01]。" + "补白。" * 80,
                                        encoding="utf-8")
        else:
            text = self.summary if name.startswith("01-") else "正文[S01]。" * 40
            task.output_path.write_text(text, encoding="utf-8")
        return type("R", (), {"succeeded": True, "engine_error": None})()


def test_不分片的节行为逐字不变(tmp_path, monkeypatch):
    """本条最重要：它是这次改动不外溢的唯一保证。

    不分片的节 `deadline=None`，包装器那一支直接 `await adapter.run(...)`，
    一行分支都不多走。拿「摘要读不出编号列表」那条路对照——全篇一节不切，
    调用序列与返回值必须与加节上限之前一模一样。
    """
    import app.report.polish.run as run_mod

    # 桩适配器必须**慢过那个墙钟**，否则这条用例量不出外溢：瞬时返回的桩在
    # 0.05 s 的闹钟下也照样过，尺子就成了摆设（造红时实测过，一开始就是这样）。
    monkeypatch.setattr(run_mod, "SECTION_TIMEOUT_SECONDS", 0.05)
    adapter = _SlowShards(per_shard=0.0, per_section=0.2,
                          summary="通篇散文没有编号列表[S01]。" * 30)
    outcome, _ = _polish(tmp_path, adapter)
    assert outcome["status"] == "ok", "不分片的节被节上限误伤了"
    assert all(v == 0 for v in outcome["shards"].values())
    assert not [c for c in adapter.calls if ".shard-" in c]
    assert adapter.calls == ["01-执行摘要.md", "02-关键发现.md", "03-论据与数据.md",
                            "04-对不同读者的含义.md", "05-附录.md"]


def test_节上限到点整节判红_死因是墙钟不是引擎崩(tmp_path, monkeypatch):
    """造红：`SectionWallClockExpired` 继承 TimeoutError 即 Exception，

    接在兜底 `except Exception` 后面的话，死因会串成「引擎进程异常退出」——
    这条用例钉的就是「到点时死因必须写墙钟」。
    """
    import app.report.polish.run as run_mod

    monkeypatch.setattr(run_mod, "SECTION_TIMEOUT_SECONDS", 0.05)
    outcome, runs = _polish(tmp_path, _SlowShards(per_shard=0.5))
    assert outcome["status"] == "failed"
    assert outcome["failed_section"] == "关键发现"
    detail = "；".join(outcome["errors"])
    assert "总墙钟到点" in detail and "不是引擎崩" in detail, detail
    assert "引擎进程异常退出" not in detail, "死因串成了引擎崩——兜底接在前面了"
    # D-051 仍成立：半份合并稿不落盘、不进 exports/。
    parts_dir = runs / "r-t" / "goals" / "polished" / "consulting-parts"
    assert not (parts_dir / "02-关键发现.md").is_file()
    assert not Path(outcome["path"]).is_file()


def test_第一片跑久不饿死后面的片_夹的是上界不是共用(tmp_path, monkeypatch):
    """隔壁 `sectioning.py:2043` 否掉的正是「共用」：

    「共用的话第 1 片跑掉 221 s，剩下三片分 109 s，必全灭」。这条用例是那句话的
    可执行版本——节预算 = 片数 × 墙钟，每片各睡 0.6 个墙钟：**四片全写成**。
    要是把节上限做成了「几片共用一个墙钟」，第 2 片就该死在这里。
    """
    import app.report.polish.run as run_mod

    monkeypatch.setattr(run_mod, "SECTION_TIMEOUT_SECONDS", 0.30)
    adapter = _SlowShards(per_shard=0.18)           # 0.18 = 0.6 个墙钟
    outcome, _ = _polish(tmp_path, adapter)
    assert outcome["status"] == "ok", "后面的片被饿死了——节上限做成了共用"
    assert outcome["shards"]["关键发现"] == 4
    assert len([c for c in adapter.calls if ".shard-" in c]) == 4
    final = Path(outcome["path"]).read_text(encoding="utf-8")
    assert all(f"## 第 {i} 条" in final for i in range(1, 5))


def test_节上限封住重试把总时长乘出去(tmp_path, monkeypatch):
    """没有它：一节最坏 = 片数 × MAX_ATTEMPTS × 墙钟；有了它 = 片数 × 墙钟。"""
    import time

    import app.report.polish.run as run_mod

    monkeypatch.setattr(run_mod, "SECTION_TIMEOUT_SECONDS", 0.20)
    budget = 4 * 0.20
    started = time.monotonic()
    # 每片要睡 5 秒——单靠片自己是停不下来的，只有节上限拦得住。
    outcome, _ = _polish(tmp_path, _SlowShards(per_shard=5.0))
    elapsed = time.monotonic() - started
    assert outcome["status"] == "failed"
    assert elapsed < budget + 1.0, f"节上限没封住：{elapsed:.2f}s"
    assert elapsed < 5.0, "连第一片自己那 5 秒都没拦住"


# —— 起跑前两条硬断言（2026-09-08 用户拍）——————————————————

class _StoreMarks(_Store):
    """可控 citation_no 的库替身：用来喂「号码对不上」和「等级查不到」。"""

    def __init__(self, marks=(1,), grades=("A",)):
        self._marks, self._grades = marks, grades

    def list_evidence(self, rid):
        return [{"id": f"ev-{m}", "platform": "xhs", "kind": "post", "citation_no": m,
                 "title": "帖", "content_excerpt": "豆包好用", "grade": g,
                 "published_at": None, "extra": "{}"}
                for m, g in zip(self._marks, self._grades)]


def _preflight(store, sources):
    from app.report.polish.run import citation_preflight

    return citation_preflight(store, "r-t", {"sources": sources})


def test_号码对不上就判红_并说清多半是rescore跑在别的库上():
    """昨夜真出过：工作稿文件 S01~S33、库里 S49~S90，等级静默全空。"""
    problems = _preflight(_StoreMarks(marks=(49, 90), grades=("A", "B")),
                          [{"mark": "S01", "grade": "A"}, {"mark": "S33", "grade": "B"}])
    assert problems and "不是同一套" in problems[0]
    assert "S01~S33" in problems[0] and "S49~S90" in problems[0]
    assert "rescore" in problems[0], "没说清怎么查，等于只报了个红"


def test_等级查不到就判红_这是挡住写手自己编等级的那道闸():
    """空是缺信息，编是假信息——09-08 那一格五条发现里编错四条。"""
    problems = _preflight(_StoreMarks(marks=(1, 2), grades=("A", "B")),
                          [{"mark": "S01", "grade": None}, {"mark": "S02", "grade": None}])
    assert any("查不到等级" in p for p in problems)
    assert any("会自己编等级" in p for p in problems)


def test_号码对得上且等级齐全就放行():
    assert _preflight(_StoreMarks(marks=(1, 2), grades=("A", "B")),
                      [{"mark": "S01", "grade": "A"}, {"mark": "S02", "grade": "B"}]) == []


def test_断言不过时一次引擎都不付(tmp_path):
    """判据是「不许起写手」，不是「跑完再报错」——要验到零调用。"""
    import app.report.polish.run as run_mod

    adapter = _Scripted()
    # 让库里的号码与工作稿文件对不上：夹具工作稿只有 [S01]，库里给 S49。
    class _Bad(_Store):
        def list_evidence(self, rid):
            return [{"id": "ev-49", "platform": "xhs", "kind": "post", "citation_no": 49,
                     "title": "帖", "content_excerpt": "豆包好用", "grade": "A",
                     "published_at": None, "extra": "{}"}]

    outcome = asyncio.run(run_mod.polish(_Bad(), "r-t", tmp_path / "runs", WORK,
                                         template="consulting", adapter=adapter))
    assert outcome["status"] == "failed"
    assert outcome["attempts"] == 0
    assert adapter.calls == [], "断言没拦住，写手已经起跑了"


# —— §POOL-1 丁′：附录里机械的三件下放给程序 ————————————————

def test_缺失清单由程序出表_机器reason翻成人话():
    from app.report.polish.run import missing_table

    md = missing_table([
        {"goal_id": "goal-1", "chapter_id": "ch-1", "reason": "timeout"},
        {"goal_id": "goal-2", "chapter_id": "ch-3", "reason": "empty_result"},
    ])
    assert md.count("|") >= 8
    assert "采集超时没跑完" in md and "没采到任何内容" in md
    # SKILL 第 7 条明写「不要照抄 goal-2/ch-3 empty_result 这种」——**禁的是整个串**。
    # 2026-09-09 那一轮我把它读成「只禁 reason」，于是 goal-1/ch-1 原样印进正文、
    # 尺子①红了 4 处。现在两半都禁。
    assert "empty_result" not in md and "timeout" not in md
    assert "goal-1" not in md and "ch-1" not in md


def test_表外的reason不印机器词_只说原因未记录():
    """**这条 2026-09-09 改了语义**：原先「宁可露出机器词也不瞎猜」——但机器词本身
    就是尺子①禁的东西，露出来照样判红。改成统一说「原因未记录」，既不瞎猜也不泄露。
    """
    from app.report.polish.run import missing_table

    md = missing_table([{"goal_id": "g", "chapter_id": "c", "reason": "some_new_reason"}])
    assert "some_new_reason" not in md and "原因未记录" in md


def test_没有缺失时说清是没有_不出空表():
    from app.report.polish.run import missing_table

    md = missing_table([])
    assert "没有缺失" in md and "|" not in md, "空表会被读成「这个维度没人讨论」"


def test_各表口径条数与表数一致_basis原样列():
    from app.report.polish.run import basis_table

    md = basis_table({"platform_mix": {"title": "各平台对照", "basis": "按 platform 分组计数。"},
                      "grade_mix": {"title": "等级分布", "basis": "五维合计定档。"}})
    assert md.count("\n|") == 4, "表头 2 行 + 2 张表"
    assert "按 platform 分组计数。" in md and "五维合计定档。" in md


def test_两块都拼进末节_且排在信息源清单之前(tmp_path):
    """三件都挂末节：缺失清单 → 各表口径 → 信息源清单。顺序错了读者会先看到一堆链接。"""
    from app.report.polish.run import assemble, missing_table, basis_table, section_paths

    parts = section_paths(tmp_path, "r-t", "consulting", ["执行摘要", "附录"])
    parts[0][1].parent.mkdir(parents=True, exist_ok=True)
    for _, path in parts:
        path.write_text("正文[S01]。", encoding="utf-8")
    out = assemble(parts, [{"mark": "S01", "grade": "A", "title": "帖", "url": "u"}],
                   appendix_blocks=(missing_table([{"goal_id": "g", "chapter_id": "c",
                                                    "reason": "timeout"}]),
                                    basis_table({"t": {"title": "表", "basis": "口径。"}})))
    assert out.index("哪些没采到") < out.index("各表口径") < out.index("信息源清单")


def test_不给两块时行为逐字不变(tmp_path):
    """默认空——不分片、不下放的调用方（含既有用例）一个字都不该受影响。"""
    from app.report.polish.run import assemble, section_paths

    parts = section_paths(tmp_path, "r-t", "consulting", ["附录"])
    parts[0][1].parent.mkdir(parents=True, exist_ok=True)
    parts[0][1].write_text("正文[S01]。", encoding="utf-8")
    assert assemble(parts) == assemble(parts, appendix_blocks=())


# —— §POOL-1 修 13 处内部词（2026-09-09 那一轮，红点全在程序生成的两张表里）——

def _internal_word_hits(text: str) -> list[str]:
    """**复用尺子①那份词表**，不另造一份——两处定义迟早会一处放行一处拦下。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cp", Path(__file__).resolve().parents[1]
        / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    return [m.group(0) for pattern in cp.FORBIDDEN
            for m in re.finditer(pattern, text)]


def test_缺失清单不含任何内部词_且段落名读者看得懂():
    from app.report.polish.run import missing_table

    md = missing_table(
        [{"goal_id": "goal-1", "chapter_id": "ch-1", "reason": "timeout"},
         {"goal_id": "goal-3", "chapter_id": "ch-6/sec-1", "reason": "conclusion_invalid"}],
        [{"goal_id": "goal-1", "objective": "从豆包官网采集产品定位与官方口径。"},
         {"goal_id": "goal-3", "objective": "采集 DeepSeek、Kimi 的产品定位与口碑要点。"}])
    assert _internal_word_hits(md) == [], f"漏了内部词：{_internal_word_hits(md)}"
    assert "从豆包官网采集产品定位" in md, "段落名要让读者认出是哪一段"
    assert "采集超时没跑完" in md


def test_各表口径不含任何内部词():
    """13 处红点里 3 处出在这张表：evidence.platform / reports.extra / lexicon.py。"""
    from app.report.polish.run import basis_table

    md = basis_table({
        "platform_mix": {"title": "各平台对照",
                         "basis": "按 evidence.platform 分组计数；被引 = citation_no 非空。"},
        "crossref_mix": {"title": "交叉验证分布",
                         "basis": "按 reports.extra.claims[].verdict 计数。"},
        "topic_polarity": {"title": "主题极性",
                           "basis": "词表见 app/report/polish/lexicon.py。"}})
    assert _internal_word_hits(md) == [], f"漏了内部词：{_internal_word_hits(md)}"
    assert "证据的平台字段" in md and "引用角标" in md
    # 「词表见 <代码路径>」那半句**整条删掉**，不换成「固定词表」——换了会变成
    # 「词表见 固定词表」这种循环句（调度 09-09 在生成物上抓到）。这条断言原先写的是
    # `"固定词表" in md`，锁的正是被替换掉的那一版做法。
    assert "词表见" not in md and "固定词表" not in md


def test_人话映射长键先换_不被短键切碎():
    from app.report.polish.run import plain_words

    assert plain_words("按 reports.extra.claims[].verdict 计数") == "按 主张的交叉验证结论 计数"


def test_取不到目标原话时退成第N段_仍不含内部词():
    from app.report.polish.run import missing_table

    md = missing_table([{"goal_id": "goal-9", "chapter_id": "ch-1", "reason": "timeout"}])
    assert "第 1 段" in md and _internal_word_hits(md) == []


def test_表外的reason不再漏出机器词():
    """原先「表外原样保留」会把 `some_new_reason` 印进正文——那也是内部词。"""
    from app.report.polish.run import missing_table

    md = missing_table([{"goal_id": "g", "chapter_id": "c", "reason": "some_new_reason"}])
    assert "some_new_reason" not in md and "原因未记录" in md


def test_指向代码路径的半句整条删掉_不做词替换():
    """机械替换会造出「词表见 固定词表」——读者读完等于没读（调度 09-09 在生成物上抓到）。

    两种形态分开处理：带括号的删括号、句号留给前一句；不带括号的连句号一起删。
    一条正则通吃会把「…兜底（词表见 X）。」的句号也吃掉，两句黏成一句。
    """
    from app.report.polish.run import plain_words

    a = plain_words("维度优先取 evidence.extra.dimensions，缺失时用固定词表兜底"
                    "（词表见 tables.DIMENSIONS）。每行角标最多列 3 个。")
    assert "词表见" not in a and "兜底。每行角标" in a, a
    b = plain_words("不得说「X% 用户认为」。词表见 app/report/polish/lexicon.py。")
    assert "词表见" not in b and b.endswith("不得说「X% 用户认为」。"), b


def test_口径里不留英文字段名():
    """尺子词表里没有 grade / basis，但它们对客户就是英文字段名——尺子绿不等于能看。"""
    from app.report.polish.run import basis_table

    md = basis_table({"grade_mix": {"title": "等级分布",
                                    "basis": "grade 是库内生成列（五维合计 ≥8 为 A）。"}})
    assert "grade" not in md and "等级" in md


def test_两张表的表头自己也不带机器词():
    from app.report.polish.run import basis_table, missing_table

    for md in (basis_table({"t": {"title": "表", "basis": "口径。"}}),
               missing_table([{"goal_id": "g", "reason": "timeout"}])):
        assert "basis" not in md and "未经改写" not in md, md
