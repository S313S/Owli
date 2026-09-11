"""§D-059 货 2：正式稿那条路的最小 store 必须和真 `Store` 读出同一个世界。

⭐ 病躲过整整一天，是因为**同一个库有两条读法**：验收走 `store/dao.py` 的
`Store`（JSON 冻结列解开），生产整理正式稿走 `rpt1_polish.ReadOnlyStore`
（一个字段都不解）。于是 `raw_metrics` 到 `engagement_value` 手里还是字符串、
只认 dict 的它一律回 `None`，三档分级全落「取不到」那一档——

真机后果：S66 库里明明是 `{"likes":176}`，正式稿原声表却写「该平台未提供互动数」，
而「原声按互动量排序」那条判据在生产里整个空转。**两边读数当时各自都是绿的。**

⛔ 所以这里不验「解析函数会不会解析」，验的是**两个 store 回的行一不一样**。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "acceptance" / "rpt1"))

from rpt1_polish import (  # noqa: E402
    _EVIDENCE_JSON_FIELDS, _REPORT_JSON_FIELDS, ReadOnlyStore)


def test_两个store冻结列名单逐字一致():
    """⛔ 名单对不齐就会少解一个字段，而少解是静默的。"""
    source = Path(ROOT / "app" / "store" / "dao.py").read_text("utf-8")

    for field in _EVIDENCE_JSON_FIELDS:
        assert f'"{field}"' in source, f"{field} 不在 dao.py 里，名单已经漂了"
    assert set(_EVIDENCE_JSON_FIELDS) == {
        "author_meta", "raw_metrics", "norm_context", "extra"}
    assert set(_REPORT_JSON_FIELDS) == {
        "plan_snapshot", "decision_balance", "engines_used", "attachments", "extra"}


def test_raw_metrics读回来是字典不是字符串(tmp_path: Path):
    """判据：库里写的是 `{"likes":176}`，读出来必须是 dict，`engagement_value` 才量得到 176。"""
    from app.reliability.scoring import engagement_value

    database = tmp_path / "t.db"
    conn = sqlite3.connect(database)
    conn.execute("create table evidence (id text primary key, report_id text, "
                 "platform text, author_meta text, raw_metrics text, "
                 "norm_context text, extra text)")
    conn.execute("insert into evidence values (?,?,?,?,?,?,?)",
                 ("ev-1", "r-1", "reddit", None,
                  json.dumps({"likes": 176}), None, json.dumps({"content_kind": "x"})))
    conn.execute("create table reports (id text primary key, plan_snapshot text, "
                 "decision_balance text, engines_used text, attachments text, extra text)")
    conn.execute("insert into reports values (?,?,?,?,?,?)",
                 ("r-1", json.dumps({"title": "t"}), None, None, None, None))
    conn.commit(); conn.close()

    row = ReadOnlyStore(database).list_evidence("r-1")[0]

    assert isinstance(row["raw_metrics"], dict), "不解 JSON，下游一律读成「取不到互动数」"
    assert engagement_value(row) == 176, "量不到 176，排序与代表性标注就是错的"
    assert isinstance(row["extra"], dict)
    assert ReadOnlyStore(database).get_report("r-1")["plan_snapshot"] == {"title": "t"}


def test_存的不是JSON时不炸_原样留着(tmp_path: Path):
    """老库里存过非 JSON 的列：宁可这一列读不出来，也不要让整份稿子生成不了。"""
    database = tmp_path / "t.db"
    conn = sqlite3.connect(database)
    conn.execute("create table evidence (id text primary key, report_id text, "
                 "author_meta text, raw_metrics text, norm_context text, extra text)")
    conn.execute("insert into evidence values (?,?,?,?,?,?)",
                 ("ev-1", "r-1", None, "不是 JSON", None, None))
    conn.commit(); conn.close()

    assert ReadOnlyStore(database).list_evidence("r-1")[0]["raw_metrics"] == "不是 JSON"
