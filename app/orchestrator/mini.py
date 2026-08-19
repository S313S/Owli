"""M0 入口兼容层；实际运行统一由 M2 计划驱动协调器承担。"""

from app.orchestrator.runtime import RuntimeCoordinator


MiniOrchestrator = RuntimeCoordinator


__all__ = ["MiniOrchestrator", "RuntimeCoordinator"]
