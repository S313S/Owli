# 九格最后一轮 · 起跑手册

三份成稿 × 三模板 = 9 次 Opus 整理，**串行 5–6 h**。一格几分钟到半小时，
每格因红点会自己重写 5–8 轮。跑之前先确认三件事，跑起来之后只看账本。

## 起跑前的三条闸

1. **代码得是要验的那一版**：`git status --porcelain` 为空、`git log --oneline -1`
   是三包合齐之后的尖。工作区脏则账本自动带 `+dirty` 且永不复用（见下）。
2. **底料在本树**：`var/rpt1-8956.db` 与 `var/runs/<id>/` 三份。库只读，只写 `exports/`。
3. **别在 8969 上跑着别的**。跑着的树是运行时，中途 checkout 会被子进程当场吃到。

## 起跑

```bash
cd ../Owli-rpt1
nohup ../Owli/.venv/bin/python3 scripts/acceptance/rpt1/rpt1_matrix.py \
  --db var/rpt1-8956.db --runs var/runs --force \
  > var/rpt1-final-$(date +%m%d-%H%M).log 2>&1 &
echo $!    # 记下 pid
```

**日志落 `var/` 不落 `/tmp`**：09-07 早上机器重启把 `/tmp` 清空，九格日志和三个
哨兵探测器日志一起没了，读数只剩人工在清空前抄下的那份。`var/` 在 worktree 里，
重启不掉。

## 中途断了怎么接（机器重启、被 kill、断流）

**换成 `--resume`，别再用 `--force`**：

```bash
nohup ../Owli/.venv/bin/python3 scripts/acceptance/rpt1/rpt1_matrix.py \
  --db var/rpt1-8956.db --runs var/runs --resume \
  > var/rpt1-final-resume-$(date +%m%d-%H%M).log 2>&1 &
```

- 账本 `var/rpt1-matrix-progress.json`，**每跑完一格就落一次**（先写 `.tmp` 再改名，
  跑到一半被杀不会留半个账本）。
- `--resume` 只跳过**本轮真写出来过、并且过了尺子**的格；其余一律重写。
  两个条件都要（`passed and not skipped`）。只看「过了尺子」会出事：默认路径
  也会给上一轮的旧稿判 PASS 并记进账本，于是最后一轮把那几格直接跳掉，
  交出没有质量补丁的旧稿而读数 9/9 全绿。读数行里带「← 未重写，只压尺子」
  或「← 本格未出稿」的格，账本一律不认。
- 账本记的是「哪个 git HEAD 下哪一格过了」。**代码一变整本作废**，
  工作区脏也作废——改了尺子或提示词，上一段的绿一律不认，宁可多跑。
- 账本坏了或不在，就是全部重跑，不会崩、更不会当成「全过了」。

## 三种跑法别用混

| 跑法 | 语义 | 用在哪 |
|---|---|---|
| 默认（不带旗标） | **md 文件在就跳过**——判据是「文件存在」，**不是**「过了尺子」 | 只用来零成本复验尺子。**不能拿来续跑**：没过的格也会被跳过 |
| `--force` | 九格全部重写 | 整轮从头跑 |
| `--resume` | 只跳过**本轮真写出来过且过了尺子**的格，其余一律重写 | 中途断了接着跑。与 `--force` 互斥 |

单格返工：`--only <research_id>:<模板>`。

## 先单跑一格给人读，再起整轮

调度可能先要一格出稿给用户读，再起整轮。两步的旗标不能凭感觉：

```bash
# 第一步：单跑一格。**必须带 --force**——九份旧稿都还在，不带 --force 会被
# 默认路径跳过，只重新压一遍尺子，交给用户的就是上一轮那份旧稿（读数里
# 会打「← 未重写，只压尺子」，别忽略这行）。
../Owli/.venv/bin/python3 scripts/acceptance/rpt1/rpt1_matrix.py   --db var/rpt1-8956.db --runs var/runs --force   --only r-3e04f808dffd:consulting > var/rpt1-sample-$(date +%m%d-%H%M).log 2>&1

# 第二步：起剩下的八格。**用 --resume，不要用 --force**——第一格已经进账本，
# --resume 会跳过它；用 --force 则把它连同另外八格一起重跑，白烧一格。
nohup ../Owli/.venv/bin/python3 scripts/acceptance/rpt1/rpt1_matrix.py   --db var/rpt1-8956.db --runs var/runs --resume   > var/rpt1-final-$(date +%m%d-%H%M).log 2>&1 &
```

前提是两步之间**代码一个字没动**（含没提交的改动）——动了账本自动作废，
第二步会连第一格一起重跑，这是设计如此，不是故障。

## 跑完看什么

- 末尾一行 `尺子全过 N/9`，退出码 0 只在 9/9 时给。
- 九份稿在 `var/runs/<id>/exports/<id>.polished.<模板>.md`。
- 读数账本路径在末尾打印，JSON 里每格带 `ruler` 明细。
- 交链接给人之前先 `curl --noproxy '*'` 拿 200 text/html——09-03 出过
  「跑了 193 分钟，用户打开是页面不存在」的事故。
- **稿子要同时落进 `goals/` 和 `exports/`**。引擎只写得进
  `goals/polished/`，`exports/` 那一份由 `polish()` 搬——搬之前挂了的话，
  控制台照样打 PASS，而页面走的是 `exports/`，用户看到的还是旧稿。
  **跑完第一格，第一件事是核 `exports/` 里那份 md 在不在，在才给链接。**

## 某一格失败了怎么读诊断（有个坑）

失败返回里的 `missing_sections` **不可信，会少报**。

**为什么**：它是按「文件在不在」算的（`[n for n, p in parts if not p.is_file()]`），
而 `polish()` **只在每次尝试前清掉当前这一节**，开跑前不清全部分节。上一轮的
分节产物还躺在树上（09-07 实测 **69 份**），所以某格在第 K 节失败时，
第 K+1 节往后那些**本轮从没被碰过**的节，因为上一轮的文件还在，会被算成「不缺」。
明明只写成两节，报告会说一节不缺。

这是「判据落在文件存在上、不落在这一轮真产出的上」那一族的第五次现形，
前四次是：尺子误报把好稿判红 → 续跑只看文件在不在 → 读数分不清重写与未重写 →
账本分不清「过了」和「本轮写出来且过了」。

**怎么判实际写成了几节**：看 `var/runs/<id>/goals/polished/<模板>-parts/` 里各
文件的 **mtime**，跟本轮起跑时间比——比起跑时间新的才是本轮写出来的。

```bash
ls -l --time-style=full-iso var/runs/<id>/goals/polished/<模板>-parts/ 2>/dev/null \
  || stat -f '%Sm %N' -t '%F %T' var/runs/<id>/goals/polished/<模板>-parts/*
```

**这条不影响修复动作**：不管缺几节，重跑都是**整格重写**（`--force` 与
`--resume` 都是整格），所以诊断错了不会让人修错东西，只会让人对失败原因理解偏。
所以起跑前不动它——今晚要跑的就是这个撰写入口，起跑前少动一行是一行。

> **这条挂账已由 §SHARD-1 销账**（2026-09-07，提前到「随分片一起」）：`polish()`
> 开跑前调 `clear_stale_parts()` 把该格 `[0-9][0-9]-*.md` 全清一遍（分节与分片都在内），
> 清掉的文件名进返回值 `cleared`。所以现在 `missing_sections` 里的「在」就是本轮写的，
> 上面那段「按 mtime 判」的绕法只在读 09-07 及更早的日志时才需要。

## 「关键发现」现在是分片写的（§SHARD-1）

咨询体的「关键发现」按执行摘要里的关键发现**条数**切片，一条发现一片，
片数由稿子自己定（`shard_sections: [关键发现]` 写在 SKILL.md 的 frontmatter 里）。
三份底料实测：r-3e04f808dffd 4 片、r-045acebc352b 4 片、r-b10812f664d2 5 片。

- 片文件：`goals/polished/<模板>-parts/02-关键发现.shard-N.md`，写完按片序拼进
  `02-关键发现.md`，再由 `assemble` 拼进成稿。
- **任一片没写成，这一节不判 done**，连 `02-关键发现.md` 都不留（D-051 降到片级）。
- 返回值里 `shards` 是每节切了几片（0 = 没切）。摘要读不出编号列表就退回整节写一次，
  那是老行为不是新失败。
- **墙钟口径跟着变了**：`SECTION_TIMEOUT_SECONDS`（1800 s）现在是**每片**的墙钟，
  不再是每节的。好的情况下一节更快（每片 2–5 分钟）；全都超时的坏情况下，
  一节的上限从「2 次 × 30 分钟」变成「片数 × 2 次 × 30 分钟」。
  单格 `--only` 跑无所谓，**起八格串行之前要先拍这条**。
