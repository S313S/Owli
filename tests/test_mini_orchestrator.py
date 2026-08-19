from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mini_只保留计划驱动协调层兼容导出() -> None:
    from app.orchestrator.mini import MiniOrchestrator
    from app.orchestrator.runtime import RuntimeCoordinator

    source = (ROOT / "app" / "orchestrator" / "mini.py").read_text(encoding="utf-8")

    assert MiniOrchestrator is RuntimeCoordinator
    assert "keyword-extractor" not in source
    assert "hn-collector" not in source
    assert "report-writer" not in source
    assert "固定执行" not in source


def test_M0_结果语义已迁入_M2_唯一链路() -> None:
    source = (ROOT / "tests" / "test_m2_wiring.py").read_text(encoding="utf-8")

    assert "飞书竞品优缺点" in source
    assert "completed" in source
    assert "# 结论" in source
    assert "# 信息源" in source
    assert "citation_marks_resolvable" in source
    assert "no_orphan_citation" in source
