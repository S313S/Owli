from __future__ import annotations


class FakeGet:
    def __init__(self, content_type: str | None, body: bytes) -> None:
        self.content_type = content_type
        self.body = body
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url: str, timeout: float, max_bytes: int):
        self.calls.append((url, timeout, max_bytes))
        return self.content_type, self.body


def test_正文优先取_article_并剥掉_script_style_nav() -> None:
    from app.sources._page_text import fetch_page_text

    getter = FakeGet("text/html; charset=utf-8", b"""
        <html><body>
          <nav>navigation noise</nav>
          <main>main fallback <article>article body</article></main>
          <script>script noise</script><style>style noise</style>
        </body></html>
    """)

    text = fetch_page_text("https://example.com/a", http_get=getter)

    assert text == "article body"
    assert getter.calls == [("https://example.com/a", 10.0, 200_000)]


def test_没有_article_main_时取_body_内最长文本块() -> None:
    from app.sources._page_text import fetch_page_text

    getter = FakeGet("text/html", """
        <body>
          <div>短块</div>
          <section><p>这是正文第一段。</p><p>这是正文第二段，比旁边的短块更长。</p></section>
        </body>
    """.encode())

    assert fetch_page_text("https://example.com/b", http_get=getter) == (
        "这是正文第一段。 这是正文第二段，比旁边的短块更长。"
    )


def test_非_html_返回_none() -> None:
    from app.sources._page_text import fetch_page_text

    getter = FakeGet("application/pdf", b"not html")

    assert fetch_page_text("https://example.com/file.pdf", http_get=getter) is None

