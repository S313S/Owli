"""§M6-b 货 4：闸门后闸门——装上了要选得出、批得过、名额下得去。

[[add-source-needs-three-tables]]：历史上每加一源必踩一次「装上了却静默不
干活」。这三条把「选得出（规则 23）」「手册里有」「名额下得去」各钉死一条。
"""

from __future__ import annotations

from tests.plan_factory import make_plan_dict


def _weibo_collector(agent: dict) -> dict:
    agent["capability"].update({
        "profile": "web-collector",
        "tools": ["source.weibo", "fs.write", "db.write"],
        "sources": ["weibo"],
        "network": "sources_only",
    })
    return agent


def test_微博在cn_product许可名单里且过规则23() -> None:
    from app.plan.lint import _SOURCE_MARKET_PROFILES, lint

    assert "weibo" in _SOURCE_MARKET_PROFILES["cn_product"]
    # 只进国内档：池里是中文关键词采的国内热点面。
    assert "weibo" not in _SOURCE_MARKET_PROFILES["global_product"]

    plan = make_plan_dict()
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "产品主要面向中国大陆用户。"
    agent = plan["goals"][0]["agents"][0]
    _weibo_collector(agent)
    agent["chapter"] = {
        "chapter_id": "ch-1", "chapter_type": "collection",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {"inputs": [], "task": agent["task"], "acceptance": ["完成"]},
        "closing": {"output": {"path": agent["output"]["path"]},
                    "entities": ["小罐茶"], "expected_count": 1, "notes": {}},
    }
    assert not any("规则23" in item for item in lint(plan)["errors"])


def test_信息源手册自动列出微博且参数名对() -> None:
    from app.plan.generate import render_source_handbook
    from app.sources.registry import source_limit_parameters

    assert source_limit_parameters()["weibo"] == "limit"
    page = render_source_handbook(scale="fast")
    assert "source.weibo" in page and "微博" in page
