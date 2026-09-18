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


def gate_text(row: Mapping[str, Any]) -> str:
    """闸看的那段文字：标题 + 摘要。一处定义，本行与「父帖过没过闸」共用同一把尺子。

    §SEL-2 货 4：先剔掉**尾部独占整行**的平台 UI 词缀（微博的「豆包翻译」按钮文案）。
    那四个字不是人说的话，却让一条追星帖过了闸、进了池、还被编成「豆包·主体」。
    ⛔ 只按位置剔，不做全文替换——「我用豆包翻译了这段」是真口碑，剔了就是静默丢证据。
    """
    from app.reliability.platform_noise import strip_ui_affordances

    return strip_ui_affordances(
        f"{row.get('title') or ''} {row.get('content_excerpt') or ''}")


def rows_naming_entities(rows: Sequence[Mapping[str, Any]], plan: Any,
                         *, inherit_parent: bool = True) -> list[dict[str, Any]]:
    """点名了任一被评实体的证据行。

    口径是用户拍的那一条：**只挡「谁都没提」那一类**——讲竞品的照样进池，
    因为竞品对照那一节本来就要引它。要收紧到「只认研究主角」得另请拍板，
    并且得同时回答「竞品节怎么办」。

    拿不到叫法就原样返回（不设闸），与 `_names_the_entity` 的 `accepted` 为空同族：
    宁可不筛，也不要在拿不到判据时把池筛空。

    **`inherit_parent`（§SEL-2 货 1，默认开）**：评论自己没点名、但**父帖过了闸**时
    也放行。病根是这道闸只看本行 `title + content_excerpt`，而评论行的 `title` 是
    「评论 · 父帖标题」、`content_excerpt` 是评论自己的话——**父帖正文里点的名，
    评论继承不到**（父帖标题里的名倒是早就漏继承了，因为标题被拼进来了）。
    实测 `r-3b3482ca7f8b`：闸挡下 356 条，其中 328 条是评论、而它们的父帖自己过了闸。

    同一条评论，`tables.coding_entity_roles` 的规则 ③ 认它「说的是 Kimi」，这道闸却
    判它「谁都没提」直接挡掉——**一处放行一处拦下，两边读数都是绿的**，正是本文件
    头注释警告的那个形状。父帖的映射走 `parentage.parent_index`，与实体归属那边
    **共用同一份**，⛔ 两边不各写一份。

    传 `inherit_parent=False` 复现旧行为（§SEL-2 的对照读数脚本拿它量两侧，
    ⛔ 不在脚本里另写一份继承逻辑当尺子）。
    """
    from app.reliability.coding import _names_the_entity      # 只读复用，不改那个文件

    names = entity_names_of(plan)
    rows = [dict(row) for row in rows]
    if not names:
        return rows
    # 判定按**行位置**留，⛔ 不按 id 建集合去筛：既有用例 `_row()` 的 rid 默认全是
    # `ev-1`，按 id 筛会把三条同 id 的行一起放行，红得还很像「口径变了」。
    passed = [_names_the_entity(gate_text(row), names) for row in rows]
    if not inherit_parent:
        return [row for row, ok in zip(rows, passed) if ok]
    from app.reliability.parentage import parent_index        # 评论→父帖：与实体归属共用那一份

    parents = parent_index(rows)
    passed_ids = {str(row.get("id")) for row, ok in zip(rows, passed) if ok}
    kept: list[dict[str, Any]] = []
    for row, ok in zip(rows, passed):
        if ok:
            kept.append(row)
            continue
        parent = parents.get(str(row.get("id")))
        if parent is not None and str(parent.get("id")) in passed_ids:
            kept.append(row)
    return kept
