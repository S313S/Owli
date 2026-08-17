#!/usr/bin/env bash
# Owli 开发期 Codex 启动器
#
# 作用：用一个隔离的 CODEX_HOME 启动 Codex，避免你的全局 config / skill / 记忆
#      污染项目上下文。适配层第 2 条（独立 CODEX_HOME）原本是产品运行期的要求，
#      开发期同样适用 —— 终端 A 实测记录：隔离后单任务 token 消耗预计降一个数量级
#      （.docs-ref/tech-verification-result-terminal-a.md 第 198 行）。
#
# 这是**开发期**的隔离 home（默认 ~/.owli/codex_home_dev）。产品运行期由
# app/adapters/codex.py 用另一个（~/.owli/codex_home，环境变量 OWLI_CODEX_HOME）。
# 两者刻意分开：合并会让开发期的会话历史污染产品运行期的可复现性。
#
# 用法：
#   bash scripts/owli-codex.sh                      # 交互模式
#   bash scripts/owli-codex.sh exec --json "任务"    # 非交互模式
#   OWLI_CODEX_HOME_DEV=/别的/路径 bash scripts/owli-codex.sh
#
# 为什么必须由你（而不是 Codex 自己）来做：CODEX_HOME 是 Codex 进程**启动时**
# 读取的环境变量。等 Codex 跑起来，它读到的值已经定了，改不了自己的。
set -euo pipefail

# 刻意放在仓库之外：CODEX_HOME 里会落 auth.json（凭证），
# 而 Owli 代码仓是公开仓库 —— 凭证一律不进工作树。
export CODEX_HOME="${OWLI_CODEX_HOME_DEV:-$HOME/.owli/codex_home_dev}"
mkdir -p "$CODEX_HOME"

echo "CODEX_HOME = $CODEX_HOME"

if [ ! -f "$CODEX_HOME/auth.json" ]; then
  cat <<EOF

⚠️  这个隔离 home 里还没有凭证，二选一（只做一次）：

  1) 在隔离 home 里单独登录一次：
       CODEX_HOME="$CODEX_HOME" codex login

  2) 复用你现有的登录（推荐，软链会跟着 token 自动续期、不会过期失效）：
       ln -s "\$HOME/.codex/auth.json" "$CODEX_HOME/auth.json"
     （如果你的 Codex 默认目录不是 ~/.codex，按实际路径改）

EOF
fi

exec codex "$@"
