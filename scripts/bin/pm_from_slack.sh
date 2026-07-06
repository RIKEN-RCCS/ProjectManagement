#!/usr/bin/env bash
# pm_from_slack.sh
#
# Slack取得 (slack_pipeline.py) → pm.db抽出 (pm_ingest.py slack) を連続実行する。
#
# Usage:
#   bash scripts/pm_from_slack.sh -c CHANNEL_ID
#   bash scripts/pm_from_slack.sh -c CHANNEL_ID --since 2026-01-01
#   bash scripts/pm_from_slack.sh -c CHANNEL_ID --dry-run
#
# Options:
#   -c CHANNEL_ID         対象チャンネルID（未指定時は環境変数 PM_DEFAULT_SLACK_CHANNEL）
#   --since YYYY-MM-DD    この日付以降のみ対象（両スクリプトに渡す）
#   --dry-run             DB保存なし・確認のみ（両スクリプトに渡す）
#   --no-encrypt          平文モード（両スクリプトに渡す）
#   --skip-fetch          Slack API取得をスキップ（slack_pipeline.py のみ）
#   --force-reextract     抽出済みスレッドも再処理（pm_ingest.py slack のみ）
#   --db-slack PATH       Slack DB のパス（デフォルト: data/slack.db）
#   --db-pm PATH          pm.db のパス

set -euo pipefail

. ~/.secrets/slack_tokens.sh
# RiVault トークン（Self-Consistency の embedding 取得に必要）
[ -f ~/.secrets/rivault_tokens.sh ] && . ~/.secrets/rivault_tokens.sh

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
  PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$ARCH" == "x86_64" ]]; then
  PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
  echo "Unknown architecture: $ARCH"; exit 1
fi

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

[ -f ~/.secrets/localLLM.sh ] && . ~/.secrets/localLLM.sh

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
CHANNEL="${PM_DEFAULT_SLACK_CHANNEL:-}"
SINCE=""
DRY_RUN=""
NO_ENCRYPT=""
SKIP_FETCH=""
FORCE_REEXTRACT=""
DB_SLACK=""
DB_PM=""

if [[ -z "$CHANNEL" && "$#" -eq 0 ]]; then
    echo "[ERROR] -c CHANNEL_ID または PM_DEFAULT_SLACK_CHANNEL を指定してください" >&2
    exit 1
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--channel)         CHANNEL="$2";        shift 2 ;;
        --since)              SINCE="$2";           shift 2 ;;
        --dry-run)            DRY_RUN="--dry-run";  shift   ;;
        --no-encrypt)         NO_ENCRYPT="--no-encrypt"; shift ;;
        --skip-fetch)         SKIP_FETCH="--skip-fetch"; shift ;;
        --force-reextract)    FORCE_REEXTRACT="--force-reextract"; shift ;;
        --db-slack)           DB_SLACK="$2";        shift 2 ;;
        --db-pm)              DB_PM="$2";           shift 2 ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "不明なオプション: $1" >&2
            exit 1 ;;
    esac
done

# DB パスのデフォルト
DB_SLACK="${DB_SLACK:-${REPO_ROOT}/data/slack.db}"
DB_PM="${DB_PM:-${REPO_ROOT}/data/pm.db}"

# --------------------------------------------------------------------------- #
# 共通オプション組み立て
# --------------------------------------------------------------------------- #
COMMON_OPTS=()
[[ -n "$SINCE" ]]      && COMMON_OPTS+=(--since "$SINCE")
[[ -n "$DRY_RUN" ]]    && COMMON_OPTS+=("$DRY_RUN")
[[ -n "$NO_ENCRYPT" ]] && COMMON_OPTS+=("$NO_ENCRYPT")

PIPELINE_OPTS=("${COMMON_OPTS[@]}")
[[ -n "$SKIP_FETCH" ]]       && PIPELINE_OPTS+=("$SKIP_FETCH")

EXTRACTOR_OPTS=("${COMMON_OPTS[@]}")
[[ -n "$FORCE_REEXTRACT" ]]  && EXTRACTOR_OPTS+=("--slack-force-reextract")

# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #
# Canvas 上の人手編集を ingest 前に pm.db に取り込む（dry-run 時はスキップ）。
if [[ -z "$DRY_RUN" ]]; then
    # shellcheck source=_lib_sync_canvas.sh
    source "$SCRIPT_DIR/bin/_lib_sync_canvas.sh"
    sync_canvas_before_pm_update "$DB_PM"
    echo ""
fi

echo "================================================================"
echo "ステップ1: Slack取得 (slack_pipeline.py)"
echo "  チャンネル : $CHANNEL"
echo "  Slack DB   : $DB_SLACK"
[[ -n "$SINCE" ]]      && echo "  since      : $SINCE"
[[ -n "$DRY_RUN" ]]    && echo "  dry-run    : on"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/data-pipeline/slack_pipeline.py" \
    -c "$CHANNEL" \
    --db "$DB_SLACK" \
    "${PIPELINE_OPTS[@]}"

echo ""
echo "================================================================"
echo "ステップ2: pm.db抽出 (pm_ingest.py slack)"
echo "  チャンネル : $CHANNEL"
echo "  Slack DB   : $DB_SLACK"
echo "  pm.db      : $DB_PM"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/ingest/pm_ingest.py" slack \
    --slack-channel "$CHANNEL" \
    --slack-db "$DB_SLACK" \
    --db "$DB_PM" \
    "${EXTRACTOR_OPTS[@]}"

echo ""
echo "✓ pm_from_slack.sh 完了"
