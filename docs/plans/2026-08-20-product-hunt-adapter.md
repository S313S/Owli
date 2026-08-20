# Product Hunt 适配器实施计划

> **执行要求：** 使用 `executing-plans` 逐项实施，并严格遵守 TDD 的 RED → GREEN → REFACTOR。

**目标：** 新增 Product Hunt GraphQL 信息源适配器，按时间窗内票数排序采集、归一化、五维打分并通过 Store 入库。

**架构：** `app/sources/product_hunt.py` 独立声明 `SOURCE_SPEC`，不修改共享注册文件。`search()` 保持返回 `Evidence[]`；空窗口、BACKOFF 与恢复状态默认经 M1 normalized_event 日志发布，`on_event` 仅作附加通知。适配器使用共享滚动预算器和响应头校准额度，所有凭证只由 `~/.owli/.env` 加载。

**技术栈：** Python 标准库 `urllib`、M1 `RouteState/RouteDecision/NormalizedEvent`、M3-a `normalize_evidence_metrics/score_evidence`、SQLite `Store`、pytest/unittest mock。

---

### 任务 1：事件发布公共入口

**文件：** 修改 `app/adapters/ratelimit.py`；测试 `tests/test_product_hunt.py`。

1. 写失败测试：显式发布 `CONTINUE` 时，即使无回调也写 routing event；挂回调时额外收到同一事件。
2. 运行该测试，确认因公共发布函数不存在而失败。
3. 新增最小公共 `publish_route_decision()`，默认保持 M1 原行为；仅调用方明确要求时发布 `CONTINUE`。
4. 运行定向测试，确认通过。

### 任务 2：GraphQL 构造、分页与证据映射

**文件：** 新建 `app/sources/product_hunt.py`；测试 `tests/test_product_hunt.py`。

1. 写失败测试：查询必须同时含 `postedAfter`、`order: VOTES`、游标和 `pageInfo`，且两页结果按 API 顺序拼接。
2. 实现 `search(query, window) -> Evidence[]`、窗口解析、GraphQL 请求、游标分页、官方 permalink 校验、ISO 8601 `fetched_at`。
3. 写并验证空窗口测试：返回 `[]`，同时默认落一条结构化 `CONTINUE/empty_window` 事件。

### 任务 3：额度计数、429 退避与恢复

**文件：** 修改 `app/sources/product_hunt.py`；测试 `tests/test_product_hunt.py`。

1. 写失败测试：本地预算不足和 HTTP 429 都发布 `BACKOFF`，按 reset 秒退避，随后成功请求发布 `CONTINUE/recovered`。
2. 实现 6250 点/900 秒滚动预算器、每请求预留 100 点、响应头校准、有限次数重试。
3. 断言未挂 `on_event` 时日志事件仍完整，挂载后回调只是副本。

### 任务 4：归一化、五维打分与 Store 入库

**文件：** 修改 `app/sources/product_hunt.py`；测试 `tests/test_product_hunt.py`。

1. 写失败测试：20 条同平台证据按 `votes_count` 计算百分位；`raw_metrics` 保留 GraphQL 原字段及 M3-a 蛇形指标；五维分和 `rating_notes` 齐全。
2. 调用 M3-a 归一化与打分纯函数；提供 Store 时补齐 report/goal/id 并调用 `add_evidence_batch()`。
3. 读取数据库验证行数、五维、原始指标及归一化上下文。

### 任务 5：凭证、安全、注册声明与验收

**文件：** 修改 `app/sources/product_hunt.py`；测试 `tests/test_product_hunt.py`。

1. 写失败测试：只解析 `~/.owli/.env` 的 `PRODUCT_HUNT_TOKEN`；错误中不含 token；`SOURCE_SPEC` 映射 `product_hunt → source.product_hunt`。
2. 实现最小加载器与模块内声明。
3. 运行 Product Hunt 定向测试、全量 pytest、grep 安全审计。
4. 使用真实 token 拉近 7 天前 20 条并验证 URL、票数顺序、ISO 时间与入库内容。
5. 运行双引擎脚本和无人值守全链路；按真实输出逐条报告 PASS/FAIL，不以退出码替代协议断言。
