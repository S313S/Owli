"""评论二跳的统一形状与最小清洗（§CMT-1 货 1）。

三个源（小红书 / Reddit / 抖音）各自的评论端点返回体字段名互不相同，
但下游（source_mcp 二跳、evidence 入库、评级、写手）只认一种形状：

    {parent_permalink, permalink, author, text, likes, published_at,
     platform, comment_id}

`permalink` 允许为空串——小红书没有公开的单条评论链接，入库时由调用方
以父帖链接加锚点参数合成（见 §CMT-1 货 3）。`comment_id` 为平台原生
评论 ID，是入库去重的首选键；两者都没有时才回落到
「父帖链接 + 作者 + 正文前 64 字」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


__all__ = ["Comment", "CommentBatch", "MIN_TEXT_LENGTH", "is_low_value", "clean"]

MIN_TEXT_LENGTH = 4

# 纯表情/标点/空白的评论没有观点，入库只会稀释证据池。
_MEANINGFUL = re.compile(
    r"[0-9A-Za-z一-鿿぀-ヿЀ-ӿ]"
)
_XHS_EMOJI_TAG = re.compile(r"\[[^\[\]]{1,12}\]")


@dataclass(frozen=True)
class Comment:
    parent_permalink: str
    permalink: str
    author: str
    text: str
    likes: int
    published_at: str | None
    platform: str
    comment_id: str


@dataclass(frozen=True)
class CommentBatch:
    """一条父帖的评论采集结果：清洗后的评论 + 丢弃计数 + 调用次数。"""

    comments: list[Comment] = field(default_factory=list)
    dropped_short: int = 0
    calls: int = 0


def is_low_value(text: str) -> bool:
    """长度不足或没有一个有意义字符（纯表情/标点）的评论不入库。"""

    stripped = str(text or "").strip()
    if len(stripped) < MIN_TEXT_LENGTH:
        return True
    # 小红书正文里的 [失望R] 这类表情标记先剥掉再判有没有真话。
    return _MEANINGFUL.search(_XHS_EMOJI_TAG.sub("", stripped)) is None


def published_at(value: Any) -> str | None:
    """秒级 Unix 时间戳转 UTC ISO；已是字符串则原样保留。"""

    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return None


def integer(item: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def clean(comments: list[Comment], *, limit: int) -> tuple[list[Comment], int]:
    """按原顺序丢掉低价值评论与重复评论，截到 limit 条，返回丢弃计数。"""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 必须为正整数")
    kept: list[Comment] = []
    seen: set[str] = set()
    dropped = 0
    for comment in comments:
        if is_low_value(comment.text):
            dropped += 1
            continue
        fingerprint = comment.comment_id or f"{comment.author} {comment.text[:64]}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        kept.append(comment)
        if len(kept) >= limit:
            break
    return kept, dropped
