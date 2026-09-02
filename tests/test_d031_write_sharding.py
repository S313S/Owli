from __future__ import annotations

from pathlib import Path


def _item(index: int, excerpt: str = "正文") -> dict:
    return {
        "citation": f"[S{index:02d}]",
        "permalink": f"https://example.com/{index}",
        "title": f"来源 {index}",
        "content_excerpt": excerpt,
        "platform": "xhs",
        "goal_id": "goal-1",
    }


def test_d031_池按条数切片_30条切3片():
    from app.orchestrator.sectioning import write_shard_sizes

    assert write_shard_sizes([_item(i) for i in range(1, 31)]) == [10, 10, 10]


def test_d031_池不超过一片时走原路():
    """≤10 条 = 一片 = 与分片前逐字一致，这是本包的回归锚。"""
    from app.orchestrator.sectioning import write_shard_sizes

    assert write_shard_sizes([_item(i) for i in range(1, 10)]) == [9]
    assert write_shard_sizes([_item(1)]) == [1]
    assert write_shard_sizes([]) == []


def test_d031_重条目按字节先封片():
    from app.orchestrator.sectioning import write_shard_sizes

    heavy = [_item(i, "正" * 900) for i in range(1, 7)]
    sizes = write_shard_sizes(heavy)
    assert sizes == [2, 2, 2], sizes
    # 单条就超预算也不丢条：自成一片。
    assert write_shard_sizes([_item(1, "正" * 5000)]) == [1]


def test_d031_片数上限把多出来的条目并进最后一片():
    from app.orchestrator.sectioning import WRITE_SHARD_MAX, write_shard_sizes

    sizes = write_shard_sizes([_item(i) for i in range(1, 61)])
    assert len(sizes) == WRITE_SHARD_MAX
    assert sum(sizes) == 60
    assert sizes == [10, 10, 10, 30]


def test_d031_片产物路径不是声明产物路径():
    from app.orchestrator.sectioning import write_shard_path

    assert write_shard_path(Path("/x/report/sec-1.md"), 2) == Path(
        "/x/report/sec-1.part.2.md"
    )


def test_d031_片合并_单结论单信息源且按链接去重():
    from app.report.markdown import merge_section_shards

    shard_1 = (
        "## 结论\n\n- 甲结论 [S03]\n\n"
        "## 信息源\n\n- [S03] [来源三](https://example.com/3)\n"
    )
    shard_2 = (
        "## 结论\n\n- 乙结论 [S03][S07]\n\n"
        "## 信息源\n\n- [S03] [来源三](https://example.com/3)\n"
        "- [S07] [来源七](https://example.com/7)\n"
    )
    merged = merge_section_shards(
        [shard_1, shard_2],
        citation_numbers={"https://example.com/3": 3, "https://example.com/7": 7},
    )
    assert merged.count("## 结论") == 1
    assert merged.count("## 信息源") == 1
    # 两片都引了 S03，信息源只留一条——重复条目会让 source_citations 直接抛。
    assert merged.count("[来源三](https://example.com/3)") == 1
    assert "- 甲结论 [S03]" in merged and "- 乙结论 [S03][S07]" in merged
    # 角标是证据的全局属性，合并不重排。
    assert "[S07]" in merged
    # 片级合并产出的是节不是整卷：不写缺失清单。
    assert "缺失清单" not in merged


def test_d031_片合并_解析得出的角标与信息源一一对上():
    from app.report.markdown import merge_section_shards, source_citations

    shards = [
        "## 结论\n\n- 甲 [S01]\n\n## 信息源\n\n- [S01] [一](https://example.com/1)\n",
        "## 结论\n\n- 乙 [S02]\n\n## 信息源\n\n- [S02] [二](https://example.com/2)\n",
    ]
    merged = merge_section_shards(shards)
    assert source_citations(merged) == {
        "https://example.com/1": 1, "https://example.com/2": 2,
    }


def test_d031_片合并_保留结论信息源之外的正文():
    from app.report.markdown import merge_section_shards

    shards = [
        "# 节标题\n\n## 证据缺口\n\n- 未覆盖 X\n\n"
        "## 结论\n\n- 甲 [S01]\n\n## 信息源\n\n- [S01] [一](https://example.com/1)\n",
        "## 结论\n\n- 乙 [S02]\n\n## 信息源\n\n- [S02] [二](https://example.com/2)\n",
    ]
    merged = merge_section_shards(shards)
    # 证据缺口只有第 1 片写，合并后仍在结论之前。
    assert merged.index("## 证据缺口") < merged.index("## 结论")
    assert "# 节标题" in merged
