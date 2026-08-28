"""信息源模块的分散声明契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_ND_PATTERN = re.compile(r"^([1-9]\d*)\s*d$", re.IGNORECASE)
_DIGITS_PATTERN = re.compile(r"([1-9]\d*)")

#: 人话时间窗 → `Nd`。§SRC-1 诊断：引擎实际传的是「不限时间」「all」
#: 「recent_1_year」这类写法，而三个源各自的 `^\d+d$` 正则把它们全打回，
#: 拒掉了四分之一的 source.* 调用。这里做兜底翻译，翻不出来才报错。
_WINDOW_ALIASES: dict[str, str] = {
    "all": "3650d", "any": "3650d", "unlimited": "3650d", "none": "3650d",
    "不限": "3650d", "不限时间": "3650d", "全部": "3650d", "所有时间": "3650d",
    "past_day": "1d", "today": "1d", "最近一天": "1d", "一天内": "1d",
    "past_week": "7d", "recent_1_week": "7d", "1w": "7d",
    "最近一周": "7d", "近一周": "7d", "一周内": "7d",
    "past_month": "30d", "recent_1_month": "30d", "1m": "30d",
    "最近一个月": "30d", "近一个月": "30d", "一个月内": "30d",
    "past_quarter": "90d", "recent_3_months": "90d", "3m": "90d",
    "最近三个月": "90d", "近三个月": "90d", "三个月内": "90d",
    "past_half_year": "183d", "recent_6_months": "183d", "6m": "183d",
    "最近半年": "183d", "近半年": "183d", "半年内": "183d",
    "past_year": "365d", "recent_1_year": "365d", "1y": "365d", "12m": "365d",
    "最近一年": "365d", "近一年": "365d", "一年内": "365d",
}


@dataclass(frozen=True)
class WindowParam:
    """`window` 参数在工具说明书里长什么样。

    `SOURCE_SPEC.window` 为 `None` 表示**这个源根本不要 window**——
    工具 schema 里就不会出现它，模型也不会被要求填一个用不上的参数
    （§SRC-1 货 3：抖音的 window 校验完就扔，是一道零收益、25% 拒绝率的闸门）。
    """

    examples: tuple[str, ...] = ("7d", "30d", "90d", "365d")
    description: str = (
        "时间窗，格式为「天数 + d」，例如 7d / 30d / 90d / 365d。"
        "也接受人话写法并自动折算：最近一年→365d、最近一个月→30d、"
        "最近一周→7d、不限/all→3650d。"
    )

    def normalize(self, value: Any) -> str:
        """把模型给的任意写法折算成 `Nd`；折算不出来才抛 ValueError。"""

        text = str(value or "").strip()
        if not text:
            raise ValueError(self.rejection_message(value))
        matched = _ND_PATTERN.fullmatch(text)
        if matched is not None:
            return f"{int(matched.group(1))}d"
        alias = _WINDOW_ALIASES.get(text.lower().replace(" ", "").replace("-", "_"))
        if alias is not None:
            return alias
        # 「30天」「近 90 天」这类：只要能取出唯一数字就按天算。
        digits = _DIGITS_PATTERN.findall(text)
        if len(digits) == 1:
            return f"{int(digits[0])}d"
        raise ValueError(self.rejection_message(value))

    def rejection_message(self, value: Any) -> str:
        """报错要把「该怎么写」一起说出来，别只说「你错了」。"""

        return (
            f"无法识别的时间窗 {value!r}；"
            f"请写成天数 + d，例如 {' / '.join(self.examples)}，"
            "或用「最近一年」「不限」这类说法。"
        )


@dataclass(frozen=True)
class SourceSpec:
    """源模块自带的最小注册信息。"""

    source_id: str
    tool_name: str
    entrypoint: Callable[..., Any]
    display_name: str = ""
    collector_name: str = ""
    capability_description: str = ""
    prompt_hint: str = ""
    #: `None` = 本源不向模型索取 window（工具 schema 里不出现该参数）。
    window: WindowParam | None = field(default_factory=WindowParam)

    def __post_init__(self) -> None:
        if not _SOURCE_ID_PATTERN.fullmatch(self.source_id):
            raise ValueError("source_id 必须是小写 snake_case")
        expected = f"source.{self.source_id}"
        if self.tool_name != expected:
            raise ValueError(f"工具名必须是 {expected}")
        if not callable(self.entrypoint):
            raise TypeError("entrypoint 必须可调用")
        if self.collector_name and not self.collector_name.endswith("数据抓取"):
            raise ValueError("collector_name 必须以‘数据抓取’结尾")


__all__ = ["SourceSpec", "WindowParam"]
