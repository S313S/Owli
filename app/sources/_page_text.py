"""轻量抓取网页正文；失败时由调用方退回搜索片段。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Callable
from urllib.request import Request, urlopen


HttpGet = Callable[[str, float, int], tuple[str | None, bytes]]

_IGNORED = {"script", "style", "nav"}
_BLOCKS = {"article", "main", "body", "div", "section", "p", "td", "li"}


class _BodyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._stack: list[tuple[str, list[str]]] = []
        self.blocks: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _IGNORED:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in _BLOCKS:
            self._stack.append((tag, []))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        for index in range(len(self._stack) - 1, -1, -1):
            block_tag, parts = self._stack[index]
            if block_tag == tag:
                del self._stack[index]
                text = _clean(" ".join(parts))
                if text:
                    self.blocks.setdefault(tag, []).append(text)
                return

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        for _, parts in self._stack:
            parts.append(data)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _http_get(url: str, timeout: float, max_bytes: int) -> tuple[str | None, bytes]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Owli/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.headers.get("Content-Type"), response.read(max_bytes + 1)[:max_bytes]


def fetch_page_text(
    url: str,
    *,
    http_get: HttpGet = _http_get,
    timeout_seconds: float = 10.0,
    max_bytes: int = 200_000,
) -> str | None:
    """取 article/main/最长正文块；任何抓取或解析失败均返回 ``None``。"""

    try:
        content_type, body = http_get(url, timeout_seconds, max_bytes)
        if not content_type or "text/html" not in content_type.lower():
            return None
        parser = _BodyParser()
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 —— 单页任何失败都必须退回搜索片段
        return None
    for tag in ("article", "main"):
        if parser.blocks.get(tag):
            return max(parser.blocks[tag], key=len)
    candidates = [
        text for tag, blocks in parser.blocks.items()
        if tag not in {"article", "main", "body"} for text in blocks
    ]
    if candidates:
        return max(candidates, key=len)
    bodies = parser.blocks.get("body", [])
    return max(bodies, key=len) if bodies else None
