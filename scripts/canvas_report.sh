#!/usr/bin/env bash
# canvas_report.sh
#
# Canvas同期 (pm_sync_canvas.py) → PMレポート生成・投稿 (pm_report.py) を順次実行する。
# pm_report.py の前に必ず Canvas同期を実施することで、Canvas上の編集内容を上書きする事故を防ぐ。
#
# Usage:
#   bash scripts/canvas_report.sh --db data/pm.db --canvas-id F0AAD2494VB
#   bash scripts/canvas_report.sh --db data/pm.db --canvas-id F0AAD2494VB --since 2026-03-01
#   bash scripts/canvas_report.sh --db data/pm.db --canvas-id F0AAD2494VB --dry-run
#   bash scripts/canvas_report.sh --db data/pm.db --canvas-id F0AAD2494VB --skip-sync
#
# Options:
#   --db PATH              pm.db のパス（必須）
#   --canvas-id ID         Canvas ID（必須）
#   --since YYYY-MM-DD     レポートの対象期間（pm_report.py にのみ渡す）
#   --dry-run              両スクリプトを --dry-run で実行
#   --no-encrypt           平文モード
#   --output PATH          レポートをファイルにも保存（pm_report.py にのみ渡す）
#   --show-acknowledged    確認済み決定事項も表示（pm_report.py にのみ渡す）
#   --skip-sync            Canvas同期をスキップして pm_report.py のみ実行
#   --skip-canvas          Canvas投稿をスキップ（pm_report.py にのみ渡す）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON3="${HOME}/.venv_x86_64/bin/python3"

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
DB=""
CANVAS_ID=""
SINCE=""
DRY_RUN=""
NO_ENCRYPT=""
OUTPUT=""
SHOW_ACKNOWLEDGED=""
SKIP_SYNC=false
SKIP_CANVAS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)                DB="$2";                    shift 2 ;;
        --canvas-id)         CANVAS_ID="$2";             shift 2 ;;
        --since)             SINCE="$2";                 shift 2 ;;
        --dry-run)           DRY_RUN="--dry-run";        shift   ;;
        --no-encrypt)        NO_ENCRYPT="--no-encrypt";  shift   ;;
        --output)            OUTPUT="$2";                shift 2 ;;
        --show-acknowledged) SHOW_ACKNOWLEDGED="--show-acknowledged"; shift ;;
        --skip-sync)         SKIP_SYNC=true;             shift   ;;
        --skip-canvas)       SKIP_CANVAS="--skip-canvas"; shift  ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$DB" ]]; then
    echo "[ERROR] --db が未指定です。" >&2
    echo "  例: --db data/pm.db / --db data/pm-hpc.db / --db data/pm-bmt.db" >&2
    exit 1
fi
if [[ -z "$CANVAS_ID" ]]; then
    echo "[ERROR] --canvas-id が未指定です。" >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# オプション組み立て
# --------------------------------------------------------------------------- #
SYNC_ARGS=(--db "$DB" --canvas-id "$CANVAS_ID")
[[ -n "$DRY_RUN" ]]    && SYNC_ARGS+=("$DRY_RUN")
[[ -n "$NO_ENCRYPT" ]] && SYNC_ARGS+=("$NO_ENCRYPT")

REPORT_ARGS=(--db "$DB" --canvas-id "$CANVAS_ID")
[[ -n "$SINCE" ]]             && REPORT_ARGS+=(--since "$SINCE")
[[ -n "$DRY_RUN" ]]           && REPORT_ARGS+=("$DRY_RUN")
[[ -n "$NO_ENCRYPT" ]]        && REPORT_ARGS+=("$NO_ENCRYPT")
[[ -n "$OUTPUT" ]]            && REPORT_ARGS+=(--output "$OUTPUT")
[[ -n "$SHOW_ACKNOWLEDGED" ]] && REPORT_ARGS+=("$SHOW_ACKNOWLEDGED")
[[ -n "$SKIP_CANVAS" ]]       && REPORT_ARGS+=("$SKIP_CANVAS")

# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #
if ! $SKIP_SYNC; then
    echo "================================================================"
    echo "ステップ1: Canvas同期 (pm_sync_canvas.py)"
    echo "  db        : $DB"
    echo "  canvas-id : $CANVAS_ID"
    [[ -n "$DRY_RUN" ]] && echo "  dry-run   : on"
    echo "================================================================"

    "$PYTHON3" "$SCRIPT_DIR/pm_sync_canvas.py" "${SYNC_ARGS[@]}"
    echo ""
fi

echo "================================================================"
echo "ステップ2: PMレポート生成・投稿 (pm_report.py)"
echo "  db        : $DB"
echo "  canvas-id : $CANVAS_ID"
[[ -n "$SINCE" ]]       && echo "  since     : $SINCE"
[[ -n "$DRY_RUN" ]]     && echo "  dry-run   : on"
[[ -n "$SKIP_CANVAS" ]] && echo "  skip-canvas: on"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/pm_report.py" "${REPORT_ARGS[@]}"

echo ""
echo "✓ canvas_report.sh 完了"
