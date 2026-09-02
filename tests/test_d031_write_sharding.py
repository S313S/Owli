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
