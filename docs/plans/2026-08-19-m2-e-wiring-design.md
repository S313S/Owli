# M2-e 计划驱动串线设计

## 目标

把 M0 固定链路替换为唯一的计划驱动运行路径：需求先生成计划并停在
`awaiting_review`，批准后由 Scheduler 执行 goal/agent DAG，每个 goal 完成后
经过 INTERVENE 卡片，最后基于真实运行结果收口报告。

## 架构

新增 `RuntimeCoordinator` 作为 L2 API 与纯推进 Scheduler 之间的运行期协调层。
它是唯一持有真实时钟、真实定时器、Scheduler 实例注册表、RoutedAdapter、SSE
投影和报告收口逻辑的对象。`scheduler.py` 继续只依赖注入的 `clock` / `timer`，
不读取环境变量、不操作 SQLite、不创建真实时间。

`mini.py` 不再保留固定三步状态机；原有对外名称只做计划驱动协调层的兼容导出，
避免生产路径出现两套状态机。

## 数据流

1. `POST /api/researches` 创建 drafting report 与初始状态，后台调用
   `generate_plan`，完成后发布 QUESTION 卡并停在 `awaiting_review`。
2. QUESTION 回答统一经过 `/api/cards/{id}/respond`，写回
   `decision_balance.answer/answered_at`，卡片状态与计划版本都真实迁移。
3. `/plan/approve` 冻结计划并启动该 research 的 Scheduler。
4. `RuntimeCoordinator.run_task` 把 Agent 转成 EngineTask 与 Validation Ctx，直接调用
   `RoutedAdapter.run`；Scheduler 只消费结构化 `succeeded` 与 route event。
5. Scheduler 事件由协调层投影到内存快照、卡片表与 ResearchEventBuffer；pause、
   resume、stop、respond 都调用同一个 Scheduler 实例。
6. 所有 goal 进入 done/failed/skipped 后生成最终 Markdown；报告如实列出失败 goal，
   decision_balance 以 `[^q-n]` 注释角标落盘，并执行报告校验后更新 reports。

## 运行期变更

批准后编辑计划时，API 把已完成 goal 集合交给 editing。若变更触及已完成 goal 或
其后续执行契约，先逐个明确文件删除其 goal 目录内既有产物，再把
`{goal_id,path,discarded_at}` 写入对应 change_log 的 `artifact_discarded`；现有
`commit_changes` 负责同步写入 `feedback(kind='goal_change')`。批准前仍只写
change_log。

## 自动确认

`OWLI_AUTO_CONFIRM` 仅由 RuntimeCoordinator 读取。值为 `1` 时：

- QUESTION 取第一个 options，result 标记 `auto: true`，仍调用统一卡片应答入口；
- INTERVENE 选择“继续”，result 标记 `auto: true`，仍调用 Scheduler.answer_card；
- 其他卡片不受该开关影响。

默认关闭，不跳过 awaiting_review、approved、running、awaiting_intervention 等状态。

## 失败与恢复

Adapter 的双腿判定是 agent 成败唯一来源。限流信号仍由 Scheduler 的
`TaskContext.on_event` 消费；RuntimeCoordinator 不实现第二套路由逻辑。
Scheduler 最终允许 failed、skipped 与无依赖 done 并存，报告与 research 终态如实
反映失败，不因局部失败丢失可用结果。

## 测试

- `tests/test_m2_wiring.py` 用假规划引擎与假执行引擎覆盖创建、批准、DAG、卡片、
  pause/resume/stop、运行期双写与产物删除、BACKOFF SSE 恢复、C1 报告注释。
- `scripts/t-m2-orchestrator.py` 构造两 goal 依赖计划和一个确定失败的 agent，压缩
  retry 参数，输出状态迁移序列供 M7 复用。
- 全量 pytest、M1 双引擎脚本、M0 自动确认真实链路、四条 grep 与
  `scheduler.py` 时间审计作为最终验收。
