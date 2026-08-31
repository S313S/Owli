"""§M6-b 货 6：平台表收编与漂移守卫。

第七/第八张手抄表已收编进 `app/platforms.py`；第九（`store/dao.py`）与第十
（`reliability/crossref.py`）在禁区文件里，只能留手抄——这里给它们各挂一条
守卫，将来谁改单边，用例当场红，而不是等到整批证据入库被拒收才发现。
"""

from __future__ import annotations


#: 与 `docs/design/source-reliability.md` §2 表逐行对齐（需求仓，代码读不到，
#: 故在此钉死）。加平台必改这里——改这里的人自然会回去对文档。
EXPECTED_PLATFORMS = {
    "product_hunt", "hacker_news", "x", "web_search", "reddit",
    "xhs", "douyin", "bilibili", "weibo", "zhihu", "wechat_mp",
}


def test_平台表键集合与设计稿第2节一致() -> None:
    from app.platforms import PLATFORMS

    assert set(PLATFORMS) == EXPECTED_PLATFORMS


def test_基线与主指标都从平台表派生不再手抄() -> None:
    from app.platforms import PLATFORMS, baselines, primary_metrics
    from app.reliability.scoring import PLATFORM_BASELINES, PRIMARY_METRICS

    assert PLATFORM_BASELINES == baselines()
    assert PRIMARY_METRICS == primary_metrics()
    # 五维和与设计稿基线总分一致：微博 1+2+0+1+1 = 5（C）。
    assert sum(PLATFORMS["weibo"].baseline) == 5
    assert sum(PLATFORMS["hacker_news"].baseline) == 7


def test_dao主指标镜像不得与平台表漂移() -> None:
    """第九张表：禁区文件 `app/store/dao.py:28` 的手抄件。

    两边不一致时 dao 的 `norm_context.metric` 校验会拒收**整批**证据，
    而不是只丢一条——这是「加源忘了改」代价最大的一张表。
    """

    from app.platforms import primary_metrics
    from app.store.dao import _PRIMARY_METRICS

    expected = primary_metrics()
    drift = {
        platform: (expected[platform], _PRIMARY_METRICS.get(platform))
        for platform in expected
        if platform in _PRIMARY_METRICS and _PRIMARY_METRICS[platform] != expected[platform]
    }
    assert not drift, f"dao 主指标镜像与平台表漂移：{drift}"
    missing = {
        platform for platform, metric in expected.items()
        if metric is not None and platform not in _PRIMARY_METRICS
    }
    assert not missing, f"平台表给了主指标但 dao 镜像没有：{missing}（会静默不归一化）"


def test_crossref域名白名单不得与平台表漂移() -> None:
    """第十张表：禁区文件 `app/reliability/crossref.py:128` 的手抄件。

    不在白名单里的平台，两条同域名证据走「域名相同 → 同簇」，交叉维天花板
    直接掉一档（§XSEM-1 条 3：基线交叉分正卡在 B/C 边界上）。
    """

    import ast
    from pathlib import Path

    from app.platforms import domains

    source = (Path(__file__).resolve().parents[1]
              / "app/reliability/crossref.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    literal = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", "") == "platform_domains" for target in node.targets)
    )
    mirrored = {
        key.value: frozenset(ast.literal_eval(value))
        for key, value in zip(literal.keys, literal.values)
    }
    assert mirrored == domains(), (
        "crossref 域名白名单与平台表漂移；两者都要改（crossref.py 是禁区，需单独拍）"
        f"\n镜像={mirrored}\n平台表={domains()}"
    )
