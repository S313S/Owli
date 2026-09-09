# 起跑前体检

**在付掉一轮 40–170 分钟的引擎之前，用几秒钟查出那些秒级可查的死因。**

2026-09-08 一天里三轮重放（119 分钟引擎）+ 一轮正式稿（173 分钟、FAIL），
**其中至少三轮的死因，起跑前几秒钟就能查出来**：少传适配器（零消耗崩溃）、
节墙钟烙在计划快照里导致「换档不通电」、点名一节而工具实际重写整个 goal。

## ⛔ 新增检查一律加在这里，禁止另造一套

`scripts/acceptance/` 下现有 **31 个包目录**（code1/d041/obs4/rpt1/shard1/…），
各自一套尺子、**关账即死**。要加一项新检查：

1. 在 `preflight.py` 里加一个 `check_xxx(args) -> Result` 函数，**注释里写清出处**
   （哪一天、哪一次、亏了多少分钟）——不是设想出来的检查不要加；
2. 注册进 `CHECKS`；
3. 在 `tests/test_preflight_checks.py` 里补**红绿各一条**用例。
   **只补绿的不算数**：本项目自造脚本连造过三次假数据，尺子自己也要验。

**不要**新建 `scripts/acceptance/<包名>/xxx_check.py`。

## 怎么用

```bash
# 列出八项
python3 scripts/preflight/preflight.py list

# 起跑之前：跑七项（⑧ 要等 30 秒，且只在起跑之后才有意义）
python3 scripts/preflight/preflight.py all \
    --db var/shard1-serve.db --research r-3e04f808dffd \
    --goal goal-3 --chapter ch-6 --scale standard --mode polish \
    --report-text <工作稿路径> --adapter production

# 起跑之后 30 秒：确认它还活着，活着才报「已起跑」
python3 scripts/preflight/preflight.py liveness --process <本体脚本名>

# 单项也能跑
python3 scripts/preflight/preflight.py adapter
```

**退出码**：`0` 八项全 PASS ｜ `1` 有 FAIL ｜ `3` 没红但有 SKIP。
**SKIP 不算过**——判据要写成 `passed and not skipped`，别落在「存在」上。

## 八项与各自的出处

| # | 子命令 | 检查 | 真实事故 |
|---|---|---|---|
| ① | `backfill` | 会不会回填；会就把耗时算进预算 | **D-043**：`/stop` 掐到收尾期回填 |
| ② | `collision` | 不重写的节与本轮要发的号，同号不同源即红 | **D-056** + 09-08 落点事故：同一份稿里 `[S07]` 指两条源，**一半对一半错且零报错** |
| ③ | `budget` | 节预算 vs 片墙钟 × 片数 | 09-08 sec-2 死在**片**墙钟：分片有两层墙钟，只放宽一层无效 |
| ④ | `switch` | 拧完开关读**实际生效值**（plan_snapshot），不读配置文件 | 09-08 15:5x「换 standard 档跑这一节」经核实**不通电**——节墙钟烙在计划快照里 |
| ⑤ | `citations` | 文件侧与库侧角标 **min/max 与逐条集合**都要一致 | **D-055**：硬闸只装在两个调用点之一，两处差 57 条 → 合并后 37 处越池 |
| ⑥ | `adapter` | 把本轮要用的适配器**真构造一次**（不发请求） | 09-08 16:45 少传 codex，**崩溃、零消耗、两分钟白等**；当时 `pytest 1738` 全绿 |
| ⑦ | `scope` | 播种后读 `chapter_progress`，`pending` 的才是真会被重写的 | **D-057**：`--section` 不往 `open_sandbox` 传 → **静默重写没点名的节**，起跑奏折还报了「没碰」 |
| ⑧ | `liveness` | 起跑后 sleep 30 → `pgrep` **本体脚本名** | 09-08 起跑 **106 秒即崩**，调度据此转述「还在跑」，代价 30 分钟 + 一次错误转述 |

## 三条容易踩的

- **库不能直接开**：留服在用的库要先用 **backup API** 取快照（**禁 `cp`**——漏 WAL
  会读到旧状态，表现像「功能没生效」而不像拷贝错误）。
- **别用管道判红**：`preflight.py … | head` 之后的 `$?` 是 `head` 的。落盘再看 `$?`。
- **⑧ 传本体脚本名，不是 worktree 名**：进程命令行里没有 worktree 名（那是 cwd
  不是参数），拿它做正则只会命中 zsh 包装层，那层做完就退 → **立刻误判「跑完了」**。
  脚本对这种传法直接判红。

## 零引擎

**一次模型调用都不发。** ⑥ 只走 `__init__`，不发请求；脚本里没有任何 `await` /
`asyncio` / 适配器 `.run(`（唯一的 `subprocess.run` 是 `pgrep`）。
