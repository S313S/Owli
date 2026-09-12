"""§SEC-1：让「这一节属于哪种章」参与选证据。

判据 A（本包开工前已钉死，worklog 2026-09-12）：`_evidence_index` 入参只有
`(rows, allowed_goal_ids, section_goal_id)`，所以同一个 goal 名下的撰写章与
交叉验证章拿到的是**逐条相同**的 30 条——写手换了题目，素材没换。

可用的机读差异只有一个：章的 `agent_kind`（闭集 `SECTIONED_CHAPTER_KINDS`，
不是词表）。节标题不可用——`_section_specs` 按 `plan.goals` 造节，
`title` 直接等于 `goal.title`，同 goal 各节标题逐字相同。

分席口径：交叉验证章的活是核对说法、找分歧，**要对照就得有对照物**，
所以给它更多跨 goal 席位；撰写章的活是把本 goal 讲透，给它更多本 goal 席位。
"""

from __future__ import annotations

from typing import Any


def _rows(spec: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
    """(goal_id, platform, 条数) → 证据行；id 稳定便于断言集合差。"""

    rows: list[dict[str, Any]] = []
    for goal_id, platform, count in spec:
        for index in range(count):
            rows.append({
                "id": f"ev-{goal_id}-{platform}-{index:03d}",
                "goal_id": goal_id,
                "platform": platform,
                "permalink": f"https://example.com/{goal_id}/{platform}/{index}",
                "title": None,
                "content_excerpt": None,
                "author_name": None,
                "fetched_at": "2026-09-12T00:00:00+00:00",
            })
    return rows


#: 三个 goal、五个平台，各 goal 名下都远超 30 条——席位不够分才量得出分配口径。
_SPEC = [
    ("goal-1", "web_search", 20), ("goal-1", "xhs", 25),
    ("goal-2", "weibo", 22), ("goal-2", "xhs", 30),
    ("goal-3", "douyin", 24), ("goal-3", "reddit", 26),
]


# ── 造红 1 · 同 goal 两种章类型 → 池条目必须有差异 ──────────────────

def test_同一goal的撰写章与交叉验证章拿到的条目不再逐字相同() -> None:
    """当前代码下必须是红的：入参里没有章类型，两者必然逐条相同。"""

    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows(_SPEC)
    writing = _section_evidence_rows(
        rows, "goal-2", chapter_kind="report_writing",
    )
    crossing = _section_evidence_rows(
        rows, "goal-2", chapter_kind="cross_validation",
    )

    writing_ids = {row["id"] for row in writing}
    crossing_ids = {row["id"] for row in crossing}
    # 判据 A 要的「差集非空」：两个方向都要非空，才叫「看到不同的证据」，
    # 只一头非空说明一个是另一个的子集，那只是池大小变了。
    assert writing_ids - crossing_ids, "撰写章该有交叉验证章看不到的条目"
    assert crossing_ids - writing_ids, "交叉验证章该有撰写章看不到的条目"


def test_差异能用节主题解释_交叉验证章拿到更多跨goal对照() -> None:
    """§九 判据 2：差异要「人读一眼说得出为什么」。"""

    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows(_SPEC)
    own = lambda sel: sum(1 for r in sel if r["goal_id"] == "goal-2")
    writing = _section_evidence_rows(
        rows, "goal-2", chapter_kind="report_writing",
    )
    crossing = _section_evidence_rows(
        rows, "goal-2", chapter_kind="cross_validation",
    )

    # 撰写章把本 goal 讲透；交叉验证章要对照物。
    assert own(writing) > own(crossing)
    assert len(writing) - own(writing) < len(crossing) - own(crossing)


def test_未声明章类型的节口径与现状逐字相同() -> None:
    """兜底：闭集外的 kind 与 None 一律走旧口径，既有用例才不会被改红。"""

    from app.orchestrator.sectioning import (
        SECTION_GOAL_FLOOR, _section_evidence_rows,
    )

    rows = _rows(_SPEC)
    baseline = _section_evidence_rows(rows, "goal-2")
    for kind in (None, "summary", "report", "audit", "data_collection", ""):
        selected = _section_evidence_rows(rows, "goal-2", chapter_kind=kind)
        assert [r["id"] for r in selected] == [r["id"] for r in baseline], kind
    # 保底是**下限**不是配额：余额轮转时本 goal 的平台还会再分到几个。
    assert sum(1 for r in baseline if r["goal_id"] == "goal-2") >= SECTION_GOAL_FLOOR


# ── 造红 2 · 本节 goal 保底仍守住 ─────────────────────────────────

def test_两种章类型都守住本节goal的保底席位() -> None:
    from app.orchestrator.sectioning import (
        SECTION_EVIDENCE_POOL_LIMIT, _section_goal_floor, _section_evidence_rows,
    )

    rows = _rows(_SPEC)
    for kind in ("report_writing", "cross_validation"):
        selected = _section_evidence_rows(rows, "goal-2", chapter_kind=kind)
        assert len(selected) == SECTION_EVIDENCE_POOL_LIMIT, kind
        own = [r for r in selected if r["goal_id"] == "goal-2"]
        assert len(own) >= _section_goal_floor(kind), kind
        # 跨 goal 对照永远留得住位子——一头吃光就不是两级配额了。
        assert len(selected) > len(own), kind


def test_本节goal证据不足时按有多少给多少不留空位() -> None:
    """同族于 SRC-1 既有那条；换章类型后仍不许把池缩小。"""

    from app.orchestrator.sectioning import (
        SECTION_EVIDENCE_POOL_LIMIT, _section_evidence_rows,
    )

    rows = _rows([("goal-2", "weibo", 3), ("goal-3", "reddit", 40)])
    for kind in ("report_writing", "cross_validation", None):
        selected = _section_evidence_rows(rows, "goal-2", chapter_kind=kind)
        assert len(selected) == SECTION_EVIDENCE_POOL_LIMIT, kind
        assert sum(1 for r in selected if r["goal_id"] == "goal-2") == 3, kind


# ── 造红 3 · 筛空回退 ────────────────────────────────────────────

def test_本节goal一条都没有时不返回空池() -> None:
    """池空了写手什么都写不出来，比看到几条不相关的更糟（§五 第 3 条）。"""

    from app.orchestrator.sectioning import (
        SECTION_EVIDENCE_POOL_LIMIT, _section_evidence_rows,
    )

    rows = _rows([("goal-3", "reddit", 40), ("goal-1", "web_search", 12)])
    for kind in ("report_writing", "cross_validation", None):
        selected = _section_evidence_rows(rows, "goal-9", chapter_kind=kind)
        assert len(selected) == SECTION_EVIDENCE_POOL_LIMIT, kind
        assert all(r["goal_id"] != "goal-9" for r in selected), kind


# ── 造红 4 · 平台轮转不被破坏 ─────────────────────────────────────

def test_两种章类型下平台轮转都不塌成一家独占() -> None:
    """QUOTA-1 尚未关账，本条按提货单 §八 第 4 句断言「现有平均轮转不塌」。"""

    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows(_SPEC)
    for kind in ("report_writing", "cross_validation", None):
        selected = _section_evidence_rows(rows, "goal-2", chapter_kind=kind)
        platforms: dict[str, int] = {}
        for row in selected:
            platforms[row["platform"]] = platforms.get(row["platform"], 0) + 1
        # 本 goal 两个平台都在，且谁也没吃光本 goal 那一档。
        own_platforms = {
            p: c for p, c in platforms.items() if p in {"weibo", "xhs"}
        }
        assert len(own_platforms) == 2, (kind, platforms)
        # 「不塌」的口径：两家都在，且少的那家不低于多的那家一半。
        # ⚠️ 不能写成「差 ≤1」——保底档内部是平均轮转，但余额档按全平台轮转，
        # 本 goal 两个平台在余额里拿到的数本来就可以不等（实算 crossing=12/10）。
        # 写成 ≤1 就是拿一把过紧的尺子把对的实现量成红的。
        assert min(own_platforms.values()) * 2 >= max(own_platforms.values()), (
            kind, platforms,
        )
    # 现状那一档的平均轮转必须逐字不变（上面那条用例锁的就是它）。
    flat = _section_evidence_rows(rows, "goal-2")
    flat_own: dict[str, int] = {}
    for row in flat:
        if row["goal_id"] == "goal-2":
            flat_own[row["platform"]] = flat_own.get(row["platform"], 0) + 1
    assert flat_own == {"weibo": 12, "xhs": 12}, flat_own


def test_角标编号不因章类型而漂() -> None:
    """⛔ 硬约束：动角标 = rescore。全报告编号那一路不带章类型，必须逐字不变。"""

    from app.orchestrator.sectioning import _evidence_index

    rows = _rows(_SPEC)
    allowed = {"goal-1", "goal-2", "goal-3"}
    _, baseline = _evidence_index(rows, allowed)
    for kind in ("report_writing", "cross_validation", None):
        pool, citations = _evidence_index(
            rows, allowed, section_goal_id="goal-2", chapter_kind=kind,
        )
        assert citations == baseline, kind
        # 池里每条的角标都取自同一套全报告编号。
        numbers = {item["citation"] for item in pool["items"]}
        assert len(numbers) == len(pool["items"]), kind


# ── 适用边界：本节 goal 零条时本包不生效（实测，不是推测）──────────

def test_本节goal零条时两种章仍拿到同一批_本包不覆盖这一支() -> None:
    """⚠️ 这条不是绿，是把**适用边界**钉下来，免得以后有人以为覆盖了。

    `_evidence_index` 里 `eligible` 为空会回退全池，于是所有行都算跨 goal，
    「本 goal 先占位」这个杠杆无从发挥——两种章类型必然拿到同一批。

    实测 `r-3e04f808dffd` 沙盒重放：节讲 goal-1 时池构成两种章逐条相同
    （证据表 goal-1 零行，已立卡「证据表 goal_id 与计划层不一致」）。
    **要让官方资料节也吃到本包的效果，得先修那张卡**，不是再加一个杠杆。
    """

    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows([("goal-2", "weibo", 20), ("goal-3", "reddit", 20)])
    pools = {
        kind: [r["id"] for r in _section_evidence_rows(
            rows, "goal-1", chapter_kind=kind,
        )]
        for kind in ("cross_validation", "report_writing")
    }
    assert pools["cross_validation"] == pools["report_writing"]
