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
#   qa 起動時、~/.secrets/rikyu_token.sh が存在すれば source する（K3 override
#   の ARGUS_ONESHOT_LLM_URL/_TOKEN 等。無ければ黙ってスキップ）
#
# サービス定義は SERVICES 配列で管理する。新サービスは1行追加するだけで増やせる。

set -euo pipefail

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$REPO_ROOT/logs"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

# --------------------------------------------------------------------------- #
# サービス定義
# --------------------------------------------------------------------------- #
#   key=NAME
#   value=TARGET_SCRIPT|LOG_BASENAME|SOURCE_RIVAULT|SET_DEFAULT_LLM|SOURCE_FISH|EXTRA_ARGS
#     TARGET_SCRIPT    : scripts/ からの相対パス。`venv:<名前>` と書くと venv の bin/<名前> を指す
#     LOG_BASENAME     : logs/{name}.log / logs/{name}.pid に使う識別子
#     SOURCE_RIVAULT   : 1 なら ~/.secrets/rivault_tokens.sh を読み込む
#     SET_DEFAULT_LLM  : 1 なら ~/.secrets/localLLM.sh を読み込み LOCAL_LLM_URL/TOKEN を設定
#     SOURCE_FISH      : 1 なら ~/.secrets/fish_tts.sh を読み込む
#     EXTRA_ARGS       : Python スクリプトに渡す追加引数（空可）
# --------------------------------------------------------------------------- #
declare -A SERVICES=(
    [qa]="argus/pm_qa_server.py|pm_qa_server|1|1|1|"
    [web]="pm_api.py|pm_web|0|0|0|--port ${PM_WEB_PORT:-8501}"
    [docling]="venv:docling-serve|docling_serve|0|0|0|run --host 127.0.0.1 --port ${DOCLING_PORT:-5001}"
    # fish は別サーバー運用に移行したため削除（2026-06-11）
)

# --------------------------------------------------------------------------- #
# .claude/settings.json から env を読み取って export する
# --------------------------------------------------------------------------- #
load_claude_settings_env() {
    if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
        return 0
    fi
    # jq で env オブジェクトのキー・値を取り出して export
    if command -v jq &>/dev/null; then
        while IFS="=" read -r key value; do
            if [[ -n "$key" && -z "${!key:-}" ]]; then
                export "$key"="$value"
            fi
        done < <(jq -r '.env // {} | to_entries[] | .key + "=" + .value' "$CLAUDE_SETTINGS" 2>/dev/null || true)
    else
        # jq がない場合: python3 でパース
        if command -v python3 &>/dev/null; then
            export $(python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    cfg = json.load(f)
for k, v in cfg.get('env', {}).items():
    print(f'{k}={v}')
" 2>/dev/null) || true
        fi
    fi
}

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
    if [[ "$SVC_TARGET" == venv:* ]]; then
        SVC_TARGET_PATH="$(dirname "$(detect_python)")/${SVC_TARGET#venv:}"
    else
        SVC_TARGET_PATH="$SCRIPT_DIR/$SVC_TARGET"
    fi
}

cmd_start() {
    local name="$1"
    load_service "$name"

    local python3; python3="$(detect_python)"
    [[ -x "$python3" ]] || { echo "Python3が見つかりません: $python3" >&2; exit 1; }

    # Claude Code settings.json から ANTHROPIC_* を読み込む（未設定の場合のみ）
    load_claude_settings_env

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
        export QA_INDEX_DB="${QA_INDEX_DB:-$REPO_ROOT/data/qa_index.db}"
    fi
    if [[ "$name" == "qa" && -f "$HOME/.secrets/rikyu_token.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/.secrets/rikyu_token.sh"
    fi
    if [[ "${SVC_FISH:-0}" == "1" && -f "$HOME/.secrets/fish_tts.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOME/.secrets/fish_tts.sh"
    fi
    # net_guard（外向き通信の allow-list、docs/security-architecture.md §4.7 層1）を
    # デーモンでは enforce にする。allow-list 外の宛先は resolve/connect の両段で
    # PermissionError になり、起動時には from_env の実値照合が走る（不一致なら起動しない）。
    #
    # ここで export するのは qa/web のデーモンだけ。cron スクリプトは各自が
    # ~/.secrets/*.sh を source する別経路なので影響を受けない（意図的な段階導入 —
    # デーモンを先に enforce にして1日観測し、問題なければ cron 側にも広げる）。
    # 退避が必要なときは ARGUS_NETGUARD=warn を環境に置いて再起動すればこの既定を上書きできる。
    if [[ "$name" == "qa" || "$name" == "web" ]]; then
        export ARGUS_NETGUARD="${ARGUS_NETGUARD:-enforce}"
        echo "net_guard mode: ${ARGUS_NETGUARD}"

        # モデル pin（供給網の固定、docs/security-architecture.md §4.6）。
        # 2026-08-03 の実測で「宣言と実態が一致していない」ことが判明し、その日は
        # warn のまま据え置いた:
        #
        #   - `RIVAULT_MODEL=deepseek-ai/DeepSeek-V4-Flash` が本番デーモンに設定されており、
        #     `call_argus_llm` の rivault 経路（llm.py の _try_rivault）は model 引数を
        #     渡さないのでこれが使われる。だが pin では production: false（評価専用）だった。
        #   - 第2系統（config/sensitive_terms.yaml の Llama-4-Scout / gemma3:12b）が
        #     pin に宣言が無かった。
        #
        # 同日、PM から「RiVault の本番モデルは DeepSeek-V4-Flash であり、
        # Kimi-K2-Thinking は thinking のログが滲み出て使いものにならず退役した」との
        # 回答を得たため、config/model_pin.yaml を実態に合わせて更新した
        # （DeepSeek-V4-Flash を production: true に、Kimi-K2-Thinking を
        # production: false に、第2系統・OCR 用モデルを追加宣言）。
        # `model_pin.py --check` で本番6モデル全てが enforce で通ることを確認した上で
        # enforce に切り替えている。
        #
        # 緊急退避: enforce にして全 LLM 呼び出しが ModelPinError になった場合は、
        # `ARGUS_MODEL_PIN=warn ./scripts/bin/pm_daemon.sh start qa` で退避してから
        # `python3 scripts/utils/model_pin.py --check` の結果を見て config/model_pin.yaml を更新する。
        export ARGUS_MODEL_PIN="${ARGUS_MODEL_PIN:-enforce}"
        echo "model_pin mode: ${ARGUS_MODEL_PIN}"
    fi

    # 能力分離 5b（Phase 5、docs/security-architecture.md §3.2）: 調査を
    # Read Plane の別プロセス（Slack/Box トークンを持たない）で実行する。
    # qa デーモンのみで有効化する（mention/investigate の調査経路がここでしか動かないため）。
    # 退避が必要なときは ARGUS_READ_PLANE_SUBPROCESS=0 を環境に置いて再起動すればこの既定を上書きできる。
    if [[ "$name" == "qa" ]]; then
        export ARGUS_READ_PLANE_SUBPROCESS="${ARGUS_READ_PLANE_SUBPROCESS:-1}"
        echo "read_plane subprocess: ${ARGUS_READ_PLANE_SUBPROCESS}"
    fi

    if [[ "$name" == "docling" ]]; then
        # MAX_SYNC_WAIT はサーバ側同期待ちの上限。既定 120s のままだと大型 PDF
        # （table_mode=accurate）が途中で 404 を返しクライアントがフォールバックする。
        # pm_box_crawl 側の DOCLING_TIMEOUT(600s) と揃える。
        export UVICORN_WORKERS="${UVICORN_WORKERS:-1}"
        export DOCLING_SERVE_ENABLE_UI="${DOCLING_SERVE_ENABLE_UI:-true}"
        export DOCLING_SERVE_MAX_SYNC_WAIT="${DOCLING_SERVE_MAX_SYNC_WAIT:-600}"
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
