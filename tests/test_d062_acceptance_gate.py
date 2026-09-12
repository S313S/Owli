"""§D-062：goal 验收条要求写「没有采集卡的实体」——删卡后写手被逼着编或反复被打回。

病根：D-061 把表外卡删了，规则 26 只把实体从报告章 `closing.entities` 摘掉，
**不碰 goal.acceptance**；`runtime.py` 又把验收条逐字拼进写手提示词。lint 规则 4
只查「至少一条」「不含不可判定表述」，不查提到的实体有没有卡 ⇒ 规划期静默、执行期发作。

口径（§一′，调度 09-12 代拍）：
- 可达口径：一条验收条提到的实体 ⊄「本 goal 或其传递上游有卡的实体」⇒ 整条摘，不做句内删改。
- 反向约束豁免：无卡实体出现在否定/限定语境（未出现 / 仅使用 / 不得 / 禁止 …）里的不摘——
  那是防串号的闸，摘掉等于自拆闸。
- 第三类按路径判：验收条写死的 `goals/<goal>/<file>` 不在现存产物集合里 ⇒ 整条摘。

夹具与 D-061 同一份真计划（`r-e9760470f3e0`，tests/fixtures/d061），走
`assembled.json → _build_plan → normalize_plan` 整条，删卡与摘条在同一函数里连着发生。
"""

from __future__ import annotations

from app.plan.model import Entity, Plan
from app.plan.normalize import normalize_plan
from tests.test_d061_allocation_gate import _allocation, _fixture_plan, _tiny_plan


def _acceptance(plan: Plan) -> dict[str, list[str]]:
    return {goal.goal_id: list(goal.acceptance) for goal in plan.goals}


def _mentions(text: str, *names: str) -> bool:
    return any(name in text for name in names)


# —— 判据 1：真夹具逐条预期 ————————————————————————————————


def test_红_卡没删时一条验收条都不摘():
    """不给分配表 ⇒ D-061 不删卡 ⇒ 三个实体处处可达、data-collection-3 还在 ⇒ 闸不动。

    这条锁的是「闸看的是计划里实际还在的卡」：它不是按分配表判，是按卡判。
    """
    plan = _fixture_plan()
    before = _acceptance(plan)
    notes = normalize_plan(plan)
    assert [n for n in notes if n.startswith("[修正4]")] == [], notes
    assert _acceptance(plan) == before


def test_goal1_摘第1第6条_留下的四条一个实体都不提():
    """提货单 §〇 点名的两条：第 1 条「含豆包 Kimi DeepSeek 三个实体的证据分节」、
    第 6 条「显式标注豆包与 Kimi、DeepSeek 的对照差异」。goal-1 删卡后只剩豆包、Kimi。"""
    plan = _fixture_plan()
    before = _acceptance(plan)["goal-1"]
    assert len(before) == 6
    normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
    after = _acceptance(plan)["goal-1"]
    assert after == [before[1], before[2], before[3], before[4]], after
    assert not any(_mentions(line, "DeepSeek", "深度求索") for line in after)


def test_goal2_按实体一条不摘_按路径摘第4条():
    """goal-2 有卡{豆包, DeepSeek}，上游 goal-1 有 Kimi ⇒ 三个实体都可达，第 4/5 条
    消费 goal-1 Kimi 语料是正当的（候选①只看本 goal 会误伤它们）。

    但第 4 条另写死了 `goals/goal-1/data-collection-3.json`——正是被删的微博·DeepSeek
    卡产物。实体可达 ≠ 那份产物还在，这条按路径摘。第 5 条（同样提 Kimi/DeepSeek、
    不引路径）必须留下。
    """
    plan = _fixture_plan()
    before = _acceptance(plan)["goal-2"]
    assert len(before) == 7
    assert "goals/goal-1/data-collection-3.json" in before[3]
    normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
    after = _acceptance(plan)["goal-2"]
    assert after == before[:3] + before[4:], after
    assert any(_mentions(line, "Kimi") and _mentions(line, "DeepSeek") for line in after), \
        "跨 goal 消费上游语料的第 5 条不许被摘"


def test_goal3_摘第1第4第5条_反向约束第3第6条必须留下():
    """goal-3 有卡只有豆包（上游给 Kimi），零 DeepSeek 卡。

    第 1 条要「DeepSeek 能力认知…四个小节」、第 5 条要「分别呈现 DeepSeek 观感」——摘。
    第 4 条引用被删卡产物 + DeepSeek 相关产物——摘。
    第 3 条「未出现 Kimi 或 DeepSeek 的任何叫法」、第 6 条「仅使用闭集叫法：…DeepSeek…」
    是**防串号的反向约束**，提到 DeepSeek 是为了禁止它——**这两条是尺子通电的反面判据**。
    """
    plan = _fixture_plan()
    before = _acceptance(plan)["goal-3"]
    assert len(before) == 8
    normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
    after = _acceptance(plan)["goal-3"]
    assert after == [before[1], before[2], before[5], before[6], before[7]], after
    assert "未出现 Kimi 或 DeepSeek" in after[1]
    assert "闭集叫法" in after[2]


def test_留痕_每摘一条一个说明_说得出goal与原因():
    """判据 2 的库内读数靠它：说明进 events，得说得出哪个 goal、哪条、为什么。"""
    plan = _fixture_plan()
    notes = [n for n in normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
             if n.startswith("[修正4]")]
    assert len(notes) == 6, notes                         # goal-1 ×2、goal-2 ×1、goal-3 ×3
    by_goal = {g: [n for n in notes if n.startswith(f"[修正4] {g}.acceptance")]
               for g in ("goal-1", "goal-2", "goal-3")}
    assert {g: len(v) for g, v in by_goal.items()} == {"goal-1": 2, "goal-2": 1, "goal-3": 3}, notes
    assert all("DeepSeek" in n for n in by_goal["goal-1"]), by_goal["goal-1"]
    assert "goals/goal-1/data-collection-3.json" in by_goal["goal-2"][0], by_goal["goal-2"]
    # 删卡与摘条要在同一次 normalize 里连着发生——闸排在删卡之后才看得见「卡没了」。
    assert any(n.startswith("[修正31]") and "weibo·DeepSeek" in n
               for n in normalize_plan(_fixture_plan(), collection_plan=_allocation()))


def test_幂等_再跑一遍不再摘也不再留痕():
    plan = _fixture_plan()
    normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
    snapshot = _acceptance(plan)
    again = normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
    assert [n for n in again if n.startswith("[修正4]")] == [], again
    assert _acceptance(plan) == snapshot


# —— 口径的边角：最小计划 ————————————————————————————————


def _entity(name: str, **names: object) -> Entity:
    return Entity.from_dict({"id": name, "canonical": name,
                             "names": {"zh": names.get("zh"), "en": names.get("en"),
                                       "aliases": list(names.get("aliases", []))}})


def _plan_with(cards, acceptance: dict[str, list[str]], entities: list[Entity]) -> Plan:
    plan = _tiny_plan(cards)
    plan.entities = entities
    for goal in plan.goals:
        goal.acceptance = list(acceptance.get(goal.goal_id, goal.acceptance))
    return plan


def test_提到的实体都可达时一条不动():
    plan = _plan_with([("goal-1", "xhs", "豆包"), ("goal-2", "weibo", "Kimi")],
                      {"goal-2": ["报告须给出豆包与 Kimi 的对照差异（goal-2 依赖 goal-1）"]},
                      [_entity("豆包", en="Doubao"), _entity("Kimi")])
    assert plan.goals[1].depends_on == ["goal-1"], "骨架里 goal-2 依赖 goal-1，可达口径靠它"
    assert normalize_plan(plan) == []
    assert plan.goals[1].acceptance == ["报告须给出豆包与 Kimi 的对照差异（goal-2 依赖 goal-1）"]


def test_按叫法匹配_别名与英文名也算提到():
    plan = _plan_with([("goal-1", "xhs", "豆包")],
                      {"goal-1": ["报告须含 Doubao 与 Moonshot 的对照小节",
                                  "报告须含深度求索一节",
                                  "文件存在且通过 validators"]},
                      [_entity("豆包", en="Doubao"), _entity("Kimi", aliases=["Moonshot"]),
                       _entity("DeepSeek", zh="深度求索")])
    normalize_plan(plan)
    assert plan.goals[0].acceptance == ["文件存在且通过 validators"]


def test_英文短别名按整词匹配_不误伤子串():
    """DeepSeek 的别名表里有「DS」：按裸子串会把 「HEADS」「JSON fields」这类都判成提到。"""
    plan = _plan_with([("goal-1", "xhs", "豆包")],
                      {"goal-1": ["字段名一律大写如 HEADS、IDS、JSON",
                                  "报告须含 kimi 一节"]},
                      [_entity("豆包"), _entity("Kimi", aliases=["kimi"]),
                       _entity("DeepSeek", aliases=["DS"])])
    notes = normalize_plan(plan)
    # 「kimi」是整词、Kimi 在 goal-1 无卡且无上游 ⇒ 第 2 条该摘；「HEADS/IDS」不算提到 DS。
    assert plan.goals[0].acceptance == ["字段名一律大写如 HEADS、IDS、JSON"], notes
    assert len(notes) == 1 and "Kimi" in notes[0] and "DeepSeek" not in notes[0], notes


def test_反向约束豁免_按子句判不按整条判():
    plan = _plan_with([("goal-1", "xhs", "豆包")],
                      {"goal-1": ["任务文本仅使用豆包叫法，未出现 Kimi 的任何叫法",
                                  "报告须含 Kimi 对照小节，不得遗漏 permalink"]},
                      [_entity("豆包"), _entity("Kimi")])
    normalize_plan(plan)
    assert plan.goals[0].acceptance == ["任务文本仅使用豆包叫法，未出现 Kimi 的任何叫法"]


def test_摘光时兜底一条_不许留空列表():
    """规则 4 要求至少一条；摘到空会让计划连生都生不出来。兜底那条不提任何实体。"""
    plan = _plan_with([("goal-1", "xhs", "豆包")],
                      {"goal-1": ["报告须含 Kimi 一节", "报告须含 DeepSeek 一节"]},
                      [_entity("豆包"), _entity("Kimi"), _entity("DeepSeek")])
    notes = normalize_plan(plan)
    assert len(plan.goals[0].acceptance) == 1
    assert not _mentions(plan.goals[0].acceptance[0], "Kimi", "DeepSeek")
    assert any("兜底" in n for n in notes), notes


def test_引用路径存在时不摘_不存在时摘():
    plan = _plan_with([("goal-1", "xhs", "豆包"), ("goal-2", "weibo", "豆包")],
                      {"goal-2": ["交叉验证章显式引用 goals/goal-1/data-collection-1.json",
                                  "交叉验证章显式引用 goals/goal-1/data-collection-9.json）。"]},
                      [_entity("豆包")])
    notes = normalize_plan(plan)
    assert plan.goals[1].acceptance == ["交叉验证章显式引用 goals/goal-1/data-collection-1.json"]
    assert any("goals/goal-1/data-collection-9.json" in n for n in notes), notes


def test_没有实体卡的历史计划_按实体那半不动_按路径那半照判():
    plan = _plan_with([("goal-1", "xhs", "豆包")],
                      {"goal-1": ["报告须含 DeepSeek 一节", "引用 goals/goal-1/nope.json"]}, [])
    normalize_plan(plan)
    assert plan.goals[0].acceptance == ["报告须含 DeepSeek 一节"]
