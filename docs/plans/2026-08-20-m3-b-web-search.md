# M3-b Web Search Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 接入 Exa 主源与 Tavily 备源，并完成自动发现、M3-a 打分、具名 Store 入库和降级事件。

**Architecture:** 每个源模块用 `SOURCE_SPEC` 自声明，通用 registry 扫描 `app.sources` 自动聚合。
`web_search.py` 保持 HN 风格的数组接口，并提供显式的 `collect_and_store()` 组合入口；
可靠度计算只调用 M3-a 纯函数，数据库只调用 Store 具名接口。

**Tech Stack:** Python 3.13、stdlib `urllib`、SQLite Store、pytest、NormalizedEvent JSONL。

---

### Task 1: 分散声明与自动发现注册表

**Files:**
- Create: `app/sources/spec.py`
- Create: `app/sources/registry.py`
- Modify: `app/sources/hn.py`
- Test: `tests/test_source_registry.py`

**Step 1: Write the failing test**

测试 `SourceSpec` 校验 `source_id/tool_name/entrypoint`，自动扫描能发现 HN，
`get_tool("source.hacker_news")` 返回 HN 的 `search`，重复 source id/tool name 明确拒绝，
并断言 `registry.py` 文本不含任何具体源 id。

**Step 2: Run test to verify it fails**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_source_registry.py -q`
Expected: FAIL，缺少 `app.sources.registry`。

**Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    tool_name: str
    entrypoint: Callable[..., Any]

SOURCE_SPEC = SourceSpec(
    source_id="hacker_news",
    tool_name="source.hacker_news",
    entrypoint=search,
)
```

registry 用 `pkgutil.iter_modules(app.sources.__path__)` + `importlib.import_module()` 扫描，
跳过 `_` 开头、`registry`、`spec`；仅聚合类型正确的 `SOURCE_SPEC`，重复声明抛 `ValueError`。

**Step 4: Run test to verify it passes**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_source_registry.py tests/test_hn.py -q`
Expected: PASS。

### Task 2: M3-a 识别 Tavily 发布时间降级

**Files:**
- Modify: `app/reliability/scoring.py`
- Test: `tests/test_reliability.py`

**Step 1: Write the failing test**

构造 `published_at == fetched_at` 且
`extra.freshness_degraded_source == "fetched_at"` 的 `web_search` 证据，断言
`score_freshness == 1` 且 `rating_notes` 含“抓取时刻兜底”；无标记的同时间证据仍按原规则评分。

**Step 2: Run test to verify it fails**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_reliability.py -q`
Expected: FAIL，当前返回时效 2。

**Step 3: Write minimal implementation**

只在 `_freshness()` 的纯函数路径读取受控 extra 标记；命中时要求发布时间与抓取时间均存在，
返回 `(1, "抓取时刻兜底", None)`，不在适配器里手改分数。

**Step 4: Run test to verify it passes**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_reliability.py -q`
Expected: PASS。

### Task 3: 凭证、自检、Exa 正常与空结果

**Files:**
- Create: `app/sources/web_search.py`
- Test: `tests/test_web_search.py`

**Step 1: Write failing tests**

覆盖：仅从传入的 `.env` 路径读取；不读取 `os.environ`；Exa UUID 与 Tavily `tvly-`
格式；错误信息不含 key；Exa 请求固定 `type=neural`、`numResults=10`、
`contents.text.maxCharacters=1200`；命中映射为 HN 同构字段；正常空结果返回 `[]`
且不会请求 Tavily，并发出 `outcome=empty` 的结构化事件。

**Step 2: Run tests to verify RED**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_web_search.py -q`
Expected: FAIL，缺少 `app.sources.web_search`。

**Step 3: Write minimal implementation**

实现 `CredentialError`、逐行 `.env` 解析、正则自检、可注入 `http_post`，以及：

```python
_EXA_PAYLOAD = {
    "type": "neural",
    "numResults": 10,
    "contents": {"text": {"maxCharacters": 1200}},
}
```

证据固定含 `platform/source_type/platform_item_id/permalink/title/content_excerpt/author_name/`
`source_keyword/fetch_method/published_at/fetched_at/raw_metrics/extra`；URL 非绝对 HTTP(S)
或响应缺少 results 数组时拒绝该响应。

**Step 4: Run tests to verify GREEN**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_web_search.py tests/test_hn.py -q`
Expected: PASS。

### Task 4: Exa 失败降级 Tavily、answer 隔离与事件落盘

**Files:**
- Modify: `app/sources/web_search.py`
- Test: `tests/test_web_search.py`
- Test: `tests/test_normalized_events.py`

**Step 1: Write failing tests**

覆盖 Exa 缺 key、HTTP 429、HTTP 5xx、传输错误和响应错误均降级；Exa 空结果不降级；
Tavily 请求 `search_depth=advanced`、`include_answer=true`、`include_raw_content=text`；
响应 `answer` 只进入 lead 日志事件，证据与 Store payload 中均不可出现；Tavily 每条
`published_at=fetched_at`、`extra.freshness_degraded_source=fetched_at`、
`norm_context.degraded` 标注 provider/field/source；降级 `NormalizedEvent`
既调用回调又写 routing JSONL，日志全文不含 key。

**Step 2: Run tests to verify RED**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_web_search.py tests/test_normalized_events.py -q`
Expected: FAIL，尚无 Tavily 降级与 source routing 日志。

**Step 3: Write minimal implementation**

统一把 provider 失败分类成不含凭证的中文原因；构造 `route_state="FAILOVER"`、
`failover_target="tavily"` 的 `NormalizedEvent`。扩展 `append_routing_event()` 只复用现有
结构，不新增厂商专用日志器。answer 事件 raw 只记录 answer 文本和 provider，
映射函数从不读取 answer。

**Step 4: Run tests to verify GREEN**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_web_search.py tests/test_normalized_events.py -q`
Expected: PASS。

### Task 5: M3-a 归一化、五维打分与 Store 入库

**Files:**
- Modify: `app/sources/web_search.py`
- Test: `tests/test_web_search.py`
- Test: `tests/test_dao.py`

**Step 1: Write failing tests**

用真实临时 SQLite schema 与假 HTTP 调 `collect_and_store()`；断言每条记录：
`norm_method=none`、`normalized_score IS NULL`、`norm_context.reason=no_metric_available`、
五维均为 0–2 整数、`rating_notes` 合法、`rated_by=rule:reliability@v1`；
Tavily answer 不在 evidence 任一 JSON/text 列；批内任一非法条目时 Store 整批回滚。

**Step 2: Run tests to verify RED**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_web_search.py tests/test_dao.py -q`
Expected: FAIL，尚无 `collect_and_store()`。

**Step 3: Write minimal implementation**

为每条生成 UUID id 并补 report/goal/agent 字段，整批调用：

```python
normalized = normalize_evidence_metrics(items, computed_at=fetched_at,
    report_id=report_id, goal_id=goal_id, queries=[query], filters="provider search")
for item in normalized:
    item.update(score_evidence(item), rated_by="rule:reliability@v1")
store.add_evidence_batch(items)
```

Tavily 的 `norm_context.degraded` 在纯函数返回后追加，保留 §5 全部必填键。

**Step 4: Run tests to verify GREEN**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest tests/test_web_search.py tests/test_dao.py tests/test_reliability.py -q`
Expected: PASS。

### Task 6: 全量、真实查询与架构审计

**Files:**
- Create: `scripts/t-m3-web-search.py`
- Modify: `tests/test_web_search.py`

**Step 1: Add acceptance runner**

脚本读取真实 `~/.owli/.env`，创建临时数据库与 report，经 `collect_and_store()` 查询
“飞书 竞品 协作工具 定价”，逐条检查绝对 permalink、ISO 8601 `fetched_at`、五维齐全、
热度为空；另提供 `OWLI_FORCE_EXA_429=1` 的本地验收注入，只替换 HTTP transport，
不得改业务分支。脚本打印逐项 PASS/FAIL 和证据数量，不以退出码代替断言。

**Step 2: Run full fake-HTTP suite**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/pytest -q`
Expected: 全部 PASS，0 failures。

**Step 3: Run real provider acceptance**

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/python scripts/t-m3-web-search.py --query "飞书 竞品 协作工具 定价"`
Expected: 证据不少于 5 条，逐条 URL/ISO/五维/无热度检查 PASS。

Run: `OWLI_FORCE_EXA_429=1 /Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/python scripts/t-m3-web-search.py --query "飞书 竞品 协作工具 定价"`
Expected: provider=tavily，routing 日志与事件回调含明确降级原因，answer 未入库。

**Step 4: Run audit and regressions**

Run: `rg -n "execute\(|executemany\(|SELECT |INSERT |UPDATE |DELETE " app/sources app/reliability`
Expected: 适配器与可靠度层无裸 SQL 命中。

Run: `rg -n "if .*engine|elif .*engine" app/orchestrator`
Expected: 无新增引擎分支。

Run: `rg -n "EXA_API_KEY|TAVILY_API_KEY|x-api-key|Bearer" var/logs tests -g '*.jsonl'`
Expected: 日志无 key 或认证头值。

Run: `/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/python scripts/t-m1-dual-engine.py`
Expected: Claude/Codex 均打印 PASS，结构化结论字段一致。

Run: `OWLI_AUTO_CONFIRM=1 /Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli/.venv/bin/python scripts/t-m2-orchestrator.py`
Expected: 最后一行结构化验收 PASS。

**Step 5: Commit explicit paths only**

```bash
git add app/sources/spec.py app/sources/registry.py app/sources/hn.py \
  app/sources/web_search.py app/reliability/scoring.py \
  tests/test_source_registry.py tests/test_web_search.py tests/test_reliability.py \
  tests/test_normalized_events.py scripts/t-m3-web-search.py
git commit -m "feat: 接入 Exa 主 Tavily 备的网页搜索源"
```
