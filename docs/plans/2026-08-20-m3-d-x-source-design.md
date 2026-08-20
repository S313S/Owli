# M3-d X 信息源适配器设计

## 目标与边界

本包只实现 X API v2 recent search、查询侧降噪、本地互动量过滤、平台基线打分、受控预算台账、软预算提示、平台硬闸识别与 429 BACKOFF。明确不实现 full archive、用户主页时间线与报告渲染。

权威口径按 `.docs-ref/x-api-source-guide.md` §4、§5、§7、§8 与 `.docs-ref/source-reliability.md` §2 执行；任务指令与文档冲突时采用任务指令，例如 permalink 固定为 `https://x.com/i/status/<id>`。

## 方案

### 信息源模块

新增 `app/sources/x.py`。模块内声明 `SOURCE_SPEC`，包含 `source_id=x`、工具名 `source.x` 与调用入口；不新建或修改共享 registry。M3-b 的自动发现聚合只读取各源模块的声明。

适配器构造 recent search 参数时强制加入 `-is:retweet -is:reply` 与显式 `lang:`，固定请求 `created_at,public_metrics,author_id,lang,note_tweet`，默认不请求 expansions。响应先保留实际返回数量，再按 like/retweet 阈值在本地过滤；结构化结论同时记录过滤前后计数、请求次数、新计费帖子数、预算闸类型和最终状态。

### 配置与凭证

预算、单价、余额、账期 cap、账期已用、阈值、分页上限和 API 基址全部由配置对象传入。预算护栏代码内不出现单价字面量。`X_BEARER_TOKEN` 只由固定凭证加载器从 `~/.owli/.env` 读取，不回显、不进入异常文本或日志。

### 受控预算台账

在 `app/store/` 增加固定用量 Store。SQLite SQL 只能出现在该目录的固定方法内；`app/sources/` 不导入 `sqlite3`，也不包含 SQL 字符串。

既有 schema 通过版本迁移只向前升级 `user_version`，增加两张系统运行态表：按 UTC 日累计请求数和去重读取数的 `source_usage`，以及以 `(source, utc_date, resource_id)` 为主键的 `source_usage_billed_resource`。不修改 `evidence` 冻结列。迁移由 store schema 层显式执行，启动自检继续以最终 schema 快照为准。

### 三道防线与事件

1. 请求前按 `max_results × pages × 配置单价` 预估。有效可用额度取周预算、账期 cap 剩余额度、credits 余额三者最小值。达到 80% 或预计越界时发非阻塞 `EXTRA_QUOTA_CONFIRM` 类 `card_update`，任务继续。
2. 响应后按 UTC 日和 post ID 原子去重，只对首次出现的实际返回帖子记账；expansions 不计费。
3. 平台拒绝请求时识别账期 cap 或 credits 硬闸，发送 `source_unavailable`，事件明确 `gate=platform_billing_cycle_cap` 或 `gate=platform_credits_balance`。若与周预算同时冲突，以平台硬闸为最终状态并保留软预算预警事件。

429 映射到 M1 `RouteState.BACKOFF`，`resets_at` 直接读取 `x-rate-limit-reset`。适配器等待到重置时刻后重试，并依次发出挂起与恢复事件；假 HTTP 测试注入假时钟和 sleeper，不进行真实等待。

## 入库与打分

每条 X 证据保留 like、retweet、reply、quote 四项指标到 `raw_metrics`，使用 `x` 平台基线 `1/2/0/1/2`，`rated_by=baseline:x@v1`。批量入库只调用 Store 的具名方法，适配器不持有裸数据库通道。

## 验证

假 HTTP 测试覆盖查询降噪、本地过滤、三道防线逐道、UTC 去重记账、硬闸冲突、429 BACKOFF/恢复和 token 日志安全。随后运行全量 pytest、SQL 越界 grep、硬编码单价 grep、双引擎脚本与无人值守全链路。真实小额查询最多 10 条；Console 用量只能在可读取真实账户状态时判 PASS，否则明确记录 BLOCKED。
