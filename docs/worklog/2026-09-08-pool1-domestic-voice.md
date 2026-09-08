
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

## 第 3 步 · 自动闸：三条全不过，停下，未出正式稿

重放 13:40:53 → 14:2x，约 45 分钟。底料原件零改动（脚本指纹自证）。

| 闸 | 判据 | 实测 | |
|---|---|---|---|
| ① | sec-2 与 sec-3 都 done | **sec-3 done（attempts 3）✅；sec-2 missing/timeout（attempts 5）❌** | 不过 |
| ② | 落地 sec-2.md 里 xhs 真引用 ≥8 | **0**（sec-2.md 是 125 B 占位符） | 不过 |
| ③ | 三个热点进 ≥2 | **0/3**（同上） | 不过 |

**乙这一改是有效的**：上一轮 sec-3 判 `conclusion_invalid`，这一轮 **sec-3 = done、29,117 B、引用 30 条**——
越池问题真被治好了。sec-2 这次死因**换成了 timeout**（不再是 conclusion_invalid），
且缺 `sec-2.part.3.md`（只写出 1/2/4 三片）——**是第 3 片没写完，不是判据打回**。

被判红那份 `sec-2.rejected.md`（23,437 B）里 **xhs 真引用 7 条、热点 2/3**——
内容方向是对的，只差把第 3 片写完。

## 甲′ · 改这份研究快照里的节墙钟（用户 09-08 拍）

**为什么不是「换 standard 档」**：重放的节墙钟不从档位取——`app/replay/section.py:157`
是 `goal.retry_policy.get("chapter_deadline_seconds")`，与 `scale` / `ResearchScaleConfig`
无关。这份计划三个 goal 都烙着 `330`（规划期按 fast 档算完写死进快照的），
改 `plan.scale` 一个字重放也读不到。上一次拍板照跑会一模一样地失败。

**改了什么**：`var/shard1-serve.db` → `reports.plan_snapshot`（`id=r-3e04f808dffd`）
→ **只有 `goals[2]`（goal-3）的 `retry_policy.chapter_deadline_seconds`：330 → 1800**。
goal-1 / goal-2 **未动**（本轮只重跑 goal-3/ch-6/sec-2，爆炸半径压到最小）。

**⚠️ 记账（不恢复，但不许悄悄留着）**：这份研究的快照从此与规划期算出来的值不一致——
`r-3e04f808dffd` 的 goal-3 节墙钟是 1800 而不是 fast 档的 330，用户 2026-09-08 拍板，
理由是「四片整体逼近 300 s 上限，第 3 片连撞三次」。goal-1/goal-2 仍是 330。

**备份与比对**：备份 `var/backups/shard1-serve.pre-deadline-0908-1639.db`（backup API，禁 cp）。
逐行逐列比对：10 张表 / **129,919 个单元格 / 差异 1 处 / 越界 0 处**；
plan_snapshot 内部 2,092 个叶子 / 差异 1 个 = `.goals[2].retry_policy.chapter_deadline_seconds` 330→1800。

**算式**：节预算 = 1800 × 4 片 = **7,200 s**；片上限给 **900 s** → 4 × 900 = **3,600 s ≤ 7,200 s** ✅。
900 s 是第 3 片历史最慢那次（335.6 s）的 **2.7 倍**。
