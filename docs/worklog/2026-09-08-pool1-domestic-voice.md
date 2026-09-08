
## 第 1 步 · 重放 goal-3/ch-6 sec-2 + sec-3（整章重写）

- 起跑 2026-09-08 13:5x，base `60d6a3d`（乙已提交），工作区干净。
- 沙盒 `var/replay/g3-ch6-full`（新建，不复用上一轮那个）。
- 重放前基线：sec-2 = 19,637 B（原轮产物）、sec-3 = 24,142 B、sec-1 = 124 B 占位符。
  工作稿 `goal-3-report.json` 角标 S01~S33（33 个）；服务库 citation_no S01~S33（33 条）。

## 第 2 步 · 编号对齐脚本（已备好，等第 1 步产物）

`scripts/pool1_align_citations.py`——零引擎，只把库里的 `citation_no` 按新工作稿重设一遍。

**为什么需要它**：`app/replay/section.py` 里 `replace_evidence_citations` /
`report_citations` / `validate` / `assemble` **全部零命中**，沙盒本轮 `report_validation`
事件 **0 条**——重放只跑节、**不回填**。所以跑完必然「工作稿新号、库旧号」，
`polish.citation_preflight` 会当场拦下（实测三种组合：现状 ✅、沙盒配对 ❌、只搬文件 ❌）。

**它不是 rescore**：rescore 重跑评分并**无条件**改写工作稿+重排角标，且只落在
`--database` 指到的那个库——那正是 09-07 工作稿与底料库分家的成因。这里不碰分数、
不碰工作稿。

**三条护栏（D-022 写死）**：解析条数 > 0、与正文角标数一致、号集合相同，否则一个字不写库。
造红验过：JSON 成稿解析出 0 条 → rc=2 拦下（这正是「反手把全库清成 NULL」那个坑）；
条数不符 → rc=3 拦下。拿当前工作稿干跑：33 角标 / 33 条、三条全过、未写库。
