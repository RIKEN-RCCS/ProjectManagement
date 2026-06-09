#!/bin/bash
# pm_argus_daily_summary.sh - 毎日17時の日次サマリー生成
# 今日のSlackメッセージと議事録をまとめて投稿
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
export LOCAL_LLM_URL="${LOCAL_LLM_URL:-http://localhost:8000/v1}"

PYTHON3="$HOME/.venv_aarch64/bin/python3"
CANVAS_ID="<CANVAS_ID>"  # リーダー会議Canvas ID (既存と同じ)

echo "[$(date +'%Y-%m-%d %H:%M:%S')] 日次サマリー生成開始"
"$PYTHON3" "$SCRIPT_DIR/argus/pm_argus.py" \
    --brief-to-canvas \
    --canvas-id "$CANVAS_ID" \
    --today-only
echo "[$(date +'%Y-%m-%d %H:%M:%S')] 日次サマリー生成完了"
