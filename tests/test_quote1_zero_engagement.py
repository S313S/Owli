"""§QUOTE-1 货 2：没人理的原声不许当代表——降权 + 表里标注，⛔ 不排除。

为什么是降权不是排除：本包拿 `r-3e04f808dffd` 的真数据试过——287 条已编码行里
80 条互动量正好是 0（小红书评论的 `likes` 天生就是 0），排除会让「交互体验/负」
那一格唯一的原声消失、整格变空。空格会被读成「没人这么说」，比一条冷门原声
更误导，所以留着、降到后面、并在表里说清「无人点赞或评论」。
"""

from app.reliability.coding import CODING_VERSION, coding_tables, polish_tables


def _row(index: int, *, quote: str, likes: int | None, attitude: str = "负"):
    row = {
        "id": f"ev-{index:03d}", "platform": "xhs", "grade": "B",
        "title": "t", "content_excerpt": quote,
        "extra": {"content_kind": "user_opinion", "coding": {
            "coding_version": CODING_VERSION, "audience": "不明", "scenario": "其他",
            "attitude": attitude, "topics": ["功能与能力"], "quote": quote}},
    }
    if likes is not None:
        row["raw_metrics"] = {"liked_count": likes}
    return row


def _quotes(rows, **kwargs):
    return coding_tables(rows, **kwargs)["quotes"]


def test_零互动的排在有互动量的后面():
    rows = [_row(0, quote="没人理的一句。", likes=0),
            _row(1, quote="很多人附和的一句。", likes=500)]
    assert [q["quote"] for q in _quotes(rows)] == ["很多人附和的一句。", "没人理的一句。"]


def test_取不到互动量的排在零互动的后面():
    # 三档要分得开：有人理 > 零互动 > 该平台压根没给互动数。
    rows = [_row(0, quote="取不到的一句。", likes=None),
            _row(1, quote="零互动的一句。", likes=0),
            _row(2, quote="有人理的一句。", likes=9)]
    assert [q["quote"] for q in _quotes(rows)] == [
        "有人理的一句。", "零互动的一句。", "取不到的一句。"]


def test_零互动的仍然入表不被排除():
    # 一格里只有零互动的行时，表里还得有它——排除会把整格清空。
    rows = [_row(0, quote="唯一的一句。", likes=0)]
    assert [q["quote"] for q in _quotes(rows)] == ["唯一的一句。"]


def test_表里带得出无人点赞的标注():
    rows = [_row(0, quote="没人理的一句。", likes=0),
            _row(1, quote="取不到的一句。", likes=None),
            _row(2, quote="有人理的一句。", likes=9)]
    notes = {q["quote"]: q["engagement_note"] for q in _quotes(rows)}
    assert notes["没人理的一句。"] == "无人点赞或评论"
    assert notes["取不到的一句。"] == "该平台未提供互动数"
    assert notes["有人理的一句。"] == ""


def test_正式稿那张表出得了代表性这一列():
    # 标注要走到**写手看得见的那张表**上，只落在中间结构里等于没做。
    rows = [_row(0, quote="没人理的一句。", likes=0)]
    table = polish_tables(rows, citations={"ev-000": 7})["quotes"]
    assert "代表性" in table["columns"]
    assert table["rows"][0]["代表性"] == "无人点赞或评论"
    assert "不得把它们写成多数人的看法" in table["basis"]


def test_措辞表有公开名且与内部名是同一份():
    """§QUOTE-2 逐字复用这套措辞，两处必须永远一致。

    锁的是**结构**不是人的记性：只要它 import 的是同一个对象，就不可能分叉。
    抄一份不会立刻出错，会在将来某次改词时悄悄分叉——那时没人会想到去比对两处。
    """

    from app.reliability import coding

    assert coding.ENGAGEMENT_NOTES is coding._ENGAGEMENT_NOTES
    assert "ENGAGEMENT_NOTES" in coding.__all__
    # 措辞本身也钉住：改词等于同时改两个包的呈现，得有人先看见这条红。
    assert coding.ENGAGEMENT_NOTES[coding._ZERO_ENGAGEMENT] == "无人点赞或评论"
    assert coding.ENGAGEMENT_NOTES[coding._UNMEASURED] == "该平台未提供互动数"
    assert coding.ENGAGEMENT_NOTES[coding._ENGAGED] == ""


def test_分档函数是公开契约():
    # §QUOTE-2 直接 import 它；名字与签名冻结，改要先报调度。
    from app.reliability import coding

    assert "engagement_tier" in coding.__all__
    assert coding.engagement_tier({"platform": "xhs", "raw_metrics": {"liked_count": 9}}) == 0
    assert coding.engagement_tier({"platform": "xhs", "raw_metrics": {"liked_count": 0}}) == 1
    assert coding.engagement_tier({"platform": "xhs"}) == 2
