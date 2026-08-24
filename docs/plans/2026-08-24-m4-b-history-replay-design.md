# M4-b 工作板历史回放设计

## 目标

服务重启后，工作板仍能从 SQLite 与 `runs/<research_id>/` 读取已完成或失败研究，展示报告、章账本和缺失清单；历史页只读，不提供暂停、停止或恢复操作。

## 方案裁决

采用 API 层只读重建：内存注册表命中时维持现有运行态路径；未命中时，使用 `Store.get_report()` 和 `Store.list_chapters()` 构造与工作板兼容的快照，并标记 `snapshot_source="store"`。

排除两种方案：一是把历史研究写回运行态注册表或创建调度器，这违反任务包禁区；二是另建历史服务和重复接口，这会重复已经兼容 Store 的 `/chapters` 与 `/plan`。

## 数据流

1. `GET /api/researches/{id}` 先查内存。命中时仍调用 `runtime.sync_state_with_scheduler()`。
2. 未命中时查 `reports`。不存在则维持 404；存在则读取章账本与计划快照。
3. 以计划 goal 为骨架、账本为事实计算每个 goal 状态；进度的完成数按账本中全部章进入终态的 goal 数计算。
4. 快照返回 `report_path`、摘要、空 `actions/cards/events`，并附原始 `chapters`，供历史页直接渲染。
5. 历史 SSE 返回一条 `research_snapshot` 后结束，不访问内存事件缓冲。
6. 前端检测 Store 快照后渲染历史只读页：报告正文、章账本、缺失清单；不实例化操作按钮区域。

## 状态与异常

- 报告状态直接采用 `reports.status`；中文标签使用封闭映射，未知值回显原值。
- goal 状态以章账本为准：有 `missing/deferred` 为失败或缺失；全部 `done` 为完成；否则为待处理。
- `report_path` 为空或文件缺失时明确显示报告不可用，但账本仍可查看。
- 缺失清单只来自 `missing/deferred` 账本行，展示章、原因与错误摘要，不臆造内容。
- 历史快照永远 `actions=[]`；不写 Store、不写内存、不触发编排。

## 验证

- 后端先写历史 GET 与 SSE 失败测试，确认现状分别为 404/不结束，再写最小实现。
- Web 契约先断言 Store 分支含报告、账本、缺失清单且不渲染操作按钮。
- 跑 `tests/test_api.py` 相关套件、完整 `pytest`、Web 构建。
- 按任务包从 WAL 数据库导出一致快照，复制两条真实历史研究，在 8722 端口验证 completed/failed 历史页与新建研究创建→计划→approve 冒烟。
