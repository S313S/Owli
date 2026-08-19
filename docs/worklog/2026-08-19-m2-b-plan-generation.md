# M2-b 计划生成摘要

- 采用 single 直出：规划任务只返回 goal 骨架；所有 ID、路由、capability、prompt、重试策略、状态与 origin 均由系统补齐。
- `decision_balance` 采用系统侧 `make_questions` 生成，不增加第二次引擎调用。理由是问题数量、选项类型、空答案阻塞批准及 `affects` 引用都属于可机械保证的契约；交给引擎会增加引用悬空与自由文本漂移风险。
- baseline 在构造 `awaiting_review` Plan 时由模型层深拷贝并冻结；问题答案不属于 baseline，后续只允许修改工作树与决策答案。
- lint 区分“保存审核稿”和“批准执行”：`awaiting_review` 允许 QUESTION 答案为空；进入批准态后规则 12 仍阻断空答案。
