"""微博信息源：从**预采集池**读数据的薄源（§M6-b 货 3，丁形态）。

本文件里**没有任何抓取手法**——不起浏览器、不扫码、不发一个请求。微博内容由
本机的 MediaCrawler 预采集脚本离线采到 `~/.owli/precollect/weibo/<批次>/`
（脚本不进任何 Owli 仓，见 decision-log 2026-09-01「丁现在用、丙留产品化」），
本源只负责在研究期把池里已有的行按 query/window 挑出来、入库。

读池那段逻辑住在 `_pool_source.py`（§M6-d 货 4 抽出，与公众号共用一份）；
本文件只剩「微博是什么」——平台名、供货方、给规划器看的文案。

这样定形的两个后果，都是有意的：

1. **预采集不进章墙钟**（decision-log:1294）。扫码 + 分钟级抓取发生在研究之外，
   研究期这一步是纯读盘，毫秒级。
2. **池空不是「成功采到 0 条」**。池空、批次失败（`login_required` 等）一律发
   `source_unavailable` 并带上 `closed_reason`，走现成的源对账报 missing
   ——[[verdict-is-data-not-http200]]：判据是取到数据，不是函数没抛错。
"""

from __future__ import annotations

from functools import partial

from app.sources._pool_source import POOL_WILDCARD, search_pool
from app.sources.spec import SourceSpec

__all__ = ["POOL_WILDCARD", "SOURCE_SPEC", "search"]

PLATFORM = "weibo"

#: 从预采集池挑出匹配的微博，入库并返回。`window` 只对**拿得到发布时间**的行
#: 生效；MediaCrawler 的微博产物带 `create_time`，时间窗筛得动。
search = partial(search_pool, platform=PLATFORM, provider="media_crawler")


SOURCE_SPEC = SourceSpec(
    source_id=PLATFORM,
    tool_name="source.weibo",
    entrypoint=search,
    display_name="微博",
    collector_name="微博数据抓取",
    capability_description=(
        "读取本机 MediaCrawler 预采集池里的微博博文（关键词搜索结果、"
        "互动指标与发布地），是网页搜索在国内热点面的补充"
    ),
    prompt_hint=(
        "微博走离线预采集：池里只有预先采过的关键词，"
        "选它就把检索词写成预采集时用的词，别指望现采"
    ),
    # 池内行带 create_time，时间窗筛得动。
    limit_parameter="limit",
)
