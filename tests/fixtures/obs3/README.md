# §OBS-3 真机 transcript 样本（Claude 引擎）

来源：§OBS-2 重放落盘的 `<chapter>.transcript.jsonl`
（`../Owli-obs2/var/replay/obs2-run2/runs/{r-d10d5f216c8b,r-08e8c2433f67}/goals/goal-3/`）。

- `claude-small.transcript.jsonl`：4 条，原样未改。
- `claude-long.transcript.jsonl`：53 条，**只把 `signature` 串截到 200 字**（原件单块 7–13 KB，
  进版本库太肥）。其余字段一字未改。签名串保留一截是故意的——判据 1 的「签名不进进程栏」
  要有东西可断言。

Codex 侧无真机 transcript（现有底料全是 Claude），按 `tests/` 里既有真机 JSONL 形状
在 `test_obs3_narrate.py` 逐形态锁；重放若落到 codex 引擎再补真机样本。
