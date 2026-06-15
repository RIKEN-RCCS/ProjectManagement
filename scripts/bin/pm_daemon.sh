#!/usr/bin/env bash
# pm_daemon.sh - PM 関連デーモン（qa / web）の start/stop/status を一本化
#
# Usage:
#   bash scripts/pm_daemon.sh start qa      # Argus Socket Mode デーモンを起動
#   bash scripts/pm_daemon.sh stop qa       # 停止
#   bash scripts/pm_daemon.sh start web     # pm_api.py (FastAPI Web UI) を起動
#   bash scripts/pm_daemon.sh stop web      # 停止
#   bash scripts/pm_daemon.sh status        # 全デーモンの状態を一覧
#   bash scripts/pm_daemon.sh status qa     # 特定デーモンの状態
#
# Environment variables:
#   PM_WEB_PORT  web デーモンのポート番号（デフォルト 8501）
#
# サービス定義は SERVICES 配列で管理する。新サービスは1行追加するだけで増やせる。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$REPO_ROOT/logs"

# --------------------------------------------------------------------------- #
# サービス定義
# --------------------------------------------------------------------------- #
#   key=NAME
#   value=TARGET_SCRIPT|LOG_BASENAME|SOURCE_RIVAULT|SET_DEFAULT_LLM|SOURCE_FISH|EXTRA_ARGS
#     TARGET_SCRIPT    : scripts/ からの相対パス
#     LOG_BASENAME     : logs/{name}.log / logs/{name}.pid に使う識別子
#     SOURCE_RIVAULT   : 1 なら ~/.secrets/rivault_tokens.sh を読み込む
#     SET_DEFAULT_LLM  : 1 なら LOCAL_LLM_URL/API_KEY のデフォルト値を設定
#     SOURCE_FISH      : 1 なら ~/.secrets/fish_tts.sh を読み込む
#     EXTRA_ARGS       : Python スクリプトに渡す追加引数（空可）
# --------------------------------------------------------------------------- #
declare -A SERVICES=(
    [qa]="argus/pm_qa_server.py|pm_qa_server|1|1|1|"
    [web]="pm_api.py|pm_web|0|0|0|--port ${PM_WEB_PORT:-8501}"
    # fish は別サーバー運用に移行したため削除（2026-06-11）
)

# --------------------------------------------------------------------------- #
# 共通ヘルパ
# --------------------------------------------------------------------------- #
detect_python() {
    local arch; arch="$(uname -m)"
    if [[ "$arch" == "aarch64" ]]; then
        echo "$HOME/.venv_aarch64/bin/python3"
    elif [[ "$arch" == "x86_64" ]]; then
        echo "$HOME/.venv_x86_64/bin/python3"
    else
        echo "未知のアーキテクチャ: $arch" >&2
        exit 1
    fi
}

load_service() {
    local name="$1"
    local spec="${SERVICES[$name]:-}"
    if [[ -z "$spec" ]]; then
        echo "未知のサービス: $name（利用可能: ${!SERVICES[*]}）" >&2
        exit 1
    fi
    IFS='|' read -r SVC_TARGET SVC_LOG_BASE SVC_RIVAULT SVC_DEFAULT_LLM SVC_FISH SVC_EXTRA <<< "$spec"
    SVC_LOG_FILE="$LOG_DIR/${SVC_LOG_BASE}.log"
    SVC_PID_FILE="$LOG_DIR/${SVC_LOG_BASE}.pid"
    SVC_TARGET_PATH="$SCRIPT_DIR/$SVC_TARGET"
}

cmd_start() {
    local name="$1"
    load_service "$name"

    local python3; python3="$(detect_python)"
    [[ -x "$python3" ]] || { echo "Python3が見つかりません: $python3" >&2; exit 1; }

    # トークン読み込み
    if [[ -f "$HOME/.secrets/slack_tokens.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/.secrets/slack_tokens.sh"
    fi
    if [[ "$SVC_RIVAULT" == "1" && -f "$HOME/.secrets/rivault_tokens.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/.secrets/rivault_tokens.sh"
        if [[ -n "${RIVAULT_URL:-}" && -n "${RIVAULT_TOKEN:-}" ]]; then
            export ARGUS_PREFER_RIVAULT=1
        fi
    fi
    if [[ "$SVC_DEFAULT_LLM" == "1" ]]; then
        if [[ -f "$HOME/.secrets/localLLM.sh" ]]; then
            # shellcheck disable=SC1091
            source "$HOME/.secrets/localLLM.sh"
        fi
        export LOCAL_LLM_URL="${LOCAL_LLM_URL:-http://localhost:8000/v1}"
        export LOCAL_LLM_TOKEN="${LOCAL_LLM_TOKEN:-dummy}"
        export QA_INDEX_DB="${QA_INDEX_DB:-$REPO_ROOT/data/qa_index.db}"
    fi
    if [[ "${SVC_FISH:-0}" == "1" && -f "$HOME/.secrets/fish_tts.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/.secrets/fish_tts.sh"
    fi

    # 起動確認
    if [[ -f "$SVC_PID_FILE" ]]; then
        local pid; pid="$(cat "$SVC_PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "$name はすでに起動中です (PID $pid)"
            exit 0
        fi
        rm -f "$SVC_PID_FILE"
    fi

    mkdir -p "$LOG_DIR"

    # shellcheck disable=SC2086
    nohup "$python3" "$SVC_TARGET_PATH" $SVC_EXTRA >> "$SVC_LOG_FILE" 2>&1 &
    echo $! > "$SVC_PID_FILE"
    echo "$name を起動しました (PID $(cat "$SVC_PID_FILE"))"
    echo "ログ: $SVC_LOG_FILE"
}

cmd_stop() {
    local name="$1"
    load_service "$name"

    if [[ ! -f "$SVC_PID_FILE" ]]; then
        echo "PIDファイルが見つかりません: $SVC_PID_FILE"
        echo "$name は起動していないか、すでに停止しています"
        return 0
    fi

    local pid; pid="$(cat "$SVC_PID_FILE")"
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "プロセス $pid は存在しません（すでに停止）"
        rm -f "$SVC_PID_FILE"
        return 0
    fi

    echo "$name (PID $pid) を停止します..."
    kill -TERM "$pid"

    for _ in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "停止しました"
            rm -f "$SVC_PID_FILE"
            return 0
        fi
        sleep 1
    done

    echo "強制終了します..."
    kill -KILL "$pid" 2>/dev/null || true
    rm -f "$SVC_PID_FILE"
    echo "停止しました（SIGKILL）"
}

cmd_status() {
    local target="${1:-}"
    local targets
    if [[ -n "$target" ]]; then
        targets=("$target")
    else
        targets=("${!SERVICES[@]}")
    fi

    printf "%-6s %-10s %-8s %s\n" "NAME" "STATUS" "PID" "LOG"
    for name in "${targets[@]}"; do
        load_service "$name"
        local status="stopped" pid="-"
        if [[ -f "$SVC_PID_FILE" ]]; then
            pid="$(cat "$SVC_PID_FILE")"
            if kill -0 "$pid" 2>/dev/null; then
                status="running"
            else
                status="stale"
            fi
        fi
        printf "%-6s %-10s %-8s %s\n" "$name" "$status" "$pid" "$SVC_LOG_FILE"
    done
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
usage() {
    cat <<EOF
Usage: $0 <command> [service]

Commands:
  start  <service>   サービスを起動
  stop   <service>   サービスを停止
  status [service]   状態表示（サービス省略時は全件）

Services: ${!SERVICES[*]}
EOF
}

if [[ $# -lt 1 ]]; then
    usage
    exit 1
fi

cmd="$1"
shift

case "$cmd" in
    start)  [[ $# -eq 1 ]] || { usage; exit 1; }; cmd_start "$1" ;;
    stop)   [[ $# -eq 1 ]] || { usage; exit 1; }; cmd_stop "$1" ;;
    status) cmd_status "${1:-}" ;;
    -h|--help|help) usage ;;
    *)      usage; exit 1 ;;
esac
