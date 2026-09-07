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
- `--resume` 只跳过**本轮已经过了尺子**的格；没过的格一律重写。
- 账本记的是「哪个 git HEAD 下哪一格过了」。**代码一变整本作废**，
  工作区脏也作废——改了尺子或提示词，上一段的绿一律不认，宁可多跑。
- 账本坏了或不在，就是全部重跑，不会崩、更不会当成「全过了」。

## 三种跑法别用混

| 跑法 | 语义 | 用在哪 |
|---|---|---|
| 默认（不带旗标） | **md 文件在就跳过**——判据是「文件存在」，**不是**「过了尺子」 | 只用来零成本复验尺子。**不能拿来续跑**：没过的格也会被跳过 |
| `--force` | 九格全部重写 | 整轮从头跑 |
| `--resume` | 按账本续跑，已过的跳过、其余重写 | 中途断了接着跑。与 `--force` 互斥 |

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
