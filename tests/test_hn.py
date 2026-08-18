import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
HN_PATH = ROOT / "app" / "sources" / "hn.py"


class HackerNewsSourceTest(unittest.TestCase):
    def test_hn_适配器模块存在(self) -> None:
        self.assertTrue(HN_PATH.is_file(), "app/sources/hn.py 尚未创建")

    def test_对外只导出_search(self) -> None:
        from app.sources import hn

        self.assertEqual(getattr(hn, "__all__", None), ["search"])
        self.assertTrue(callable(getattr(hn, "search", None)))
        self.assertTrue({
            "platform", "permalink", "fetched_at",
            "raw_metrics", "source_keyword",
        }.issubset(hn.Evidence.__required_keys__))

    def test_search_固定参数并映射平台基线(self) -> None:
        from app.sources import hn

        payload = {
            "hits": [{
                "objectID": "123",
                "title": "Show HN: Example",
                "story_text": "A &amp; B",
                "author": "alice",
                "points": 88,
                "num_comments": 21,
                "created_at": "2026-08-01T00:00:00Z",
                "url": "https://example.com",
            }]
        }
        with (
            patch.object(hn, "_now_epoch", create=True, return_value=1_800_000_000),
            patch.object(
                hn, "_utc_now_iso", create=True,
                return_value="2026-08-18T00:00:00+00:00",
            ),
            patch.object(hn, "_fetch_json", create=True, return_value=payload) as fetch,
        ):
            result = hn.search("AI agent", "90d")

        self.assertEqual(len(result), 1)
        query_params = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertEqual(query_params["query"], ["AI agent"])
        self.assertEqual(query_params["tags"], ["story"])
        self.assertEqual(
            query_params["numericFilters"],
            ["created_at_i>1792224000,points>50"],
        )
        self.assertEqual(query_params["hitsPerPage"], ["1000"])
        evidence = result[0]
        self.assertEqual(evidence["platform"], "hacker_news")
        self.assertEqual(evidence["permalink"], "https://news.ycombinator.com/item?id=123")
        self.assertEqual(evidence["source_keyword"], "AI agent")
        self.assertEqual(evidence["raw_metrics"], {"points": 88, "num_comments": 21})
        self.assertEqual(evidence["content_excerpt"], "A & B")
        self.assertEqual(evidence["rated_by"], "baseline")
        self.assertEqual(evidence["score_crossref"], 0)
        self.assertEqual(
            [evidence[key] for key in (
                "score_authority", "score_freshness",
                "score_completeness", "score_independence",
            )],
            [1, 1, 2, 2],
        )

    def test_search_空命中正常返回空数组(self) -> None:
        from app.sources import hn

        with patch.object(hn, "_fetch_json", create=True, return_value={"hits": []}):
            self.assertEqual(hn.search("Feishu", "90d"), [])

    def test_search_拒绝非法时间窗且不发请求(self) -> None:
        from app.sources import hn

        with patch.object(hn, "_fetch_json") as fetch:
            with self.assertRaisesRegex(ValueError, "window 必须形如"):
                hn.search("Feishu", "90")
        fetch.assert_not_called()

    def test_fetch_json_瞬时错误后重试且每次请求前节流(self) -> None:
        from app.sources import hn

        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"hits": []}'
        with (
            patch.object(
                hn, "urlopen", create=True,
                side_effect=[URLError("temporary"), response],
            ) as open_url,
            patch.object(hn, "_throttle", create=True) as throttle,
            patch.object(hn.time, "sleep") as sleep,
        ):
            payload = hn._fetch_json("https://example.test")

        self.assertEqual(payload, {"hits": []})
        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(throttle.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_throttle_连续请求会等待最小间隔(self) -> None:
        from app.sources import hn

        hn._last_request_at = 0.0
        with (
            patch.object(
                hn.time, "monotonic", side_effect=[100.0, 100.1, 100.4]
            ),
            patch.object(hn.time, "sleep") as sleep,
        ):
            hn._throttle()
            hn._throttle()

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.15)
        self.assertEqual(hn._last_request_at, 100.4)

    def test_cli_输出_json_数组(self) -> None:
        from app.sources import hn

        output = StringIO()
        with (
            patch.object(hn, "search", return_value=[{"platform": "hacker_news"}]),
            redirect_stdout(output),
        ):
            hn._main(["--query", "Feishu", "--window", "90d"])

        self.assertEqual(json.loads(output.getvalue()), [{"platform": "hacker_news"}])


if __name__ == "__main__":
    unittest.main()
