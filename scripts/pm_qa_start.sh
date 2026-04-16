#!/usr/bin/env bash
# pm_qa_start.sh - pm_qa_server.py をバックグラウンドデーモンとして起動する
# すでに起動中の場合は何もしない（crontab からの自動再起動にも使用可）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/pm_qa_server.log"
PID_FILE="$LOG_DIR/pm_qa_server.pid"

# Python仮想環境の選択
ARCH="$(uname -m)"
if [[ "$ARCH" == "aarch64" ]]; then
    PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$ARCH" == "x86_64" ]]; then
    PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
    echo "未知のアーキテクチャ: $ARCH" >&2
    exit 1
fi

if [[ ! -x "$PYTHON3" ]]; then
    echo "Python3が見つかりません: $PYTHON3" >&2
    exit 1
fi

# トークン・環境変数の読み込み
if [[ -f "$HOME/.secrets/slack_tokens.sh" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.secrets/slack_tokens.sh"
fi

# RiVault トークンの読み込み（Argus コマンド用）
if [[ -f "$HOME/.secrets/rivault_tokens.sh" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.secrets/rivault_tokens.sh"
fi

# ローカルLLM設定（デフォルト値、.secrets/slack_tokens.sh での上書き可）
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://localhost:8000/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-dummy}"
export OPENAI_MODEL="${OPENAI_MODEL:-google/gemma-4-26B-A4B-it}"
export QA_INDEX_DB="${QA_INDEX_DB:-$REPO_ROOT/data/qa_index.db}"

# 起動確認: すでに動いていれば終了
if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "pm_qa_server はすでに起動中です (PID $PID)"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

mkdir -p "$LOG_DIR"

# 起動
nohup "$PYTHON3" "$SCRIPT_DIR/pm_qa_server.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "pm_qa_server を起動しました (PID $(cat "$PID_FILE"))"
echo "ログ: $LOG_FILE"
