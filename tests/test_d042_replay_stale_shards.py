"""§D-042：重放复位不清旧分片，旧片角标按上一轮证据池编号，合并必判 conclusion_invalid。

夜跑现场（docs/worklog/2026-09-04-mvp-night-run.md §六）：补 goal-2/ch-6/sec-1，
5 min 就判死——`_drop_artifact` 只删 `sec-N.md` 与 `.rejected.md`，
`sec-N.part.K.md` 留在盘上；`_run_section_shards` 见到可解析的片信封就
`write_shard_skipped` 复用，而旧 part.4 的角标是 S31–S85（按上一轮 85 条池编的），
本轮池只有 30 条 → 合并出的正文撞证据池唯一引用源契约 → conclusion_invalid
（闭集 reason，章内不再重派）。**凡「原跑留有半截分片」的 missing 节，重放和
§OBS-2「重跑这节」都补不回来。**

两道闸各一组用例：① 复位时连 `part.*` 一起删；② 万一还有漏网的（别的路径留下的
旧片），跳片前校验片内角标 ⊆ 当前池，越界就作废重写并报清。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_d031_write_sharding import _shard_run


# ---------- ① 复位连分片一起删 ----------

def test_复位一节连它的旧分片一起删(tmp_path: Path) -> None:
    from app.replay.import_research import _drop_artifact

    section_root = tmp_path / "goals" / "goal-1" / "report"
    section_root.mkdir(parents=True)
    for name in (
        "sec-1.md", "sec-1.rejected.md",
        "sec-1.part.1.md", "sec-1.part.2.md", "sec-1.part.4.md",
    ):
        (section_root / name).write_text("x", encoding="utf-8")
    # 同目录里别的节：一个字都不许碰
    for name in ("sec-2.md", "sec-2.part.1.md"):
        (section_root / name).write_text("别人的", encoding="utf-8")

    _drop_artifact(tmp_path, Path("goals/goal-1/report/sec-1.md"))

    remaining = sorted(p.name for p in section_root.iterdir())
    assert remaining == ["sec-2.md", "sec-2.part.1.md"], remaining


def test_复位一章不误删同名前缀的节分片(tmp_path: Path) -> None:
    """章产物 `report.md` 复位时，`report/` 目录下的节分片不归它管。"""

    from app.replay.import_research import _drop_artifact

    goal_dir = tmp_path / "goals" / "goal-1"
    (goal_dir / "report").mkdir(parents=True)
    (goal_dir / "report.md").write_text("章产物", encoding="utf-8")
    (goal_dir / "report.part.1.md").write_text("章自己的片", encoding="utf-8")
    (goal_dir / "report" / "sec-1.part.1.md").write_text("节的片", encoding="utf-8")

    _drop_artifact(tmp_path, Path("goals/goal-1/report.md"))

    assert not (goal_dir / "report.md").exists()
    assert not (goal_dir / "report.part.1.md").exists()
    assert (goal_dir / "report" / "sec-1.part.1.md").exists(), "节的片不该被章复位带走"


# ---------- ② 跳片前的角标守卫 ----------

def _stale_seed() -> dict[int, str]:
    """一份「上一轮池」写的片：角标 S31 在本轮 30 条池里根本不存在。"""

    return {
        1: "## 结论\n\n- 上一轮第 1 片 [S31]\n\n"
           "## 信息源\n\n- [S31] [旧来源 31](https://example.com/031)\n",
    }


def test_旧片角标越出当前池就作废重写(tmp_path: Path) -> None:
    result, _, bodies, events, output = _shard_run(
        tmp_path, evidence=30, seed_parts=_stale_seed(),
    )

    stale = [e for e in events if e["type"] == "write_shard_stale"]
    assert len(stale) == 1, "越界片必须被识别出来"
    assert stale[0]["is_error"] is True
    assert stale[0]["data"]["shard"] == 1
    assert stale[0]["data"]["citations"] == ["[S31]"]
    assert stale[0]["data"]["pool_items"] == 30

    # 这一片不许被跳过，必须重写
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == []
    assert "sec-1.part.1.md" in bodies, "越界片没重跑，等于闸没起作用"

    # 合并稿里不再留上一轮的角标 → 不会再撞 evidence_pool_only
    assert result.succeeded is True
    section = json.loads(
        (output.parent / output.stem / "sec-1.md").read_text(encoding="utf-8")
    )
    assert "[S31]" not in section["markdown"]
    assert "上一轮第 1 片" not in section["markdown"]


def test_角标都在池内的旧片照旧跳过(tmp_path: Path) -> None:
    """闸只对越界片开：D-031「只重跑失败片」那条省钱路径不许被误伤。"""

    seeded = {
        1: "## 结论\n\n- 上一轮第 1 片 [S01]\n\n"
           "## 信息源\n\n- [S01] [来源 1](https://example.com/001)\n",
    }
    result, _, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, seed_parts=seeded,
    )

    assert [e for e in events if e["type"] == "write_shard_stale"] == []
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1]
    assert "sec-1.part.1.md" not in bodies, "池内的片被白重跑了一遍"
    assert result.succeeded is True
