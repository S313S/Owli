# M3-b 网页搜索适配器设计

## 目标与边界

在 `web_search` 源下接入 Exa 主源与 Tavily 备源，返回与 HN 适配器同构的证据条目；
每条证据经 M3-a 纯函数完成归一化与五维打分，再通过 `Store.add_evidence_batch()` 入库。
本次不接 Reddit、不写 Serper 代码、不生成报告角标。

## 模块划分

- `app/sources/web_search.py`：凭证加载、自检、假 HTTP 可注入的 Exa/Tavily 请求、响应映射、
  降级事件、批量打分与具名 Store 入库。
- `app/sources/spec.py`：定义无具体源知识的 `SourceSpec` 数据契约。
- `app/sources/registry.py`：扫描 `app.sources` 下的模块，聚合其中的 `SOURCE_SPEC`；
  不硬编码任何源 id、工具名或模块名。
- `app/sources/hn.py`：只增加模块内 `SOURCE_SPEC` 声明，现有 `search()` 行为不变。
- `app/reliability/scoring.py`：M3-a 纯函数识别“发布时间由抓取时刻降级代替”的受控标记，
  时效分封顶为 1，理由写为抓取时刻兜底。

M3-c/d 可分别只改自己的 `product_hunt.py`、`x.py`，无需编辑注册聚合器。

## 数据流

1. 从 `~/.owli/.env` 读取 `EXA_API_KEY` 与 `TAVILY_API_KEY`，不继承进程环境变量。
2. 对存在的 key 做格式校验：Exa 为无前缀 36 位 UUID；Tavily 以 `tvly-` 开头。
   任一存在但格式错误即拒绝启动该源，错误只写变量名与格式要求。
3. 优先请求 Exa `/search`：`type=neural`、`contents.text.maxCharacters=1200`。
4. Exa 正常空命中时返回 `[]`，发出结构化空结果说明，不调用 Tavily。
5. Exa 缺凭证、HTTP 429、其他 HTTP/传输/响应错误时，生成 `NormalizedEvent`
   并写 routing JSONL，再调用 Tavily。
6. Tavily 请求显式开启正文并关闭 answer 入证据路径；响应即使包含 `answer`，也只写线索日志。
7. 结果映射为 `platform=web_search` 的证据。Exa 使用绝对 ISO `publishedDate`；
   Tavily 将 `published_at=fetched_at`，同时在受控上下文标注 degraded 来源。
8. 整批调用 `normalize_evidence_metrics()`：网页搜索固定得到
   `normalized_score=NULL`、`norm_method=none`、`reason=no_metric_available`。
9. 每条调用 `score_evidence()`，随后一次性走 `Store.add_evidence_batch()`；
   适配器和注册表均不包含 SQL。

## 事件与安全

降级事件使用现有 `NormalizedEvent`，`route_state=FAILOVER`，raw 只含源 id、厂商、
HTTP 状态与分类后的原因，不含请求头、请求体或 key。事件既传给调用方回调，也由
`append_routing_event()` 落盘。空结果与 Tavily answer 线索同样用结构化事件表达，
但不伪装成证据。

## 对外接口

- `search(query, window, *, env_path=..., http_post=..., on_event=...) -> list[Evidence]`
- `collect_and_store(query, window, *, report_id, goal_id, store, ...) -> list[Evidence]`
- `SOURCE_SPEC = SourceSpec(source_id="web_search", tool_name="source.web_search", entrypoint=search)`
- `discover_sources()` / `get_source()` / `get_tool()` 由注册表提供。

## 测试与验收

假 HTTP 覆盖 Exa 正常、Exa 空结果、Exa 错误/429 降级、缺 Exa key 降级、
Tavily answer 拒入库、两家凭证格式自检、Tavily 时效降级、自动发现与重复声明拒绝。
完成后运行全量 pytest、真实查询、拔 key/429 降级、数据库审计、grep 审计、
双引擎脚本与无人值守回归；退出码只作辅助，最终以结构化 PASS 与实际产物为准。
