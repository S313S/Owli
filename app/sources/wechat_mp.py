"""微信公众号信息源：从**预采集池**读数据的薄源（§M6-d 货 4，丁形态）。

与 `weibo.py` 同形：本文件里没有任何抓取手法，读池逻辑共用 `_pool_source.py`。
公众号的发现与正文抓取都在本机脚本里（`owli-precollect/`，不进仓），产物落到
`~/.owli/precollect/wechat_mp/<批次>/`，字段映射在 `precollect.PLATFORM_PROFILES`。

两件与微博不同、值得写下来的事：

1. **permalink 有两种，代价不一样**。固定链 `mp.weixin.qq.com/s/<id>` 永久可回溯；
   搜狗等补充面给的是带 `timestamp+signature` 的临时链，几小时到一天就过期。
   已拍口径：固定链优先，临时链只在补充面收且**必须伴正文快照落盘**，闸门在
   `precollect._require_snapshot`——缺快照当场拒收，不留到交付一周后才发现角标全断。
2. **没有可比的互动量**。阅读数/在看数未登录抓不到，`platforms.py` 里公众号的
   primary_metric 是 None，整批走 `norm_method=none / no_metric_available`
   ——不编分数，比编一个 0 诚实。热度维度由微博/抖音覆盖，公众号出的是长文观点。
"""

from __future__ import annotations

from functools import partial

from app.sources._pool_source import POOL_WILDCARD, search_pool
from app.sources.spec import SourceSpec

__all__ = ["POOL_WILDCARD", "SOURCE_SPEC", "search"]

PLATFORM = "wechat_mp"

#: 从预采集池挑出匹配的公众号文章，入库并返回。`window` 只对拿得到发布时间的
#: 行生效；池内行带 `publish_time`，时间窗筛得动。
search = partial(search_pool, platform=PLATFORM, provider="owli_precollect")


SOURCE_SPEC = SourceSpec(
    source_id=PLATFORM,
    tool_name="source.wechat_mp",
    entrypoint=search,
    display_name="微信公众号",
    collector_name="微信公众号数据抓取",
    capability_description=(
        "读取本机预采集池里的微信公众号文章全文（行业媒体与品牌官号的署名长文），"
        "是国内行业观点与深度分析面的来源，与微博的热点短讯互补"
    ),
    prompt_hint=(
        "公众号走离线预采集：池里只有预先采过的关键词，"
        "选它就把检索词写成预采集时用的词；它出的是长文观点不是热度数据"
    ),
    limit_parameter="limit",
)
