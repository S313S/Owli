# 报告节化第二轮阻断修复 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修通报告节标题契约与 Claude CLI 结构化输出，同时固定 `conclusion_invalid` 只属于系统账本判因的语义。

**Architecture:** 在节任务 body 中显式声明两个必需且只覆盖本节的标题；在 Claude 执行章构造 SDK options 时，仅从传给 CLI 的 schema 副本剥离顶层元数据，磁盘 schema 保持不变。agent 自报 reason 与系统账本 reason 保持两个独立闭集，并用测试记录边界。

**Tech Stack:** Python 3.12、pytest、claude-agent-sdk 0.1.81、JSON Schema

---

### Task 1: 补齐报告节标题契约

**Files:**
- Modify: `tests/test_m3h_sectioning.py`
- Modify: `app/orchestrator/sectioning.py:258-269`

**Step 1: Write the failing test**

在已有节化调用测试中断言每个实际执行的节任务 body 都要求逐字使用“结论”和“信息源”标题，并说明两节只覆盖当前报告节。

**Step 2: Run test to verify it fails**

Run: `../Owli/.venv/bin/python -m pytest tests/test_m3h_sectioning.py::test_standard_报告章按节短调用_失败详情进SSE与账本 -q`

Expected: FAIL，body 不含新增标题契约。

**Step 3: Write minimal implementation**

只在节任务 body 拼接处增加两句明确要求，不改拼装器和 validators。

**Step 4: Run test to verify it passes**

Run: 同 Step 2。

Expected: PASS。

### Task 2: 清洗传给 Claude CLI 的 schema 元数据

**Files:**
- Modify: `tests/test_m1_wiring.py`
- Modify: `app/adapters/claude.py:91-98,241-248`
- Preserve: `app/prompts/common/owli-result.schema.json`

**Step 1: Write the failing test**

断言 SDK `output_format.schema` 不含顶层 `$schema`、`$id`、`$defs`，同时保留 `reason` union 与 `summary.maxLength=200`；另断言磁盘 schema 原样含 `$schema`。

**Step 2: Run test to verify it fails**

Run: `../Owli/.venv/bin/python -m pytest tests/test_m1_wiring.py::test_claude_执行章优先使用SDK结构化结论 -q`

Expected: FAIL，当前 options 仍含 `$schema`。

**Step 3: Write minimal implementation**

新增一个只构造 CLI schema 副本的小函数，剥离三个顶层 meta 键；`build_claude_options` 使用该副本。

真实夹具若证明 SDK 通过内部 `StructuredOutput` 工具回传 schema 结果，则只在 permission callback
中放行该协议工具；它不计入业务 capability，也不得获得文件、网络或 shell 权限。

**Step 4: Run test to verify it passes**

Run: 同 Step 2，并补跑相关 adapter 测试。

Expected: PASS。

### Task 3: 固定两类 reason 的语义边界

**Files:**
- Modify: `tests/test_validation.py`
- Preserve: `app/prompts/common/owli-result.schema.json`

**Step 1: Record the contract**

新增回归断言：`conclusion_invalid` 不在 agent 自报 schema enum 内，agent 自报该值会被 parser 拒绝；系统账本与 DAO 的既有测试继续证明它是合法终态原因。

**Step 2: Run the contract tests**

Run: `../Owli/.venv/bin/python -m pytest tests/test_validation.py tests/test_m3h_ledger.py -q`

Expected: PASS，证明两个闭集有意不同而非遗漏。

### Task 4: 验收

**Files:**
- Verify only: `scripts/fixtures/report_sectioning_fixture.py`

**Step 1: Run focused regression**

Run: `../Owli/.venv/bin/python -m pytest tests/test_m1_wiring.py tests/test_m3h_sectioning.py tests/test_validation.py tests/test_m3h_ledger.py -q`

**Step 2: Run the report fixture**

在不改夹具语义、也不批量删除目录的前提下，把现有 `/tmp/owli-report-fixture` 活跃输出改名备份，再运行用户给定命令；检查三节账本、报告正文、structured output 与错误原因。

**Step 3: Verify rejected artifact and SDK error event**

分别运行已有构造失败测试与 SDK `ResultMessage(is_error=True)` 测试，核对 `.rejected.md`、非 `empty_result` 和 `engine_error` 原文。

**Step 4: Run full regression**

Run: `../Owli/.venv/bin/python -m pytest -q`

Expected: 尾行 0 failed，数量不低于工作日志基线 500。
