"""§SRC-1：采集 agent 共用的信息源手册。"""

from __future__ import annotations


def test_手册的逐源表跟着注册表走而不是手工维护() -> None:
    """M6 加源只改各源自己的 SOURCE_SPEC，这张表自动更新。"""

    from app.plan.generate import render_source_handbook
    from app.sources.registry import planning_catalog

    handbook = render_source_handbook("fast")

    for spec in planning_catalog():
        assert f"`{spec.tool_name}`" in handbook
        assert spec.display_name in handbook


def test_不要时间窗的源在表里明说() -> None:
    from app.plan.generate import render_source_handbook

    handbook = render_source_handbook("fast")
    douyin_row = next(
        line for line in handbook.splitlines() if "`source.douyin`" in line
    )
    xhs_row = next(
        line for line in handbook.splitlines() if "`source.xhs`" in line
    )

    assert "本源不要这个参数" in douyin_row
    assert "`7d`" in xhs_row


def test_名额按档位显示() -> None:
    from app.plan.generate import render_source_handbook

    fast = render_source_handbook("fast")
    standard = render_source_handbook("standard")

    assert "`max_results=20`" in fast          # 货 8 抬过的网页搜索名额
    assert "`max_results=10`" in standard


def test_手册只挂在采集章上() -> None:
    from app.plan.generate import _agent_prompt

    output = {
        "format": "json", "shape": "array", "path": "goals/goal-1/x.json",
        "validators": ["file_exists"],
    }
    collection = _agent_prompt(
        "查询", "采集", output, "data_collection",
        source_id="xhs", source_item_limit=25, scale="fast",
    )
    writing = _agent_prompt("查询", "撰写", output, "report_writing", scale="fast")

    assert "# 信息源手册（采集 agent 共用）" in collection
    assert "信息源手册" not in writing


def test_手册讲了失败怎么办且和真实closed_reason对得上() -> None:
    """手册里列的失败原因必须是源真会发的那些，别写一套查不到的。"""

    from app.plan.generate import render_source_handbook
    from app.sources import douyin

    handbook = render_source_handbook("fast")

    for kind, status in (("http", 429), ("http", 401), ("http", 503)):
        reason = douyin.TikHubError(
            kind, endpoint="/p", http_status=status,
        ).closed_reason
        assert reason in handbook
