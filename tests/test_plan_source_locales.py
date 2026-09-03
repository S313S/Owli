"""§ENT-2：规划期的源语域表必须和采集期的那张一致。

规划期按语域决定「排不排这个源」（`app.plan.lint._SOURCE_LOCALES`），采集期按语域
决定「用哪个名字去搜」（`app.adapters.source_mcp._SOURCE_LOCALES`）。两边分家是因为
adapters 是本包禁区、搬不过去共用；分家就得有人守——加源清单里「过三张映射表」的
同一个道理（[[add-source-needs-three-tables]]）：漏改一张，新源会被排进分配表却拿
错语域的名字去搜，而且不报错，只是召回悄悄归零。
"""

from __future__ import annotations

from app.adapters.source_mcp import _SOURCE_LOCALES as COLLECT_LOCALES
from app.plan.lint import _SOURCE_LOCALES as PLAN_LOCALES
from app.sources.registry import planning_catalog


def test_规划期与采集期的源语域表逐字一致() -> None:
    assert PLAN_LOCALES == COLLECT_LOCALES


def test_每个已注册的源都在语域表里有归属() -> None:
    missing = {
        spec.source_id for spec in planning_catalog()
    } - set(PLAN_LOCALES)
    assert not missing, f"新源没进语域表，规划期会按语域漏排：{sorted(missing)}"
