#!/usr/bin/env bash
# pm_web_stop.sh - pm_web.py (Streamlit) を停止する

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PID_FILE="$REPO_ROOT/logs/pm_web.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "PIDファイルが見つかりません: $PID_FILE"
    echo "pm_web は起動していないか、すでに停止しています"
    exit 0
fi

PID="$(cat "$PID_FILE")"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "プロセス $PID は存在しません（すでに停止）"
    rm -f "$PID_FILE"
    exit 0
fi

echo "pm_web (PID $PID) を停止します..."
kill -TERM "$PID"

# 最大10秒待機
for i in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "停止しました"
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

echo "強制終了します..."
kill -KILL "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "停止しました（SIGKILL）"
