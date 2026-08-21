# M3-f Session Stall Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为执行期连续非限流 `api_retry` 增加可配置、可取证、只触发一次的会话停滞终止机制，并把它作为传输故障接入现有断路器与 goal 重试链。

**Architecture:** 新增纯 `SessionStallDetector`，只接受归一化事件和注入时钟，不访问网络或真实时钟。`RoutedAdapter` 为每个非规划 run 独立持有 detector；触发后发 `SESSION_STALL`、调用当前适配器 `interrupt()`、记录一次 transport failure 并让本次 run 失败。Claude 事件归一化只负责把 `SystemMessage(subtype="api_retry")` 转成结构化语义，Scheduler 不新增状态。

**Tech Stack:** Python 3.12、asyncio、Claude Agent SDK 归一化事件、pytest。

---

### Task 1: 配置与纯状态机

**Files:**
- Create: `app/adapters/session_stall.py`
- Modify: `app/config.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_session_stall.py`

1. 写 RED：默认 `session_stall_timeout_seconds == 600`，环境覆盖生效，非正整数拒绝。
2. 写 RED：首个非限流 `API_RETRY` 只进入计时；达到阈值触发一次并返回 `elapsed_seconds/api_retry_count`；`TOOL_CALL/OUTPUT` 复位；限流 retry 退出计时。
3. 运行 `/private/tmp/owli-m3f-py312/bin/python -m pytest -q tests/test_config.py tests/test_session_stall.py`，确认因字段/模块缺失而 RED。
4. 在 `ResilienceConfig` 末尾增加默认字段 `session_stall_timeout_seconds=600`，并从 `OWLI_SESSION_STALL_TIMEOUT_SECONDS` 加载、校验。
5. 实现无副作用 `SessionStallDetector.observe(event)`：时钟只由构造函数注入；状态为 `ACTIVE/RETRYING/TRIPPED`；返回不可变 `SessionStallEvidence` 或 `None`。
6. 重跑同一命令确认 GREEN。
7. 明确添加四个文件并提交 `feat: 增加执行期会话停滞状态机`。

### Task 2: 归一化真实 api_retry 事件

**Files:**
- Modify: `app/adapters/events.py`
- Modify: `tests/test_normalized_events.py`
- Modify: `tests/test_session_stall.py`

1. 写 RED：Claude `SystemMessage(subtype="api_retry")` 归一为 `outcome="API_RETRY"`；`data.api_error_status=429` 或结构化 rate-limit info 时 `cause="rate_limit"`，普通 retry 不预先标 transport。
2. 写 RED：重放首条后每 60–180 秒一条、总跨度 68 分钟的真实形态事件；注入时钟推进后断言 600 秒处产生一条且只有一条 evidence，计数准确。
3. 运行对应节点确认预期 RED。
4. 扩展 Claude SystemMessage 归一化，保持其它 system subtype 行为不变；限流只读结构化字段，不使用自由文本白名单。
5. 重跑对应节点确认 GREEN。
6. 提交 `feat: 归一化 Claude api_retry 会话信号`。

### Task 3: RoutedAdapter 终止与断路接线

**Files:**
- Modify: `app/adapters/routing.py`
- Modify: `tests/test_m1_wiring.py`
- Modify: `tests/test_m3f_acceptance.py`

1. 写 RED：执行期达到停滞阈值后发一次 `SESSION_STALL`，raw 包含 `elapsed_seconds/api_retry_count`，调用 `interrupt()`，本次 run 强制失败并给断路器累计一次。
2. 写 RED：工具活动后旧 retry 窗口不触发；限流 retry 不触发；规划 run 即使收到同序列也不创建 detector、不 interrupt、不 FAILOVER。
3. 写 RED：把停滞阈值改为 120 秒后行为同步改变。
4. 运行三个测试文件的停滞节点，确认 RED 原因是 RoutedAdapter 尚未接线。
5. 给 `RoutedAdapter` 增加必需的注入时钟参数；由调用方/测试明确传入，不在状态机或 Scheduler 内读取真实时钟。每个执行 run 新建 detector。
6. detector 触发时先投影 `SESSION_STALL/BACKOFF/cause=transport`，再调用当前 adapter 的 `interrupt()`；无论底层最终返回何种结果，本 run 都按失败处理并只调用一次 `_trip_if_needed`。
7. 保持规划固定 Claude、限流 BACKOFF 和现有并发 trip 门闩不变。
8. 重跑专项确认 GREEN。
9. 提交 `feat: 接入执行期 SESSION_STALL 终止与断路`。

### Task 4: 文档与完整验收

**Files:**
- Modify: `docs/deployment-resilience.md`
- Modify: `docs/plans/2026-08-21-m3-f-transport-resilience-design.md`

1. 文档增加默认 600 秒、部署调整场景、事件取证字段，以及“完全静默仍由 12 小时总闸兜底”的已知边界。
2. 运行专项：配置、状态机、事件归一化、断路/限流、M3-f acceptance。
3. 运行全量 `/private/tmp/owli-m3f-py312/bin/python -m pytest -q`。
4. 复跑 `t-m1-dual-engine.py`、`OWLI_AUTO_CONFIRM=1 t-m2-orchestrator.py`、`t-m3-multi-source.py` 与 M3-f 五项注入验收；不以退出码替代结构化输出。
5. 运行审计 grep：编排层引擎分支、退出码判成功、非存储层裸 SQL、danger-full-access、Scheduler/RoutedAdapter 停滞逻辑真实时钟均零命中。
6. 运行 `git diff --check`，确认 `var/` 私有快照未跟踪、工作树只含批准文件。
7. 提交 `test: 补齐会话停滞重放与回归验收`。
