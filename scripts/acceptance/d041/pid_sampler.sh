#!/bin/zsh
# §D-041 货 4：每 0.5 s 抓一次引擎 CLI 子进程，带毫秒时间戳落盘。
# 用法：./pid_sampler.sh <落盘文件> <采样秒数>
out=${1:?}
seconds=${2:-300}
end=$(( $(date +%s) + seconds ))
while (( $(date +%s) < end )); do
  ts=$(python3 -c 'import datetime;print(datetime.datetime.now().isoformat(timespec="milliseconds"))')
  ps -ax -o pid,ppid,etime,command \
    | grep -E '(^| )[0-9]+ .*(codex|claude)( |$)|codex exec|claude-code' \
    | grep -v -E 'grep|pid_sampler|Claude\.app|Claude Code|claude-in-chrome' \
    | while read -r line; do print -r -- "$ts $line" >> "$out"; done
  sleep 0.5
done
