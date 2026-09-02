"""§D-034：片数超上限时溢出条目不再全并进末片，改为在 max_shards 片内按字节均摊。

真因见 docs/acceptance/loop/defects/D-034.md：r-f59fdba77cd7 goal-3/ch-5/sec-2 的 30 条池
被切成 5/4/4/17，末片 18179 字节跑 305.6 s 撞 claude.py 300 s 硬顶——双封顶在末片形同虚设。
"""

import json
import math

from app.orchestrator.sectioning import (
    WRITE_SHARD_BYTES,
    WRITE_SHARD_ITEMS,
    WRITE_SHARD_MAX,
    write_shard_sizes,
)


def _item(index: int, text: str = "证据正文") -> dict[str, object]:
    return {
        "citation": f"[S{index:02d}]",
        "permalink": f"https://example.com/p/{index}",
        "content_excerpt": text,
    }


def _weight(item: object) -> int:
    return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))


def _overflow_pool() -> list[dict[str, object]]:
    """30 条各 ~1.5 KB：双封顶按字节切出 8 片 > WRITE_SHARD_MAX=4，必然溢出。"""

    return [_item(i, "正" * 480) for i in range(1, 31)]


def test_溢出条目均摊不并进末片():
    items = _overflow_pool()
    sizes = write_shard_sizes(items)
    cap = math.ceil(len(items) / WRITE_SHARD_MAX)
    assert max(sizes) <= cap, sizes

    weights = [_weight(item) for item in items]
    per_shard: list[int] = []
    offset = 0
    for size in sizes:
        per_shard.append(sum(weights[offset : offset + size]))
        offset += size
    # 任一片不比「最均衡方案」（总字节 / 片数）胖超过单条最大权重。
    even = sum(weights) / WRITE_SHARD_MAX
    assert max(per_shard) <= even + max(weights), (per_shard, even)
    # 末片不再是最胖的那片（旧实现末片 = 池总量 − 前三片）。
    assert per_shard[-1] <= even + max(weights), per_shard


def test_片数不超过max_shards():
    sizes = write_shard_sizes(_overflow_pool())
    assert len(sizes) == WRITE_SHARD_MAX, sizes
    # 溢出时片数恰为上限：既不多切也不少切。
    assert all(size >= 1 for size in sizes), sizes


def test_不丢条_角标连续():
    items = _overflow_pool()
    sizes = write_shard_sizes(items)
    assert sum(sizes) == len(items), sizes
    # 按 sizes 顺序切回去，池原序逐条不变。
    rebuilt: list[object] = []
    offset = 0
    for size in sizes:
        rebuilt.extend(items[offset : offset + size])
        offset += size
    assert rebuilt == items


def test_未溢出时切法不变():
    """guard：没触发上限的池，切法与修前逐字相同（修前修后都必须绿）。"""

    assert write_shard_sizes([]) == []
    assert write_shard_sizes([_item(1)]) == [1]
    assert write_shard_sizes([_item(i) for i in range(1, 10)]) == [9]
    assert write_shard_sizes([_item(i) for i in range(1, 31)]) == [10, 10, 10]
    assert write_shard_sizes([_item(i) for i in range(1, 41)]) == [10, 10, 10, 10]
    assert write_shard_sizes([_item(i, "正" * 900) for i in range(1, 7)]) == [2, 2, 2]
    assert write_shard_sizes([_item(1, "正" * 5000)]) == [1]
    assert (WRITE_SHARD_ITEMS, WRITE_SHARD_BYTES, WRITE_SHARD_MAX) == (10, 6_000, 4)
