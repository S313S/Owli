"""§FE-1 货 2：角标关——写作提示词与结论区校验的接缝。

D-025 实测：6 节里 5 节栽在 citation_marks_resolvable，19 个被点名的
「结论列表项未带 [Sxx]」里 18 个是把『证据缺口』『假设与不确定性』写成
`###` 挂在 `## 结论` 底下——那些是「本节没有 X 的证据」这类天然无源可引的
陈述，被当成结论条目逐条要求带角标。剩 1 个是角标写成 `（S03、S10）`。
本包只改提示词，不放宽任何校验语义，故这里的用例分两类：
  一、提示词必须写死层级与方括号约束（防止改回「只说独立一段」）；
  二、提示词自带的骨架示例，必须自己就能通过它所教的那道关。
"""
from __future__ import annotations

from app.adapters import validation


def _inner_results(markdown: str):
    """复刻 sectioning.py 池校验里的 inner_ctx 三件套。"""
    ctx = validation.Ctx(
        output_path="sec-1.md", output_format="markdown", research_id="", goal_id="",
        agent_id="report-writing", read_text=lambda: markdown, read_json=dict,
        store=None, source_domains=frozenset(),
    )
    return (
        validation.sections_exist(ctx, ["结论", "信息源"]),
        validation.citation_marks_resolvable(ctx, []),
        validation.no_orphan_citation(ctx, []),
    )


SKELETON = (
    "# 节标题\n\n## 结论\n\n- 结论一 [S01][S02]\n\n"
    "## 证据缺口\n\n- 未覆盖 X：本节可见证据里没有 …\n\n"
    "## 信息源\n\n- [S01] [标题一](permalink1)\n- [S02] [标题二](permalink2)"
)


def test_提示词骨架示例自己能过它所教的那道关() -> None:
    # 骨架若自己都过不了，等于教写手写一份必被退的产物。
    for result in _inner_results(SKELETON):
        assert result.verdict is validation.Verdict.PASS, (result.name, result.message)


def test_证据缺口写成三级标题会被判成结论条目() -> None:
    # 这是 D-025 那 5 节的真实形状：只把 `##` 换成 `###`，同样的内容立刻被退。
    nested = SKELETON.replace("\n## 证据缺口\n", "\n### 证据缺口\n")
    failed = [r for r in _inner_results(nested) if r.verdict is not validation.Verdict.PASS]
    assert [r.name for r in failed] == ["citation_marks_resolvable"]
    assert "未带 [Sxx] 角标" in failed[0].message


def test_角标写成中文括号内联同样不算带角标() -> None:
    inline = SKELETON.replace("- 结论一 [S01][S02]", "- 结论一（S01、S02）")
    failed = [r for r in _inner_results(inline) if r.verdict is not validation.Verdict.PASS]
    assert "citation_marks_resolvable" in [r.name for r in failed]


def test_提示词写死了层级与方括号约束并真的下发到写手(tmp_path) -> None:
    """光在源码里写不算数——要断言它真进了发给写手的 body。"""
    from test_w1_evidence_pool import _add_evidence, _run_sectioned

    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(store, evidence_id="ev-1", goal_id=goal_id,
                      permalink="https://evidence.example/visible", platform="xhs")

    def render(pool, task):
        return ("## 结论\n\n- 有证据的判断 [S01]\n\n"
                "## 证据缺口\n\n- 未覆盖 X：本节可见证据里没有 …\n\n"
                f"## 信息源\n\n- [S01] [可见证据]({pool['items'][0]['permalink']})\n")

    result, _, bodies, _, _, _ = _run_sectioned(
        tmp_path, goal_ids=["goal-1"], declared_paths=[], seed=seed,
        render=render, agent_kind="cross_validation",
    )
    body = bodies["sec-1.md"]
    assert "与 `## 结论` 同级" in body
    assert "不得出现任何 `###`" in body
    assert "半角方括号" in body
    assert "## 证据缺口" in body  # 骨架示例里要有形可依
    # 写手照新提示词写出的形状（证据缺口写成 `##` 同级）必须能过关
    assert result.succeeded is True
