"""后台任务的异常兜底（§D-013 货 2）。

`asyncio.create_task` 起出去的协程，异常不会自己冒到任何地方：没人 `await`、
没人 `task.exception()`，解释器只会在任务被回收时补打一句
`Task exception was never retrieved`——那时候等这个任务的 goal 已经死等完了。

§W-1 六轮里这个洞漏掉过两次真异常：
- 第 5 轮 `_persist_goal_evidence` 的 `IntegrityError`（D-019），goal-2 判 failed 之前没人看见；
- 第 6 轮 `ValueError: 未知卡片：card-1`（serve log 第 76 行）。

所以凡是 fire-and-forget 的任务，都挂上 `guard_task`：异常一律取走并落错误日志。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


def guard_task(
    task: asyncio.Task[Any],
    *,
    logger: logging.Logger,
    context: str = "后台任务",
) -> asyncio.Task[Any]:
    """给后台任务挂异常回调；取消不算异常，正常结束不留痕。"""

    def report(finished: asyncio.Task[Any]) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is None:
            return
        name = finished.get_name()
        logger.error(
            "%s 异常未被处理：name=%s %s: %s",
            context,
            name,
            type(error).__name__,
            error,
            exc_info=error,
        )

    task.add_done_callback(report)
    return task
