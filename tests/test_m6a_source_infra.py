"""§M6-a 货 1：采集条数参数名的单一事实源——五张手抄表收敛成 SOURCE_SPEC 一处。

历史上每加一源必踩：装上了却「限额被静默丢弃」。此前 hacker_news 在
`plan/generate.py` 写 hitsPerPage、在 `adapters/source_mcp.py` 写 limit；
`sources_probe.py` 把 x 写成 limit，而 x 只有 max_results。
"""

from __future__ import annotations

import inspect

from app.sources.registry import discover_sources, source_limit_parameters


def test_每个注册源声明的条数参数真在入口签名里() -> None:
    """漂移用例：谁手抄错了，这里当场红。"""
    offenders = {
        spec.source_id: spec.limit_parameter
        for spec in discover_sources().values()
        if spec.limit_parameter not in inspect.signature(spec.entrypoint).parameters
    }

    assert offenders == {}


def test_规划提示词与适配器读同一张表() -> None:
    from app.plan.generate import _limit_parameter

    truth = source_limit_parameters()
    assert truth  # 注册表非空，避免空表让断言假绿
    assert {sid: _limit_parameter(sid) for sid in truth} == truth
    # 漂移的那一格：两边现在都是 limit，不再是 hitsPerPage。
    assert truth["hacker_news"] == "limit"


def test_探活入参的条数参数名也来自同一张表() -> None:
    from app.sources_probe import probe_kwargs

    assert probe_kwargs("x") == {"max_results": 2}
    assert probe_kwargs("hacker_news") == {"limit": 2}
    # 源特有参数原样保留，不被条数键挤掉。
    assert probe_kwargs("douyin") == {"limit": 1, "comment_video_limit": 1}


def test_条数参数名必须是合法形参名() -> None:
    import pytest

    from app.sources.spec import SourceSpec

    with pytest.raises(ValueError, match="limit_parameter"):
        SourceSpec(
            source_id="fake", tool_name="source.fake", entrypoint=lambda *a, **k: [],
            limit_parameter="not a name",
        )
