# 报告节化四条修复 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复报告节化的产物保全、结构化结论、诚实拼装计数与执行错误落账，使报告夹具可作为下一次整跑的前置门禁。

**Architecture:** 适配层统一请求并优先消费 Claude SDK `structured_output`，文本围栏只作为兼容兜底；编排层只根据统一 `EngineRunResult` 判定是否做一次结论定向重试。失败产物在同目录改名保全，父报告始终落盘，但零个成功节以结构化 missing 终态返回。

**Tech Stack:** Python 3.13、Claude Agent SDK、SQLite STRICT、pytest。

---

### Task 1: 保全被拒绝的非空节正文

**Files:**
- Modify: `app/orchestrator/sectioning.py`
- Test: `tests/test_m3h_sectioning.py`

1. 写集成测试：失败节已有非空正文时，断言正文被原样移动到 `sec-N.rejected.md`，占位仍写入原路径，账本错误字段含保留路径。
2. 运行该测试，确认当前实现因原路径直接覆盖而失败。
3. 实现单文件保全与错误字段增记，不新增冻结列。
4. 运行该测试，确认通过。

### Task 2: SDK 结构化结论与一次定向重试

**Files:**
- Modify: `app/adapters/claude.py`
- Modify: `app/orchestrator/sectioning.py`
- Modify: `app/store/dao.py`
- Modify: `app/store/schema.py`
- Modify: `app/store/schema.sql`
- Create: `app/store/migrations/v5_add_conclusion_invalid_reason.sql`
- Test: `tests/test_m1_wiring.py`
- Test: `tests/test_m3h_sectioning.py`
- Test: `tests/test_m3h_ledger.py`

1. 写适配器测试：执行任务传入 `owli-result.schema.json`，并优先把 `ResultMessage.structured_output` 转为 `OwliResult`。
2. 写节化测试：artifact 非空且 validators PASS、仅结论无效时只重试一次；第二次仍无效则 reason 为 `conclusion_invalid`。
3. 写账本与 v4→v5 迁移测试：新闭集值可以持久化。
4. 分别运行测试确认 RED。
5. 实现 schema 加载、结构化值解析、围栏兜底、定向重试与闭集迁移。
6. 分别运行测试确认 GREEN。

### Task 3: 拼装器不制造标题并诚实返回 done 数

**Files:**
- Modify: `app/orchestrator/sectioning.py`
- Test: `tests/test_m3h_sectioning.py`

1. 写测试断言拼装结果不会补 `# 结论` / `# 信息源`。
2. 写零成功节测试：占位报告存在，返回 `succeeded=False`、`chapter_status=missing`、`actual_count=0`。
3. 写部分成功测试：`actual_count` 等于账本真实 done 节数。
4. 运行测试确认 RED，实施最小改动，再运行确认 GREEN。

### Task 4: 执行路径汇总原始 engine_error

**Files:**
- Modify: `app/adapters/claude.py`
- Test: `tests/test_m1_wiring.py`

1. 写 SDK 流测试：`ResultMessage(subtype=success, is_error=True)` 不抛异常时，`engine_error` 必须含原始结果、`subtype` 与 `api_error_status`。
2. 运行测试确认 RED。
3. 对齐规划短流，把错误事件原文汇总后传入 `EngineRunResult`。
4. 运行测试确认 GREEN。

### Task 5: 夹具与全量验收

**Files:**
- Test only: `scripts/fixtures/report_sectioning_fixture.py`

1. 运行报告夹具并保存完整输出，逐条核对 rejected 文件、done 数、报告正文、账本 reason 与 `actual_count`。
2. 对 engine_error 人为断连项，优先用可重复的 SDK 错误事件测试取证；只有能安全控制真实代理时才做人工掐断，不把未执行项冒充 PASS。
3. 运行全量 pytest，记录尾行。
4. 检查 git diff，确认未改 `source_mcp.py`、未改变三个夹具的构造语义、未引入凭证或运行产物。
