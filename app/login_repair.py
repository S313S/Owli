"""登录态修复卡（§M6-c，LOGIN_REPAIR 甲·预采集失败通知）。

已拍口径（M6-0 货 3 拍② + 提货单）：

- 阻塞档 `none` ——通知不拦研究；动作 CHOICE_2「已补登录 / 跳过」，
  **不用 OPEN_URL**：登录必须在起浏览器那台机器上做，给链接也没用。
- 发卡方是读池的 Owli 侧组件，不是 scheduler——本模块只造卡与记账，
  接线在 runtime 的读池事件管道上（scheduler.py / cards.py 零改动）。
- 幂等：同一研究同一源同一批次只发一张卡；答「已补登录」重试一次，
  失败两次（首败=发卡那次，二败=重试仍失败）落 degraded 停手不再发卡；
  「跳过」直接 degraded 记录。防的是新的卡死面：卡不许无限重试、无限重发。
- 重试的语义边界（丁形态）：Owli 侧**不能起真机浏览器**——「重试」=
  重扫池 + 重导入 + 重探活该源；先决条件是用户已在登录助手里登好、
  且本机重跑过预采集脚本，池里有了新的成功批次。卡文案要把这步说给人听。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.plan.cards import (
    Card,
    CardActionType,
    CardBlocking,
    CardStatus,
    CardType,
)
from app.precollect import LOGIN_REQUIRED_REASON, latest_login_failure

__all__ = [
    "LOGIN_REQUIRED_REASON", "RELOGIN_ACTION_ID", "SKIP_ACTION_ID",
    "LoginRepairLedger", "RetryOutcome", "build_login_repair_card",
    "card_id_for", "retry_pool_read",
]

RELOGIN_ACTION_ID = "relogin"
SKIP_ACTION_ID = "skip"


def card_id_for(research_id: str, platform: str, batch_id: str) -> str:
    """确定性卡号：幂等靠它——同一批次重复触发只会指向同一张卡。"""

    return f"{research_id}-login-{platform}-{batch_id}"


_BODY_TEMPLATE = (
    "本机预采集批次 {batch_id} 因登录态失效没采到数据（{platform} 走离线预采集，"
    "Owli 这边无法代登录）。请在跑采集的那台机器上：\n"
    "① 运行登录助手，在弹出的浏览器窗口里用任意方式把 {platform} 登上；\n"
    "② 重跑一次预采集脚本，等它采出新的成功批次；\n"
    "③ 回到这里点「已补登录」——Owli 会重扫池、重导入、重探活一次。\n"
    "重试仍失败则该源本次研究按 degraded 记录、不再提醒；"
    "点「跳过」则直接按 degraded 记录。本卡不阻塞研究继续跑。"
)


def build_login_repair_card(
    *, research_id: str, goal_id: str | None, agent_id: str | None,
    platform: str, batch_id: str, query: str, window: str,
    limit: Any, created_at: str, pool_root: str | None = None,
) -> Card:
    """构造 LOGIN_REPAIR 卡。target 里带齐货 3 重试所需的原读取口径。"""

    target: dict[str, Any] = {
        "display_name": f"{platform} 预采集批次 {batch_id}",
        "platform": platform, "batch_id": batch_id,
        "query": query, "window": window, "limit": limit,
    }
    if pool_root is not None:
        # 只给夹具/沙盒指路用；缺省走真实池（app.precollect.POOL_ROOT）。
        target["pool_root"] = pool_root
    return Card(
        card_id=card_id_for(research_id, platform, batch_id),
        card_type=CardType.LOGIN_REPAIR,
        research_id=research_id,
        goal_id=goal_id,
        agent_id=agent_id,
        title=f"{platform} 登录态失效，预采集批次没拿到数据",
        body=_BODY_TEMPLATE.format(batch_id=batch_id, platform=platform),
        target=target,
        actions=[
            {"type": CardActionType.CHOICE_2.value, "id": RELOGIN_ACTION_ID,
             "label": "已补登录", "value": RELOGIN_ACTION_ID, "default": True},
            {"type": CardActionType.CHOICE_2.value, "id": SKIP_ACTION_ID,
             "label": "跳过", "value": SKIP_ACTION_ID},
        ],
        blocking=CardBlocking.NONE,
        deadline=None,
        status=CardStatus.PENDING,
        result=None,
        created_at=created_at,
        resolved_at=None,
    )


class LoginRepairLedger:
    """发卡台账：幂等 + 两败 degraded 停手。进程内状态，重启后归零

    ——阻塞档是 none，最坏后果是重启后同一批次再提醒一次，不是卡死。
    """

    def __init__(self) -> None:
        self._issued: set[tuple[str, str, str]] = set()
        self._degraded: dict[tuple[str, str], str] = {}

    def is_degraded(self, research_id: str, platform: str) -> bool:
        return (research_id, platform) in self._degraded

    def degraded_cause(self, research_id: str, platform: str) -> str | None:
        return self._degraded.get((research_id, platform))

    def should_issue(self, research_id: str, platform: str, batch_id: str) -> bool:
        """degraded 停手 > 同批次幂等；两条都过才发卡。"""

        if self.is_degraded(research_id, platform):
            return False
        return (research_id, platform, batch_id) not in self._issued

    def note_issued(self, research_id: str, platform: str, batch_id: str) -> None:
        self._issued.add((research_id, platform, batch_id))

    def mark_degraded(self, research_id: str, platform: str, *, cause: str) -> None:
        self._degraded[(research_id, platform)] = cause

    def clear_degraded(self, research_id: str, platform: str) -> None:
        """重试成功后清零——将来**新的**失败批次可以再发卡（新批次新卡号）。"""

        self._degraded.pop((research_id, platform), None)


@dataclass(frozen=True)
class RetryOutcome:
    """「已补登录」重试一次的全部读数；判据落在池状态与入库行数上。"""

    recovered: bool
    imported: int
    failed_batch_id: str | None
    events: list[dict[str, Any]] = field(default_factory=list)


def retry_pool_read(
    *, platform: str, query: str, window: str, limit: Any,
    store: Any, report_id: str, goal_id: str | None, agent_name: str | None,
    pool_root: str | None = None,
) -> RetryOutcome:
    """重扫池 + 重导入 + 重探活一次（已拍口径 5：Owli 侧不起浏览器）。

    走注册表拿薄源入口按原口径重放读取：池里有新的成功批次 → 行直接
    入库（重导入）；复核尺子是 `latest_login_failure`——最新批次仍是
    login_required 失败即「第二败」，由调用方落 degraded。
    """

    from app.sources.registry import discover_sources

    spec = discover_sources().get(platform)
    if spec is None:
        raise ValueError(f"未注册的池型源，无法重试：{platform}")
    events: list[dict[str, Any]] = []
    call_kwargs: dict[str, Any] = {
        "store": store, "report_id": report_id, "goal_id": goal_id,
        "agent_name": agent_name, "on_event": events.append,
    }
    if pool_root is not None:
        call_kwargs["pool_root"] = pool_root
    if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1:
        call_kwargs[spec.limit_parameter or "limit"] = limit
    returned = spec.entrypoint(query or "*", window or "", **call_kwargs)
    failed_batch = latest_login_failure(platform, root=pool_root)
    return RetryOutcome(
        recovered=failed_batch is None,
        imported=len(returned),
        failed_batch_id=failed_batch,
        events=events,
    )
