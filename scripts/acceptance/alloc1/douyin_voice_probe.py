"""§ALLOC-1 探针：用生产抖音源 + 生产取词逻辑，量「豆包」与「Kimi」在抖音的声量对照。

⛔ 不改任何源代码；只 import `app.sources.douyin.search`（生产入口）与
`app.adapters.source_mcp.entity_queries`（生产取词），实体卡从真实研究的
plan_snapshot 只读取出。limit 取生产 fast 档 `source_item_limits["douyin"]`。
每次 HTTP 请求都计数，成本按 TikHub $0.001/次 估。

用法：PYTHONPATH=. python scripts/acceptance/alloc1/douyin_voice_probe.py <db> <research_id> <out.json>
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter

from app.adapters.source_mcp import _dedupe_evidence, entity_queries
from app.config import load_research_scale_config
from app.sources import douyin

MAX_ATTEMPTS = 4

COMPETITOR_HINTS = {
    "豆包": ["豆包", "Doubao", "豆包AI", "豆包大模型", "字节豆包"],
    "Kimi": ["Kimi", "kimi", "KIMI", "Kimi Chat", "月之暗面", "Moonshot"],
    "DeepSeek": ["DeepSeek", "deepseek", "深度求索", "DeepSeek-R1", "DeepSeek-V3"],
    "文心一言": ["文心一言", "文小言", "百度文心", "ERNIE"],
}


def _slim_event(event) -> dict:
    """保留分诊字段（closed_reason / endpoint / http_status / calls），截掉长 detail。"""
    if not isinstance(event, dict):
        return {"raw": str(event)[:200]}
    data = event.get("data") or {}
    keep = {k: v for k, v in data.items() if k in {
        "from_version", "to_version", "closed_reason", "endpoint", "http_status",
        "upstream_code", "calls", "search_version", "returned", "reason", "aweme_id",
        "fallback_version", "fallback_closed_reason", "fallback_endpoint", "fallback_http_status",
    }}
    if data.get("detail"):
        keep["detail"] = str(data["detail"])[:160]
    return {"type": event.get("type"), **keep}


def mentions(text: str, names: list[str]) -> bool:
    return any(name in text for name in names)


def main() -> None:
    db, rid, out = sys.argv[1], sys.argv[2], sys.argv[3]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    snap = json.loads(con.execute("select plan_snapshot from reports where id=?", (rid,)).fetchone()[0])
    scale = snap.get("scale") or "fast"
    limit = load_research_scale_config().profile(scale).source_item_limits["douyin"]
    entities = {e["id"]: e for e in snap.get("entities") or []}
    targets = {"豆包": entities["豆包"], "Kimi": entities["Kimi"]}

    requests: Counter = Counter()

    def counting_http(method, url, headers, body, timeout):
        key = "comments" if douyin._COMMENTS_PATH in url else ("search_v4" if douyin._SEARCH_PATH_V4 in url else "search_v5")
        requests[key] += 1
        return douyin._default_http_request(method, url, headers, body, timeout)

    report: dict = {"research_id": rid, "scale": scale, "limit": limit, "entities": {}}
    for label, entity in targets.items():
        queries = entity_queries(entity, "zh", entity["canonical"])
        events: list = []
        batches = []
        t0 = time.time()
        attempts: dict[str, int] = {}
        for q in queries:
            # 探针侧重试：生产源层对 TikHub 400「Request failed. Please retry」与传输错误
            # 都不重试（源层是禁区，这里不改），一次抖动就让整词 0 条。探针要量的是
            # 「抖音上有多少人聊」，不是「这一秒端点抖不抖」，所以同词最多试 MAX_ATTEMPTS
            # 次，直到拿到非空结果；试了几次单独记下，抖动读数与声量读数分开报。
            rows: list = []
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts[q] = attempt
                rows = douyin.search(q, "", limit=limit, on_event=lambda e: events.append(e), http_request=counting_http)
                if rows:
                    break
            batches.append(rows)
        merged = _dedupe_evidence(batches) or []
        names = COMPETITOR_HINTS[label]
        others = [n for k, v in COMPETITOR_HINTS.items() if k != label for n in v]
        post_hit = post_other = with_comments = 0
        comments_total = comments_hit = 0
        digg = declared_comments = 0
        dates: list[str] = []
        samples = []
        for row in merged:
            desc = row.get("title") or ""
            full_desc = (row.get("content_excerpt") or "").split("\n\n评论：")[0]
            texts = (row.get("extra") or {}).get("comment_texts") or []
            if mentions(full_desc, names):
                post_hit += 1
            if mentions(full_desc, others):
                post_other += 1
            if texts:
                with_comments += 1
            comments_total += len(texts)
            comments_hit += sum(1 for t in texts if mentions(t, names))
            rm = row.get("raw_metrics") or {}
            digg += int(rm.get("digg_count") or 0)
            declared_comments += int(rm.get("comments_count") or 0)
            if row.get("published_at"):
                dates.append(row["published_at"][:10])
            samples.append({
                "desc": full_desc[:120], "digg": rm.get("digg_count"), "comments": rm.get("comments_count"),
                "published": (row.get("published_at") or "")[:10], "kw": row.get("source_keyword"),
                "mentions_self": mentions(full_desc, names), "mentions_other": mentions(full_desc, others),
                "comment_hits": sum(1 for t in texts if mentions(t, names)), "comment_n": len(texts),
            })
        report["entities"][label] = {
            "queries": queries,
            "attempts_per_query": attempts,
            "per_query_rows": [len(b) for b in batches],
            "posts_after_dedupe": len(merged),
            "posts_desc_mentions_self": post_hit,
            "posts_desc_mentions_other_entities": post_other,
            "posts_with_comments_fetched": with_comments,
            "comments_fetched": comments_total,
            "comments_mention_self": comments_hit,
            "sum_digg": digg,
            "sum_declared_comments": declared_comments,
            "published_range": [min(dates), max(dates)] if dates else None,
            "seconds": round(time.time() - t0, 1),
            "events": [_slim_event(e) for e in events],
            "samples": samples,
        }
    report["http_requests"] = dict(requests)
    report["est_cost_usd"] = round(sum(requests.values()) * 0.001, 3)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    slim = {k: {kk: vv for kk, vv in v.items() if kk != "samples"} for k, v in report["entities"].items()}
    print(json.dumps({**{k: v for k, v in report.items() if k != "entities"}, "entities": slim}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
