"""§RPT-9：表格要自己交代清楚——「其他」是什么、没角标为什么、空正文别算进分母。

四条用户意见/新问题各自有一个红侧：

- 货 1（用户意见第 9 条）：场景表的「其他」占到 28%，表下一个字不解释它是什么；
- 货 2（第 5 条呈现半）：对照实体表里文心一言负 20 条、角标列整片「—」，表下无交代；
- 货 3（新问题 13）：2 条只有题面没有正文的 Reddit 链接帖被计进「已编码 90 条」；
- 货 4②（第 8 条归类半）：抖音评论把**父帖标题**「放过豆包吧！」摘成自己的原声，
  进了正式稿的「交互体验·正」，而它自己说的是「不许碰我的电子闺蜜」。

⛔ 本文件的夹具一律带 `content_excerpt`：真行永远有自己的正文，
省掉那一栏就造不出本包要拦的两种坏形状。
"""

from __future__ import annotations

from app.reliability.coding import (
    CODING_VERSION,
    SCENARIO_OTHER,
    coding_targets,
    has_own_body,
    own_source_text,
    polish_tables,
    quote_context,
    quote_is_own_words,
)


def _row(index: int, *, scenario="学习", topics=("功能与能力",), attitude="中",
         quote="", body=None, title=None, kind="post", liked=0,
         citation_no=None, grade="B", audience="不明", coded=True):
    """一条已编码的 UGC。`body` 不给就拿 quote 当正文（真行就是这个关系）。"""
    return {
        "id": f"ev-{index:03d}", "platform": "xhs", "kind": kind, "grade": grade,
        "title": title, "content_excerpt": body if body is not None else (quote or "这条的正文"),
        "citation_no": citation_no,
        "raw_metrics": {"liked_count": liked, "comments_count": 0, "collected_count": 0},
        "extra": {"content_kind": "user_opinion", **({"coding": {
            "coding_version": CODING_VERSION, "audience": audience, "scenario": scenario,
            "attitude": attitude, "topics": list(topics), "quote": quote,
        }} if coded else {})},
    }


def _basis(rows, name, **kwargs):
    return polish_tables(rows, **kwargs)[name]["basis"]


# --------------------------------------------------------------------------
# 货 1 · 「其他」必须自己交代构成
# --------------------------------------------------------------------------

def test_其他占比超阈值时场景表下面出构成说明():
    """⭐ 红侧：修前 `scenario_counts.basis` 里没有一个字交代「其他」是什么。"""
    rows = ([_row(i, scenario=SCENARIO_OTHER, topics=("竞品对比",)) for i in range(3)]
            + [_row(10 + i, scenario=SCENARIO_OTHER, topics=("价格与付费", "广告与推广"))
               for i in range(2)]
            + [_row(20 + i, scenario="学习") for i in range(5)])

    basis = _basis(rows, "scenario_counts")

    assert "**不是一种使用场景**" in basis, "得先说清「其他」不是一个被漏掉的场景"
    assert f"「{SCENARIO_OTHER}」这一行的 5 条" in basis
    assert "竞品对比 3 条" in basis, "构成数要从现有 coding.topics 算出来"
    assert "价格与付费 2 条" in basis and "广告与推广 2 条" in basis
    assert "一条可命中多个主题" in basis, "相加大于条数要自己说明，否则读者以为算错了"


def test_其他占比不到阈值就不加这句话():
    """口径栏是给人读的。兜底桶只有零头时不必啰嗦——阈值 20%，依据写在常量注释里。"""
    rows = ([_row(0, scenario=SCENARIO_OTHER, topics=("竞品对比",))]
            + [_row(10 + i, scenario="学习") for i in range(19)])

    assert "**不是一种使用场景**" not in _basis(rows, "scenario_counts")


def test_构成说明按互动量取两条例子且走原声表同一道正文子串闸():
    """例子只从 `coding.quote` 取、且必须是本人自己的话，⛔ 不另写一道摘句逻辑。"""
    rows = ([_row(1, scenario=SCENARIO_OTHER, topics=("竞品对比",),
                  quote="豆包和它们比起来更像玩具。", liked=900),
             _row(2, scenario=SCENARIO_OTHER, topics=("竞品对比",),
                  quote="这几家我都试过一轮。", liked=500),
             _row(3, scenario=SCENARIO_OTHER, topics=("竞品对比",),
                  quote="互动量最低的这句不该被选中。", liked=1),
             # 评论摘了父帖标题：闸要把它挡在例子之外（货 4② 同一道闸）
             _row(4, scenario=SCENARIO_OTHER, topics=("竞品对比",), kind="comment",
                  title="评论 · 谁才是最强国产 AI", quote="谁才是最强国产 AI",
                  body="前排", liked=9999)]
            + [_row(20 + i, scenario="学习") for i in range(6)])

    basis = _basis(rows, "scenario_counts")

    assert "例如：" in basis
    assert "「豆包和它们比起来更像玩具。」" in basis, "互动量最高的该排第一"
    assert "「这几家我都试过一轮。」" in basis
    assert "互动量最低的这句不该被选中" not in basis, "只取两条"
    assert "谁才是最强国产 AI" not in basis, \
        "摘了父帖标题的不是本人说的话，不许当例子"
    assert "本轮未进引用池、点不到原文" in basis, \
        "没角标的例子要写明点不到原文，⛔ 不许装作能点开"


# --------------------------------------------------------------------------
# 货 2 · 有条数就得有交代
# --------------------------------------------------------------------------

def test_有条数没角标的格子表下必须交代是哪一层没给():
    """⭐ 红侧：修前这几张表只有一句笼统的「没进池」，数不出是几个格子几条，
    对照实体表更是一个字都没有。"""
    rows = [_row(1, scenario="学习", attitude="正", citation_no=7),
            _row(2, scenario="办公", attitude="负"),
            _row(3, scenario="办公", attitude="负"),
            _row(4, scenario="编程", attitude="负", grade="D")]

    basis = _basis(rows, "scenario_attitude", citations={"ev-001": 7})

    assert "本表有 2 个格子有条数、一个角标都没有，共 3 条" in basis
    assert "本轮没进引用池" in basis
    assert "其中 1 条是 D 级线索、本来就不进池" in basis, \
        "D 级不进池是设计如此，与「这一轮取数没排上」不是一回事"
    assert "另 2 条是这一轮取数没排上" in basis
    assert "数据缺失" not in basis, "⛔ 糊弄话：说不出是哪一层等于没交代"


def test_全部格子都有角标时不加这句():
    rows = [_row(1, scenario="学习", attitude="正", citation_no=7)]
    assert "一个角标都没有" not in _basis(rows, "scenario_attitude",
                                         citations={"ev-001": 7})


def test_对照实体表同样要交代不是只修露出来的那一张():
    """⚠️ 通用规矩：同族的表都要照办。用户 09-18 圈的正是这张附录表。"""
    subject = {"ev-001": {"entity": "主体", "entity_name": "豆包"}}
    contrast = {f"ev-{i:03d}": {"entity": "对照", "entity_name": "文心一言"}
                for i in (2, 3)}
    rows = [_row(1, scenario="学习", citation_no=7),
            _row(2, scenario="办公", attitude="负"),
            _row(3, scenario="办公", attitude="负")]

    basis = polish_tables(rows, citations={"ev-001": 7},
                          entity_roles={**subject, **contrast})["contrast_attitude"]["basis"]

    assert "本表有 1 个格子有条数、一个角标都没有，共 2 条" in basis
    assert "本轮没进引用池" in basis


def test_主题表与人群表也照办():
    rows = [_row(1, scenario="学习", topics=("功能与能力",), citation_no=7,
                 audience="学生"),
            _row(2, scenario="学习", topics=("价格与付费",), attitude="负",
                 audience="职场")]
    tables = polish_tables(rows, citations={"ev-001": 7})

    assert "一个角标都没有" in tables["attitude_by_topic"]["basis"]
    assert "一个角标都没有" in tables["audience_attitude"]["basis"]


# --------------------------------------------------------------------------
# 货 3 · 空正文不算进编码分母
# --------------------------------------------------------------------------

def test_只有题面没有正文的行不进编码分母():
    """⭐ 红侧：修前 `polish_tables` 把它当一条「已编码评论」计进 n。

    真机两条都是 Reddit 图片/视频链接帖，原帖**本来就没有正文**——
    不是采集丢了，补采也补不出来。
    """
    rows = [_row(1, scenario="学习"), _row(2, scenario="办公"),
            _row(3, scenario=SCENARIO_OTHER, title="Я скачал doubao...", body="")]

    tables = polish_tables(rows)

    assert tables["scenario_counts"]["n"] == 2, "分母只数有正文的那两条"
    assert sum(r["条数"] for r in tables["scenario_counts"]["rows"]) == 2
    assert tables["scenario_counts"]["coverage"]["只有题面无正文条数"] == 1


def test_剔掉的条数不许静默要写在表下():
    rows = [_row(1, scenario="学习"),
            _row(2, scenario="办公", title="只有题面", body="   ")]

    basis = _basis(rows, "scenario_counts")

    assert "另有 1 条已编码的行只有帖子题面、没有正文" in basis
    assert "这不是漏采——原帖本身就没有正文" in basis, \
        "⛔ 不许写成待补的缺口：补采补不出来"


def test_编码不再挑只有题面的行():
    """闸下沉到 `coding_targets`：将来这种行连编码的钱都不该付。"""
    bodyless = _row(9, title="ByteDance's Doubao Phone 2 drops this September",
                    body="", coded=False)
    normal = _row(1, quote="有正文", coded=False)

    assert has_own_body(normal) is True
    assert has_own_body(bodyless) is False
    assert [item["id"] for item in coding_targets([normal, bodyless])] == ["ev-001"]


def test_没编码的空正文行不受影响():
    """⚠️ 只踢**已编码**的：没编码的行本来就不进任何一张编码表，
    连带踢掉会让别处的读数无声改口径。"""
    plain = _row(900, title="只有题面", body="", coded=False)
    rows = [_row(1, scenario="学习"), plain]

    tables = polish_tables(rows, total_evidence=2)

    assert tables["scenario_counts"]["n"] == 1
    assert tables["scenario_counts"]["coverage"]["全库证据条数"] == 2
    assert "只有题面无正文条数" not in tables["scenario_counts"]["coverage"]


# --------------------------------------------------------------------------
# 货 4 · 评论不许把父帖标题当成自己的话
# --------------------------------------------------------------------------

#: 真机原话，一字不改：抖音视频《放过豆包吧！它只是一个无性别的 ai…》下的一条评论，
#: 编码把**父帖标题的前半句**摘成了这条评论的原声，S39 就这么进了正式稿。
_PARENT_TITLE = "评论 · 放过豆包吧！它只是一个无性别的ai（无任何不良引导）#豆包 #ai #离谱"
_BORROWED = "放过豆包吧！"
_OWN_WORDS = "不许碰我的电子闺蜜[发怒]"


def test_评论摘了父帖标题的不算它自己的话():
    """⭐ 红侧：修前 `quote_is_own_words` 不存在，这句是 `source_text` 的子串，
    编码期与出表期两道闸都判它合格。"""
    borrowed = _row(1, kind="comment", title=_PARENT_TITLE,
                    quote=_BORROWED, body=_OWN_WORDS, citation_no=39)
    genuine = _row(2, kind="comment", title=_PARENT_TITLE,
                   quote=_OWN_WORDS, body=_OWN_WORDS, citation_no=40)

    assert quote_is_own_words(borrowed) is False
    assert quote_is_own_words(genuine) is True
    # 帖子行的标题是发帖人自己写的，照旧算他的话
    assert own_source_text(_row(3, title="标题", quote="标题")).startswith("标题")
    assert own_source_text(borrowed) == _OWN_WORDS


def test_借来的原声不进表且被计数交代():
    rows = [_row(1, kind="comment", title=_PARENT_TITLE, attitude="正",
                 topics=("交互体验",), quote=_BORROWED, body=_OWN_WORDS,
                 citation_no=39, liked=2)]

    tables = polish_tables(rows, citations={"ev-001": 39})

    assert tables["quotes"]["rows"] == [], "父帖标题不许当成评论人的原声"
    assert "不是发言人自己的话" in tables["quotes"]["basis"], \
        "少了一行要自己交代，⛔ 不许静默筛掉"
    assert "那是楼主写的" in tables["quotes"]["basis"]


def test_同一父帖下六条评论借同一句标题全部拦下():
    """真机 r-3b3482ca7f8b：同一个父帖下 6 条评论的 quote 都是这半句标题。"""
    rows = [_row(i, kind="comment", title=_PARENT_TITLE, attitude="正",
                 topics=("交互体验",), quote=_BORROWED, body=f"这是第 {i} 条评论自己的话",
                 citation_no=30 + i, liked=i) for i in range(1, 7)]

    tables = polish_tables(rows, citations={f"ev-{i:03d}": 30 + i for i in range(1, 7)})

    assert tables["quotes"]["rows"] == []


def test_真人自己说的话一条都不许被误伤():
    """这道闸的风险是丢太多。评论说自己的话、帖子引自己的标题，都得留住。"""
    rows = [_row(1, kind="comment", title=_PARENT_TITLE, attitude="正",
                 topics=("交互体验",), quote=_OWN_WORDS, body=_OWN_WORDS,
                 citation_no=39, liked=5)]

    tables = polish_tables(rows, citations={"ev-001": 39})

    assert [r["原声"] for r in tables["quotes"]["rows"]] == [_OWN_WORDS]


def test_原声表带出处栏让读者看得出上下文():
    """货 4③：「🔹豆包：app标识就很社交感。」编码没判错，错在读者看不见
    它出自一篇一口气点评 5 个产品的盘点帖。出处是库里现成的事实，摆出来就够。"""
    rows = [_row(1, attitude="正", topics=("功能与能力",),
                 title="在手机用上这五个免费AI的我简直强的可怕",
                 quote="🔹豆包：app标识就很社交感。",
                 body="……🔹豆包：app标识就很社交感。整体的交互感非常好。",
                 citation_no=96, liked=23579)]

    table = polish_tables(rows, citations={"ev-001": 96})["quotes"]

    assert "出处" in table["columns"]
    assert table["rows"][0]["出处"] == "帖子《在手机用上这五个免费AI的我简直强的可怕》正文"
    assert quote_context(_row(2, kind="comment", title=_PARENT_TITLE)) \
        .startswith("《放过豆包吧！"), "评论要显示它挂在哪一篇下面"
    assert "评论 · " not in quote_context(_row(3, kind="comment", title=_PARENT_TITLE)), \
        "机器前缀不摆给读者看"
