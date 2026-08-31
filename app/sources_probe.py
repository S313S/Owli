"""§X-1 货 4：起跑前探活——逐源调适配器做一次最小搜索，判据是「取到数据」不是 HTTP 200。

只调用 `app/sources/*` 的入口，不改源；每源 ≤2 次请求、总超时 ≤30s；凭证从 ~/.owli/.env
读存在性、不打印；不自动挡起跑（挡不挡归 M6）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.sources.registry import discover_sources, source_limit_parameters

DEFAULT_ENV_PATH = Path("~/.owli/.env").expanduser()
DEFAULT_QUERY = "AI 助手"
DEFAULT_WINDOW = "30d"
TOTAL_TIMEOUT_SECONDS = 30.0

#: 各源凭证键（任一存在即视为「配了凭证」）；空元组 = 无需凭证的公开源。
CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    "douyin": ("TIKHUB_API_KEY",),
    "xhs": ("TIKHUB_API_KEY",),
    "web_search": ("EXA_API_KEY", "TAVILY_API_KEY"),
    "reddit": ("PROWLO_API_KEY",),
    "x": ("X_BEARER_TOKEN",),
    "product_hunt": ("PRODUCT_HUNT_TOKEN",),
    "hacker_news": (),
}

#: 最小搜索条数；抖音 comment_video_limit 下限是 1，1 视频搜索 + 1 次评论 = 2 次请求。
PROBE_ITEM_LIMITS: dict[str, int] = {"douyin": 1}
DEFAULT_PROBE_ITEM_LIMIT = 2

#: 条数以外的源特有探活参数。
PROBE_EXTRA_KWARGS: dict[str, dict[str, Any]] = {
    "douyin": {"comment_video_limit": 1},
}


def _env_keys(env_path: Path) -> set[str]:
    """只读 ~/.owli/.env 里有值的键名，值不出模块。"""
    try:
        lines = Path(env_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    keys: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip().strip("\"'"):
            keys.add(key.strip().removeprefix("export ").strip())
    return keys


def configured_sources(
    *, env_path: Path = DEFAULT_ENV_PATH, registry: Mapping[str, Any] | None = None,
) -> list[str]:
    """缺省探活集：注册表里凭证已配（或无需凭证）的源。"""
    present = _env_keys(env_path)
    ids = sorted(registry) if registry is not None else sorted(discover_sources())
    return [
        source for source in ids
        if not CREDENTIAL_KEYS.get(source, ()) or any(
            key in present for key in CREDENTIAL_KEYS.get(source, ())
        )
    ]


def missing_credentials(source: str, *, env_path: Path = DEFAULT_ENV_PATH) -> bool:
    required = CREDENTIAL_KEYS.get(source, ())
    return bool(required) and not any(key in _env_keys(env_path) for key in required)


def probe_kwargs(source: str) -> dict[str, Any]:
    """探活入参：条数参数名问注册表，不再手抄（§M6-a 货 1 第五张表）。

    此前这里把 x 的条数写成 `limit`，而 x.search 只有 `max_results` ——
    每次探活 x 都是 TypeError，被当成「源不可用」记进 failure。
    """
    parameter = source_limit_parameters().get(source, "limit")
    return {
        parameter: PROBE_ITEM_LIMITS.get(source, DEFAULT_PROBE_ITEM_LIMIT),
        **PROBE_EXTRA_KWARGS.get(source, {}),
    }


def _call(source: str, entrypoint: Callable[..., Any], has_window: bool, query: str) -> Any:
    kwargs = probe_kwargs(source)
    if has_window:
        return entrypoint(query, DEFAULT_WINDOW, **kwargs)
    return entrypoint(query, **kwargs)


async def _probe_one(
    source: str, entrypoint: Callable[..., Any], *, has_window: bool, query: str,
    timeout_seconds: float, env_path: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    if missing_credentials(source, env_path=env_path):
        return {"ok": False, "items": 0, "elapsed_s": 0.0, "failure": "missing_credentials"}
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_call, source, entrypoint, has_window, query), timeout_seconds,
        )
        items = len(result) if isinstance(result, (list, tuple)) else 0
        failure = None if items > 0 else "empty"
    except asyncio.TimeoutError:
        items, failure = 0, f"timeout>{timeout_seconds:g}s"
    except Exception as exc:  # noqa: BLE001 —— 探活只报不抛
        items, failure = 0, f"{type(exc).__name__}: {str(exc)[:200]}"
    return {"ok": items > 0, "items": items,
            "elapsed_s": round(time.monotonic() - started, 3), "failure": failure}


async def probe_sources(
    sources: list[str] | None = None, *, registry: Mapping[str, Any] | None = None,
    query: str = DEFAULT_QUERY, timeout_seconds: float = TOTAL_TIMEOUT_SECONDS,
    env_path: Path = DEFAULT_ENV_PATH,
) -> dict[str, dict[str, Any]]:
    """并发逐源探活；registry 缺省读注册表（值为 SourceSpec），也可注入 {source: callable}。"""
    specs = dict(registry) if registry is not None else dict(discover_sources())
    targets = sources if sources else configured_sources(env_path=env_path, registry=specs)
    unknown = [s for s in targets if s not in specs]
    if unknown:
        raise KeyError(f"未注册的信息源：{', '.join(unknown)}")
    per_source = min(timeout_seconds, TOTAL_TIMEOUT_SECONDS)
    tasks = []
    for source in targets:
        spec = specs[source]
        entrypoint = getattr(spec, "entrypoint", spec)
        has_window = getattr(spec, "window", None) is not None if hasattr(spec, "entrypoint") else False
        tasks.append(_probe_one(source, entrypoint, has_window=has_window, query=query,
                                timeout_seconds=per_source, env_path=env_path))
    results = await asyncio.gather(*tasks)
    return dict(zip(targets, results))
