# M3-f Transport Resilience Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 Owli 增加 research 级执行期断路器、规划分段续写和集中韧性配置，同时保持规划只走 Claude 与既有限流行为。

**Architecture:** `ResilienceConfig` 统一提供数值；M3-a 分类器给决策标结构化原因，`RoutedAdapter` 独占断路和路由覆盖；规划器通过适配层专用短流协议逐段落盘并确定性拼接。Scheduler 仅转发健康事件，不保存或计算引擎倾向。

**Tech Stack:** Python 3.11、asyncio、Claude Agent SDK、pytest。

---

### Task 1: 集中韧性配置

**Files:** Create `app/config.py`; Create `tests/test_config.py`; Modify `app/orchestrator/scheduler.py`。

1. 先写默认值、环境覆盖、非法非正数拒绝的失败测试。
2. 运行 `pytest tests/test_config.py -q`，确认因模块缺失 RED。
3. 实现不可变 `ResilienceConfig` 与 `load_resilience_config(environ=None)`；默认 3/3/60/900/300，并校验 initial <= max。
4. Scheduler 注入配置，把硬编码 BACKOFF 序列改为 `min(initial * 2**count, max)`；不得增加路由状态。
5. 运行 `pytest tests/test_config.py tests/test_scheduler.py -q`，确认 GREEN。
6. 明确添加相关文件并提交 `feat: 集中 M3-f 韧性配置`。

### Task 2: 结构化传输原因与 research 级断路器

**Files:** Create `app/adapters/circuitbreaker.py`; Create `tests/test_circuitbreaker.py`; Modify `app/adapters/ratelimit.py`, `app/adapters/events.py`, `app/adapters/routing.py`, `tests/test_ratelimit.py`, `tests/test_m1_wiring.py`。

1. 写 RED：单次/两次传输仅 BACKOFF，第三次执行期升级；规划期不升级；429/529 不计数；非传输结果打断连续计数；候选不健康不让路；周期探活成功发 `PROBE_OK/RESET`。
2. 运行 `pytest tests/test_circuitbreaker.py tests/test_ratelimit.py -q`，确认预期失败。
3. 给 `RouteDecision`/`NormalizedEvent` 增加 `cause` 和健康事件字段；公开复用 M3-a 传输指纹分类，不复制正则。
4. 实现纯状态 `ResearchCircuitBreaker`；`RoutedAdapter` 聚合一次 run 的最终原因、独占 `_route_override`，规划任务绕过升级。
5. 给 Claude/Codex adapter 增加真实小请求 `probe`，成功只认结构化输出标记，不看退出码；probe 不参与断路计数。
6. 删除 Scheduler 的 `future_engine`、`_switch_targets` 和引擎备选计算；用户 C3 切换仍以卡片结果作为下一轮 task override 传给适配层，不形成全局倾向。
7. 运行专项测试并提交 `feat: 增加执行期传输断路与探活复位`。

### Task 3: 规划短流协议与确定性续写

**Files:** Modify `app/adapters/contracts.py`, `app/adapters/claude.py`, `app/adapters/routing.py`; Create `app/plan/segments.py`; Create `tests/test_plan_segments.py`。

1. 写 RED：partial 与正式文件分离、重试前清 partial、最长重叠去重、无重叠直拼、JSON 字符串 token 中断、完整 envelope 双腿判定。
2. 运行 `pytest tests/test_plan_segments.py -q`，确认 RED。
3. 定义 `PlanningSegmentRequest/Result`；`RoutedAdapter.run_planning_segment` 强制选择 planning 默认路由且忽略断路覆盖。
4. Claude 规划短流开启 partial messages，逐 text delta 回调落盘；中断返回已收前缀和结构化 transport 原因；续写使用官方 user-continuation。
5. 实现 `merge_continuation(prefix, suffix)` 的最长重叠确定性拼接及 envelope 解析。
6. 运行专项测试并提交 `feat: 增加规划段级 partial 续写协议`。

### Task 4: 骨架与逐 goal 分段生成

**Files:** Modify `app/plan/generate.py`, `app/plan/lint.py`, `tests/test_plan_generate.py`, `tests/test_plan_lint.py`。

1. 写 RED：`runs/<research_id>/plan-segments/` 产生 skeleton/goal-N/assembled；局部段按配置重试；规划永不访问 Codex；拼装后 lint 通过。
2. 写 RED：跨 goal 路径冲突阻断；报告 validator 双向角标缺一即阻断；两条规则只检查结构字段。
3. 运行对应节点，确认均为预期 RED。
4. 把现有整份生成循环替换为骨架→逐 goal→确定性 assembled→`_build_plan`→lint；错误按结构定位回灌目标段。
5. 报告输出默认注入 `citation_marks_resolvable` 与 `no_orphan_citation`；正式计划仍走既有 snapshot 原子保存。
6. 运行 `pytest tests/test_plan_generate.py tests/test_plan_lint.py -q` 并提交 `feat: 将计划生成改为分段落盘与 lint 收口`。

### Task 5: 文档、集成与验收

**Files:** Modify `README.md` 或新增公开部署文档；Modify `tests/test_m2_wiring.py`；不得修改验收 runner。

1. 写配置与调整场景文档，明示特定环境只在部署配置调数值。
2. 加路由到 Runtime 的集成测试，断言 Scheduler 对象没有路由覆盖属性。
3. 运行专项与全量：`pytest -q`、`python scripts/t-m1-dual-engine.py`、`python scripts/t-m2-orchestrator.py`、`python scripts/t-m3-multi-source.py`。
4. 运行故障注入、限流注入、规划断流续写、配置覆盖和无人值守真实全链路，保存五项完整输出。
5. 运行审计 grep，检查编排层引擎分支、退出码判成功、裸 SQL、danger-full-access、Scheduler 真实时钟。
6. 核对 `git diff --check`、公开文件白名单、凭证与 `var/` 未跟踪，再提交 `test: 补齐 M3-f 全链路验收`。
