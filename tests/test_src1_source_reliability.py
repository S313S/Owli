"""§SRC-1 货 1–3：源失败留状态码、时间窗说明书、删掉要了又不用的参数。"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import urlparse

import pytest


class ImmediateGate:
    def wait(self) -> None:
        return None


_NOW = lambda: datetime(2026, 8, 28, tzinfo=timezone.utc)  # noqa: E731


# ── 货 1：源失败要留下「谁拒绝了我们」 ──────────────────────────────

@pytest.mark.parametrize(
    ("http_status", "upstream_code", "expected"),
    [
        (429, 429, "tikhub_http_429"),
        (401, 401, "tikhub_auth"),
        (403, 403, "tikhub_auth"),
        (503, 503, "tikhub_http_5xx"),
        (404, 404, "tikhub_http_404"),
        (200, 500, "tikhub_upstream_code"),
    ],
)
def test_抖音失败按状态码分诊而不是一个笼统原因(
    http_status: int, upstream_code: int, expected: str,
) -> None:
    """诊断根因：此前一律 `tikhub_request_failed`，事后查不出 429 还是 5xx。"""

    from app.sources import douyin

    def http_request(method, url, headers, body, timeout):
        return douyin.HttpResponse(http_status, {"code": upstream_code})

    events: list[dict] = []
    result = douyin.search(
        "通义听悟", limit=2, comment_video_limit=1, token="runtime-secret",
        http_request=http_request, rate_gate=ImmediateGate(),
        on_event=events.append, now=_NOW,
    )

    assert result == []
    (event,) = [item for item in events if item["type"] == "source_unavailable"]
    data = event["data"]
    assert data["closed_reason"] == expected
    assert data["http_status"] == http_status
    assert data["upstream_code"] == upstream_code
    assert data["endpoint"] == douyin._SEARCH_PATH


def test_抖音传输层失败与协议失败各有各的分类() -> None:
    from app.sources import douyin

    # 传输层异常在真实 HTTP 边界被翻成 TikHubError，注入版直接抛同一种。
    def transport(method, url, headers, body, timeout):
        raise douyin.TikHubError("transport", endpoint=url, detail="URLError")

    events: list[dict] = []
    assert douyin.search(
        "通义听悟", limit=2, comment_video_limit=1, token="t", http_request=transport,
        rate_gate=ImmediateGate(), on_event=events.append, now=_NOW,
    ) == []
    assert events[0]["data"]["closed_reason"] == "tikhub_transport"

    def bad_shape(method, url, headers, body, timeout):
        return douyin.HttpResponse(200, {"code": 200, "data": {}})

    events.clear()
    assert douyin.search(
        "通义听悟", limit=2, comment_video_limit=1, token="t", http_request=bad_shape,
        rate_gate=ImmediateGate(), on_event=events.append, now=_NOW,
    ) == []
    assert events[0]["data"]["closed_reason"] == "tikhub_bad_response"


def test_响应体摘要不会把长串凭证写进事件() -> None:
    """错误体可能回显 token；摘要必须脱敏，事件与日志都不许留。"""

    from app.sources import douyin

    secret = "sk-" + "a1b2c3d4e5" * 4
    summary = douyin._body_summary({"message": f"invalid key {secret} rejected"})
    assert secret not in summary
    assert "<REDACTED>" in summary
    assert len(summary) <= douyin._BODY_SUMMARY_LIMIT


def test_小红书与抖音同构地报出状态码() -> None:
    from app.sources import xhs

    def http_get(url, headers, timeout):
        return xhs.HttpResponse(429, {"code": 429, "message": "too many requests"})

    events: list[dict] = []
    assert xhs.search(
        "通义听悟", "30d", limit=2, token="t", http_get=http_get,
        rate_gate=ImmediateGate(), on_event=events.append, now=_NOW,
    ) == []
    data = events[0]["data"]
    assert data["closed_reason"] == "tikhub_http_429"
    assert data["http_status"] == 429
    assert data["detail"] == "too many requests"


# ── 货 2：时间窗说明书 ────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7d", "7d"), ("365d", "365d"), (" 30 d ", "30d"),
        ("all", "3650d"), ("不限时间", "3650d"), ("不限", "3650d"),
        ("recent_1_year", "365d"), ("最近一年", "365d"), ("1y", "365d"),
        ("past_month", "30d"), ("最近一个月", "30d"),
        ("recent-1-week", "7d"), ("90天", "90d"), ("近 30 天", "30d"),
    ],
)
def test_人话时间窗被折算成Nd(raw: str, expected: str) -> None:
    """整跑实测：引擎传的就是 all / 不限时间 / recent_1_year 这些写法。"""

    from app.sources.spec import WindowParam

    assert WindowParam().normalize(raw) == expected


def test_折算不出来时报错要连该怎么写一起说() -> None:
    from app.sources.spec import WindowParam

    with pytest.raises(ValueError) as excinfo:
        WindowParam().normalize("随便什么时候")
    message = str(excinfo.value)
    assert "7d" in message and "365d" in message
    assert "最近一年" in message


def test_小红书与网页搜索都接受人话时间窗() -> None:
    from app.sources import web_search, xhs

    captured: dict[str, object] = {}

    def http_get(url, headers, timeout):
        captured["url"] = url
        return xhs.HttpResponse(200, {"code": 200, "data": {"items": []}})

    xhs.search("通义听悟", "最近一年", limit=2, token="t", http_get=http_get,
               rate_gate=ImmediateGate(), on_event=None, now=_NOW)
    assert "url" in captured  # 没有被 window 正则挡在门外

    def http_post(url, headers, payload, timeout):
        captured["payload"] = payload
        return {"results": []}

    web_search.search("通义听悟", "all", max_results=1,
                      env_path=_env_with_exa(), http_post=http_post)
    assert "payload" in captured


def _env_with_exa():
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / ".env"
    path.write_text("EXA_API_KEY=3fa85f64-5717-4562-b3fc-2c963f66afa6\n",
                    encoding="utf-8")
    return path


def test_工具说明书把时间窗格式写给模型看() -> None:
    """诊断根因：schema 里只有 {"type": "string"}，模型无从知道要写 7d。"""

    from app.adapters.source_mcp import _tool_definition

    class FakeTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    schema = _tool_definition(FakeTool, "xhs").inputSchema
    window = schema["properties"]["window"]
    assert "7d" in window["description"]
    assert "最近一年" in window["description"]
    assert window["examples"] == ["7d", "30d", "90d", "365d"]


# ── 货 3：不向模型索取用不上的参数 ────────────────────────────────

def test_抖音工具不再向模型索取时间窗() -> None:
    from app.adapters.source_mcp import _tool_definition

    class FakeTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    schema = _tool_definition(FakeTool, "douyin").inputSchema
    assert "window" not in schema["properties"]
    assert schema["required"] == ["query"]


def test_抖音不传时间窗也能搜且参照集不谎称按窗过滤() -> None:
    from app.sources import douyin

    def http_request(method, url, headers, body, timeout):
        if urlparse(url).path == douyin._SEARCH_PATH:
            return douyin.HttpResponse(200, {"code": 200, "data": {
                "items": [{"aweme_info": {
                    "aweme_id": "v-1", "desc": "文案", "create_time": 1787799195,
                    "author": {"uid": "u", "sec_uid": "s", "nickname": "作者"},
                    "statistics": {"digg_count": 1, "comment_count": 0},
                }}],
                "pagination": {"has_more": 0},
            }})
        return douyin.HttpResponse(200, {"code": 200, "data": {
            "comments": [], "cursor": 0, "has_more": 0, "total": 0,
        }})

    result = douyin.search(
        "通义听悟", limit=1, comment_video_limit=1, token="t",
        http_request=http_request, rate_gate=ImmediateGate(), now=_NOW,
    )

    assert len(result) == 1
    context = result[0]["norm_context"]
    assert "window=" not in str(context)


def test_源声明自己要不要时间窗() -> None:
    """加源只改自己的 SOURCE_SPEC，不必回来改禁区文件 source_mcp.py。"""

    from app.sources.registry import get_source

    assert get_source("douyin").window is None
    assert get_source("xhs").window is not None
    assert get_source("web_search").window is not None


def test_真实HTTP边界把代理拒绝翻成传输层失败(monkeypatch) -> None:
    """本机代理会伪装成对方拒绝；它必须落在 transport 桶里，不是 http 桶。"""

    from app.sources import douyin

    def boom(*args, **kwargs):
        raise URLError("proxy refused")

    monkeypatch.setattr(douyin, "urlopen", boom)
    with pytest.raises(douyin.TikHubError) as excinfo:
        douyin._default_http_request(
            "POST", f"{douyin._API_BASE}{douyin._SEARCH_PATH}", {}, b"{}", 1.0,
        )
    assert excinfo.value.closed_reason == "tikhub_transport"
    assert excinfo.value.endpoint == douyin._SEARCH_PATH
