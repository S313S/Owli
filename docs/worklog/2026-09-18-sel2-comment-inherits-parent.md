# §SEL-2 工作日志 · 评论说的是谁：相关闸与实体归属两处各说各话

> worktree `../Owli-sel2`，分支 `sel2-comment-inherits-parent`，base `c5f7b7c`。
> 解释器 `../Owli/.venv/bin/python`。零引擎、零采集、不重出稿。

## 一、§〇 世界模型逐条核（接判据前）

调度在提货单 §〇 给了五条前提。**五条在实质上全部成立**，不构成范围变更；
但有三处行号漂移、一处键的做法**必须纠正**（照提货单 §四 的字面写会出错）。

### 前提 1 ✅ 成立（行号漂移）

- 提货单写 `relevance.py:56-58`；该文件原本只有 49 行，判定在 **`app/reliability/relevance.py:47-49`**：

  ```python
  return [dict(row) for row in rows
          if _names_the_entity(
              f"{row.get('title') or ''} {row.get('content_excerpt') or ''}", names)]
  ```

- 「评论行 `title` 是『评论 · <父帖标题>』」库里实测：`r-3b3482ca7f8b` 的 563 条
  `kind='comment'`，`title` 前缀 `Counter` **只有 `评论 · ` 这一个值**（563/563）。
- ⇒ 推论成立：**父帖正文里点的名，评论继承不到；父帖标题里点的名，评论已经继承到了。**

### 前提 2 ⚠️ 结论成立，但**键的做法要改**（提货单 §四 的 `url.split("?")[0]` 有两个坑）

结论层面 ✅：563 条评论全部有 `parent_permalink`，**563/563** 匹配得到父帖。

但 §四 那段示例代码的键有两处会静默出错，**货 0 没有照抄**：

1. **索引必须只收 `kind='post'` 的行。** 示例把**全部 820 行**（含评论）塞进 `bypm`。
   实测 **485/563 条评论的 `dekey(permalink) == dekey(parent_permalink)`**——小红书/抖音的
   评论没有自己的公开链接，入库 permalink 是在父帖链接上合成的。有 **27 个键同时挂着
   1 个 post 和 17~20 条 comment**。用全量索引，命中谁取决于 `SELECT` 的返回顺序
   ⇒ 尺子不确定（`verification-ruler-needs-verifying`）。
2. **`url.split("?")[0]` 会把 Hacker News 打成一锅粥。** HN 的 permalink 形如
   `https://news.ycombinator.com/item?id=xxx`，身份就在 query 里。裸切 `?` 之后
   **7 条 goal-1 的 HN 帖全部塌进同一个键 `https://news.ycombinator.com/item`**。

   生产里**早就有一把对的尺子**：`app/reliability/crossref.normalize_origin_url`，
   文档字符串原文「去 scheme/www/追踪查询串/锚点；**保留 HN 等身份参数 id**」。
   采集侧 `app/adapters/source_mcp.py:435 _origin_key` 与认行侧
   `app/store/dao.py::_comment_identity` 用的都是它（后者还优先读采集侧落下的
   `extra.parent_origin_key`）。⇒ 按 `reuse-before-rewrite`，货 0 **复用它**。
   （`crossref.py` / `source_mcp.py` / `dao.py` 都在禁区，这里全是**只读 import / 只读参考**，
   一个字不改——与 `relevance.py` 只读 import `coding._names_the_entity` 同一个办法。）

   **换成 `normalize_origin_url` 之后的读数**：257 条父帖 → **257 个互不相同的键**
   （零碰撞，HN 身份保住），评论仍是 **563/563** 命中；
   另外 563 条评论**全部**带 `extra.parent_origin_key`，按它匹配也是 563/563。

### 前提 3 ✅ 成立

`rows_naming_entities` 全仓唯一生产调用点 **`app/orchestrator/sectioning.py:2490`**
（另两处是 `tests/test_relevance_gate.py` 与只读脚本 `scripts/acceptance/secq1/three_tables.py`）。
⇒ 相关闸跑在撰写期，改了对 `r-3b3482ca7f8b` 这份已出的稿不生效。

### 前提 4 ⚠️ 成立（行号漂移）

`coding_entity_roles` 定义在 **`tables.py:448`**，生产调用点在 **`tables.py:877`**
（`build_tables` 内 `polish_tables(... entity_roles=coding_entity_roles(plan, rows))`），
不是提货单写的 803。另一条回写库的路 `coding.assign_coding_entities` → `app/api/delivery.py:126` 属实。

### 前提 5 ✅ 成立

`tables.py:450` 文档字符串原文「程序算，不经模型」。⇒ 货 2/货 3 零引擎。

### 另一条现场事实（提货单没写，但决定货 2 的写法）

`_own_text(row)`（`tables.py:428-445`）对 `kind='comment'` **有意剔掉 `title`**，只用
`content_excerpt`（没正文时才借编码原声，且原声不能是标题里的字）。注释写明理由：
09-15 实测有 3 条评论的「原声」摘的就是父帖标题，借了它「父帖名就又漏回来了」。

⇒ **两侧的不对称正是本包的病根**：相关闸看得见父帖**标题**（它把 `title` 拼进去了），
实体归属看不见父帖的任何字（`_own_text` 把 `title` 剔了）。
所以货 2 的「随父帖」必须拿**父帖自己的 `_own_text`** 去判，
⛔ 不能拿评论行的 `title` 当父帖正文——那等于把上面那个已经修好的坑重新挖开。

## 二、货 0 · 共用只读 helper：评论 → 父帖

新文件 **`app/reliability/parentage.py`**（不在禁区）。一处实现，两个消费方
（`relevance.rows_naming_entities` / `tables.coding_entity_roles`）都 import 它。

- `origin_key(permalink)`：复用 `crossref.normalize_origin_url`，剥不动就原样返回。
- `parent_key_of(row)`：只对 `kind='comment'` 出键；优先 `extra.parent_origin_key`，缺了才现算。
- `parent_index(rows)`：证据 id → 父帖行。**索引只拿非评论行建**；同键撞多个父帖取入参
  第一个（调用方喂的是 `ORDER BY id` 全量行，确定）；自指不建边；拿不到父帖的**原样缺席**。

放在独立文件而不是塞进 `relevance.py`：`relevance.py` 已经反向 import `tables._entity_aliases`，
再让 `tables.py` import `relevance.py` 就成环；`parentage.py` 谁都不依赖，两边都能安全 import。

## 三、货 1 · 只算不改：继承之后池会变成什么样

### 3.0 尺子先校准（⚠️ 先证明我的尺子量得准，再看新读数）

`scripts/acceptance/sel2/inherit.py` 的层 1~4 与 `../Owli-sel1/scripts/acceptance/sel1/funnel.py`
（commit `eb8019d`）**同构**，两侧都走**同一份** `rows_naming_entities(..., inherit_parent=)`，
⛔ 脚本里没有第二份继承逻辑。旧侧读数与 funnel.py 逐项对上：

| 层 | 我的脚本（旧侧） | funnel.py | |
|---|---|---|---|
| 层 1 相关闸 | 进 464 / 挡 356 | 进 464 / 挡 356 | ✅ |
| 层 2 D 闸 | 315 | 315（挡 149） | ✅ |
| 层 3 编号池 | 99 | 99 | ✅ |
| 层 4 六节可见池并集 | **78** | **78** | ✅ |
| 并集按平台 | douyin 5 / hn 2 / reddit 5 / wechat_mp 5 / weibo 5 / x 4 / xhs 52 | 同 | ✅ |
| 正文实引角标 | 22 条 | §SEL-1 结论 22 | ✅ |

> ⚠️ **78 不是 79**。交接 §三 那条「99 →(每节 30 位 挡 20) 79」里的 79 是库里
> `citation_no IS NOT NULL` 的**实跑最终池**；**复算**的六节可见池并集是 **78**，
> funnel.py 自己也印 78。两者本来就有 §SEL-1 记过的 11 条漂移（跑完又补过评级）。
> 本货下面所有「挤掉谁」都是**复算侧 78 → 82** 的对照，不是库里那 79 条的直接增减；
> 逐条我另外把库角标标出来，能落到真稿的那几条才拿来算代价。

### 3.1 读数 1 · 放进来多少

| | 旧（不继承） | 新（继承父帖） |
|---|---|---|
| 层 1 进 / 挡 | **464 / 356** | **755 / 65** |
| 按 kind | comment 235 / post 229 | comment **526** / post 229 |
| 按平台 | douyin 75, hn 2, reddit 40, wechat_mp 22, weibo 18, x 10, xhs 297 | douyin 75, hn 2, reddit **97**, wechat_mp 22, weibo 18, x 10, xhs **531** |
| 按等级 | A 15 / B 57 / C 243 / D 149 | A 15 / B **79** / C **367** / D **294** |
| 层 2 D 闸后 | 315 | **461** |
| 层 3 编号池（上限 99） | 99 | 99（满额，没变） |
| 层 4 可见池并集 | **78** | **82** |

**新放进来 291 条，全部是评论**（按平台只有 reddit 57 + xhs 234；其中**非 D 146 条**）。

> 291 与交接 §三 说的「328 条是评论、父帖自己过了闸」不同，因为 328 是「父帖过闸的评论」
> 总数，其中有一部分**自己本来就过了闸**（评论 `title` 已经带父帖标题）。
> 291 = 真正被这一改救回来的增量。

层 3 各 goal 配额与实得（`_numbered_evidence_rows`）：

- 旧 quotas/actual：goal-1 51 / goal-2 15 / goal-3 20 / goal-4 **5** / goal-5 8
- 新 quotas/actual：goal-1 **47** / goal-2 **13** / goal-3 20 / goal-4 **10** / goal-5 **9**

### 3.2 读数 2 · 挤掉谁

复算池 **78 → 82**：挤掉 **9**、新进 **13**。
挤掉的按平台 xhs 9（comment 6 / post 3）；新进的按平台 xhs 13（comment 12 / post 1）。

当前正文实引的 22 条角标：S01 S03 S06 S08 S10 S17 S19 S21 S31 S36 S38 S53 S58 S60 S69 S71 S73 S81 S86 S92 S97 S98。

逐条（复算号 / 库角标 / 平台 / 等级 / 所在节 / 是否正文实引 / 标题）：

| 复算号 | 库角标 | 平台 | 等级 | 所在节 | 正文实引 | 标题 |
|---|---|---|---|---|---|---|
| S64 | S57 | xhs | B | ch-4/sec-2 | | 评论 · DeepSeek V4.1 Flash：更强、更快、更普惠 |
| S65 | **S60** | xhs | B | ch-4/sec-2 | **★** | 评论 · 感谢每一位为DeepSeek_V4付出的贡献者🌹❤️ |
| S66 | S62 | xhs | C | ch-4/sec-2 | | 评论 · DeepSeek V4.1 Flash：更强、更快、更普惠 |
| S82 | — | xhs | C | ch-4/sec-3 | | 评论 · 月之暗面Kimi报案 |
| S83 | **S73** | xhs | C | ch-4/sec-3 | **★** | Harvey发布：基于Kimi K3的首个法律行业模型 |
| S84 | — | xhs | C | ch-4/sec-3 | | 消息称月之暗面瞄准年底20亿美元收入 |
| S85 | — | xhs | C | ch-4/sec-3 | | 评论 · Kimi首个官方教程：如何用AI做出专业的PPT？ |
| S86 | — | xhs | C | ch-4/sec-3 | | 评论 · 今晚别看2小时的Netflix，看看这位50亿美元中国AI |
| S87 | — | xhs | B | ch-4/sec-4 | | ChatGPT国内AI大模型六大平替！好用收藏！ |

⇒ **挤掉的 9 条里，只有 2 条（库角标 S60、S73）是当前正文实引的 22 条之一**；
其余 7 条要么只进了池没被写手引（4 条库里连角标都没有），要么只出现在程序直填的附录表里。

### 3.3 读数 3 · 薄主题救不救得回（⚠️ 先核过尺子再报）

| 主题 | 库 | 旧池 | 新池 | 库里 kind |
|---|---|---|---|---|
| 办公 | 19 | 5 | **6** | comment 2 / post 17 |
| 付费 | 11 | 4 | 4 | comment 1 / post 10 |
| 会员 | 15 | 2 | 2 | comment 8 / post 7 |
| 订阅 | 11 | 3 | **2** | comment 1 / post 10 |

**四组都没被救回来**，与 §SEL-1 的定论**一致**（「补池救不了办公太薄，真因是语料里
办公只有 2 条已编码 UGC」）。提货单要我「量出办公明显变多就先怀疑尺子」——办公只 +1，
不触发那条警戒；反倒是**订阅 3→2**，因为被别的行挤掉了。
库里 19 条含「办公」的证据中 **17 条是 post**，本来就不受「评论继承父帖」这一改影响。

### 3.4 读数 4 · 对照实体救不救得回

goal→实体：goal-1 豆包 / goal-2 DeepSeek / goal-3 Kimi / goal-4 文心一言 / goal-5 通义千问 / goal-6 横向对比章。

| goal | 实体 | 库 | 编号池 旧→新 | 可见池 旧→新 |
|---|---|---|---|---|
| goal-1 | 豆包 | 369 | 51 → 47 | 30 → 30 |
| goal-2 | DeepSeek | 111 | 15 → 13 | 15 → 13 |
| goal-3 | Kimi | 115 | 20 → 20 | 20 → 20 |
| goal-4 | **文心一言** | 127 | **5 → 10** | **5 → 10** |
| goal-5 | **通义千问** | 98 | **8 → 9** | **8 → 9** |

⇒ **正好救的是那两家 0 角标的**：文心一言可进池条数**翻倍**（5→10），通义千问 8→9；
代价是主角豆包的编号池 51→47、DeepSeek 15→13（豆包的**可见池**被每节 30 位封顶，仍是 30，没掉）。

### 3.5 读数 5 · 代价：要让这一改在 `r-3b3482ca7f8b` 上真的生效

#### 先说一条被提货单漏掉、但决定路怎么选的事实：**角标会全库重编**

`sectioning.py:2479-2489` 的注释把道理写死了——相关闸必须放在**两个 `_evidence_index`
共同的上游**，因为合并环节 `_merge_shard_structures` 会**按全局号重排角标**；
只过滤其中一个，「同一条证据在两处就是两个号」（2026-09-08 那轮 `goal-3/ch-6/sec-2`
的 37 处 `conclusion_invalid` 就是这么来的）。

实测这一改对编号的冲击（复算侧）：

- 新旧**两侧都有号**的 86 条里，**号不变只有 39 条、号变了 47 条**；
- 旧编号池有号而新侧没号 13 条，反之亦 13 条；
- 当前**正文实引 22 条**里：**2 条（库 S60、S73）掉出可见池**，
  **7 条复算号位移动**（S53 59→55、S58 55→51、S69 68→62、S71 76→72、S86 87→81、S92 92→91、S97 95→94）。
  另有 S81、S98 两条**本来就不在复算旧编号池里**（§SEL-1 记过的 11 条漂移之一），
  这把尺子在新旧两侧都看不见它们，⛔ 不拿来算代价。

⇒ **回答提货单那一问：会动到已定稿的角标编号，而且动得很厉害。**
重出后的稿子，S 号与已经交给客户的那一份对不上。

#### 便宜路为什么不成立

- **只重出稿（polish）不行**：相关闸在**撰写期**（前提 3），polish 不动池。
  §REISSUE-1 轮 3 实测的 **37 min / ≈$2.7** 是纯出稿口径，**救不了本货**。
- **按节补也不行**：`import_research._replay_chapters` + `_write` 的口径是
  `only_chapters` 必须配 `from_goal`，且一旦同时给，`replay_goals` 就被收成
  `{from_goal}` 一个 goal（`import_research.py:164-167`）。六个 goal 的撰写章要**链式补六轮**；
  而角标是**全局重编**的，分六轮补出来的片与合并稿对不齐，正是 D-041/D-042 那个
  「旧分片角标撞唯一引用源契约 → `conclusion_invalid`」的形状。
  实测代价：**点名一节实付整章 + 三 goal 一手核验全部重付 ≈60–80 min/节**
  （`d041-replay-stale-parts` / `d042-replay-stale-parts`），六节 = **6–8 h**，且**不自洽**。

#### 最便宜且自洽的一条：一次性重跑撰写期

`POST /api/researches/replay`，`from_goal=goal-1` + `reset_done=true`，
不给 `only_chapters`（给了就被收进一个 goal）。以旧研究为底新建 research、复制 820 条证据与产物，
把章账本整体复位后走 `runtime.start_research`。

- **估时（有来源，⛔ 不自己编）**：§D-021 干净整跑**执行期基线 67.1 min**
  （`d021-closed-rp1-start`）；09-04 豆包 fast 夜跑整轮 **6 h 21 min** completed
  （`mvp-night-run-0904`）。单节重放一轮约 **29~30 min**（`stage-replay-rp1` / `rp1-closed`）。
- **估钱**：本项目有据可查的单轮出稿是 §REISSUE-1 的 **≈$2.7（37 min，polish 口径）**；
  **撰写期整轮的钱没有可直接引用的实测**，⛔ 不编一个数——要报预算得另量。
- **副作用**：`reset_done=true` 会连**采集章**一起复位，等于再采一轮，
  新证据进来会把池再改一次 ⇒ 上面这份「挤掉谁」的读数只对**证据不变**的前提成立。
  真要「只重撰写不重采」，现有 replay 入口给不了（`only_chapters` 被锁在单 goal），
  **这是一个功能缺口，本包不修**（要修得动 `app/replay/`，禁区）。

#### 小结（呈拍用）

| | 读数 |
|---|---|
| 救回来什么 | 文心一言可进池 **5→10**（翻倍）、通义千问 **8→9**；层 1 进池 464→755，新增 291 条全是评论（非 D 146） |
| 救不回什么 | 办公 5→6、付费 4→4、会员 2→2、**订阅 3→2**（与 §SEL-1「补池救不了薄主题」一致） |
| 挤掉谁 | 复算池 78→82，挤掉 9、新进 13；**其中 2 条（S60、S73）是当前正文实引的 22 条之一** |
| 角标 | **会全库重编**：两侧都有号的 86 条里 47 条换号；正文 22 条里 7 条位移、2 条掉池 |
| 代价 | 只重出稿救不了；按节补 6–8 h 且不自洽；**唯一自洽路 = 一次性重跑撰写期**，且现有入口会连采集一起重跑 |
