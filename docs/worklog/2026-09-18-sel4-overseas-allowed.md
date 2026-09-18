# §SEL-4 工作日志 · 海外源放行（可进关键发现与建议，但必须标明是海外的声音）

> worktree `../Owli-sel4`，分支 `sel4-overseas-allowed`，base **c5f7b7c**。
> 解释器 `../Owli/.venv/bin/python`。零引擎、零采集、不重出稿。
> 提货单 `docs/handoff/sel-4-overseas-sources-allowed.md`（需求仓）。

## 〇、退场登记（`conclusions-need-scope-labels`）

**§RPT-4 货 3 的评审结论「与题面对不上的证据不进摘要关键发现」——在「海外平台」这一支上退场。**

- 退场依据：用户 2026-09-18 13:1x 拍乙（decision-log 同日条目），海外平台
  （reddit / x / hacker_news / product_hunt）的证据**可以进关键发现与建议**，
  条件是**每次用到必须让读者看得出这是海外社区的声音**。
- ⛔ **仍然成立的那一半**：「对照实体」（DeepSeek / Kimi / 文心一言 / 通义千问）的证据
  **照旧不进关键发现与建议**。用户拍的是海外，不是对照实体。
- 前提变了的地方：§RPT-4（09-14 评审）当时把「切题」理解成「对象对 + 地域对」两件事
  揉成一个「旁证」概念。09-18 之后这两件事**判定不同、待遇也不同**，必须拆开。

## 一、世界模型核查（接判据前先核，坑 25）

提货单 §一 列了三个落点 + 规则文档三处。逐条核对结果：

| # | 提货单说的 | 实测 | 结论 |
|---|---|---|---|
| 1 | `tables.py:708 offtopic_reason` 打标，`run.py:1381` 与 `tables.py:998` 吃它 | `def` 在 708，常量 `OVERSEAS_PLATFORMS` 703 / `DOMESTIC_MARKERS` 705；`run.py:1384` 渲染 `旁证·{offtopic}`；`tables.py:998` 写 `sources[].offtopic` | ✅ 符 |
| 2 | `tables.py:774 _clues` 筛法第 3 条排除旁证 | `def` 在 774，文档串第 3 条在 786，实际 `skip` 在 805；`offtopic_ids` 由 `build_tables` 947–955 算好传入 | ✅ 符 |
| 3 | `check_polished.py:849 offtopic_findings`，`OFFTOPIC_SHARE=0.8` | 832 常量、849 函数、992–999 组 `offtopic` 集合（读 `tables.json` 的 `sources[].offtopic` 真值） | ✅ 符 |
| 4 | 规则文档三处 | `consulting/SKILL.md:79-81`（关键发现）、`:129-131`（对照与旁证段）、`:150`（建议只从关键发现推）、`sentiment-brief/SKILL.md:55-56`、`_shared/writing-rules.md §5.7`（233 起） | ✅ 符 |

### ⚠️ 与提货单不符 / 提货单没穷尽的三处（**均不改变本包范围**，记录在案）

1. **第四处落点（镜像，非独立闸）**：`scripts/acceptance/rpt8/offline_recheck.py:123-136`
   **自己抄了一份** `offtopic_ids` 的构造（`build_tables` 947–955 的复制品），再喂给
   生产的 `_clues`。它 import 生产函数，所以判法不会漂；但**「传谁进去」这个决定它自己做**，
   本包改了 `build_tables` 侧而不改它，两边读数会分叉。
   - 判定：它是**落点 2 的镜像**，且提货单判据 4 明令我跑这把尺子取线索条数 ⇒ 属「尺子修正 / 脚本改动」，
     按奏折规则自拍，不单独发调度（关账奏折里附带说明）。
2. **货 3 说的「三份 SKILL.md」实为两份 + 共用规则**：`competitor-matrix/SKILL.md` 里
   `旁证 / 对照 / §5.7` **一处都没有**，且 `writing-rules.md §5.7` 末行明写
   「竞品对比稿不适用这一条」。⇒ 货 3 只改 consulting + sentiment-brief + `_shared/writing-rules.md`，
   competitor-matrix **有意不动**（改了反而凭空给它加一条它本来就豁免的规矩）。
3. **提货单 §一.5「引用池 6 条海外」是漏数**：实测 `tables.json` 里 `offtopic == "海外平台"`
   的源共 **11 条**——reddit 5（S02/S04/S05/S07/S11）、x 4（S20/S23/S28/S34）、
   hacker_news 2（S38/S50）。decision-log 同日条目也只记了 X 四 + HN 二，漏了 reddit 五条。
   ⇒ 不改本包做法，但**判据 2 的造红底料因此更宽**。

## 二、货 5 先查后办：查出来是「有」⇒ 已报调度，⛔ 不自己改口径

提货单货 5 要我先查「摘要那句『已编码评论 90 条：正 27 / 负 17 / 中 38 / 混合 8』里
有没有海外平台的行」。**读数：有，19 条（21%），全是 reddit。**

- 取数（只读，`mode=ro`；读前读后 db/-wal/-shm 三个 mtime 逐字节相同，见下）：
  库 `../Owli-src5/var/src5-backfill.db`，`research_id = r-3b3482ca7f8b`。
- 820 条证据里 **465 条已编码**；按 `coding.entity` 分：`主体` **90** / `对照` 245 / `未点名` 130。
  ⇒ 摘要那句的 90 条 **= `entity == '主体'` 的那 90 条**，口径对上了。
- 这 90 条**按平台**：xhs 37 / **reddit 19** / weibo 18 / douyin 16。
- 态度分布对照：

  | 口径 | n | 正 | 负 | 中 | 混合 |
  |---|---|---|---|---|---|
  | 现状（进稿的那句） | 90 | 27 | 17 | 38 | 8 |
  | 剔除海外后 | 71 | 22 | 12 | 31 | 6 |
  | 被混进来的海外 | 19 | 5 | 5 | 7 | 2 |

- ⇒ 题面问的是「**国内**大家对豆包的看法」，这句总体倾向里 **21% 的样本来自 Reddit**。
  按提货单「若有：报调度，⛔ 别自己改口径」——**已发奏折，本包不动这条口径**。
- 同理 `sentiment-brief` 那句「总体倾向」的口径说明，**一并挂在同一个奏折里等裁决**，本包不改。

自证（读库前后）：

```
src5-backfill.db      1789673764  （读前 = 读后）
src5-backfill.db-wal  1789685352  （读前 = 读后）
src5-backfill.db-shm  1789689643  （读前 = 读后）
```

## 三、货 1 · 把「旁证」拆成两支（代码）

**拆法**：⛔ 不加 `kind` 字段、⛔ 不返回二元组——**直接砍掉 `offtopic_reason`**，换成两个各问一件事的谓词。
理由：只要还留着一个「返回两支之一」的函数，下游就永远只问得出「它旁证吗」，
问不出「它海外吗」；留着它等于把提货单那句「⛔ 不许留一个仍把两支当同一件事的调用点」
交给下一个人去守。砍掉之后**语言本身不让你再把两支揉回去**。

```
is_contrast_source(*, contrast)         # 讲的是别家产品吗 → ⛔ 仍不进关键发现与建议
is_overseas_source(row, *, question)    # 题面只问国内而它来自海外平台吗 → ✅ 放行，但必须标明
```

⚠️ **判定逻辑一个字没变**（还是「题面带 `DOMESTIC_MARKERS`」×「平台在 `OVERSEAS_PLATFORMS`」、
`contrast` 为真即对照）。变的只是**待遇**——这一点写进了两个函数的 docstring，
省得下一个人以为口径翻案顺手改了判法。

### 判据 3 · 调用点正列举（文件:行 + 属哪一支）

| # | 调用点 | 问的是哪一支 | 改法 |
|---|---|---|---|
| 1 | `app/report/polish/tables.py:1022` `sources[].offtopic` | **对照实体** | `offtopic` 语义**收窄**成只剩对照实体（值仍是字符串 `"对照实体"` 或 `None`） |
| 2 | `app/report/polish/tables.py:1024` `sources[].overseas` | **海外** | **新增布尔位**。⛔ 没沿用 `offtopic` 那个字符串——两支揉在一个字段里，下游问不出第二个问题 |
| 3 | `app/report/polish/run.py:1388` 写手池「旁证·X」栏 | **对照实体** | 一字未动，只是它现在只可能渲染出「旁证·对照实体」 |
| 4 | `app/report/polish/run.py:1389` 写手池「海外社区」栏 | **海外** | **新增一栏**，栏名 `OVERSEAS_COLUMN = "海外社区"`，⛔ 不含「旁证」二字 |
| 5 | `app/report/polish/tables.py:968-973` → `_clues` 的入参 | **对照实体** | 改成只用 `is_contrast_source`（货 2 的实际落点） |
| 6 | `app/report/polish/tables.py:787/824` `_clues` 形参 | **对照实体** | `offtopic_ids` → `contrast_ids`（改名不是洁癖：名字还叫 offtopic，下一个人就会往里塞海外） |
| 7 | `scripts/acceptance/rpt8/offline_recheck.py:127-138`（**镜像**，提货单没列） | **对照实体** | 同步（详见 §一 补正 1；调度已认，归尺子修正自拍） |
| 8 | `scripts/acceptance/rpt1/check_polished.py:992-999` 软检 ⒞ | **对照实体** | 归货 4 一起改（见 §五） |
| 9 | `tests/test_rpt4_client_lens.py:269`、`tests/test_rpt8_confidence_tiering.py:271` | 两支各锁一条 | 锁的正是被替换的旧语义，跟着改（`existing-tests-lock-old-semantics`） |

**残留核查**：`grep -rn "offtopic_reason\|offtopic_ids"` 全仓（除 worklog 与被替换语义的注释）**零命中**。

### 「海外社区」这个栏名（调度自补的建议，我采纳）

采纳。理由不是措辞好看：写手**只读得到池里那一行字**，规则说「海外可以进关键发现」，
而池里那一栏写着「旁证·海外平台」——两句话直接打架，写手会按栏名办（栏名离他更近）。
⇒ 栏名必须先跟口径对齐。用例里钉了一条 `assert "旁证" not in OVERSEAS_COLUMN` 防回退。

## 四、货 2 · 线索块放海外进来

`_clues` 筛法第 3 条：「不是旁证（对照实体 + 海外平台）」⇒ **「不是对照实体」**。

**为什么不影响另外三条**（提货单要求说清楚）：另外三条问的分别是
①「这条信号硬不硬」（`verdict == "SINGLE"`）、②「是不是当事人自己说的」（`firsthand`）、
④「是不是各章留的登记备注」（`_CLUE_NOISE`）——**三条都不读 `platform`、也不读题面**，
判域与「这条证据来自哪个地域」**没有任何交集**。本次改动只换了传进来的那个 id 集合的构造，
`_clues` 函数体里除了形参改名（`offtopic_ids` → `contrast_ids`）**一行逻辑都没动**。
`_CLUE_NOISE`、`firsthand`、`SINGLE` 三条按提货单要求**一字未动**（diff 可核）。
