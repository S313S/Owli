"""§D-064：采集卡为凑多个叫法反复调源，把 fast 章墙钟烧光。

底料 r-50600e09f7dd goal-1 小红书卡：任务文本写「豆包 / Doubao / 豆包AI / 字节豆包」，
适配层（§ENT-1 货 4）不论 query 写什么都换成「豆包」「豆包AI」两个词分别检索。
模型看不见这一步，按剩下的叫法一个个重调（+23 s / +167 s / +240 s 三次），
每次又被换回同样两个词，300 s 引擎超时 → 两轮 timeout → missing。

修法（用户 09-13 拍 D）：执行期拼任务文本时告诉模型「一次调用实际搜了哪几个词、
其余叫法不再调、写进缺口并注明系统检索词上限 2」。本文件锁三件事：
1. 采集卡带提示、非采集卡 / 多源卡 / 没实体的卡不带；
2. **提示里的词与源工具实际搜的词同源**（接缝用例：真走 `SourceToolAdapter.call`）；
3. 没有库时适配层不换词，此时不能写提示（否则提示说谎）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.adapters.source_mcp import SourceToolAdapter
from app.orchestrator.runtime import RuntimeCoordinator

RESEARCH = "r-d064"
ENTITY_NAMES = {"zh": "豆包", "en": "Doubao", "aliases": ["Doubao", "doubao", "豆包AI", "字节豆包"]}


class _SnapshotStore:
    """只够 `_locale_queries` 读计划快照的假库；`_database_path` 模拟真库在场。"""

    def __init__(self, database_path: str | None = "/tmp/owli-d064.db") -> None:
        self._database_path = database_path
        self.snapshot = {
            "market_profile": "cn_product",
            "entities": [{"id": "豆包", "canonical": "豆包", "names": ENTITY_NAMES}],
            "goals": [{
                "goal_id": "goal-1",
                "agents": [
                    {"agent_id": "data-collection", "entity": "豆包"},
                    {"agent_id": "data-collection-2", "entity": "豆包"},
                    {"agent_id": "data-collection-9", "entity": None},
                ],
            }],
        }

    def get_report(self, research_id: str):
        return {"plan_snapshot": self.snapshot} if research_id == RESEARCH else None


def _coordinator(tmp_path: Path, store) -> RuntimeCoordinator:
    async def publish(research_id, payload):
        return None

    return RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc),
    )


def _plan():
    return SimpleNamespace(
        research_id=RESEARCH,
        entities=[SimpleNamespace(id="豆包", names=ENTITY_NAMES)],
    )


def _agent(agent_id: str, sources: list[str], entity: str | None = "豆包"):
    return SimpleNamespace(
        agent_id=agent_id, entity=entity,
        capability={"profile": "web-collector", "sources": sources},
    )


def test_小红书采集卡_提示写明实际检索词_只调一次_其余叫法进缺口(tmp_path: Path) -> None:
    runtime = _coordinator(tmp_path, _SnapshotStore())
    hint = runtime._source_query_hint(_plan(), _agent("data-collection", ["xhs"]), ["xhs"])

    assert "「豆包」、「豆包AI」" in hint
    assert "只调用 source.xhs 一次" in hint
    assert "系统检索词上限 2" in hint
    # 其余叫法按 casefold 去重：Doubao / doubao 只算一个
    assert "（Doubao、字节豆包）" in hint
    assert "doubao" not in hint
    # 名额没传时不写具体条数；传了写进去（重放 green2：50 条收敛 25 条烧掉 110 s）
    assert "按互动量取前 本章名额" in hint
    limited = runtime._source_query_hint(
        _plan(), _agent("data-collection", ["xhs"]), ["xhs"], item_limit=25,
    )
    assert "按互动量取前 25 条" in limited and "只做一次格式校验" in limited


def test_抖音卡同形覆盖_Reddit卡换英文语域的词(tmp_path: Path) -> None:
    runtime = _coordinator(tmp_path, _SnapshotStore())
    douyin = runtime._source_query_hint(_plan(), _agent("data-collection-2", ["douyin"]), ["douyin"])
    assert "「豆包」、「豆包AI」" in douyin and "只调用 source.douyin 一次" in douyin

    reddit = runtime._source_query_hint(_plan(), _agent("data-collection-2", ["reddit"]), ["reddit"])
    # §D-066：Doubao / doubao 按 casefold 去重是同一个叫法，这张卡英文语域只剩一个词，不凑数
    assert "改用 「Doubao」 分别检索" in reddit and "doubao」" not in reddit
    assert "（豆包、豆包AI、字节豆包）" in reddit


def test_不该带提示的卡(tmp_path: Path) -> None:
    runtime = _coordinator(tmp_path, _SnapshotStore())
    plan = _plan()
    # 非采集卡
    assert runtime._source_query_hint(plan, _agent("data-cleaning", ["xhs"]), ["xhs"]) == ""
    # 多源卡：适配层按工具调用换词，一张卡两个源的提示写不准，不写
    assert runtime._source_query_hint(
        plan, _agent("data-collection", ["xhs", "douyin"]), ["xhs", "douyin"]
    ) == ""
    # 卡没登记实体：适配层原样用模型的 query
    assert runtime._source_query_hint(
        plan, _agent("data-collection-9", ["xhs"], entity=None), ["xhs"]
    ) == ""


def test_没有库时适配层不换词_提示也不写(tmp_path: Path) -> None:
    runtime = _coordinator(tmp_path, _SnapshotStore(database_path=None))
    assert runtime._source_query_hint(
        _plan(), _agent("data-collection", ["xhs"]), ["xhs"]
    ) == ""


def test_接缝_提示里的词就是源工具实际检索的词(tmp_path: Path) -> None:
    store = _SnapshotStore()
    hint = _coordinator(tmp_path, store)._source_query_hint(
        _plan(), _agent("data-collection", ["xhs"]), ["xhs"],
    )

    searched: list[str] = []

    def stub(query: str, window: str) -> list[dict]:
        searched.append(query)
        return []

    adapter = SourceToolAdapter({"source.xhs": stub}, store=store)
    asyncio.run(adapter.call(
        "source.xhs", "Doubao", "30d",   # 模型想补的叫法——会被换掉
        research_id=RESEARCH, goal_id="goal-1", agent_id="data-collection",
        capability=SimpleNamespace(tools=("source.xhs",), sources=("xhs",), network="sources_only"),
        with_comments="off",
    ))

    assert searched == ["豆包", "豆包AI"]
    assert "、".join(f"「{item}」" for item in searched) in hint
