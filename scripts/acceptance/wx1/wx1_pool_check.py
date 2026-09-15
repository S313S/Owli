"""§WX-1 判据 1 的尺子：公众号预采集池读数。

    ../Owli/.venv/bin/python scripts/acceptance/wx1/wx1_pool_check.py \
        [--root ~/.owli/precollect] [--query 豆包] [--window 365d] [--entity-card tests/fixtures/alloc2/entity-1.json]

替代提货单里写的 `owli-precollect/verify_wechat_batch.py`，因为那把尺子在本包用不了：
路径写死 `Owli-m6d` 旧树；要清单带 `link_kind/screenshot/note`，影刀产物没有；
②③ 用 urllib 重抓正文，skill 09-08 实测非浏览器客户端拿到「环境异常」验证页 ⇒ 必假红。

**本尺子刻意不自己实现判断**（[[verification-ruler-needs-verifying]]）：
  · 池里读得出什么——调生产读池函数 `app.precollect.load_evidence`，研究期薄源走的就是它；
  · 是不是永久链——生产正则 `PLATFORM_PROFILES["wechat_mp"].permanent_permalink_pattern`；
  · 点没点名豆包——`app.plan.entities.mentions` × `lint._entity_names`，与 D-059 / D-062 同一把尺子。
量在「池 → 证据字典」这一层（`to_evidence` 之后、入库之前），正文已按生产口径截 8000 字。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from app.plan.entities import mentions  # noqa: E402
from app.plan.lint import _entity_names  # noqa: E402
from app.precollect import PLATFORM_PROFILES, load_evidence  # noqa: E402

PERMANENT = re.compile(PLATFORM_PROFILES["wechat_mp"].permanent_permalink_pattern)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=None, help="池根目录；缺省 ~/.owli/precollect")
    parser.add_argument("--query", default="豆包")
    parser.add_argument("--window", default=None, help="如 365d；缺省不筛")
    parser.add_argument("--entity-card", default=str(REPO / "tests/fixtures/alloc2/entity-1.json"))
    parser.add_argument("--batch", default=None, help="只量这个批次 id 的行")
    args = parser.parse_args()

    card = json.loads(Path(args.entity_card).read_text(encoding="utf-8"))
    card.setdefault("id", card.get("canonical"))
    names = _entity_names(card)
    result = load_evidence("wechat_mp", query=args.query or None, window=args.window, root=args.root)
    items = [
        item for item in result.items
        if args.batch is None or item["extra"].get("precollect_batch") == args.batch
    ]
    rows = []
    for item in items:
        text = f"{item.get('title') or ''}\n{item.get('content_excerpt') or ''}"
        rows.append({
            "id": item["platform_item_id"],
            "title": (item.get("title") or "")[:40],
            "account": item.get("author_name"),
            "published_at": item.get("published_at"),
            "permanent": bool(PERMANENT.match(item["permalink"])),
            "body_chars": len(item.get("content_excerpt") or ""),
            "names_subject": any(mentions(text, name) for name in names),
            "keyword": item.get("source_keyword"),
        })
    accounts = Counter(row["account"] for row in rows)
    summary = {
        "names": names,
        "query": args.query, "window": args.window, "batch": args.batch,
        "batches_scanned": result.batches_scanned, "rows_seen": result.rows_seen,
        "dropped_by_query": result.dropped_by_query, "dropped_by_window": result.dropped_by_window,
        "articles": len(rows),
        "names_subject": sum(row["names_subject"] for row in rows),
        "body_nonempty": sum(row["body_chars"] > 0 for row in rows),
        "permanent": sum(row["permanent"] for row in rows),
        "published_at_present": sum(bool(row["published_at"]) for row in rows),
        "published_2026": sum(str(row["published_at"] or "").startswith("2026") for row in rows),
        "accounts": len(accounts),
        "top_account_share": (
            f"{accounts.most_common(1)[0][1]}/{len(rows)}" if rows else "0/0"
        ),
        "body_chars_median": sorted(row["body_chars"] for row in rows)[len(rows) // 2] if rows else 0,
    }
    verdict = {
        "新增≥10篇": summary["articles"] >= 10,
        "点名主角≥5篇": summary["names_subject"] >= 5,
        "正文非空100%": bool(rows) and summary["body_nonempty"] == len(rows),
        "永久链100%": bool(rows) and summary["permanent"] == len(rows),
    }
    print(json.dumps({"summary": summary, "verdict": verdict, "rows": rows}, ensure_ascii=False, indent=2))
    return 0 if all(verdict.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
