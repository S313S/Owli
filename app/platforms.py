"""平台表：证据「来自哪个平台」这件事的单一事实源（§M6-b 货 6）。

为什么要有这张表，而不是把这些字段挂到 `SOURCE_SPEC` 上：**平台不等于源**。
`bilibili` 只在设计稿里有基线、没有源模块；`weibo` 的采集手法留在本机脚本里、
公开仓只有一个读池的薄源；反过来 `web_search` 一个源横跨无数站点。所以
「源怎么调」归 `app/sources/spec.py`，「平台是什么」归这里，两张表各管一段。

收编进来的是 M6-a 关账时挂账的**第七张手抄表**（`scoring.PLATFORM_BASELINES`）
及其同族：

| # | 原位置 | 本包处置 |
|---|---|---|
| 7 | `reliability/scoring.py` `PLATFORM_BASELINES` | ✅ 改为从本表派生 |
| 8 | `reliability/scoring.py` `PRIMARY_METRICS` | ✅ 改为从本表派生 |
| 9 | `store/dao.py:28` `_PRIMARY_METRICS` | ⛔ 禁区文件，留手抄；`tests/test_m6b_platform_table.py` 加漂移守卫 |
| 10 | `reliability/crossref.py:128` `platform_domains` | ⛔ 禁区文件，留手抄；同上加守卫 |

数值口径出自 `docs/design/source-reliability.md` §2（需求仓，代码读不到，
所以键集合由守卫用例钉死——改表必改用例，改用例的人自然会去对文档）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

__all__ = ["PLATFORMS", "PlatformProfile", "baselines", "domains", "primary_metrics"]

#: 五维顺序与 `reliability.scoring.SCORE_FIELDS` 一致：权威/时效/交叉/完整/无关。
SCORE_ORDER = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence",
)


@dataclass(frozen=True)
class PlatformProfile:
    """一个平台在评级体系里的全部固有属性。"""

    display_name: str
    #: 五维基线，顺序同 SCORE_ORDER；`docs/design/source-reliability.md` §2。
    baseline: tuple[int, int, int, int, int]
    #: 平台内归一化的主指标；None = 该平台没有可比的互动量。
    primary_metric: str | None = None
    #: 注册域名白名单：同平台两条证据都落在这些域名上时，「机构主体不同」
    #: 的判断改按平台惯例走（同一 thread/问题下不同作者天然分属不同簇）。
    domains: frozenset[str] = field(default_factory=frozenset)

    def scores(self) -> dict[str, int]:
        return dict(zip(SCORE_ORDER, self.baseline))


PLATFORMS: dict[str, PlatformProfile] = {
    "product_hunt": PlatformProfile(
        "Product Hunt", (2, 2, 0, 1, 1), "votes_count"),
    "hacker_news": PlatformProfile(
        "Hacker News", (1, 1, 1, 2, 2), "points", frozenset({"ycombinator.com"})),
    "x": PlatformProfile(
        "X", (1, 2, 0, 1, 2), "like_count", frozenset({"x.com", "twitter.com"})),
    "web_search": PlatformProfile("网页搜索", (1, 1, 1, 1, 1)),
    "reddit": PlatformProfile(
        "Reddit", (0, 1, 0, 0, 2), None, frozenset({"reddit.com"})),
    "xhs": PlatformProfile(
        "小红书", (1, 2, 0, 1, 1), "liked_count",
        frozenset({"xiaohongshu.com", "xhslink.com"})),
    "douyin": PlatformProfile(
        "抖音", (1, 2, 0, 1, 1), "digg_count", frozenset({"douyin.com"})),
    "bilibili": PlatformProfile(
        "B站", (1, 1, 0, 2, 1), "view", frozenset({"bilibili.com"})),
    # 普通平台族（M6-0 拍板）：MediaCrawler 一条通道 + 平台参数。
    # 微博主指标 = MediaCrawler 的 attitudes_count 落成的 liked_count；
    # `store/dao.py:28` 那份禁区镜像已随本包同步（2026-09-01 用户拍板解禁一行）。
    "weibo": PlatformProfile("微博", (1, 2, 0, 1, 1), "liked_count"),
    "zhihu": PlatformProfile(
        "知乎", (1, 1, 1, 1, 1), None, frozenset({"zhihu.com"})),
    # 公众号文章全在同一个域名下。不进白名单，两篇**不同公众号**的文章会被判
    # 「域名相同 → 同簇」，交叉维天花板掉一档；进了才按平台惯例认作不同主体。
    # `crossref.py:128` 那份禁区镜像已随本包同步（2026-09-02 用户拍板解禁一处）。
    "wechat_mp": PlatformProfile(
        "微信公众号", (1, 1, 0, 1, 1), None,
        frozenset({"mp.weixin.qq.com"})),
}


def baselines() -> dict[str, dict[str, int]]:
    """派生第七张表：平台 → 五维基线。"""

    return {name: profile.scores() for name, profile in PLATFORMS.items()}


def primary_metrics() -> dict[str, str | None]:
    """派生第八张表：平台 → 归一化主指标。"""

    return {name: profile.primary_metric for name, profile in PLATFORMS.items()}


def domains() -> dict[str, frozenset[str]]:
    """派生域名白名单：只回有白名单的平台，空集不进表。"""

    return {
        name: profile.domains
        for name, profile in PLATFORMS.items() if profile.domains
    }
