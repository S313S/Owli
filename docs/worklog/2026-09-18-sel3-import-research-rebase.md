# §SEL-3 工作日志 · 复制研究时漏改「产物存在哪」

> worktree `../Owli-sel3`，分支 `sel3-import-research-rebase`，base **c5f7b7c**。
> 解释器 `../Owli/.venv/bin/python`。零引擎、零采集、不重出稿。

## 〇、开工先核提货单 §一 的五条世界模型

| # | 提货单的说法 | 核的结果 |
| --- | --- | --- |
| 1 | `actual_output_path` 在 replay 出来的研究里仍指向源树 | ✅ 成立。`r-3b3482ca7f8b` 账本 110 条带路径的行，**一条都不在自己的树下** |
| 2 | 判为漏改：`sandbox.py::_rebase_ledger_paths` 做过同样的事，`import_research` 没调它 | ✅ 成立。全仓 `_rebase_ledger_paths` 只有 sandbox 自己一处调用 |
| 3 | goal-6/ch-4 的 18 条 inputs 命中 0/18、六节 `cross_goal_count` 全 0 | ⚠️ **前半条成立，后半条要改口径**：0/18 复现到了；但六节是 **0/0/0/0/0/30**，第六节（`sec-6`，goal-6 自己）是 30。详见 §三 |
| 4 | 反事实已验：不影响引用池总量 | ✅ 沿用，本包不碰池子大小，worklog 里也不这么写 |
| 5 | `sandbox` 那版用 `f"/{research_id}/"` 当 marker，import 是换 id 的场景，直接抄会认不出来——得先拿到源 id | ✅ 成立，而且**比这还要深一层**：一份账本里同时带着**好几代** id，只拿「直接源」那一个 id 仍然一行都改不动。详见 §一 |

第 5 条是本包最容易踩空的一脚，实测确实踩得到——见下。

## 一、⚠️ 世界模型第 5 条要再深一层：源 id 不止一个

`r-3b3482ca7f8b` 是三代导入的产物（`extra.replay_of` 链）：

```
r-20271e8a5028（Owli-wx1 的库，本库里没有这一行）
  → r-f9bb30969cf2 → r-96e0257a86b4 → r-3b3482ca7f8b
```

而 `actual_output_path` 这一列**从来没被改写过**，所以它带的是「当初真跑出那份产物的那一代」的 id。
在库副本上按 base 代码导入一次（新 id `r-834ebd1aee3a`），账本 110 条带路径的行按 id 分布：

| 路径里的 id | 行数 |
| --- | --- |
| `r-20271e8a5028` | 104 |
| `r-f9bb30969cf2` | 4 |
| `r-96e0257a86b4` | 2 |
| **直接源 `r-3b3482ca7f8b`** | **0** |

→ 如果照提货单字面只拿「直接源 id」当 marker，**这次导入一行都改不动，命中仍是 0/18**。
所以 marker 必须是**整条溯源链**：源自己的 id + 它一路 `replay_of` 上来的祖先 id。

**源 id 的来源（提货单要求写清）**：不靠猜、不靠 id 形状正则（真机存在 `r-wx1-0914-1239`
这种不合 `r-[0-9a-f]{12}` 的 id，正则会认错）。走 `reports.extra.replay_of`——§RP-1 自己写下的
溯源格。链断在库外（祖先那一行跑在另一台 worktree 的库里，本库查不到）时，**它的 id 仍从子代的
`replay_of` 里拿得到**，只是走不动下一跳就停在那儿；真机 `r-20271e8a5028` 正是这个形态。
认不出任何 marker 的路径**原样保留**（⛔ 不猜着改）。

## 二、一货：改了什么

沿用 `sandbox.py::_rebase_ledger_paths`，⛔ 没另写一份路径改写（记忆 `reuse-before-rewrite`）。
改它的那一个函数，改成两边都能用：

- `app/replay/sandbox.py::_rebase_ledger_paths` 加关键字参数 `source_research_ids`
  （「路径里可能出现的旧 id」），`research_id` 仍是**目标**（行的作用域 + 新树下的目录名）。
  marker 由一个变成一组，取最左的那一处命中。
  **为什么不会动到沙盒那条已验过的路**：沙盒调用不传 `source_research_ids`，
  markers 退化成 `[f"/{research_id}/"]` 一个元素，与 §RD-1 验过的那版逐字等价；
  并且用例 `test_沙盒那条路不传源id时行为不变` 把这条钉住（不传源 id 时，带祖先 id 的那条
  **不许**被改、只认自己那一条）。
- `app/replay/import_research.py` 新增 `_source_lineage()`（读 `extra.replay_of` 走溯源链），
  在 `_write()` 里 `_copy_chapters` 之后调一次 `_rebase_ledger_paths`。

改动只这两个文件、`app/replay/` 下只这两个（`import_research.py` 是用户解禁的那一处，
`sandbox.py` 是为它改的那一个函数）。

## 三、判据

### 判据 1 · 全量 pytest

```
../Owli/.venv/bin/python -m pytest tests/ > /tmp/sel3-pytest.log 2>&1; echo $?
→ exit=0
→ 2312 passed, 4 skipped in 33.23s
```

基线 2306 passed / 4 skipped + 本包 6 条新用例 = **2312 / 4**，零回归。
（4 skipped 依赖 `../Owli-rpt8/var/rpt8/` 真机产物，别树静默跳过，属预期。）

### 判据 2 · 新用例回 base 造红

副本：`git archive c5f7b7c | tar -x -C /tmp/owli-base-c5f7b7c`（不动 `.git`，比 worktree 更干净），
只搬用例文件进去。签出确认：副本里 `source_research_ids` 命中 0 次、`import_research.py` 里
无 `_rebase_ledger_paths`，确实是修前的代码。

```
cd /tmp/owli-base-c5f7b7c && pytest tests/test_sel3_import_rebases_ledger_paths.py
→ exit=1，3 failed / 3 passed
```

**被判红的原文**（红的正是这件事）：

```
E  AssertionError: 带祖先 id 的路径没跟到新树：
   /Users/someone/Owli-别的树/var/runs/r-sel3-ancestor-00/goals/goal-1/agent-1.md
E  AssertionError: 章级声明的上游产物没被认出来，跨 goal 证据在新研究里就是不可见的
E  assert 0 == 1   # ['goal-2'].count('goal-1')
E  ImportError: cannot import name '_source_lineage' from 'app.replay.import_research'
```

另两条（`认不出marker的路径原样保留`、`沙盒那条路不传源id时行为不变`）在 base 上**是绿的**——
它们钉的正是**不该变**的行为，修前修后都必须绿。

**造红量出非零（真机那份）**：base 代码在库副本上导入一次（新 id `r-834ebd1aee3a`），
账本里 **110 条路径全部仍指着源树**，例如

```
goal-1/ch-1  status=done
/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection/Owli-wx1/var/runs/r-20271e8a5028/goals/goal-1/data-collection.json
```

### 判据 3 · 真机验证（库副本上做，⛔ 没写 `../Owli-src5`）

库副本走 **backup API**（⛔ 不用 `cp`：`src5-backfill.db-wal` 当时 7.3 MB，`cp` 必读到旧状态）：

```python
origin = sqlite3.connect("file:../Owli-src5/var/src5-backfill.db?mode=ro", uri=True)
origin.backup(sqlite3.connect("var/sel3-base.db"))   # → 18 808 832 B, user_version=10
```

读数尺 `scripts/acceptance/sel3/measure_closure.py`：只用**消费侧**生产函数
（`_artifact_key` / `_ledger_inputs` / `_merge_declared_done_inputs` /
`_allowed_evidence_goal_ids` / `_evidence_index`）离线重算一遍组池；被测的是导入侧的路径改写，
两边不是同一段代码（记忆 `verification-ruler-needs-verifying`）。

**先验尺子**：拿它去量**生产现场那一行** `r-3b3482ca7f8b`（`runs-root=../Owli-src5/var/runs`），
与当时真落库的 `section_pool_composed` 事件对：

| 节 | 事件里的 pool/own/cross | 尺子量出的 | |
| --- | --- | --- | --- |
| ch-4/sec-1 | 30 / 30 / 0 | 30 / 30 / 0 | ✅ |
| ch-4/sec-2 | 15 / 15 / 0 | 15 / 15 / 0 | ✅ |
| ch-4/sec-3 | 20 / 20 / 0 | 20 / 20 / 0 | ✅ |
| ch-4/sec-4 | 6 / 6 / 0 | 5 / 5 / 0 | ⚠️ 差 1 条 |
| ch-4/sec-5 | 8 / 8 / 0 | 8 / 8 / 0 | ✅ |
| ch-4/sec-6 | 30 / 0 / **30** | 30 / 0 / **30** | ✅ |

`cross_goal_count` 六格全中；sec-4 的池大小差 1，是事件写下之后又发生过 backfill/评级改动
（库名就叫 `src5-backfill`），不影响本包读数（本包比的是同一把尺子在修前/修后的差）。
同一把尺子在生产现场量出 **18 条 inputs 命中 0/18**，与 §SEL-1 的读数一致 —— 前提复现到了。

**修前 / 修后**（同一份底料、同一条命令，只差代码）：

```
python scripts/acceptance/sel3/replay_import.py \
  --source-database var/sel3-base.db --source-research-id r-3b3482ca7f8b \
  --source-runs ../Owli-src5/var/runs \
  --target-database var/sel3-{before,after}.db --runs-root var/runs-{before,after} \
  --from-goal goal-6 --only-chapters ch-4
```

（场景 = 真机最常用的那个「补最终页」：`from_goal=goal-6 only_chapters=ch-4`，
只复位 `goal-6/ch-4` 与它的六个节，其余章原样搬。）

| | 修前 `r-834ebd1aee3a` | 修后 `r-78a56dd29e9c` |
| --- | --- | --- |
| 账本里被 rebase 的路径 | **0 / 110** | **110 / 110** |
| goal-6/ch-4 的 18 条 inputs 命中 | **0 / 18** | **16 / 18** |
| 指向不存在文件的路径 | — | **0 条**（110 条逐条 `is_file()` 核过） |

六节 `cross_goal_count`：

| 节 | goal | 修前 pool/own/cross | 修后 pool/own/cross |
| --- | --- | --- | --- |
| ch-4/sec-1 | goal-1 | 30 / 30 / **0** | 30 / 30 / **0** |
| ch-4/sec-2 | goal-2 | 15 / 15 / **0** | 30 / 15 / **15** |
| ch-4/sec-3 | goal-3 | 20 / 20 / **0** | 30 / 20 / **10** |
| ch-4/sec-4 | goal-4 | 5 / 5 / **0** | 30 / 5 / **25** |
| ch-4/sec-5 | goal-5 | 8 / 8 / **0** | 30 / 8 / **22** |
| ch-4/sec-6 | goal-6 | 30 / 0 / **30** | 30 / 0 / **30** |

`allowed_goal_ids` 修前每节只剩自己（`['goal-1']`…），修后六节全是
`['goal-1'..'goal-6']` —— 闭包真的追到了上游。

两处要讲清楚，⛔ 别当成没修好：

- **sec-1 修后仍是 0**：goal-1 自己的证据就把 30 个池位占满了（own=30=池上限），
  轮不到跨 goal 的进来。这是配额，不是闭包——`allowed_goal_ids` 已经是六个 goal。
- **sec-6 修前就是 30**：goal-6 名下**一条证据都没有**，`_evidence_index` 的空集兜底
  （`if not eligible and numbered: eligible = numbered`）回退到全池，于是 30 条全算「跨 goal」。
  所以提货单 §一.3「六节 `cross_goal_count` 全 0」严格说是**五节 0 + 一节 30（兜底态）**。
  修后这一格数值没变，但**成因换了**：从「被兜底捞回来的」变成「闭包真的放行了」。

**18 条里没中的 2 条**：`goals/goal-1/data-collection-7.json`（账本 `goal-1/ch-13`，
status=**missing**，reason=`tool_unavailable`）与 `goals/goal-1/data-collection-8.json`
（`goal-1/ch-15`，status=**missing**，reason=`empty_result`）。它们本来就没跑出产物，
`_merge_declared_done_inputs` 只收 done 行——**16/18 就是这份底料的上限**，不是漏改。
其余 16 条逐条命中（1↔goal-6/ch-3、2↔goal-1/ch-20、3↔goal-2/ch-8、4↔goal-3/ch-5、
5↔goal-4/ch-6、6↔goal-5/ch-5、7↔ch-1、8↔ch-3、9↔ch-5、10↔ch-7、11↔ch-9、12↔ch-11、
15↔goal-2/ch-1、16↔goal-3/ch-1、17↔goal-4/ch-1、18↔goal-5/ch-1）。

### 判据 4 · 禁区正列举扫（⛔ 没用否定前瞻）

`git diff --name-only c5f7b7c HEAD` 完整输出（四行，请调度逐行核）：

```
app/replay/import_research.py
app/replay/sandbox.py
docs/worklog/2026-09-18-sel3-import-research-rebase.md
scripts/acceptance/sel3/measure_closure.py
scripts/acceptance/sel3/replay_import.py
tests/test_sel3_import_rebases_ledger_paths.py
```

正列举扫禁区（`app/store/|migrations|app/sources/|app/adapters/|app/reliability/crossref|app/reliability/ratelimit|app/config.py`）：**零命中**。
`app/replay/` 下只有 `import_research.py`（用户解禁的那一处）与 `sandbox.py`（为它改的那一个函数），
`app/replay/section.py`、`app/replay/__init__.py` 零命中。

### 判据 5 · `../Owli-src5` 零改动、8981 不重启

起工时刻文件 `var/.sel3-start-marker`（13:14:58 建）。

```
find ../Owli-src5 -newer var/.sel3-start-marker -type f | head   → 空
```

8981 全程没停没重启；`../Owli-src5` 只做了两件读操作：`mode=ro` 连库做 backup、
`copytree` 读 `var/runs/r-3b3482ca7f8b`。`../Owli-wx1` 一个字没碰。

## 四、同族漏改：发现了，⛔ 没顺手改（按解禁范围）

用户只解禁 `import_research.py` 补路径 rebase 这一处。下面几条是这次读代码时看见的，
**本包一个字没动**，记在这里等调度定夺：

1. **`extra` 列里的绝对路径没跟着改**。`_copy_chapters` 把源行的 `extra` 原样抄过去
   （`app/replay/import_research.py` 的 UPDATE 里有 `extra = ?`），里面若存过绝对路径，
   导入后同样指着源树。本包只改 `actual_output_path` 一列（与 sandbox 一致：闭包只读它）。
2. **`reports.extra.replay_source_database` 记的是「上一跳」的库**，不是最初那台。
   要沿链回溯得一跳一跳查，链断在库外就查不动了（本包的 `_source_lineage` 只取 id，不取库路径，
   不受影响）。
3. **事件 payload 里的路径**（`events` 表、transcript）同样带源树绝对路径，导入后不改写。
   只读展示用，没进任何判定。

## 五、可复跑读数命令

```bash
# 生产现场那一行（只读 ../Owli-src5）
../Owli/.venv/bin/python scripts/acceptance/sel3/measure_closure.py \
  --database var/sel3-base.db --research-id r-3b3482ca7f8b \
  --runs-root ../Owli-src5/var/runs --chapter goal-6/ch-4

# 在库副本上重放一次 import 并量命中（backup API 拷库，底料只读）
../Owli/.venv/bin/python scripts/acceptance/sel3/replay_import.py \
  --source-database var/sel3-base.db --source-research-id r-3b3482ca7f8b \
  --source-runs ../Owli-src5/var/runs \
  --target-database var/sel3-after.db --runs-root var/runs-after \
  --from-goal goal-6 --only-chapters ch-4
```
