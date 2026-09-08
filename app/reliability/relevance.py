"""相关性硬闸：标题/正文里不含任何被评实体叫法的证据行不进池（用户 2026-09-08 拍）。

**为什么单独一个文件**：判定要复用 §CODE-2 写在 `coding.py` 里的 `_names_the_entity`
和 §RPT-1 写在 `polish/tables.py` 里的 `_entity_aliases`——两个都只读 import、
一个字不改（`coding.py` 是 CODE-2 已关账的地界，改它有可能动到那道原声闸的行为）。

**为什么不自己写一个叫法匹配**：重写出来的差异是静默的。同一个「这段文字点没点某个
实体的名」如果有两处定义，迟早会一处放行一处拦下，而两边读数都是绿的。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def entity_names_of(plan: Any) -> list[str]:
    """计划里被评实体的全部叫法。取不到就空表——调用方据此决定不设闸。

    **叫法走 `_entity_aliases`（按 canonical 把「豆包」「Doubao」并成一个实体），
    不是 `plan["subjects"]`**：那份未归一，§D-053 查到分配器正是拿错了它，
    同一个产品被当成两个、吃掉两个 goal 的采集名额。
    """
    from app.report.polish.tables import _entity_aliases      # 延迟 import：避免成环

    data = plan.to_dict() if hasattr(plan, "to_dict") else plan
    if not isinstance(data, Mapping):
        return []
    return sorted({name for group in _entity_aliases(data).values() for name in group
                   if len(str(name).strip()) >= 2})


def rows_naming_entities(rows: Sequence[Mapping[str, Any]], plan: Any) -> list[dict[str, Any]]:
    """点名了任一被评实体的证据行。

    口径是用户拍的那一条：**只挡「谁都没提」那一类**——讲竞品的照样进池，
    因为竞品对照那一节本来就要引它。要收紧到「只认研究主角」得另请拍板，
    并且得同时回答「竞品节怎么办」。

    拿不到叫法就原样返回（不设闸），与 `_names_the_entity` 的 `accepted` 为空同族：
    宁可不筛，也不要在拿不到判据时把池筛空。
    """
    from app.reliability.coding import _names_the_entity      # 只读复用，不改那个文件

    names = entity_names_of(plan)
    if not names:
        return [dict(row) for row in rows]
    return [dict(row) for row in rows
            if _names_the_entity(
                f"{row.get('title') or ''} {row.get('content_excerpt') or ''}", names)]
