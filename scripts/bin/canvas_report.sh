#!/usr/bin/env bash
# canvas_report.sh
#
# XLSX 同期 (pm_xlsx_sync.py) → XLSX レポート生成・Box アップロード・
# Canvas 目録投稿 (pm_xlsx_report.py) を順次実行する。
# pm_xlsx_report.py の前に必ず Box XLSX → pm.db 同期を実施することで、
# XLSX 上の人手編集を上書きする事故を防ぐ。
#
# Usage:
#   bash scripts/canvas_report.sh
#   bash scripts/canvas_report.sh --since 2026-03-01
#   bash scripts/canvas_report.sh --filter リーダー会議系 --filter HPCアプリケーションWG系
#   bash scripts/canvas_report.sh --dry-run
#   bash scripts/canvas_report.sh --skip-sync
#
# Options:
#   --db PATH              pm.db のパス（デフォルト: data/pm.db）
#   --canvas-id ID         Canvas ID（未指定時は argus_config.yaml の report.canvas_id）
#   --box-folder-id ID     Box folder ID（未指定時は argus_config.yaml の report.box_folder_id）
#   --filename NAME        Box 上のファイル名（デフォルト: pm_report.xlsx）
#   --xlsx-out PATH        ローカル XLSX 出力先（任意）
#   --since YYYY-MM-DD     レポートの対象期間
#   --filter PRESET        フィルタプリセット（複数指定可）
#   --dry-run              両スクリプトを --dry-run で実行
#   --no-encrypt           平文モード
#   --show-acknowledged    確認済み決定事項も表示
#   --skip-sync            XLSX → pm.db 同期をスキップ
#   --skip-upload          Box アップロードをスキップ
#   --skip-canvas          Canvas 目録投稿をスキップ

set -euo pipefail

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    . "$HOME/.venv_aarch64/bin/activate"
elif [[ "$_arch" == "x86_64" ]]; then
    . "$HOME/.venv_x86_64/bin/activate"
else
    echo "Unknown architecture: $_arch"; exit 1
fi

. ~/.secrets/slack_tokens.sh

# cron 実行時に box CLI (Node 製) が見つかるよう PATH を補う
export PATH="$HOME/.nvm_arm64/versions/node/v20.19.5/bin:$PATH"

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
PYTHON3="${HOME}/.venv_$(uname -m)/bin/python3"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# --------------------------------------------------------------------------- #
# ログ出力先
# --------------------------------------------------------------------------- #
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/canvas_report.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "======== canvas_report.sh 開始: $(date '+%Y-%m-%d %H:%M:%S') ========"

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
DB=""
CANVAS_ID=""
BOX_FOLDER_ID=""
FILENAME=""
XLSX_OUT=""
SINCE=""
DRY_RUN=""
NO_ENCRYPT=""
SHOW_ACKNOWLEDGED=""
SKIP_SYNC=false
SKIP_UPLOAD=""
SKIP_CANVAS=""
FILTERS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)                DB="$2";                    shift 2 ;;
        --canvas-id)         CANVAS_ID="$2";             shift 2 ;;
        --box-folder-id)     BOX_FOLDER_ID="$2";         shift 2 ;;
        --filename)          FILENAME="$2";              shift 2 ;;
        --xlsx-out)          XLSX_OUT="$2";              shift 2 ;;
        --since)             SINCE="$2";                 shift 2 ;;
        --filter)            FILTERS+=("$2");            shift 2 ;;
        --dry-run)           DRY_RUN="--dry-run";        shift   ;;
        --no-encrypt)        NO_ENCRYPT="--no-encrypt";  shift   ;;
        --show-acknowledged) SHOW_ACKNOWLEDGED="--show-acknowledged"; shift ;;
        --skip-sync)         SKIP_SYNC=true;             shift   ;;
        --skip-upload)       SKIP_UPLOAD="--skip-upload"; shift  ;;
        --skip-canvas)       SKIP_CANVAS="--skip-canvas"; shift  ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

# --------------------------------------------------------------------------- #
# オプション組み立て
# --------------------------------------------------------------------------- #
SYNC_ARGS=()
[[ -n "$DB" ]]            && SYNC_ARGS+=(--db "$DB")
[[ -n "$BOX_FOLDER_ID" ]] && SYNC_ARGS+=(--box-folder-id "$BOX_FOLDER_ID")
[[ -n "$FILENAME" ]]      && SYNC_ARGS+=(--filename "$FILENAME")
[[ -n "$DRY_RUN" ]]       && SYNC_ARGS+=("$DRY_RUN")
[[ -n "$NO_ENCRYPT" ]]    && SYNC_ARGS+=("$NO_ENCRYPT")

REPORT_ARGS=()
[[ -n "$DB" ]]                && REPORT_ARGS+=(--db "$DB")
[[ -n "$CANVAS_ID" ]]         && REPORT_ARGS+=(--canvas-id "$CANVAS_ID")
[[ -n "$BOX_FOLDER_ID" ]]     && REPORT_ARGS+=(--box-folder-id "$BOX_FOLDER_ID")
[[ -n "$FILENAME" ]]          && REPORT_ARGS+=(--filename "$FILENAME")
[[ -n "$XLSX_OUT" ]]          && REPORT_ARGS+=(--xlsx-out "$XLSX_OUT")
[[ -n "$SINCE" ]]             && REPORT_ARGS+=(--since "$SINCE")
[[ -n "$DRY_RUN" ]]           && REPORT_ARGS+=("$DRY_RUN")
[[ -n "$NO_ENCRYPT" ]]        && REPORT_ARGS+=("$NO_ENCRYPT")
[[ -n "$SHOW_ACKNOWLEDGED" ]] && REPORT_ARGS+=("$SHOW_ACKNOWLEDGED")
[[ -n "$SKIP_UPLOAD" ]]       && REPORT_ARGS+=("$SKIP_UPLOAD")
[[ -n "$SKIP_CANVAS" ]]       && REPORT_ARGS+=("$SKIP_CANVAS")
if [[ ${#FILTERS[@]} -gt 0 ]]; then
    for f in "${FILTERS[@]}"; do
        REPORT_ARGS+=(--filter "$f")
    done
fi

# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #
if ! $SKIP_SYNC; then
    echo "================================================================"
    echo "ステップ1: XLSX → pm.db 同期 (pm_xlsx_sync.py)"
    [[ -n "$DB" ]]      && echo "  db        : $DB"
    [[ -n "$DRY_RUN" ]] && echo "  dry-run   : on"
    echo "================================================================"

    if [[ ${#SYNC_ARGS[@]} -gt 0 ]]; then
        "$PYTHON3" "$SCRIPT_DIR/reporting/pm_xlsx_sync.py" "${SYNC_ARGS[@]}"
    else
        "$PYTHON3" "$SCRIPT_DIR/reporting/pm_xlsx_sync.py"
    fi
    echo ""
fi

echo "================================================================"
echo "ステップ2: XLSX レポート生成・Box アップロード・Canvas 目録投稿 (pm_xlsx_report.py)"
[[ -n "$DB" ]]          && echo "  db          : $DB"
[[ -n "$SINCE" ]]       && echo "  since       : $SINCE"
[[ ${#FILTERS[@]} -gt 0 ]] && echo "  filter      : ${FILTERS[*]}"
[[ -n "$DRY_RUN" ]]     && echo "  dry-run     : on"
[[ -n "$SKIP_UPLOAD" ]] && echo "  skip-upload : on"
[[ -n "$SKIP_CANVAS" ]] && echo "  skip-canvas : on"
echo "================================================================"

if [[ ${#REPORT_ARGS[@]} -gt 0 ]]; then
    "$PYTHON3" "$SCRIPT_DIR/reporting/pm_xlsx_report.py" "${REPORT_ARGS[@]}"
else
    "$PYTHON3" "$SCRIPT_DIR/reporting/pm_xlsx_report.py"
fi

echo ""
echo "✓ canvas_report.sh 完了"
