"""飞书单向推送（§DLV-1 货 6）：两张多维表格 + 云文档正文，SQLite 为权威。

传输层可插拔：`OpenApiTransport`（`~/.owli/.env` 的 FEISHU_APP_ID/SECRET/
BITABLE_APP_TOKEN，urllib + ProxyHandler({}) 显式绕本机代理）→ `LarkCliTransport`
（PATH 上有 lark-cli）→ 都没有则 `skipped`。字段按 `report-store-schema.md` §4.2。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Sequence

OVERVIEW_TABLE = "报告总览"
SOURCES_TABLE = "信息源清单"
OVERWRITE_NOTICE = "本表由 Owli 单向同步（SQLite 为权威）；飞书侧手改会在下次同步被覆盖，想改数据请回 Owli 前端改。"


class FeishuTransport(ABC):
    """最小传输面：建 base / 建表 / 按锚点 upsert / 建云文档。"""

    name = "abstract"

    @abstractmethod
    def ensure_base(self, name: str) -> str: ...

    @abstractmethod
    def ensure_table(self, base_token: str, name: str, fields: Sequence[Mapping[str, Any]]) -> str: ...

    @abstractmethod
    def upsert(self, base_token: str, table_id: str, anchor_field: str, anchor: str,
               record: Mapping[str, Any]) -> str: ...

    @abstractmethod
    def create_doc(self, title: str, markdown: str) -> tuple[str, str]:
        """返回 (document_id, url)。"""


class LarkCliTransport(FeishuTransport):
    """PATH 上有 `lark-cli` 时可用（本机烟测用）；每步一条 +shortcut 子进程。"""

    name = "lark-cli"

    def __init__(self, binary: str | None = None, identity: str = "bot") -> None:
        self.binary = binary or shutil.which("lark-cli") or "lark-cli"
        self.identity = identity

    def _run(self, *args: str) -> Any:
        proc = subprocess.run(
            [self.binary, *args, "--as", self.identity, "--format", "json"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"lark-cli {' '.join(args[:2])} 失败：{(proc.stderr or proc.stdout).strip()[:300]}")
        return json.loads(proc.stdout or "{}")

    def ensure_base(self, name: str) -> str:
        # §FU-1 真机取证：lark-cli 1.0.83 返回 data.base.base_token，没有 OpenAPI 的 app_token。
        data = self._run("base", "+base-create", "--name", name)
        token = _dig(data, "base_token") or _dig(data, "app_token")
        if not token:
            raise RuntimeError(f"建多维表格后拿不到 base_token：{json.dumps(data, ensure_ascii=False)[:200]}")
        return str(token)

    def ensure_table(self, base_token: str, name: str, fields: Sequence[Mapping[str, Any]]) -> str:
        # 真机取证：+table-list 返回 data.tables[]，每项的主键叫 id 不叫 table_id。
        listed = self._run("base", "+table-list", "--base-token", base_token)
        for table in _dig(listed, "tables") or _dig(listed, "items") or []:
            if table.get("name") == name:
                return str(table.get("table_id") or table["id"])
        data = self._run("base", "+table-create", "--base-token", base_token, "--name", name,
                         "--fields", json.dumps([_cli_field(f) for f in fields], ensure_ascii=False))
        table_id = _dig(data, "table_id") or _dig(data, "id")
        if not table_id:
            raise RuntimeError(f"建表「{name}」后拿不到 table_id：{json.dumps(data, ensure_ascii=False)[:200]}")
        return str(table_id)

    def upsert(self, base_token: str, table_id: str, anchor_field: str, anchor: str,
               record: Mapping[str, Any]) -> str:
        existing = self._find(base_token, table_id, anchor_field, anchor)
        args = ["base", "+record-upsert", "--base-token", base_token, "--table-id", table_id,
                "--json", json.dumps({k: _cli_value(v) for k, v in record.items()}, ensure_ascii=False)]
        if existing:
            args += ["--record-id", existing]
        # 真机取证：+record-upsert 的响应里没有 record_id（只有 record.update / updated），
        # 新建行的 id 只能按锚点回查——直接 str(_dig(...)) 会把 None 写成字符串 "None"。
        record_id = _dig(self._run(*args), "record_id") or existing \
            or self._find(base_token, table_id, anchor_field, anchor)
        if not record_id:
            raise RuntimeError(f"写入「{anchor}」后按锚点回查不到 record_id")
        return str(record_id)

    def _find(self, base_token: str, table_id: str, anchor_field: str, anchor: str) -> str | None:
        """按锚点查 record_id。lark-cli 的 filter 是 tuple 协议（{logic, conditions:[[字段,操作符,值]]}），
        不是 OpenAPI 的 {conjunction, conditions:[{field_name,operator,value}]}；命中行是列式的
        data.data[]，record_id 另在 data.record_id_list[]。"""
        found = self._run("base", "+record-list", "--base-token", base_token, "--table-id", table_id,
                          "--filter-json", json.dumps(
                              {"logic": "and", "conditions": [[anchor_field, "==", anchor]]},
                              ensure_ascii=False))
        ids = _dig(found, "record_id_list") or [
            item.get("record_id") for item in (_dig(found, "items") or []) if isinstance(item, Mapping)
        ]
        return str(ids[0]) if ids else None

    def create_doc(self, title: str, markdown: str) -> tuple[str, str]:
        data = self._run("docs", "+create", "--title", title, "--doc-format", "markdown", "--content", markdown)
        doc_id = str(_dig(data, "document_id") or _dig(data, "doc_token") or "")
        return doc_id, str(_dig(data, "url") or f"https://feishu.cn/docx/{doc_id}")


def _cli_field(field: Mapping[str, Any]) -> dict[str, Any]:
    """OpenAPI 字段规格（数字 type / field_name / property）→ lark-cli 字段 JSON。"""
    kind = int(field["type"])
    spec: dict[str, Any] = {"name": field["field_name"]}
    if kind in (3, 4):
        spec.update(type="select", multiple=kind == 4,
                    options=[{"name": o["name"]} for o in (field.get("property") or {}).get("options") or []])
    elif kind == 2:
        spec["type"] = "number"
    elif kind == 15:
        spec.update(type="text", style={"type": "url"})
    else:
        spec["type"] = "text"
    return spec


def _cli_value(value: Any) -> Any:
    """OpenAPI CellValue → lark-cli 快乐路径：超链接对象降为裸 URL。"""
    if isinstance(value, Mapping) and "link" in value:
        return str(value["link"])
    return value


def _dig(node: Any, key: str) -> Any:
    """深搜第一个同名键（lark-cli 与 OpenAPI 的包裹层次不同）。"""
    stack = [node]
    while stack:
        current = stack.pop(0)
        if isinstance(current, Mapping):
            if key in current:
                return current[key]
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None


class OpenApiTransport(FeishuTransport):
    """飞书 OpenAPI 直连：标准库 urllib，`ProxyHandler({})` 绕本机代理。"""

    name = "openapi"
    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, base_token: str | None = None) -> None:
        self.app_id, self.app_secret, self.base_token = app_id, app_secret, base_token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._token: str | None = None

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None, *, auth: bool = True) -> Any:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = f"Bearer {self._tenant_token()}"
        request = urllib.request.Request(
            f"{self.BASE}{path}", method=method, headers=headers,
            data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None,
        )
        with self._opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code") not in (0, None):
            raise RuntimeError(f"飞书 {path} 返回 {payload.get('code')}：{payload.get('msg')}")
        return payload.get("data", payload)

    def _tenant_token(self) -> str:
        if self._token is None:
            data = self._call("POST", "/auth/v3/tenant_access_token/internal",
                              {"app_id": self.app_id, "app_secret": self.app_secret}, auth=False)
            self._token = str(data["tenant_access_token"])
        return self._token

    def ensure_base(self, name: str) -> str:
        if self.base_token:
            return self.base_token
        data = self._call("POST", "/bitable/v1/apps", {"name": name})
        self.base_token = str(data["app"]["app_token"])
        return self.base_token

    def ensure_table(self, base_token: str, name: str, fields: Sequence[Mapping[str, Any]]) -> str:
        listed = self._call("GET", f"/bitable/v1/apps/{base_token}/tables?page_size=100")
        for table in listed.get("items") or []:
            if table.get("name") == name:
                return str(table["table_id"])
        data = self._call("POST", f"/bitable/v1/apps/{base_token}/tables",
                          {"table": {"name": name, "fields": list(fields)}})
        return str(data["table_id"])

    def upsert(self, base_token: str, table_id: str, anchor_field: str, anchor: str,
               record: Mapping[str, Any]) -> str:
        found = self._call("POST", f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/search",
                           {"filter": {"conjunction": "and", "conditions": [
                               {"field_name": anchor_field, "operator": "is", "value": [anchor]}]}})
        existing = found.get("items") or []
        if existing:
            record_id = str(existing[0]["record_id"])
            self._call("PUT", f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
                       {"fields": dict(record)})
            return record_id
        data = self._call("POST", f"/bitable/v1/apps/{base_token}/tables/{table_id}/records",
                          {"fields": dict(record)})
        return str(data["record"]["record_id"])

    def create_doc(self, title: str, markdown: str) -> tuple[str, str]:
        data = self._call("POST", "/docx/v1/documents", {"title": title})
        doc_id = str(data["document"]["document_id"])
        blocks = [{"block_type": 2, "text": {"elements": [{"text_run": {"content": line}}]}}
                  for line in markdown.splitlines() if line.strip()]
        for start in range(0, len(blocks), 50):
            self._call("POST", f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                       {"children": blocks[start:start + 50]})
        return doc_id, f"https://feishu.cn/docx/{doc_id}"


def _text(name: str) -> dict[str, Any]:
    return {"field_name": name, "type": 1}


def _select(name: str, options: Sequence[str] = ()) -> dict[str, Any]:
    return {"field_name": name, "type": 3, "property": {"options": [{"name": o} for o in options]}}


OVERVIEW_FIELDS: tuple[dict[str, Any], ...] = (
    _text("report_id"), _text("标题"), _text("调研问题"),
    _select("用例", ("social_competitor", "product_competitor", "other")),
    _select("状态", ("running", "completed", "failed", "archived")),
    _text("完成时间"), {"field_name": "标签", "type": 4, "property": {"options": []}},
    {"field_name": "证据总数", "type": 2}, {"field_name": "A·B 级占比", "type": 2},
    {"field_name": "报告云文档", "type": 15}, _text("执行摘要"), _text("同步说明"),
)
SOURCE_FIELDS: tuple[dict[str, Any], ...] = (
    _text("evidence_id"), _text("report_id"), {"field_name": "角标号", "type": 2},
    _select("平台"), _select("采集方式"), _text("标题"), {"field_name": "原文链接", "type": 15},
    _text("发布时间"), _text("抓取时间"), _select("可靠度等级", ("A", "B", "C", "D")),
    _text("五维明细"), _text("评分理由"), _text("原始热度"),
)
_LABELS = ("权威", "时效", "交叉", "完整", "无关")


def overview_record(report: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]],
                    tags: Sequence[str], doc_url: str | None) -> dict[str, Any]:
    """「报告总览」一行（schema §4.2）；锚点 report_id。"""
    graded = [e for e in evidence if e.get("grade") in ("A", "B")]
    record: dict[str, Any] = {
        "report_id": str(report["id"]), "标题": str(report.get("title") or ""),
        "调研问题": str(report.get("research_question") or ""), "用例": str(report.get("use_case") or "other"),
        "状态": str(report.get("status") or ""), "完成时间": str(report.get("completed_at") or ""),
        "标签": list(tags), "证据总数": len(evidence),
        "A·B 级占比": round(len(graded) / len(evidence), 4) if evidence else 0,
        "执行摘要": str(report.get("summary") or report.get("summary_line") or ""),
        "同步说明": OVERWRITE_NOTICE,
    }
    if doc_url:
        record["报告云文档"] = {"link": doc_url, "text": "报告正文"}
    return record


def source_record(item: Mapping[str, Any]) -> dict[str, Any]:
    """「信息源清单」一行；锚点 evidence_id；原始热度只做文本（R4）。"""
    from app.reliability.scoring import SCORE_FIELDS

    dims = "·".join(f"{label}{item.get(field) if item.get(field) is not None else '?'}"
                    for label, field in zip(_LABELS, SCORE_FIELDS))
    record: dict[str, Any] = {
        "evidence_id": str(item["id"]), "report_id": str(item["report_id"]),
        "角标号": int(item["citation_no"]), "平台": str(item.get("platform") or ""),
        "采集方式": str(item.get("fetch_method") or ""),
        "标题": str(item.get("title") or item.get("content_excerpt") or ""),
        "原文链接": {"link": str(item["permalink"]), "text": "原文"},
        "发布时间": str(item.get("published_at") or ""), "抓取时间": str(item.get("fetched_at") or ""),
        "五维明细": dims, "评分理由": str(item.get("rating_notes") or ""),
        "原始热度": json.dumps(item.get("raw_metrics") or {}, ensure_ascii=False),
    }
    if item.get("grade"):
        record["可靠度等级"] = str(item["grade"])
    return record


def doc_markdown(view: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]) -> str:
    """云文档正文：角标降级为 `[n]`，文末信息源清单（R6 云文档形态）。"""
    import re

    def degrade(text: str) -> str:
        return re.sub(r"\[S(\d{2})\]", lambda m: f"\\[{int(m.group(1))}\\]", text)

    lines = [f"# {view.get('title') or '报告'}", "", f"> {OVERWRITE_NOTICE}", ""]
    if view.get("conclusions"):
        lines += ["## 结论", "", *[f"- {degrade(c)}" for c in view["conclusions"]], ""]
    for section in view.get("sections") or []:
        if section.get("placeholder"):
            lines += [f"## {section.get('title') or section.get('section_id')}", "",
                      f"- 此节未写出，原因：{section.get('missing_reason')}", ""]
        elif section.get("markdown"):
            lines += [degrade(section["markdown"]), ""]
    lines += ["## 信息源", ""]
    lines += [f"{int(s['citation_no'])}. [{s.get('title') or s['permalink']}]({s['permalink']})" for s in sources]
    if view.get("missing"):
        lines += ["", "## 缺失清单", "", *[f"- {m.get('goal_id')}/{m.get('chapter_id')}：{m.get('reason')}" for m in view["missing"]]]
    return "\n".join(lines) + "\n"


def _read_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or Path(os.environ.get("OWLI_ENV_FILE") or Path.home() / ".owli" / ".env")
    values: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.startswith("#"):
                values[key.strip()] = value.strip().strip('"')
    return {**values, **{k: v for k, v in os.environ.items() if k.startswith("FEISHU_")}}


def select_transport(env: Mapping[str, str] | None = None) -> FeishuTransport | None:
    """env 齐 → OpenApi；否则 lark-cli 在 PATH → cli；否则 None（skipped）。"""
    values = _read_env() if env is None else dict(env)
    if values.get("FEISHU_APP_ID") and values.get("FEISHU_APP_SECRET"):
        return OpenApiTransport(values["FEISHU_APP_ID"], values["FEISHU_APP_SECRET"],
                                values.get("FEISHU_BITABLE_APP_TOKEN") or None)
    if os.environ.get("OWLI_FEISHU_DISABLE_CLI") != "1" and shutil.which("lark-cli"):
        return LarkCliTransport()
    return None


def push_to_feishu(store: Any, research_id: str, report_text: str, *,
                   transport: FeishuTransport | None = None, base_token: str | None = None) -> dict[str, Any]:
    """推一份报告：总览 1 行 + 被引证据 N 行 + 云文档；状态落 extra.feishu。"""
    from app.export.registry import record_feishu
    from app.report.render import parse_report

    chosen = transport or select_transport()
    if chosen is None:
        record_feishu(store, research_id, "skipped", message="未配置飞书（无 FEISHU_* 凭证且无 lark-cli）")
        return {"kind": "feishu", "status": "skipped", "message": "未配置飞书：请在 ~/.owli/.env 配置 FEISHU_APP_ID/SECRET，或安装 lark-cli"}
    report = store.get_report(research_id)
    evidence = store.list_evidence(research_id)
    cited = sorted((e for e in evidence if e.get("citation_no") is not None), key=lambda e: int(e["citation_no"]))
    view = parse_report(report_text)
    try:
        base = base_token or _read_env().get("FEISHU_BITABLE_APP_TOKEN") or chosen.ensure_base("Owli 研究报告")
        doc_id, doc_url = chosen.create_doc(str(view.get("title") or report["title"]), doc_markdown(view, cited))
        overview = chosen.ensure_table(base, OVERVIEW_TABLE, OVERVIEW_FIELDS)
        sources = chosen.ensure_table(base, SOURCES_TABLE, SOURCE_FIELDS)
        tags = store.read_validation_path("report_tags", research_id)
        record_id = chosen.upsert(base, overview, "report_id", research_id, overview_record(report, evidence, tags, doc_url))
        for item in cited:
            chosen.upsert(base, sources, "evidence_id", str(item["id"]), source_record(item))
    except Exception as error:  # noqa: BLE001 — 失败要落状态，不能让接口 500
        record_feishu(store, research_id, "failed", transport=chosen.name,
                      error=str(error)[:300], message=f"推送失败：{str(error)[:200]}")
        return {"kind": "feishu", "status": "failed", "message": f"推送失败：{str(error)[:200]}"}
    state = record_feishu(store, research_id, "synced", transport=chosen.name, base_token=base, doc_token=doc_id,
                          doc_url=doc_url, record_id=record_id, sources_pushed=len(cited))
    return {"kind": "feishu", "status": "synced", "message": f"已推送：总览 1 行、信息源 {len(cited)} 行、云文档 1 篇", **state}
