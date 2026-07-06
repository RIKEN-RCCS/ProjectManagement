#!/bin/bash
# pm_argus_daily_summary.sh - 毎日17時の日次サマリー生成
# 今日のSlackメッセージと議事録をまとめて投稿
set -eu

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# トークンを読み込む
if [[ -f ~/.secrets/slack_tokens.sh ]]; then
    source ~/.secrets/slack_tokens.sh
fi
if [[ -f ~/.secrets/rivault_tokens.sh ]]; then
    source ~/.secrets/rivault_tokens.sh
fi

# デフォルトでローカルLLMを優先
if [[ -f ~/.secrets/localLLM.sh ]]; then
    source ~/.secrets/localLLM.sh
fi

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$_arch" == "x86_64" ]]; then
    PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
    echo "Unknown architecture: $_arch"; exit 1
fi
CANVAS_ID="F0ATCN7E2D9"  # リーダー会議Canvas ID

echo "[$(date +'%Y-%m-%d %H:%M:%S')] 日次サマリー生成開始"
"$PYTHON3" "$SCRIPT_DIR/argus/pm_argus.py" \
    --brief-to-canvas \
    --canvas-id "$CANVAS_ID" \
    --today-only
echo "[$(date +'%Y-%m-%d %H:%M:%S')] 日次サマリー生成完了"
