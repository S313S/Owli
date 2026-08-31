"""微博信息源：从**预采集池**读数据的薄源（§M6-b 货 3，丁形态）。

本文件里**没有任何抓取手法**——不起浏览器、不扫码、不发一个请求。微博内容由
本机的 MediaCrawler 预采集脚本离线采到 `~/.owli/precollect/weibo/<批次>/`
（脚本不进任何 Owli 仓，见 decision-log 2026-09-01「丁现在用、丙留产品化」），
本源只负责在研究期把池里已有的行按 query/window 挑出来、入库。

这样定形的两个后果，都是有意的：

1. **预采集不进章墙钟**（decision-log:1294）。扫码 + 分钟级抓取发生在研究之外，
   研究期这一步是纯读盘，毫秒级。
2. **池空不是「成功采到 0 条」**。池空、批次失败（`login_required` 等）一律发
   `source_unavailable` 并带上 `closed_reason`，走现成的源对账报 missing
   ——[[verdict-is-data-not-http200]]：判据是取到数据，不是函数没抛错。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.precollect import LOGIN_REQUIRED_REASON, load_evidence
from app.reliability.scoring import normalize_evidence_metrics
from app.sources.spec import SourceSpec

__all__ = ["SOURCE_SPEC", "search"]

EventCallback = Callable[[dict[str, Any]], None]

PLATFORM = "weibo"

#: 池健康检查用的通配检索词：不按关键词过滤，只问「池里有没有可读的行」。
#: 池里有哪些词是预采集时定的，拿固定关键词探活必然空手而归，那量的是
#: 「这批池没采过那个词」，不是「源坏了」（`sources_probe.PROBE_QUERIES`）。
POOL_WILDCARD = "*"


def _emit(on_event: EventCallback | None, event_type: str, **data: Any) -> None:
    if on_event is not None:
        on_event({"type": event_type, "data": {"source": PLATFORM, **data}})


def _unavailable(
    on_event: EventCallback | None, *, closed_reason: str,
    reason: str = "tool_unavailable", **fields: Any,
) -> list[dict[str, Any]]:
    _emit(
        on_event, "source_unavailable",
        reason=reason, closed_reason=closed_reason,
        provider="media_crawler", task_continues=True, **fields,
    )
    return []


def search(
    query: str,
    window: str = "",
    *,
    limit: int = 20,
    store: Any | None = None,
    report_id: str | None = None,
    goal_id: str | None = None,
    agent_name: str | None = None,
    on_event: EventCallback | None = None,
    pool_root: Any | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[dict[str, Any]]:
    """从预采集池挑出匹配的微博，入库并返回。

    `window` 只对**拿得到发布时间**的行生效；MediaCrawler 的微博产物带
    `create_time`，缺时间的行不因时间窗被丢——丢了等于假装它不存在。
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100 整数")

    moment = now()
    needle = query.strip()
    result = load_evidence(
        PLATFORM, query=None if needle == POOL_WILDCARD else needle,
        window=window or None,
        limit=limit, root=pool_root, now=moment,
    )
    # §M6-c 货 1：**最新批次**因未登录失败 → 事件 reason 直接写 login_required
    # （新增 reason 取值、沿用 source_unavailable 事件形状，不新建类型）。
    # 老批次还有货时照样发——卡片阻塞档是 none，通知不拦研究；query/window/limit
    # 一并带上，货 3 的「重扫池+重导入+重探活」要按原口径重放这一次读取。
    login_failed = result.latest_failure_reason == LOGIN_REQUIRED_REASON
    login_fields: dict[str, Any] = {
        "reason": LOGIN_REQUIRED_REASON, "batch_id": result.latest_batch_id,
        "query": needle, "window": window or "", "limit": limit,
    }
    if not result.items:
        if login_failed:
            return _unavailable(
                on_event, closed_reason=result.closed_reason,
                batches_scanned=result.batches_scanned, rows_seen=result.rows_seen,
                dropped_by_query=result.dropped_by_query,
                dropped_by_window=result.dropped_by_window,
                **login_fields,
            )
        return _unavailable(
            on_event, closed_reason=result.closed_reason,
            batches_scanned=result.batches_scanned, rows_seen=result.rows_seen,
            dropped_by_query=result.dropped_by_query,
            dropped_by_window=result.dropped_by_window,
        )
    if login_failed:
        _emit(
            on_event, "source_unavailable",
            closed_reason=LOGIN_REQUIRED_REASON, provider="media_crawler",
            task_continues=True, **login_fields,
        )

    fetched_at = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    normalized = normalize_evidence_metrics(
        result.items, computed_at=fetched_at,
        report_id=report_id or "unpersisted", goal_id=goal_id or "unpersisted",
        queries=[needle],
        filters=f"precollect_pool;{PLATFORM};window={window or 'none'}",
    )
    if store is not None:
        assert report_id is not None and goal_id is not None
        store.upsert_evidence_batch([
            {
                **item,
                "id": f"ev-{report_id}-{PLATFORM}-{item['platform_item_id']}",
                "report_id": report_id,
                "goal_id": goal_id,
                # 章归属：为空的行算不到任何章头上（§M6-a 货 2）。
                "agent_name": agent_name,
            }
            for item in normalized
        ])
    _emit(
        on_event, "source_usage_reconciled", provider="media_crawler",
        calls={"precollect_pool_read": result.batches_scanned},
        returned=len(normalized), rows_seen=result.rows_seen,
        task_continues=True,
    )
    return normalized


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
