"""§ENT-1 货 1：规划期实体卡解析——同一产品在国内外叫什么，是不是同一个产品。

**它回答的问题**：题面写 workbuddy，采集就拿 WorkBuddy 去搜小红书；写「豆包」，
海外源就搜不到 Doubao。规划期在生成 goals 之前多走一步：把骨架已经认定的研究
实体（`subjects`）逐个查一次网页、再让模型出一张实体卡，把中外叫法、官方账号、
「中外名字是不是同一个产品」写进计划，供采集查询词组装（货 4）、计划编辑页
（货 5）与报告「研究对象」节（货 6）共用。

**为什么复用 subjects 而不另抽一遍**：骨架提示词已经把「可被搜索引擎当专有名词
的产品/品牌/公司」这条限定写死，且下游规则 25（每个 subject 必须有采集章）、
分配表、采集卡 `entity` 全按 subjects 的原字符串对齐。另起一套抽取只会多一个
对不齐的名字空间——所以 `Entity.id` 就取 subjects 里的原名。

**抽不出实体、查不动网、模型不配合，都不阻塞规划**：整步降级成 `entities=[]`，
计划照常生成，只是没有别名扩展。规划期仍走 Claude（`planning-stays-on-claude`）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping, Sequence

from app.plan.model import Entity

#: 每份计划最多几张实体卡（用户 2026-09-03 午后拍板）。多出来的让用户在编辑页补。
MAX_ENTITIES = 5

#: 每个实体查一次网页时取几条命中；只用来喂模型，不进证据库。
SEARCH_RESULTS_PER_ENTITY = 5

#: 网页搜索的时间窗：实体的中外叫法是长期事实，窗口开大一点更容易命中官网与百科。
SEARCH_WINDOW = "1095d"

#: 单张卡最多问几轮（§ENT-2 货 4）。第二轮是补丁式的，只让模型改折不动的那一处。
#: 这不额外放宽预算——`PlanSegmentWorkspace` 按段名数总尝试次数，数满照样抛。
CARD_ATTEMPTS = 2


def _card_defect(value: Any) -> str:
    """卡折不动的原因；返回空串表示**不该重试**（模型自己说了这不是实体）。"""
    if not isinstance(value, Mapping):
        return "顶层不是一个 JSON object"
    if "canonical" in value and not str(value.get("canonical") or "").strip():
        return ""
    if not str(value.get("canonical") or "").strip():
        return "缺 canonical（这个产品最常用的正式名）"
    return "字段类型不对，names 必须是 object、same_product 必须是 true/false"


def _search_context(items: Any) -> str:
    """把网页搜索命中折成喂给模型的几行线索；没有命中就返回空串。"""
    lines: list[str] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip().replace("\n", " ")
        permalink = str(item.get("permalink") or "").strip()
        excerpt = str(item.get("content_excerpt") or "").strip().replace("\n", " ")
        if not (title or excerpt):
            continue
        lines.append(f"- {title}｜{permalink}｜{excerpt[:180]}")
    return "\n".join(lines[:SEARCH_RESULTS_PER_ENTITY])


def entity_prompt(
    query: str, name: str, context: str,
    *, defect: str = "", previous: str | None = None,
) -> str:
    """单张实体卡的短流提示词；只输出一个 JSON object，不带任何说明文字。

    `defect` / `previous` 是 §ENT-2 货 4 的补丁式重试（沿 §PLAN-1 规则 21 写法）：
    上一轮的卡折不动时，把原文和折不动的原因一起递回去，只让它改那一处。
    """
    evidence = (
        f"下面是刚查到的网页线索（标题｜链接｜摘录），只作参考，写不进卡就别硬凑：\n{context}\n"
        if context.strip()
        else "这次没查到可用的网页线索，只按你已知的事实写；不确定的字段留空或留 null。\n"
    )
    return (
        "你在为一次市场调研确认「研究对象到底是哪个产品」。\n"
        f"调研需求原文：{query}\n"
        f"本次要确认的研究对象：{name}\n\n"
        f"{evidence}\n"
        "只输出一个 JSON object，字段表如下，不要输出任何解释文字或代码围栏之外的内容：\n"
        "{\n"
        '  "canonical": "这个产品最常用的正式名（可以就是研究对象原名）",\n'
        '  "names": {"zh": "中文正式名或 null", "en": "英文/罗马化正式名或 null",\n'
        '            "aliases": ["其他会被人写进帖子标题的叫法，最多 4 个"]},\n'
        '  "official_handles": {"平台键": "官方账号名"},\n'
        '  "same_product": true,\n'
        '  "note": "一句话说清这是什么产品；same_product=false 时必须写清差异"\n'
        "}\n\n"
        "判定规则，逐条照做：\n"
        "1. `names.en` 只能填**这个产品自己的**英文名或罗马化名，不能填同公司面向"
        "另一个市场的另一个产品。抖音的 en 是 Douyin，不是 TikTok；飞书的 en 是 "
        "Feishu，不是 Lark。\n"
        "2. `same_product` 说的是**中文名与英文名是不是同一个产品**：豆包 / Doubao "
        "是同一个（true）；抖音 / TikTok、飞书 / Lark 是两个独立产品（false）。"
        "填 false 时 `note` 必须写清它们在数据、内容生态或功能上的差异。\n"
        "3. `aliases` 只写真会出现在帖子和评论里的叫法（简称、旧名、大小写变体、"
        "常见误写）；不要写「AI 助手」这类品类词，也不要把同类竞品写进去。\n"
        "4. `official_handles` 的平台键只能取："
        "xhs / douyin / weibo / wechat_mp / x / reddit / bilibili / zhihu；"
        "没有把握就给空对象 {}，宁缺毋滥。\n"
        "5. 全部字段都写不出来（这不是一个真实存在的产品/品牌/公司）时，"
        '只输出 {"canonical": ""}，规划会把它跳过。\n'
        + _patch_tail(defect, previous)
    )


def _patch_tail(defect: str, previous: str | None) -> str:
    """§ENT-2 货 4：补丁式重试的尾巴——只改报错处，其余逐字保留。"""
    if not defect:
        return ""
    tail = f"\n上一轮这张卡没能成形：{defect}。"
    if previous:
        tail += f"\n上一轮原文={previous}\n只修改上面点名的地方，其余字段逐字保留，"
        tail += "仍输出完整 JSON。"
    else:
        tail += "重新输出一个符合上面字段表的完整 JSON object。"
    return tail


_HANDLE_PLATFORMS = frozenset({
    "xhs", "douyin", "weibo", "wechat_mp", "x", "reddit", "bilibili", "zhihu",
})
_MAX_ALIASES = 4

# §ENT-2 货 3（ENT-1 挂账②）：别名清洗。ENT-1 真机样本里模型吐过「跳动」（字节跳动
# 的截断）、「TT」「DS」这类两字缩写，还把 Lark 写进了飞书卡的别名——而那张卡自己
# 刚写完 same_product=false。别名有两条下游：它是第二个检索词（`entity_queries`），
# 也进规则 32 的闭集，所以噪音别名既污染召回也污染 lint。
_MIN_LATIN_ALIAS = 3   # 「TT」「DS」两字母缩写在正文里误命中率太高，不作检索词
_MIN_CJK_ALIAS = 3     # 「跳动」同理；本实体自己的正式名走的是另一条路，不受此限
_MIN_CJK_OVERLAP = 2   # 「飞书文档」与「飞书」共 2 字 → 认得出是同一串名字
_MIN_LATIN_OVERLAP = 3


def _has_cjk(text: str) -> bool:
    return any("一" <= char <= "\u9fff" for char in text)


def _normalized(text: str) -> str:
    return "".join(
        char.lower() for char in text if char.isalnum() or _has_cjk(char)
    )


def _overlap(left: str, right: str) -> int:
    """两个名字的最长公共子串长度（已归一化）。"""
    best = 0
    for start in range(len(left)):
        for end in range(start + best + 1, len(left) + 1):
            if left[start:end] in right:
                best = end - start
            else:
                break
    return best


def _looks_like_same_name(alias: str, names: Sequence[str]) -> bool:
    """别名是不是本实体某个正式名的可辨认变体（大小写、加后缀、加空格…）。"""
    text = _normalized(alias)
    if not text:
        return False
    for name in names:
        other = _normalized(name)
        if not other:
            continue
        floor = _MIN_CJK_OVERLAP if _has_cjk(text) or _has_cjk(other) else _MIN_LATIN_OVERLAP
        if _overlap(text, other) >= min(floor, len(text), len(other)):
            return True
    return False


def clean_aliases(
    aliases: Sequence[str], names: Sequence[str], *, same_product: bool,
) -> list[str]:
    """丢掉噪音别名。`names` 是本实体的正式名（id / canonical / zh / en）。

    三条闸，逐条都有真机样本：
    1. **太短**：拉丁 <3 字母、中文 <3 字的别名一律丢（「TT」「DS」「跳动」）。
       等于本实体某个正式名的除外——「豆包」本来就是两个字。
    2. **截断**：是某个正式名的真子串（「字节跳动」→「字节」）。截断不带来新召回，
       只带来更宽的误命中。代价：canonical 带后缀时（「豆包 AI」→「豆包」）会误伤，
       但那种情况正式名里通常已有干净的一个，第二个检索词位留给更不一样的叫法更值。
    3. **`same_product=false` 的对方名字**：卡自己都说了中外名不是同一个产品，
       那么和本实体所有正式名都认不出关系的别名（飞书卡里的「Lark」、抖音卡里的
       「TikTok」），基本只能是那个被排除掉的对方产品，绝不放行。
       `same_product=true` 时不设这一闸——豆包的「Cici」正是长得完全不一样的真别名。
    """
    formal = [str(name).strip() for name in names if str(name).strip()]
    lowered = {name.lower() for name in formal}
    kept: list[str] = []
    for raw in aliases:
        alias = str(raw).strip()
        if not alias or alias.lower() in {item.lower() for item in kept}:
            continue
        if alias.lower() not in lowered:
            floor = _MIN_CJK_ALIAS if _has_cjk(alias) else _MIN_LATIN_ALIAS
            if len(alias.replace(" ", "")) < floor:
                continue
            if any(alias != name and alias in name for name in formal):
                continue
            if not same_product and not _looks_like_same_name(alias, formal):
                continue
        kept.append(alias)
    return kept


def entity_card(value: Any, *, entity_id: str) -> Entity | None:
    """把模型返回的 JSON 折成实体卡；折不动或模型自认不是实体时返回 None。

    机械修正在这里做完（`§PLAN-1` 的口径：确定性的就代码修，别回灌让模型重写）：
    别名去重截断、平台键过闭集、`same_product=false` 但 note 空时降级成 true。
    """
    if not isinstance(value, Mapping):
        return None
    canonical = str(value.get("canonical") or "").strip()
    if not canonical:
        return None
    raw_names = value.get("names")
    names: dict[str, Any] = raw_names if isinstance(raw_names, Mapping) else {}
    handles = {
        str(key): str(handle).strip()
        for key, handle in (value.get("official_handles") or {}).items()
        if str(key) in _HANDLE_PLATFORMS and str(handle).strip()
    } if isinstance(value.get("official_handles"), Mapping) else {}
    note = str(value.get("note") or "").strip()
    same_product = value.get("same_product")
    same_product = True if not isinstance(same_product, bool) else same_product
    # 清洗要在 same_product 定下来之后做——第三条闸只对 same_product=false 生效。
    aliases = clean_aliases(
        [a for a in names.get("aliases") or [] if str(a).strip() not in {entity_id, canonical}],
        [entity_id, canonical, names.get("zh"), names.get("en")],
        same_product=same_product,
    )
    if not same_product and not note:
        # note 是 same_product=false 的硬要求；模型没写就不敢下这个断言，
        # 退回 true 比整卡作废好——退回只是少一条「不要交叉」的提示。
        same_product = True
    try:
        return Entity.from_dict({
            "id": entity_id,
            "canonical": canonical,
            "names": {
                "zh": _name_or_none(names.get("zh")),
                "en": _name_or_none(names.get("en")),
                "aliases": aliases[:_MAX_ALIASES],
            },
            "official_handles": handles,
            "same_product": same_product,
            "note": note,
        })
    except (TypeError, ValueError):
        return None


def _name_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


async def _web_lookup(name: str, search: Any) -> str:
    """给一个实体查一次网页；查不动就返回空线索，绝不把规划带死。

    `web_search.search` 是同步阻塞的 HTTP 调用，放线程里跑，别占住规划的事件循环。
    """
    if search is None:
        return ""
    try:
        items = await asyncio.to_thread(
            search,
            f"{name} 官网 英文名 是什么产品",
            SEARCH_WINDOW,
            max_results=SEARCH_RESULTS_PER_ENTITY,
        )
    except Exception:  # 缺凭证、限流、超时、代理伪装的 403 —— 一律降级成无线索
        return ""
    return _search_context(items)


async def resolve_entities(
    query: str,
    subjects: list[str],
    workspace: Any,
    adapter: Any,
    *,
    on_progress: Any = None,
    search: Any = None,
) -> list[dict[str, Any]]:
    """逐个实体查一次网页 + 一次模型调用，产出 ≤MAX_ENTITIES 张实体卡。

    单张卡失败只丢这一张（记一条进度），不影响其余实体，也不影响计划生成。
    """
    cards: list[dict[str, Any]] = []
    for index, name in enumerate(subjects[:MAX_ENTITIES], start=1):
        entity_id = str(name).strip()
        if not entity_id:
            continue
        context = await _web_lookup(entity_id, search)
        segment = f"entity-{index}"
        card = None
        defect = ""
        for attempt in range(1, CARD_ATTEMPTS + 1):
            try:
                value = await workspace.generate(
                    segment,
                    entity_prompt(
                        query, entity_id, context, defect=defect,
                        previous=workspace.previous_text(segment) if defect else None,
                    ),
                    adapter,
                )
            except Exception as exc:
                # 段预算耗尽也走这里：workspace.generate 自己数着次数，
                # 补丁重试不会把预算翻倍，只是把最后一次问得更准。
                await _progress(on_progress, f"实体卡 {entity_id} 解析失败，跳过：{exc}")
                break
            card = entity_card(value, entity_id=entity_id)
            if card is not None:
                break
            defect = _card_defect(value)
            if not defect:  # 模型明确说「这不是一个真实存在的产品」——不该再问一遍
                await _progress(on_progress, f"实体卡 {entity_id} 模型判定不是实体，跳过")
                break
            if attempt < CARD_ATTEMPTS:
                await _progress(
                    on_progress, f"实体卡 {entity_id} 未成形，按报错处补一轮：{defect}",
                )
        if card is None:
            if defect:
                await _progress(on_progress, f"实体卡 {entity_id} 未成形，跳过")
            continue
        cards.append(card.to_dict())
        await _progress(
            on_progress,
            f"实体卡 {entity_id}：zh={card.names.get('zh')} en={card.names.get('en')} "
            f"别名 {len(card.names.get('aliases') or [])} 个"
            f"{'' if card.same_product else '｜中外名字不是同一个产品'}",
        )
    return cards


async def _progress(on_progress: Any, text: str) -> None:
    if on_progress is None:
        return
    result = on_progress(text)
    if asyncio.iscoroutine(result):
        await result


__all__ = [
    "CARD_ATTEMPTS", "MAX_ENTITIES", "clean_aliases", "entity_card",
    "entity_prompt", "resolve_entities",
]
