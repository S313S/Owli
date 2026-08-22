from __future__ import annotations

import copy

import pytest


@pytest.fixture(autouse=True)
def isolate_engine_error_logs(tmp_path, monkeypatch):
    """pytest 不写仓内 var/logs/engine-errors，避免污染真实验收取证目录。"""

    log_root = tmp_path / "engine-errors"
    import app.adapters.claude as claude
    import app.adapters.codex as codex
    import app.adapters.logging as logging
    import app.adapters.ratelimit as ratelimit
    import app.sources.product_hunt as product_hunt
    import app.sources.web_search as web_search

    for module in (claude, codex, logging, ratelimit, product_hunt, web_search):
        monkeypatch.setattr(module, "DEFAULT_LOG_ROOT", log_root, raising=False)

    functions = [
        claude.ClaudeAdapter.__init__,
        codex.CodexAdapter.__init__,
        ratelimit.publish_route_decision,
        ratelimit.route,
        ratelimit.R8Confirm.__init__,
        product_hunt.search,
        web_search.search,
    ]
    originals = {function: copy.copy(function.__kwdefaults__) for function in functions}
    for function in functions:
        if function.__kwdefaults__ and "log_root" in function.__kwdefaults__:
            function.__kwdefaults__["log_root"] = log_root
    yield
    for function, defaults in originals.items():
        function.__kwdefaults__ = defaults
