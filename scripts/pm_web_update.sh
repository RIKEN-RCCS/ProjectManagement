#!/usr/bin/env bash
# pm_web_update.sh
#
# 外部Web情報の取得と FTS5 インデックスへの組み込みを連続実行する。
#   ステップ1: pm_web_fetch.py  — web_sources.yaml のソースから記事を取得・保存
#   ステップ2: pm_embed.py --web-only — web_articles.db を FTS5 インデックスに組み込み
#
# Usage:
#   bash scripts/pm_web_update.sh
#   bash scripts/pm_web_update.sh --source "Top500"
#   bash scripts/pm_web_update.sh --dry-run
#
# Options:
#   --source NAME         特定ソースのみ取得（web_sources.yaml の name 値）
#   --index-name NAME     特定インデックスのみ embed（pm / pm-hpc / pm-bmt / pm-pmo）
#   --dry-run             DB保存なし・確認のみ（両スクリプトに渡す）
#   --full-refetch        全件再取得（既存URLも上書き）
#   --full-rebuild        FTS5 インデックスを全件再構築（pm_embed.py のみ）
#   --skip-embed          ステップ2（pm_embed.py）をスキップ

set -euo pipefail

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
    PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$ARCH" == "x86_64" ]]; then
    PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
    echo "Unknown architecture: $ARCH"; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOG_DIR/pm_web_update_${TIMESTAMP}.log"

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
SOURCE=""
INDEX_NAME=""
DRY_RUN=""
FULL_REFETCH=""
FULL_REBUILD=""
SKIP_EMBED=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)         SOURCE="$2";                  shift 2 ;;
        --index-name)     INDEX_NAME="$2";              shift 2 ;;
        --dry-run)        DRY_RUN="--dry-run";          shift   ;;
        --full-refetch)   FULL_REFETCH="--full-refetch"; shift  ;;
        --full-rebuild)   FULL_REBUILD="--full-rebuild"; shift  ;;
        --skip-embed)     SKIP_EMBED="1";               shift   ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

# ログファイルにも出力
exec > >(tee -a "$LOG_FILE") 2>&1

# --------------------------------------------------------------------------- #
# ステップ1: Web記事取得
# --------------------------------------------------------------------------- #
FETCH_OPTS=()
[[ -n "$SOURCE" ]]       && FETCH_OPTS+=(--source "$SOURCE")
[[ -n "$DRY_RUN" ]]      && FETCH_OPTS+=("$DRY_RUN")
[[ -n "$FULL_REFETCH" ]] && FETCH_OPTS+=("$FULL_REFETCH")

echo "================================================================"
echo "ステップ1: Web記事取得 (pm_web_fetch.py)"
[[ -n "$SOURCE" ]]       && echo "  ソース      : $SOURCE"
[[ -n "$DRY_RUN" ]]      && echo "  dry-run     : on"
[[ -n "$FULL_REFETCH" ]] && echo "  full-refetch: on"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/pm_web_fetch.py" "${FETCH_OPTS[@]}"

# --------------------------------------------------------------------------- #
# ステップ2: FTS5 インデックス更新
# --------------------------------------------------------------------------- #
if [[ -n "$SKIP_EMBED" ]]; then
    echo ""
    echo "ステップ2: pm_embed.py はスキップします (--skip-embed)"
    echo ""
    echo "✓ pm_web_update.sh 完了（FTS5未更新）"
    exit 0
fi

EMBED_OPTS=(--web-only)
[[ -n "$INDEX_NAME" ]]   && EMBED_OPTS+=(--index-name "$INDEX_NAME")
[[ -n "$FULL_REBUILD" ]] && EMBED_OPTS+=("$FULL_REBUILD")
[[ -n "$DRY_RUN" ]]      && EMBED_OPTS+=("$DRY_RUN")

echo ""
echo "================================================================"
echo "ステップ2: FTS5インデックス更新 (pm_embed.py --web-only)"
[[ -n "$INDEX_NAME" ]]   && echo "  インデックス : $INDEX_NAME"
[[ -n "$FULL_REBUILD" ]] && echo "  full-rebuild : on"
[[ -n "$DRY_RUN" ]]      && echo "  dry-run      : on"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/pm_embed.py" "${EMBED_OPTS[@]}"

echo ""
echo "✓ pm_web_update.sh 完了"
echo "  ログ: $LOG_FILE"
