# M4-b 工作板历史回放实施计划

> **For Claude：** 必须使用 `executing-plans` 按任务执行，并对功能改动遵循 TDD。

**目标：** 服务重启后从 Store 重建历史研究只读快照，并在工作板展示报告、章账本与缺失清单。

**架构：** 运行态继续以 RuntimeCoordinator 为事实源；历史态只在 API 层调用 Store 固定读取接口构造 DTO。前端按 `snapshot_source` 选择实时工作板或历史只读视图，历史 SSE 单次下发 Store 快照后结束。

**技术栈：** Python 3.13、FastAPI、SQLite、pytest、React 19、TypeScript、Ant Design、Vite。

---

### 任务 1：历史只读快照 API

**文件：**
- 修改：`app/api/main.py`
- 新建：`tests/test_m4b_history_replay.py`

**步骤：**
1. 写失败测试：只向 Store 写 report、plan_snapshot 与章账本，不写 `app.state.researches`，断言详情接口 200、`snapshot_source=store`、进度按 goal 汇总、`actions=[]`、报告字段齐全。
2. 运行 `../Owli/.venv/bin/python -m pytest tests/test_m4b_history_replay.py -q`，确认因历史详情 404 而 RED。
3. 在 API 层实现纯只读快照构造函数；不改 Store、RuntimeCoordinator 或 adapters。
4. 重跑同一测试，确认 GREEN；再断言内存命中路径行为不变。

### 任务 2：历史 SSE 单次快照

**文件：**
- 修改：`app/api/main.py`
- 修改：`tests/test_m4b_history_replay.py`

**步骤：**
1. 写失败测试：历史研究 SSE 的生成器第一条业务事件为 `research_snapshot`，随后 StopAsyncIteration；并断言事件来自 Store 快照。
2. 运行目标测试确认现状不会正常结束而 RED。
3. 在 `/events` 入口识别 Store 历史研究，使用同一快照构造函数直接返回单事件流。
4. 重跑历史 SSE 与现有 `tests/test_events.py`，确认 GREEN。

### 任务 3：历史只读工作板

**文件：**
- 修改：`web/src/types.ts`
- 修改：`web/src/useResearchStream.ts`
- 修改：`web/src/WorkboardPage.tsx`
- 视需要修改：`web/src/styles.css`
- 修改：`tests/test_web_contract.py`

**步骤：**
1. 写失败契约测试：历史分支必须包含报告、章账本、缺失清单，并确保历史组件不接收或渲染操作按钮。
2. 运行 `../Owli/.venv/bin/python -m pytest tests/test_web_contract.py -q`，确认 RED。
3. 扩展快照类型，历史态只拉一次详情并关闭 EventSource；实现只读历史组件并读取报告正文。
4. 把 `web/dist` 软链替换为本 worktree 的实体目录，再运行 `npm run build`。
5. 重跑 Web 契约测试，确认 GREEN。

### 任务 4：真实数据与回归验收

**文件：**
- 产物：`runs/m4b-frontend/`（不入库）

**步骤：**
1. 用 `sqlite3 ../Owli-m4a/var/owli.db ".backup var/owli.db"` 导出一致快照；复制任务包指定的两条历史 runs。
2. 在 8722 启动无人值守服务，所有 curl 使用 `--noproxy '*'`。
3. 验证 completed 与 failed 历史 GET、SSE 正常结束、报告/账本/缺失清单接口数据。
4. 每个 POST 使用新的 `X-Request-ID`，验证创建→计划→approve。
5. 运行 `tests/test_api.py`、全量 pytest、Web 构建与 DOM 断言或截图。
6. 用明确路径 `git add`，确认无凭证、`.docs-ref`、`runs/`、`var/`、`web/dist` 构建产物进入提交，再创建中文 commit。
