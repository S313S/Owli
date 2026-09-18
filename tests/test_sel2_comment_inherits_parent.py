"""§SEL-2：同一条评论「说的是谁」，相关闸与实体归属必须给同一个答案。

病根（§SEL-1 定因）：相关闸 `rows_naming_entities` 只看本行 `title + content_excerpt`，
而评论行的 `title` 是「评论 · 父帖标题」、`content_excerpt` 是评论自己的话
⇒ **父帖正文里点的名，评论继承不到**。实测 `r-3b3482ca7f8b` 闸挡下 356 条，
其中 328 条是评论、而它们的父帖自己过了闸。同一条评论，`coding_entity_roles`
的规则 ③ 认它「说的是 Kimi」，闸却判它「谁都没提」直接挡掉——
**一处放行一处拦下，两边读数都是绿的**，正是 `relevance.py` 文件头警告的那个形状。
"""

from __future__ import annotations

from app.reliability.relevance import rows_naming_entities

PLAN = {
    "research_question": "国内大家对豆包的看法", "title": "国内大家对豆包的看法",
    "entities": [
        {"id": "豆包", "canonical": "豆包", "names": {"zh": "豆包", "en": "Doubao", "aliases": []}},
        {"id": "Kimi", "canonical": "Kimi", "names": {"zh": "Kimi", "en": "Kimi", "aliases": ["月之暗面"]}},
        {"id": "DeepSeek", "canonical": "DeepSeek",
         "names": {"zh": "DeepSeek", "en": "DeepSeek", "aliases": ["深度求索"]}},
    ],
    "goals": [{"goal_id": "goal-1", "agents": [
        {"agent_id": "xhs-doubao", "entity": "豆包", "chapter": {"chapter_id": "ch-1"}},
        {"agent_id": "xhs-kimi", "entity": "Kimi", "chapter": {"chapter_id": "ch-2"}},
        {"agent_id": "xhs-deepseek", "entity": "DeepSeek", "chapter": {"chapter_id": "ch-3"}},
    ]}],
}

NOTE = "https://www.xiaohongshu.com/explore/note-{}"


def _post(rid: str, title: str, body: str = "", *, url: str = "", agent: str = "xhs-doubao") -> dict:
    return {"id": rid, "kind": "post", "agent_name": agent, "platform": "xhs",
            "permalink": url or NOTE.format(rid), "title": title, "content_excerpt": body}


def _comment(rid: str, body: str, parent: dict, *, agent: str = "", token: str = "c1") -> dict:
    """评论行按真机形状造：`title` 是「评论 · 父帖标题」，permalink 在父帖链接上合成。"""
    return {"id": rid, "kind": "comment", "agent_name": agent or parent["agent_name"],
            "platform": "xhs",
            # 小红书评论没有自己的公开链接，permalink 是在父帖链接上挂锚点合成的，
            # 且带一次性 xsec_token（09-15 实测两轮就变）——所以键必须去签名。
            "permalink": f"{parent['permalink']}?xsec_token={token}&owli_comment={rid}",
            "parent_permalink": f"{parent['permalink']}?xsec_token=TOKEN-{token}",
            "title": f"评论 · {parent['title']}", "content_excerpt": body}


# ------------------------------------------------------------ 货 1 相关性硬闸

def test_货1_评论自己没点名但父帖正文点了名_继承放行() -> None:
    """父帖**正文**点名（标题不点），旧口径下评论继承不到，会被闸挡掉。"""
    parent = _post("p1", "今天试了个 AI", "用豆包这么唱老爸应该不会发现吧")
    child = _comment("c1", "知道我笑的多崩溃吗", parent)
    kept = {r["id"] for r in rows_naming_entities([parent, child], PLAN)}
    assert kept == {"p1", "c1"}, "父帖过了闸，挂在它下面的评论也该进池"


def test_货1_传False复现旧行为_同一份实现量两侧() -> None:
    parent = _post("p1", "今天试了个 AI", "用豆包这么唱老爸应该不会发现吧")
    child = _comment("c1", "知道我笑的多崩溃吗", parent)
    kept = {r["id"] for r in rows_naming_entities([parent, child], PLAN, inherit_parent=False)}
    assert kept == {"p1"}, "旧口径就是把这条评论挡掉——量两侧靠这个开关，不靠另写一份"


def test_货1_父帖自己也没点名_评论照样挡掉() -> None:
    parent = _post("p1", "今天试了个 AI", "这玩意儿真好玩")
    child = _comment("c1", "知道我笑的多崩溃吗", parent)
    assert rows_naming_entities([parent, child], PLAN) == []


def test_货1_拿不到父帖时不继承_宁可不继承也不要归错() -> None:
    """父帖没入库（跨报告/没采到）⇒ 原样退回，与「拿不到叫法就不设闸」同族。"""
    orphan = {"id": "c1", "kind": "comment", "agent_name": "xhs-doubao", "platform": "xhs",
              "permalink": NOTE.format("x") + "?xsec_token=a&owli_comment=c1",
              "parent_permalink": NOTE.format("不在库里的父帖") + "?xsec_token=b",
              "title": "评论 · 不在库里的父帖", "content_excerpt": "笑死我了"}
    assert rows_naming_entities([orphan], PLAN) == []


def test_货1_父帖索引只收父帖_评论不会继承到自己或兄弟评论() -> None:
    """⚠️ 小红书/抖音评论的 permalink 是在父帖链接上合成的。

    实测 `r-3b3482ca7f8b` 里 **485/563 条评论去签名后与父帖逐字同键**，27 个键上
    同时挂着 1 个 post 和 17~20 条 comment。索引要是把评论也收进去，命中谁取决于
    入参顺序——评论可能「继承」到自己或隔壁那条评论，两边读数还都是绿的。
    """
    parent = _post("p1", "今天试了个 AI", "这玩意儿真好玩")          # 父帖谁都没点名
    named = _comment("c1", "豆包真的顶", parent)                     # 兄弟评论自己点了名
    plain = _comment("c2", "笑死我了", parent, token="c2")
    kept = {r["id"] for r in rows_naming_entities([parent, named, plain], PLAN)}
    assert kept == {"c1"}, "c2 只能看父帖，⛔ 不许从兄弟评论 c1 那里继承"


def test_货1_HackerNews的id是身份不是追踪参数_不许被去签名切掉() -> None:
    """⛔ `permalink.split("?")[0]` 会把所有 HN 帖塌成 `news.ycombinator.com/item`。

    实测 `r-3b3482ca7f8b` 里 7 条 goal-1 的 HN 帖正是这么塌成同一个键的。
    生产的 `crossref.normalize_origin_url` 原文就写着「保留 HN 等身份参数 id」。
    """
    hn = "https://news.ycombinator.com/item?id={}"
    named = _post("p1", "ByteDance launches 豆包 office product", url=hn.format("111"))
    plain = _post("p2", "Show HN: A FOSS Expense Splitter", url=hn.format("222"))
    child = {"id": "c1", "kind": "comment", "agent_name": "xhs-doubao", "platform": "hacker_news",
             "permalink": hn.format("222") + "#c1", "parent_permalink": hn.format("222"),
             "title": "评论 · Show HN: A FOSS Expense Splitter", "content_excerpt": "neat project"}
    kept = {r["id"] for r in rows_naming_entities([named, plain, child], PLAN)}
    assert kept == {"p1"}, "c1 挂在没点名的 222 下；切掉 ?id= 会让它错误继承 111"


# ------------------------------------------------------------ 货 2 实体归属

def _coded(rid: str, *, agent: str, body: str, title: str, parent_permalink: str = "",
           permalink: str = "") -> dict:
    from app.reliability.coding import CODING_VERSION

    row = {"id": rid, "kind": "comment", "agent_name": agent, "platform": "xhs",
           "title": title, "content_excerpt": body, "grade": "B",
           "permalink": permalink or NOTE.format(rid),
           "extra": {"content_kind": "user_opinion", "coding": {
               "coding_version": CODING_VERSION, "audience": "不明", "scenario": "其他",
               "attitude": "负", "topics": ["回答质量"], "quote": ""}}}
    if parent_permalink:
        row["parent_permalink"] = parent_permalink
    return row


def test_货2_评论自己没点名但父帖点了名_随父帖不随章() -> None:
    """§SEL-2 的核心病象：附录对照实体态度表里的数**归错了家**。

    实测 `r-3b3482ca7f8b`「文心一言·负」那一格里有一批评论来自同一个父帖，
    而那个父帖被编码判成 DeepSeek（「D老师」是 DeepSeek 的叫法）——
    规则 ③ 按**章的实体**兜底，章是采集时分配的，父帖才是这条评论真正挂着的那篇。
    """
    from app.report.polish.tables import coding_entity_roles

    parent = _post("p1", "D老师又更新了", "DeepSeek 这版长对话稳多了", agent="xhs-deepseek")
    # 这条评论挂在 DeepSeek 的帖下，却被分配进了**文心一言那一章**（agent 是 xhs-kimi 代指另一家）
    child = _coded("c1", agent="xhs-kimi", body="这是一定要逼开会员啊",
                   title=f"评论 · {parent['title']}",
                   parent_permalink=parent["permalink"] + "?xsec_token=t",
                   permalink=parent["permalink"] + "?xsec_token=t&owli_comment=c1")
    roles = coding_entity_roles(PLAN, [parent, child])
    assert roles["c1"] == {"entity": "对照", "entity_name": "DeepSeek"}, \
        "随父帖（DeepSeek），⛔ 不随章（Kimi）"


def test_货2_评论自己点了名_仍然优先于父帖() -> None:
    """规则 ①② 不动：评论自己说的话最权威。"""
    from app.report.polish.tables import coding_entity_roles

    parent = _post("p1", "D老师又更新了", "DeepSeek 这版长对话稳多了", agent="xhs-deepseek")
    child = _coded("c1", agent="xhs-deepseek", body="我还是觉得豆包好用",
                   title=f"评论 · {parent['title']}",
                   parent_permalink=parent["permalink"] + "?xsec_token=t",
                   permalink=parent["permalink"] + "?xsec_token=t&owli_comment=c1")
    roles = coding_entity_roles(PLAN, [parent, child])
    assert roles["c1"] == {"entity": "主体", "entity_name": "豆包"}


def test_货2_父帖也没点名_落回规则三随章() -> None:
    from app.report.polish.tables import coding_entity_roles

    parent = _post("p1", "今天试了个 AI", "这玩意儿真好玩", agent="xhs-kimi")
    child = _coded("c1", agent="xhs-kimi", body="这是一定要逼开会员啊",
                   title=f"评论 · {parent['title']}",
                   parent_permalink=parent["permalink"] + "?xsec_token=t",
                   permalink=parent["permalink"] + "?xsec_token=t&owli_comment=c1")
    roles = coding_entity_roles(PLAN, [parent, child])
    assert roles["c1"] == {"entity": "对照", "entity_name": "Kimi"}, "父帖没点名才轮到章"


def test_货2_父帖判法走父帖自己的_own_text_标题对父帖算数对评论不算数() -> None:
    """⚠️ 「标题算不算自己的话」，帖和评论是**两套**，别把两边搞混。

    - 对**父帖**：`_own_text` = 标题 + 正文 + 原声 ⇒ 标题**算**它自己的话，
      所以父帖只在标题里点名也能被继承。
    - 对**评论**：`_own_text` **有意剔掉标题**（评论的 title 是「评论 · 父帖标题」，
      09-15 实测有 3 条评论的「原声」摘的就是父帖标题，借了它父帖名就漏回来了）。

    ⇒ 继承必须拿**父帖那一行**去算，⛔ 不能图省事拿评论 title 里那份父帖标题的拷贝——
    那样父帖只在**正文**里点名的情况（`r-3b3482ca7f8b` 里正是大头）一条都继承不到。
    这里父帖只在标题点 Kimi、正文什么都不点，评论被分在 DeepSeek 章下：随的必须是 Kimi。
    """
    from app.report.polish.tables import coding_entity_roles

    parent = _post("p1", "Kimi 首个官方教程", "如何用 AI 做出专业的 PPT", agent="xhs-kimi")
    child = _coded("c1", agent="xhs-deepseek", body="学到了",
                   title=f"评论 · {parent['title']}",
                   parent_permalink=parent["permalink"] + "?xsec_token=t",
                   permalink=parent["permalink"] + "?xsec_token=t&owli_comment=c1")
    roles = coding_entity_roles(PLAN, [parent, child])
    assert roles["p1"] == {"entity": "对照", "entity_name": "Kimi"}, "父帖的标题算它自己的话"
    assert roles["c1"] == {"entity": "对照", "entity_name": "Kimi"}, \
        "随父帖（Kimi），⛔ 不随章（DeepSeek）"
