# AGENTS.md — Owli 代码仓 Agent 工作规范

> 本文件是所有 AI Agent（Codex CLI / Claude Code 等）进入**本仓库**的第一入口。
> 开始任何工作前先读完本文件，再读 `.docs-ref/architecture.md`（总装图）。

## 1. 这个仓库是什么

- **项目名**：Owli（Owl + Sight）—— 面向运营与产品经理的 AI 市场调研工作台。
- **本仓库 = 代码仓**（公开，MIT）。产品设计与需求文档在**另一个私有仓库**，不在这里。
- **阶段**：需求访谈 ✅ · 方案评审 ✅ · 技术验证 T1–T9 ✅（双引擎架构成立）· 设计文档定稿 ✅ ·
  **当前：M0 骨架切片（walking skeleton）开发中**。
- 产品概览见 `README.md`。里程碑与验收判据见 `.docs-ref/` 里的里程碑文档。

## 2. 设计文档挂载点：`.docs-ref/`

设计文档**不在本仓库**（它们属于私有仓库，不进公开历史）。开发时通过一个
**被 gitignore 的软链**挂载到本机：

```bash
ln -s /绝对路径/到/私有仓/docs/design .docs-ref
```

挂载后按下表找权威文件。**任何冲突以下表指定的文件为准，不以本文件为准**：

| 主题 | 权威文件 |
|---|---|
| 六层职责边界、接缝、模块清单、M0–M7 | `.docs-ref/architecture.md` |
| 前端形态、接口清单、卡片规格 | `.docs-ref/frontend-requirements.md` |
| 前端视觉令牌、信息层级、可点原型 | `.docs-ref/frontend-visual-spec.md` + `frontend-prototype/` |
| 计划书字段契约、capability、恢复初始化 | `.docs-ref/agents-spec.md` |
| 五维可靠度判据、血缘簇算法、归一化 | `.docs-ref/source-reliability.md` |
| 表结构、冻结列、扩展键登记 | `.docs-ref/report-store-schema.md` |
| Excel 6 sheet、角标、图表选型 | `.docs-ref/report-attachment-spec.md` + `chart-selection-guide.md` |

**没挂载 `.docs-ref/` 就不要开始写代码** —— 契约字段名靠猜必然对不上，返工成本远高于问一句。

## 3. 语言规范

- **所有输出一律中文**：终端回复、文档、代码注释、commit message。
- 专有名词（Owli、Agent、API、SSE、token 等）保留英文原文。

## 4. 开发期硬约束（八条，每条都是实测踩出来的，不是建议）

违反其中任何一条，代码评审直接打回。

1. **退出码不可信。** 三终端实测：Claude 中断返回 0，Codex 语义失败和越权也返回 0。
   任务成败判定 = **产物按协议落盘并通过校验** + **结构化结论可解析**，两条腿都要站住。
   **禁止 `if exit_code == 0: success` 及其任何变体。** 全仓 grep 不得命中。
2. **验收标准必须可判定。** 不写「做得好」「合理」；要写「6 个 sheet 齐全且命名顺序正确」
   「角标 ID 集合无悬空」这类能用代码断言的条件。理由同第 1 条 —— 没有可断言的验收，
   就没有办法知道一步到底做成没做成。
3. **不在编排层写引擎分支。** `app/orchestrator/` 里出现 `if engine ==` 就是架构走形。
   任务 spec 里只有能力声明（工具 / 信息源 / 文件 / 网络 / 命令行五维）与一个可选引擎偏好；
   选哪个引擎由适配层按路由规则 + 当前额度状态决定。前端零感知。
4. **不给 agent 裸 SQL 通道。** 存储层只暴露固定写入接口，参数 = 冻结列 + 一个 `extra` dict。
   启动自检比对实际表结构与预期 schema，不一致即拒绝启动。
5. **`CODEX_HOME` 必须隔离，且放在仓库之外。** 它含 `auth.json` 凭证，而本仓库公开
   —— **凭证不进工作树**。两个隔离 home 刻意分开，不要合并（合并会让开发期的会话历史
   污染产品运行期的可复现性）：
   - **产品运行期**（`app/adapters/codex.py` 拉子进程）：默认 `~/.owli/codex_home/`，
     可用环境变量 `OWLI_CODEX_HOME` 覆盖。**不要写死路径。**
   - **开发期**（你在终端跑 Codex 写 Owli 的代码）：默认 `~/.owli/codex_home_dev/`，
     一律走 `bash scripts/owli-codex.sh`（见第 6 节），不要直接敲 `codex`。
6. **凭证一律不落仓库。** API key / token / cookie 只走 `.env`（已 gitignore）或系统钥匙串。
   不写进代码、不写进注释、不写进文档、不写进测试固件、不打印到日志。
7. **可写路径白名单收敛到 `runs/<research_id>/goals/goal-N/**`。** 越界即产物校验失败。
   注意 Claude 侧 `cwd` **不是沙箱**，路径必须显式自校，不能依赖引擎兜底。
8. **新建 worktree 清单（§RD-1 起，09-03 事故落档）。** 前端构建产物 `web/dist` 不进版本库，
   新 worktree 建完必须先 `ln -s ../../Owli/web/dist web/dist`，否则页面路由全 404「页面不存在」
   而 `/api/...` 照常 200——跑了 193 min 的报告没人能打开。起服务脚本起完必须
   `curl /researches/<任意id>` 断言 **200 + text/html**，红即停服务（需求仓
   `scripts/acceptance/rd1/rd1_serve.sh`）；把报告链接交人前自己先 curl 页面路由。
   关账判据从此固定含「可看性」（`scripts/acceptance/rd1/check_readable.py` 全绿）。

## 5. 目录结构

```
Owli/
├── AGENTS.md               本文件
├── LICENSE                 MIT
├── README.md               公开项目首页（改动前先看它是对外展示页）
├── app/                    Python 后端（单进程 FastAPI，端口 8721）
│   ├── api/                L2  路由、SSE、幂等、事件缓冲
│   ├── orchestrator/       L3  计划执行、依赖调度、两套状态机、重试与总闸
│   ├── adapters/           L4  claude.py / codex.py / events.py / ratelimit.py
│   │                           validation.py / selfcheck.py / capability.py / logging.py
│   ├── sources/            L5  hn.py / producthunt.py / x.py / websearch.py / ...
│   ├── store/              L6  schema.sql / dao.py（固定写入接口）/ recall.py（FTS5）
│   ├── report/                 markdown 成稿 + excel.py（openpyxl 6 sheet）
│   └── prompts/                common/v1（用户不可编辑前缀）+ 各 agent 模板
├── web/                    L1  React + Vite + Ant Design（SPA）
├── scripts/                开发工具脚本
├── runs/                   ⛔ 不入库 · 产物目录 <research_id>/goals/goal-N/
├── var/                    ⛔ 不入库 · owli.db · logs/
└── .docs-ref               ⛔ 不入库 · 指向私有设计文档的软链
```

`runs/` 与 `var/` 不进版本库，但 **`runs/` 的路径结构是契约**（见硬约束第 7 条）。

## 6. 怎么启动 Codex

**一律用启动器，不要直接敲 `codex`**：

```bash
bash scripts/owli-codex.sh                      # 交互模式
bash scripts/owli-codex.sh exec --json "任务"    # 非交互模式
```

它负责把 `CODEX_HOME` 指到仓库之外的隔离目录（硬约束第 5 条）。直接敲 `codex`
会读你的全局配置与记忆 —— 隐私、可复现性、成本三重问题，实测 token 消耗可差一个数量级。

## 7. 落盘优先输出规范（防断连丢失）

本机代理对**单次流式响应超过约 5–6 分钟**会掐断，这是已确认的根因，不是偶发网络问题。

1. **长内容分块落盘，每块 ≤100 行**：先 Write 第一块，再 Edit 追加。一次性输出长文必然重试死循环。
2. **小步提交**：长任务拆成多步，每完成一步立即落盘，不要攒到最后。
3. **先写文件再回终端**：超过一屏的结论写进文件，终端只输出短摘要 + 文件路径。

## 8. 提交规范

- commit message 用中文，格式 `<类型>: <做了什么>`，类型取 `feat` / `fix` / `docs` / `chore` / `refactor` / `test`。
- **本仓库是公开仓库。** 提交前确认：没有凭证、没有内部设计文档、没有 `runs/` 或 `var/` 下的内容。
- 不要 `git add -A`。用明确的路径。

