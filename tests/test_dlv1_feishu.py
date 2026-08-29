"""§DLV-1 货 6：飞书推送——假传输验字段映射与 upsert 锚点；未配置则 skipped。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.store.dao import Store
from tests.test_dlv1_delivery import URL_A, _seed_evidence, _seed_history, _write_json_report


class FakeTransport:
    name = "fake"

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self.docs: list[tuple[str, str]] = []

    def ensure_base(self, name: str) -> str:
        return "bascFAKE"

    def ensure_table(self, base_token: str, name: str, fields: Any) -> str:
        self.tables.setdefault(name, list(fields))
        return f"tbl_{name}"

    def upsert(self, base_token: str, table_id: str, anchor_field: str, anchor: str, record: Any) -> str:
        assert record[anchor_field] == anchor
        self.records.setdefault(table_id, {})[anchor] = dict(record)
        return f"rec_{anchor}"

    def create_doc(self, title: str, markdown: str) -> tuple[str, str]:
        self.docs.append((title, markdown))
        return "doxFAKE", "https://feishu.cn/docx/doxFAKE"


def _seeded(tmp_path: Path):
    from app.store.dao import Store

    database, research_id, report_path = _seed_history(tmp_path)
    _write_json_report(report_path)
    _seed_evidence(database, research_id)
    return Store(database), research_id, report_path.read_text(encoding="utf-8")


def test_假传输_两表字段映射与_upsert_锚点(tmp_path: Path) -> None:
    from app.export.feishu import OVERVIEW_TABLE, OVERWRITE_NOTICE, SOURCES_TABLE, push_to_feishu

    store, research_id, text = _seeded(tmp_path)
    fake = FakeTransport()
    result = push_to_feishu(store, research_id, text, transport=fake)
    assert result["status"] == "synced" and result["sources_pushed"] == 2
    assert {t["field_name"] for t in fake.tables[OVERVIEW_TABLE]} >= {"report_id", "标题", "报告云文档", "同步说明"}
    overview = fake.records[f"tbl_{OVERVIEW_TABLE}"][research_id]
    assert overview["证据总数"] == 3 and overview["同步说明"] == OVERWRITE_NOTICE
    assert overview["报告云文档"]["link"] == "https://feishu.cn/docx/doxFAKE"
    rows = fake.records[f"tbl_{SOURCES_TABLE}"]
    assert set(rows) == {"ev-1", "ev-2"}  # 只推 citation_no 非空的行
    assert rows["ev-1"]["角标号"] == 1 and rows["ev-1"]["原文链接"]["link"] == URL_A
    assert rows["ev-1"]["五维明细"] == "权威1·时效2·交叉?·完整1·无关1"
    assert rows["ev-1"]["原始热度"] == '{"digg_count": 3}' and "可靠度等级" not in rows["ev-1"]
    title, markdown = fake.docs[0]
    assert title == "JSON 成稿" and "\\[1\\]\\[2\\]" in markdown and "[S01]" not in markdown
    assert f"1. [小红书笔记]({URL_A})" in markdown and "## 缺失清单" in markdown
    feishu = store.get_report(research_id)["extra"]["feishu"]
    assert feishu["status"] == "synced" and feishu["doc_token"] == "doxFAKE" and feishu["record_id"] == f"rec_{research_id}"
    row = store.get_report(research_id)  # §FU-1 起四列由 set_feishu_sync 回填（DLV-1 挂账①已关）
    assert row["feishu_sync_status"] == "synced" and row["feishu_doc_token"] == "doxFAKE"
    assert row["feishu_record_id"] == f"rec_{research_id}" and row["feishu_synced_at"]

    push_to_feishu(store, research_id, text, transport=fake)
    assert len(fake.records[f"tbl_{SOURCES_TABLE}"]) == 2  # 二次推送按锚点覆盖不增行


def test_未配置飞书时_skipped(tmp_path: Path, monkeypatch) -> None:
    from app.export.feishu import push_to_feishu, select_transport

    monkeypatch.setenv("OWLI_FEISHU_DISABLE_CLI", "1")
    monkeypatch.setenv("OWLI_ENV_FILE", str(tmp_path / "no.env"))
    assert select_transport({}) is None
    store, research_id, text = _seeded(tmp_path)
    result = push_to_feishu(store, research_id, text)
    assert result["status"] == "skipped" and "未配置飞书" in result["message"]
    assert store.get_report(research_id)["extra"]["feishu"]["status"] == "skipped"


class BrokenTransport(FakeTransport):
    def create_doc(self, title: str, markdown: str) -> tuple[str, str]:
        raise RuntimeError("云文档导入被拒：no permission")


def test_fu1_推送失败_四列记_failed_且不擦上次锚点(tmp_path: Path) -> None:
    """§FU-1 货 2：失败落 failed + extra.feishu.error；doc_token/record_id 保上次成功值。"""
    from app.export.feishu import push_to_feishu

    store, research_id, text = _seeded(tmp_path)
    push_to_feishu(store, research_id, text, transport=FakeTransport())
    result = push_to_feishu(store, research_id, text, transport=BrokenTransport())
    assert result["status"] == "failed"
    row = store.get_report(research_id)
    assert row["feishu_sync_status"] == "failed"
    assert row["feishu_doc_token"] == "doxFAKE"  # COALESCE 保住上次成功的锚点
    assert row["feishu_record_id"] == f"rec_{research_id}"
    assert "no permission" in row["extra"]["feishu"]["error"]


def test_fu1_非法_status_抛错不落库(tmp_path: Path) -> None:
    import pytest

    store, research_id, _ = _seeded(tmp_path)
    with pytest.raises(ValueError):
        store.set_feishu_sync(research_id, status="done")
    assert store.get_report(research_id)["feishu_sync_status"] == "pending"
    with pytest.raises(KeyError):
        store.set_feishu_sync("r-不存在", status="synced")


def test_fu1_读侧四列优先_extra_兜底(tmp_path: Path) -> None:
    """打真 /report：四列压过 extra 的旧账，四列放不下的细节仍由 extra 兜底。"""
    from app.export.registry import record_feishu
    from tests.test_dlv1_delivery import _app, _get, _seed_history, _write_json_report

    database, research_id, report_path = _seed_history(tmp_path)
    _write_json_report(report_path)
    _seed_evidence(database, research_id)
    store = Store(database)
    record_feishu(store, research_id, "synced", doc_token="doxNEW", record_id="recNEW",
                  transport="fake", doc_url="https://feishu.cn/docx/doxNEW")
    # 只改 extra 不动四列，模拟 §FU-1 之前留下的旧账：读侧必须以四列为准。
    with store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "UPDATE reports SET extra = json_set(extra, '$.feishu.doc_token', 'doxOLD',"
            " '$.feishu.status', 'pending') WHERE id = ?", (research_id,))

    feishu = _get(_app(tmp_path, database), f"/api/researches/{research_id}/report").json()["data"]["feishu"]
    assert feishu["doc_token"] == "doxNEW" and feishu["status"] == "synced"
    assert feishu["record_id"] == "recNEW" and feishu["synced_at"]
    assert feishu["doc_url"] == "https://feishu.cn/docx/doxNEW"  # 四列放不下的细节仍来自 extra
