# M3-f 传输层韧性设计

## 目标

在不改变 M3-a 限流与单次传输故障分类的前提下，补齐三项通用韧性：执行期持续传输故障断路让路、规划期分段生成与断点续写、部署级数值配置。

## 边界

- 断路器只存在于适配层，并按单个 research 隔离状态。
- 规划任务永不切换到 Codex。
- 限流事件不进入传输故障计数，继续执行既有 BACKOFF/R8 流程。
- Scheduler 只投影 `ENGINE_DOWN`、`PROBE_OK`、`RESET` 事件，不保存引擎倾向或路由覆盖。
- 规划分段是 research 级产物，落在 `runs/<research_id>/plan-segments/`。

## 适配层断路器

M3-a 的消息分类增加结构化原因字段。传输指纹仍返回 BACKOFF；限流仍返回原有 BACKOFF/WARN/FAILOVER。`RoutedAdapter` 在一次 `run` 结束后按聚合结果更新连续故障计数，避免同一失败流中的普通消息错误复位计数。

执行期同一引擎连续传输故障达到阈值后，适配层先真实探测候选引擎。候选健康才发布 `ENGINE_DOWN` 与 `FAILOVER`，并只在 `RoutedAdapter` 内设置 research 级临时路由覆盖。让路后周期性真实探测原引擎；探测成功发布 `PROBE_OK`、`RESET` 并清除覆盖。规划任务只保留 BACKOFF 和段级重试，不触发断路让路。

探测调用必须请求真实模型并校验约定标记，不使用 CLI 退出码判定健康。探测任务不参与断路计数，避免自激循环。

## 规划分段与续写

计划生成拆成以下短调用：

1. `skeleton.json`：只含 goal 清单与依赖。
2. `goal-N.json`：逐 goal 生成 objective、deliverable、acceptance 与 agents 骨架。
3. `assembled.json`：确定性代码拼装全部段。
4. 系统补齐 ID、路由、capability、prompt、retry、origin 与状态字段。
5. 完整计划通过 `plan_lint` 和既有双腿校验后原子保存。

每段正式文件与 `<name>.partial` 明确区分。每次重试开始前只删除该段 `.partial`，不删除已验证正式段。流中收到的文本增量即时追加到 `.partial`。传输中断后，新请求携带已收前缀并只返回剩余后缀；确定性拼接器按最长“前缀后缀重叠”去重，可处理重复 token 和 JSON token 中间断流。拼接整体解析和校验成功后才写正式段。

骨架错误只重跑骨架；单 goal 结构错误只重跑对应 goal。完整 lint 发现跨段问题时按结构化定位信息重跑涉及段，不退化为整份计划长调用。

## 计划质量闸

新增整计划 lint 规则只读取结构化字段：

- 跨 goal 的 `deliverable.path` 与 agent `output.path` 不得冲突；同 goal 最终 agent 输出等于本 goal deliverable 是唯一允许的同路径关系。
- 报告型 agent 的 validator 集合必须同时包含 `citation_marks_resolvable` 与 `no_orphan_citation`，不得只覆盖单向角标。

规则不解析自由文本措辞，不新增正则白名单。

## 配置

`ResilienceConfig` 集中读取并校验以下部署变量：

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `OWLI_TRANSPORT_FAILURE_THRESHOLD` | 3 | 执行期连续传输故障升级阈值 |
| `OWLI_PLAN_SEGMENT_RETRIES` | 3 | 每个规划段的尝试次数 |
| `OWLI_BACKOFF_INITIAL_SECONDS` | 60 | 指数退避起点 |
| `OWLI_BACKOFF_MAX_SECONDS` | 900 | 指数退避上限 |
| `OWLI_ENGINE_PROBE_INTERVAL_SECONDS` | 300 | 原引擎恢复探测间隔 |

默认值与环境无关。为特定代理、节点或部署调大重试与退避、调短探活，只允许修改部署配置。

## 验证

- 单元测试覆盖传输连续计数、成功复位、限流隔离、阈值升级、候选探测、周期探活与 RESET。
- 路由集成测试证明执行期让路、规划期不让路、Scheduler 无路由倾向状态。
- 规划测试覆盖分段即时落盘、局部重试、partial 清理、重叠去重、JSON token 中断与整计划 lint。
- 配置测试覆盖默认值、环境覆盖与非法值拒绝。
- 最终运行专项 pytest、全量 pytest、t-m1、t-m2-orchestrator、t-m3-multi-source、无人值守全链路和审计 grep。
