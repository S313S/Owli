"""§QUOTE-1 二班货 1：切批的字节顶。

**为什么非要有这道顶**：原来只封条数。真行实测单条 172–4,322 字节、最胖是中位的
13.3 倍，于是同样「25 条」可能 4.3 KB、也可能 108 KB——一批要跑多久差一个数量级，
而本机代理会随机掐断长流（引擎原话 `The socket connection was closed unexpectedly`），
**跑得越久越躲不过**。隔壁 `write_shard_sizes` 的 §D-034 已经为「只按条数切」付过学费。
"""

from __future__ import annotations

from app.reliability.coding import (
    CODING_BATCH_BYTES, CODING_BATCH_ITEMS, coding_batch_sizes, engine_input,
)

import json


def _row(row_id: str, text: str) -> dict:
    return {"id": row_id, "platform": "xhs", "kind": "post",
            "title": "t", "content_excerpt": text, "published_at": None}


def _weight(item) -> int:
    return len(json.dumps(engine_input(item), ensure_ascii=False).encode("utf-8"))


def test_字节顶会在条数顶之前封批() -> None:
    """五条胖行——条数顶还没到（5），字节顶先到，所以不该切成一批 5 条。"""
    fat = [_row(f"f{i}", "啊" * 800) for i in range(5)]
    sizes = coding_batch_sizes(fat)
    assert sum(sizes) == 5, "一条都不许丢"
    assert max(sizes) < CODING_BATCH_ITEMS, "字节顶没起作用的话这里会是 5"
    start = 0
    for n in sizes:
        used = sum(_weight(x) for x in fat[start:start + n])
        assert n == 1 or used <= CODING_BATCH_BYTES, "多条批不许超字节顶"
        start += n


def test_单条自己就超顶时自成一批而不是被丢掉() -> None:
    """⛔ 丢一条证据是内容错误，比慢一点坏得多。"""
    rows = [_row("small-1", "短"), _row("huge", "啊" * 5000), _row("small-2", "短")]
    sizes = coding_batch_sizes(rows)
    assert sum(sizes) == 3, "胖条不许被丢"
    start = 0
    picked = []
    for n in sizes:
        picked.append([r["id"] for r in rows[start:start + n]])
        start += n
    assert ["huge"] in picked, "超顶那条要自成一批"


def test_切批一条不丢一条不重且不打乱原序() -> None:
    rows = [_row(f"r{i}", "内容" * (i % 40)) for i in range(60)]
    sizes = coding_batch_sizes(rows)
    start = 0
    rebuilt = []
    for n in sizes:
        rebuilt += rows[start:start + n]
        start += n
    assert [r["id"] for r in rebuilt] == [r["id"] for r in rows]
    assert len(rebuilt) == len(rows)


def test_空表不炸() -> None:
    assert coding_batch_sizes([]) == []
