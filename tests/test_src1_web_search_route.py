"""§SRC-1 货 8：证据只能来自自家源工具；快速档网页搜索名额 5 → 20。"""

from __future__ import annotations


def _collection_prompt(source_id: str, scale: str = "fast") -> str:
    """取一段真实的采集章提示词正文（走生产的 `_agent_prompt`）。"""

    from app.plan.generate import _agent_prompt

    return _agent_prompt(
        "通义听悟 口碑",
        "采集用户口碑证据",
        {"format": "json", "shape": "array", "path": "goals/goal-1/x.json",
         "validators": ["file_exists", "json_array_min_items:1"]},
        "data_collection",
        source_id=source_id,
        source_item_limit=20,
        scale=scale,
    )


def test_快速档网页搜索名额提到与国内源同量级() -> None:
    """诊断根因：5 是 xhs/douyin(25) 的 1/5，「网页搜索少」是这个数字的天花板。"""

    from app.config import load_research_scale_config

    limits = load_research_scale_config().fast.source_item_limits

    assert limits["web_search"] == 20
    assert limits["xhs"] == 25 and limits["douyin"] == 25


def test_提示词把自家源工具写成硬约束() -> None:
    """诊断根因：六轮里 source.web_search 零调用，89/76 次搜索全走引擎自带工具；
    自带工具的结果只落在产物文件里，章一超时整批作废，而 source.* 是直接落库的。"""

    body = _collection_prompt("web_search")

    assert "调用 source.web_search" in body
    assert "本章证据**只能**来自 source.web_search 的返回值" in body
    assert "引擎自带的联网搜索只可用来想查询式" in body
    assert "不得用自带搜索补位" in body


def test_每个源的硬约束都点自己的工具名() -> None:
    for source_id, tool_name in (
        ("xhs", "source.xhs"),
        ("douyin", "source.douyin"),
        ("web_search", "source.web_search"),
    ):
        body = _collection_prompt(source_id)
        assert f"本章证据**只能**来自 {tool_name} 的返回值" in body


def test_没有注册源的采集章不加这条约束() -> None:
    """MediaCrawler / 浏览器自动化这类没有 SOURCE_SPEC 的档位不受影响。"""

    body = _collection_prompt("mediacrawler")

    assert "按 capability 声明的信息源执行采集" in body
    assert "只能**来自" not in body
