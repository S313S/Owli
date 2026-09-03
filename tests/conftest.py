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
    # §ENT-1 货 1：规划期实体卡会走一次真实网页搜索（runtime 注入 web_search.search）。
    # 单元套件不许读开发机的 ~/.owli/.env、更不许打网络——把凭证路径指到不存在的
    # 文件，search 在读凭证那一步就抛 CredentialError，实体卡降级成「无线索」。
    # 直接测 web_search 的用例一律自带 env_path=，不受这条影响；恢复由下面的
    # originals 兜底（本行在 originals 抓取之后，改的是同一份 __kwdefaults__）。
    if web_search.search.__kwdefaults__ and "env_path" in web_search.search.__kwdefaults__:
        web_search.search.__kwdefaults__["env_path"] = tmp_path / "no-such-owli.env"
    yield
    for function, defaults in originals.items():
        function.__kwdefaults__ = defaults
