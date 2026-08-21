# 传输韧性部署配置

Owli 的传输断路、规划分段重试、限流退避与恢复探测统一由
`ResilienceConfig` 读取。产品默认值不区分机器、代理或部署环境。

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `OWLI_TRANSPORT_FAILURE_THRESHOLD` | `3` | 单 research 内同一引擎连续传输故障达到该次数后升级 `ENGINE_DOWN` |
| `OWLI_PLAN_SEGMENT_RETRIES` | `3` | 单个规划段最多尝试次数 |
| `OWLI_BACKOFF_INITIAL_SECONDS` | `60` | 限流指数退避起点 |
| `OWLI_BACKOFF_MAX_SECONDS` | `900` | 限流退避封顶 |
| `OWLI_ENGINE_PROBE_INTERVAL_SECONDS` | `300` | FAILOVER 后原引擎真实恢复探测间隔 |

所有值必须为正整数，且退避起点不得大于退避上限。非法配置会在加载时拒绝，
不会静默回退。

## 调整场景

- 已确认部署链路存在较长代理抖动时，可提高连续故障阈值或规划段重试次数。
- 上游明确给出更长的限流恢复窗口时，可提高退避起点或上限。
- 故障引擎恢复较慢、探测本身成本较高时，可提高探活间隔；需要更快恢复首选级时可降低。

为特定环境调大或调小任何韧性数值，只允许发生在这里列出的部署环境变量中；
不得在 Python 代码、引擎分支、主机名判断或代理特判中写入单机魔法数。

规划期始终使用 Claude，不因配置变化让路到 Codex；限流指纹只触发 BACKOFF，
不计入连续传输故障，也不升级 `ENGINE_DOWN`。
