"""§M6-d 货 5：闸门后闸门——装上了但「选不出/批不过/限额丢/掉一档」。

加源 checklist 十三条里，真正会静默不干活的那几条各钉一条用例。
"""

from __future__ import annotations


def test_公众号进cn_product许可名单否则规划器永远选不出() -> None:
    from app.plan.lint import _SOURCE_MARKET_PROFILES

    assert "wechat_mp" in _SOURCE_MARKET_PROFILES["cn_product"]
    # 池里是中文关键词采的国内行业长文，进 global 只会让规划器选个必然空手的源。
    assert "wechat_mp" not in _SOURCE_MARKET_PROFILES["global_product"]


def test_两档名额都给了公众号否则限额被静默丢弃() -> None:
    from app.config import load_research_scale_config

    config = load_research_scale_config()
    assert config.standard.source_item_limits["wechat_mp"] == 20
    assert config.fast.source_item_limits["wechat_mp"] == 25


def test_探活用通配词且不要凭证() -> None:
    from app.sources_probe import CREDENTIAL_KEYS, PROBE_QUERIES, missing_credentials

    assert PROBE_QUERIES["wechat_mp"] == "*"
    assert CREDENTIAL_KEYS["wechat_mp"] == ()
    assert missing_credentials("wechat_mp") is False


def test_同域名两篇不同公众号文章判为不同主体而不是同簇() -> None:
    """第十张表的实证：不进白名单，交叉维天花板掉一档。

    公众号文章全在 mp.weixin.qq.com 一个域名下，这是它与别的平台最不一样的
    地方——按「域名相同 → 同簇」处理，两篇不同公众号的独立报道会被算成一个来源。
    """

    from app.reliability.crossref import _institutions_differ

    left = {"platform": "wechat_mp", "permalink": "https://mp.weixin.qq.com/s/aaa"}
    right = {"platform": "wechat_mp", "permalink": "https://mp.weixin.qq.com/s/bbb"}
    assert _institutions_differ(left, right) is True


def test_平台表与crossref镜像同步且公众号无主指标() -> None:
    from app.platforms import PLATFORMS, domains, primary_metrics

    # 值是注册域名 qq.com——白名单比的是 `_registered_domain` 归约结果，
    # 写全主机名 mp.weixin.qq.com 这道闸静默不生效（本包实测，见上一条用例）。
    assert domains()["wechat_mp"] == frozenset({"qq.com"})
    # 阅读数/在看数未登录抓不到；None 让整批走 no_metric_available，不编分数。
    assert primary_metrics()["wechat_mp"] is None
    assert sum(PLATFORMS["wechat_mp"].baseline) == 4
