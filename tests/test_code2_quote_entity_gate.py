"""§CODE-2：原声必须点名被评实体，且同一句不跨格重复。

护的是评审第二轮 #3 那条真机缺陷：正式稿把「但是workbuddy不会觉得自己是你的
对立面」标成豆包的**正向**原声，而原帖这句夸的是 WorkBuddy、语境在暗踩豆包——
逐字摘对了，摘的却是在夸别人的话。子串闸拦不住它，因为它确实是子串。
"""

from __future__ import annotations

from app.reliability.coding import CODING_VERSION, coding_tables, polish_tables

#: 就是真机上引反的那一句，一字不改。
_MISQUOTE = "但是workbuddy不会觉得自己是你的对立面"
_ENTITY = ["豆包", "Doubao", "豆包AI"]


def _coded(index: int, quote: str, *, attitude: str = "正",
           topics: list[str] | None = None, platform: str = "weibo"):
    return {
        "id": f"ev-{index:03d}", "platform": platform, "goal_id": "goal-2",
        "extra": {"coding": {
            "coding_version": CODING_VERSION, "audience": "不明", "scenario": "工作",
            "attitude": attitude, "topics": topics or ["功能与能力"], "quote": quote,
        }},
    }


def _quotes(rows, **kwargs):
    return coding_tables(rows, **kwargs)["quotes"]


def test_不点名被评实体的原声被丢弃且计数():
    """货 1 造红：喂一条不含叫法的 quote，必须被丢弃且计数 +1。"""
    rows = [_coded(1, _MISQUOTE)]
    data = coding_tables(rows, entity_names=_ENTITY)

    assert data["quotes"] == [], "没点名被评实体的原声不许出表"
    dropped = data["quotes_dropped"]
    assert dropped["丢弃"] == 1, "丢弃必须计数，不是静默跳过"
    assert dropped["点了别的名"]["条数"] == 1, "这句点了 WorkBuddy，该归「点了别的名」"
    assert dropped["点了别的名"]["样本"][0]["quote"] == _MISQUOTE, "样本要留原文供抽查"


def test_点名了被评实体的原声照常出表():
    """闸不能连对的一起丢——这道闸的风险是丢太多，不是丢错。"""
    rows = [_coded(1, "豆包的回答非常套路化，浮于表面", attitude="负")]

    assert len(_quotes(rows, entity_names=_ENTITY)) == 1
    assert _quotes(rows, entity_names=_ENTITY)[0]["quote"].startswith("豆包")


def test_不给叫法表时不设闸_行为与加闸前一字不差():
    """备料与离线核数要看全量：`entity_names` 为空时行为不变，和 citations 同理。"""
    rows = [_coded(1, _MISQUOTE)]

    assert len(_quotes(rows)) == 1, "不给叫法表就不该过滤"
    assert coding_tables(rows)["quotes_dropped"]["设闸"] is False


def test_同一句原声跨格只出一行():
    """货 2 造红：一条 UGC 命中两个主题，原样出表会在两格各占一行。

    评审第二轮实测 S58 出现两行——读者数不出这是一个人说的还是两个人说的，
    等于把 1 条声音读成 2 条。
    """
    rows = [_coded(1, "豆包写代码挺顺手", topics=["功能与能力", "交互体验"])]

    picked = _quotes(rows, entity_names=_ENTITY)
    assert len(picked) == 1, "同一句跨格必须去重"


def test_两条证据摘出同一句话也只出一行():
    """按句子去重不按证据 id：转发同一句话的两条证据，对读者也是同一句。"""
    rows = [_coded(1, "豆包挺好用"), _coded(2, "豆包挺好用")]

    assert len(_quotes(rows, entity_names=_ENTITY)) == 1


def test_去重后该格仍补满三条而不是缺一条():
    """先按 seen 过滤再截 3 条：去掉重复的那格不该只剩 2 条。"""
    shared = "豆包挺好用"
    rows = [_coded(1, shared, topics=["功能与能力"]),
            _coded(2, shared, topics=["交互体验"]),
            _coded(3, "豆包写作文很快", topics=["交互体验"]),
            _coded(4, "豆包的语音也行", topics=["交互体验"]),
            _coded(5, "豆包做表格不错", topics=["交互体验"])]

    picked = [q for q in _quotes(rows, entity_names=_ENTITY) if q["topic"] == "交互体验"]
    assert len(picked) == 3, "第 4 条候选该补上来，不能因为去重就空一格"
    assert len({q["quote"] for q in picked}) == 3


def test_表注写明筛掉了多少条():
    """调度 09-07 晚补：行数少必须自己交代是筛短的。

    4 行全在境外平台时，读者会读出两个都不成立的结论——「国内没人评这个产品」
    和「我们没采到国内声音」。空表会被读成「没人这么说」，筛短的表同样会。
    """
    rows = [_coded(1, _MISQUOTE), _coded(2, "豆包挺好用")]

    basis = polish_tables(rows, entity_names=_ENTITY)["quotes"]["basis"]
    assert "1" in basis and "没提到研究对象" in basis, "筛掉几条要写在表注里"
    assert "不代表没人讨论" in basis, "要挡住「行数少＝没人说」这个误读"
    assert "quotes" not in basis and "entity_names" not in basis, "表注不许出机器名"


def _plan_with_two_cards():
    """真机计划里「豆包」和「Doubao」确实是**两张卡**（骨架把它们当两实体的坑还在）。"""
    return {"subjects": ["豆包", "Doubao"], "entities": [
        {"id": "豆包", "canonical": "豆包",
         "names": {"zh": "豆包", "en": "Doubao", "aliases": ["豆包AI"]}},
        {"id": "Doubao", "canonical": "豆包",
         "names": {"zh": "豆包", "en": "Doubao", "aliases": ["字节豆包"]}},
    ]}


def test_叫法表把豆包和Doubao归到同一个实体():
    """沿用前先验这一条：验不过就说明它不是该用的那张表。

    记忆 `ent2-closed` 记着骨架把「豆包」「Doubao」当两实体的坑。出表闸靠的是
    `_entity_aliases` 按 canonical 合并，中英文名归一后英文原声才不会被误丢。
    """
    from app.report.polish.tables import _entity_aliases

    merged = _entity_aliases(_plan_with_two_cards())
    assert list(merged) == ["豆包"], "两张卡必须并成一个实体，不能出两行"
    assert {"豆包", "Doubao", "豆包AI", "字节豆包"} <= set(merged["豆包"])


def test_英文原声不因为被评实体是中文名就被丢掉():
    """归一的实际后果：`using Doubao is just funny.` 这类海外原声要留得住。"""
    from app.report.polish.tables import _entity_aliases

    names = sorted({n for v in _entity_aliases(_plan_with_two_cards()).values() for n in v})
    rows = [_coded(1, "using Doubao is just funny.", attitude="负", platform="reddit")]

    assert len(_quotes(rows, entity_names=names)) == 1


def test_原声全被丢光时整张表不出而不是出空表():
    """判据 4：`n=0` 不出空表。

    空表会被写手读成「没人这么说」——那是把**没数据**写成**没人谈**，
    等于往报告里塞一个假结论，还一路绿到用户眼前。
    """
    from app.report.polish.tables import build_tables

    rows = [_coded(1, _MISQUOTE), _coded(2, "但是没有Claude接入飞书的体验好")]
    built = build_tables(report={}, plan=_plan_with_two_cards(), evidence=rows,
                         claims=[], view={})

    assert "quotes" not in built["tables"], "一行都不剩就不出这张表"
    assert built["omitted_tables"]["quotes"], "不出表要写明原因，让人看得见"
