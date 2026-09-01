"""读池薄源的公共体（§M6-d 货 4）。

微博（§M6-b）与微信公众号（§M6-d）是同一件事的两个实例：**本机脚本离线采、
公开仓只读池**。两者的差别全在 `precollect.PLATFORM_PROFILES` 的字段映射里，
读池这一段逻辑一个字都不差——所以它只该有一份。

这个文件的存在理由说白了就是：这个仓库已经为「手抄一份镜像」付过太多次账
（加源 checklist 十三张表里有两张就是手抄镜像，改单边当场拒收整批证据）。
再复制一份 120 行的读池薄源，等于自愿添第十一张。

平台专属的东西（`PLATFORM`、事件里的 provider 名、SOURCE_SPEC 文案）留在各自
的源模块里，那些本来就该各写各的。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.precollect import LOGIN_REQUIRED_REASON, load_evidence
from app.reliability.scoring import normalize_evidence_metrics

__all__ = ["POOL_WILDCARD", "search_pool"]

EventCallback = Callable[[dict[str, Any]], None]

#: 池健康检查用的通配检索词：不按关键词过滤，只问「池里有没有可读的行」。
#: 池里有哪些词是预采集时定的，拿固定关键词探活必然空手而归，那量的是
#: 「这批池没采过那个词」，不是「源坏了」（`sources_probe.PROBE_QUERIES`）。
POOL_WILDCARD = "*"


def _emit(
    on_event: EventCallback | None, event_type: str, *, platform: str, **data: Any
) -> None:
    if on_event is not None:
        on_event({"type": event_type, "data": {"source": platform, **data}})


def search_pool(
    query: str,
    window: str = "",
    *,
    platform: str,
    provider: str,
    limit: int = 20,
    store: Any | None = None,
    report_id: str | None = None,
    goal_id: str | None = None,
    agent_name: str | None = None,
    on_event: EventCallback | None = None,
    pool_root: Any | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[dict[str, Any]]:
    """从预采集池挑出匹配的行，入库并返回。

    `window` 只对**拿得到发布时间**的行生效；缺时间的行不因时间窗被丢
    ——丢了等于假装它不存在。

    池空、批次失败一律发 `source_unavailable` 并带上 `closed_reason`，走现成的
    源对账报 missing——[[verdict-is-data-not-http200]]：判据是取到数据，不是
    函数没抛错。
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100 整数")

    moment = now()
    needle = query.strip()
    result = load_evidence(
        platform, query=None if needle == POOL_WILDCARD else needle,
        window=window or None,
        limit=limit, root=pool_root, now=moment,
    )
    # §M6-c 货 1：**最新批次**因未登录失败 → 事件 reason 直接写 login_required
    # （新增 reason 取值、沿用 source_unavailable 事件形状，不新建类型）。
    # 老批次还有货时照样发——卡片阻塞档是 none，通知不拦研究；query/window/limit
    # 一并带上，货 3 的「重扫池+重导入+重探活」要按原口径重放这一次读取。
    login_failed = result.latest_failure_reason == LOGIN_REQUIRED_REASON
    login_fields: dict[str, Any] = {
        "batch_id": result.latest_batch_id,
        "query": needle, "window": window or "", "limit": limit,
    }
    pool_readout: dict[str, Any] = {
        "closed_reason": result.closed_reason,
        "batches_scanned": result.batches_scanned, "rows_seen": result.rows_seen,
        "dropped_by_query": result.dropped_by_query,
        "dropped_by_window": result.dropped_by_window,
    }
    if not result.items:
        _emit(
            on_event, "source_unavailable", platform=platform,
            reason=LOGIN_REQUIRED_REASON if login_failed else "tool_unavailable",
            provider=provider, task_continues=True,
            **pool_readout, **(login_fields if login_failed else {}),
        )
        return []
    if login_failed:
        _emit(
            on_event, "source_unavailable", platform=platform,
            reason=LOGIN_REQUIRED_REASON, closed_reason=LOGIN_REQUIRED_REASON,
            provider=provider, task_continues=True, **login_fields,
        )

    fetched_at = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    normalized = normalize_evidence_metrics(
        result.items, computed_at=fetched_at,
        report_id=report_id or "unpersisted", goal_id=goal_id or "unpersisted",
        queries=[needle],
        filters=f"precollect_pool;{platform};window={window or 'none'}",
    )
    if store is not None:
        assert report_id is not None and goal_id is not None
        store.upsert_evidence_batch([
            {
                **item,
                "id": f"ev-{report_id}-{platform}-{item['platform_item_id']}",
                "report_id": report_id,
                "goal_id": goal_id,
                # 章归属：为空的行算不到任何章头上（§M6-a 货 2）。
                "agent_name": agent_name,
            }
            for item in normalized
        ])
    _emit(
        on_event, "source_usage_reconciled", platform=platform, provider=provider,
        calls={"precollect_pool_read": result.batches_scanned},
        returned=len(normalized), rows_seen=result.rows_seen,
        task_continues=True,
    )
    return normalized
