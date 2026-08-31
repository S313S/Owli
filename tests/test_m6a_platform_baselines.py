"""§M6-a 货 5/6：普通平台族（微博/知乎/公众号）的两处代码镜像补齐。

M6-0 把三行写进了 `docs/design/source-reliability.md` §2，但代码没跟上：
`PLATFORM_BASELINES` 缺键时一律回落 `web_search`（1/1/1/1/1），
于是文档写「微博交叉 0」、运行时拿到的是 1；知乎「同问题不同答主互为独立簇」
则靠 `crossref._institutions_differ` 里的 `platform_domains` 白名单，不在表里
的平台，两条同注册域名的证据判**同簇**。
"""

from __future__ import annotations

import pytest

from app.reliability.crossref import _institutions_differ
from app.reliability.scoring import PLATFORM_BASELINES, SCORE_FIELDS


@pytest.mark.parametrize(("platform", "scores"), [
    ("weibo", (1, 2, 0, 1, 1)),
    ("zhihu", (1, 1, 1, 1, 1)),
    ("wechat_mp", (1, 1, 0, 1, 1)),
])
def test_普通平台族三行与设计稿逐维一致(platform, scores) -> None:
    assert PLATFORM_BASELINES[platform] == dict(zip(SCORE_FIELDS, scores))


def test_微博与公众号的交叉基线是0不是回落来的1() -> None:
    """回落 web_search 会给交叉 1；§XSEM-1 条 3 之后这一分直接加在先验等级上。"""
    fallback = PLATFORM_BASELINES["web_search"]["score_crossref"]

    assert fallback == 1
    assert PLATFORM_BASELINES["weibo"]["score_crossref"] == 0
    assert PLATFORM_BASELINES["wechat_mp"]["score_crossref"] == 0


def _row(permalink: str, author: str) -> dict:
    return {"platform": "zhihu", "permalink": permalink, "author_name": author,
            "author_meta": None, "extra": {}}


def test_知乎同问题不同答主互为独立簇() -> None:
    assert _institutions_differ(
        _row("https://www.zhihu.com/question/1/answer/11", "答主甲"),
        _row("https://www.zhihu.com/question/1/answer/22", "答主乙"),
    ) is True


def test_不在白名单的平台同域名仍判同簇() -> None:
    left = _row("https://weibo.com/1/abc", "博主甲")
    right = _row("https://weibo.com/2/def", "博主乙")
    left["platform"] = right["platform"] = "weibo"

    assert _institutions_differ(left, right) is False
