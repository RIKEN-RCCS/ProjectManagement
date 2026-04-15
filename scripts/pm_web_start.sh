#!/usr/bin/env bash
# pm_web_start.sh - pm_api.py (FastAPI) をバックグラウンドで起動する
# すでに起動中の場合は何もしない

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/pm_web.log"
PID_FILE="$LOG_DIR/pm_web.pid"
PORT="${PORT:-8501}"

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

# 起動確認: すでに動いていれば終了
if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "pm_web はすでに起動中です (PID $PID, port $PORT)"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

mkdir -p "$LOG_DIR"

PM_WEB_PORT="$PORT" \
nohup "$PYTHON3" "$SCRIPT_DIR/pm_api.py" --port "$PORT" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "pm_web を起動しました (PID $(cat "$PID_FILE"), port $PORT)"
echo "URL:  http://localhost:$PORT"
echo "ログ: $LOG_FILE"
