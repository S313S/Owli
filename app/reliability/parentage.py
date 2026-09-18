"""评论 → 父帖：**一处实现**，相关性硬闸与实体归属两个消费方共用。

**为什么单独一个文件**：这份映射有两个消费方——`relevance.rows_naming_entities`
（撰写期的相关性硬闸）和 `report.polish.tables.coding_entity_roles`（出稿期的实体归属）。
`relevance.py` 的文件头已经把道理写死了：*「同一个『这段文字点没点某个实体的名』
如果有两处定义，迟早会一处放行一处拦下，而两边读数都是绿的。」*
「这条评论挂在哪个帖下」是同一族的判定，⛔ 两边各写一份必然静默漂移。
放在这里而不是放进 `relevance.py`，是因为 `tables.py` 反过来被 `relevance.py` import，
再加一条反向边就成环；本模块谁都不依赖，两边都能安全 import。

**键为什么不是 `permalink.split("?")[0]`**（§SEL-2 实测，两个坑）：

1. **裸切 `?` 会把 Hacker News 打成一锅粥**。HN 的链接形如
   `news.ycombinator.com/item?id=xxx`，身份就在 query 里；切掉之后
   `r-3b3482ca7f8b` 里 7 条 goal-1 的 HN 帖全部塌进同一个键。
   生产里早有一把对的尺子——`crossref.normalize_origin_url`，原文
   「去 scheme/www/追踪查询串/锚点；**保留 HN 等身份参数 id**」，
   采集侧 `source_mcp._origin_key` 与认行侧 `dao._comment_identity` 用的都是它。
   ⛔ 不自己写第二把（`reuse-before-rewrite`）。
2. **索引只能收父帖，不能收全部行**。小红书/抖音的评论没有自己的公开链接，
   入库 `permalink` 是在父帖链接上合成的——`r-3b3482ca7f8b` 里 563 条评论有
   485 条的去签名键与父帖**逐字相同**，27 个键同时挂着 1 个 post 和 17~20 条 comment。
   拿全量行建索引，命中谁取决于 `SELECT` 的返回顺序，尺子就不确定了。

**拿不到父帖就原样退回**（不在映射里），与 `rows_naming_entities`「拿不到叫法就不设闸」
同族：宁可不继承，也不要在拿不到判据时把池筛空、把归属写错。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: 评论行的 `kind`（与 `dao` / `tables._own_text` 同一个字面量）。
COMMENT_KIND = "comment"


def origin_key(permalink: Any) -> str:
    """链接的去签名形式；剥不动就原样返回，绝不因此丢一条评论。

    与 `app.adapters.source_mcp._origin_key` 逐字同一个办法（只读复用 `crossref`，
    一个字不改那个文件）。
    """
    from app.reliability.crossref import normalize_origin_url   # 只读复用，不改那个文件

    raw = str(permalink or "").strip()
    if not raw:
        return ""
    try:
        return normalize_origin_url(raw)
    except Exception:       # noqa: BLE001 —— 归一化失败不该掐掉继承
        return raw


def parent_key_of(row: Mapping[str, Any]) -> str:
    """这条评论的父帖键。非评论、或压根没有父帖线索时返回空串。

    优先读采集侧已经落下的 `extra.parent_origin_key`（`source_mcp` 写的，
    `dao._comment_identity` 认行时也优先按它比），缺了才回落现算 `parent_permalink`。
    """
    if str(row.get("kind") or "") != COMMENT_KIND:
        return ""
    extra = row.get("extra")
    if isinstance(extra, str):
        import json
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = {}
    if isinstance(extra, Mapping):
        persisted = str(extra.get("parent_origin_key") or "").strip()
        if persisted:
            return persisted
    return origin_key(row.get("parent_permalink"))


def parent_index(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """证据 id → 它的父帖行。**只收得进父帖的评论才在表里**，其余原样缺席。

    索引只拿 `kind != 'comment'` 的行建（理由见模块头注释 2）；同一个键撞上多个父帖时
    取**入参顺序里的第一个**——调用方喂的是 `ORDER BY id` 的全量行，所以这条是确定的。
    """
    by_key: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if str(row.get("kind") or "") == COMMENT_KIND:
            continue
        key = origin_key(row.get("permalink"))
        if key and key not in by_key:
            by_key[key] = row
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = parent_key_of(row)
        if not key:
            continue
        parent = by_key.get(key)
        # 自指防一手：真出现「父帖就是自己」时不建边，免得调用方绕圈。
        if parent is None or str(parent.get("id")) == str(row.get("id")):
            continue
        out[str(row.get("id"))] = parent
    return out
