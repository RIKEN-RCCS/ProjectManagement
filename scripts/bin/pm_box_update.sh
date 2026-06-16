#!/usr/bin/env bash
# pm_box_update.sh
#
# ドキュメントレジストリの更新と FTS5 インデックスへの組み込みを連続実行する。
#   ステップ1: pm_slack_box_links.py  — Slack BOXリンクを収集・LLMでメタデータ抽出
#   ステップ2: pm_box_crawl.py  — BOXフォルダからドキュメント本文を取得・Markdown変換
#   ステップ3: pm_embed.py            — docs_*.db + box_docs.db を FTS5 インデックスに組み込み
#
# Usage:
#   bash scripts/pm_box_update.sh
#   bash scripts/pm_box_update.sh --index-name pm
#   bash scripts/pm_box_update.sh --dry-run
#
# Options:
#   --index-name NAME     特定インデックスのみ処理（pm / pm-hpc / pm-pmo）
#   -c CHANNEL_ID         特定チャンネルのみ抽出（pm_slack_box_links.py のみ）
#   --dry-run             DB保存なし・確認のみ（全スクリプトに渡す）
#   --force               抽出済み・変換済みも再処理
#   --full-rebuild        FTS5 インデックスを全件再構築（pm_embed.py のみ）
#   --skip-embed          ステップ3（pm_embed.py）をスキップ
#   --skip-box-content    ステップ2（pm_box_crawl.py）をスキップ
#   --since YYYY-MM-DD    この日付以降のメッセージのみ対象（pm_slack_box_links.py のみ）

set -euo pipefail

# box CLI の PATH を通す（cron 環境では .bashrc がロードされないため）
export PATH="$HOME/.nvm_arm64/versions/node/v20.19.5/bin:$PATH"

. ~/.secrets/slack_tokens.sh
[ -f ~/.secrets/localLLM.sh ] && . ~/.secrets/localLLM.sh

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

# LOCAL_LLM_* は ~/.secrets/localLLM.sh で設定（gemma-4 / RiVault 等）

# --------------------------------------------------------------------------- #
# ログ出力先
# --------------------------------------------------------------------------- #
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/pm_box_update.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "======== pm_box_update.sh 開始: $(date '+%Y-%m-%d %H:%M:%S') ========"

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
INDEX_NAME=""
CHANNEL=""
DRY_RUN=""
FORCE=""
FULL_REBUILD=""
SKIP_EMBED=""
SKIP_BOX_CONTENT=""
SINCE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --index-name)        INDEX_NAME="$2";              shift 2 ;;
        -c|--channel)        CHANNEL="$2";                 shift 2 ;;
        --dry-run)           DRY_RUN="--dry-run";          shift   ;;
        --force)             FORCE="--force";              shift   ;;
        --full-rebuild)      FULL_REBUILD="--full-rebuild"; shift   ;;
        --skip-embed)        SKIP_EMBED="1";               shift   ;;
        --skip-box-content)  SKIP_BOX_CONTENT="1";         shift   ;;
        --since)             SINCE="$2";                   shift 2 ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

# --------------------------------------------------------------------------- #
# ステップ1: ドキュメント抽出
# --------------------------------------------------------------------------- #
EXTRACT_OPTS=()
[[ -n "$INDEX_NAME" ]] && EXTRACT_OPTS+=(--index-name "$INDEX_NAME")
[[ -n "$CHANNEL" ]]    && EXTRACT_OPTS+=(-c "$CHANNEL")
[[ -n "$DRY_RUN" ]]    && EXTRACT_OPTS+=("$DRY_RUN")
[[ -n "$FORCE" ]]      && EXTRACT_OPTS+=("$FORCE")
[[ -n "$SINCE" ]]      && EXTRACT_OPTS+=(--since "$SINCE")

echo "================================================================"
echo "ステップ1: BOXリンク抽出 (pm_slack_box_links.py)"
[[ -n "$INDEX_NAME" ]] && echo "  インデックス : $INDEX_NAME"
[[ -n "$CHANNEL" ]]    && echo "  チャンネル   : $CHANNEL"
[[ -n "$SINCE" ]]      && echo "  since        : $SINCE"
[[ -n "$DRY_RUN" ]]    && echo "  dry-run      : on"
[[ -n "$FORCE" ]]      && echo "  force        : on"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/pm_slack_box_links.py" "${EXTRACT_OPTS[@]}"

# --------------------------------------------------------------------------- #
# ステップ2: BOXドキュメント本文抽出
# --------------------------------------------------------------------------- #
if [[ -n "$SKIP_BOX_CONTENT" ]]; then
    echo ""
    echo "ステップ2: pm_box_crawl.py はスキップします (--skip-box-content)"
else
    BOX_OPTS=(--scan --convert)
    [[ -n "$DRY_RUN" ]] && BOX_OPTS+=("$DRY_RUN")
    [[ -n "$FORCE" ]]   && BOX_OPTS+=(--force)

    echo ""
    echo "================================================================"
    echo "ステップ2: BOXドキュメント本文抽出 (pm_box_crawl.py)"
    [[ -n "$DRY_RUN" ]] && echo "  dry-run      : on"
    [[ -n "$FORCE" ]]   && echo "  force        : on"
    echo "================================================================"

    "$PYTHON3" "$SCRIPT_DIR/pm_box_crawl.py" "${BOX_OPTS[@]}"
fi

# --------------------------------------------------------------------------- #
# ステップ3: FTS5 インデックス更新
# --------------------------------------------------------------------------- #
if [[ -n "$SKIP_EMBED" ]]; then
    echo ""
    echo "ステップ3: pm_embed.py はスキップします (--skip-embed)"
    echo ""
    echo "✓ pm_box_update.sh 完了（FTS5未更新）"
    exit 0
fi

EMBED_OPTS=()
[[ -n "$INDEX_NAME" ]]   && EMBED_OPTS+=(--index-name "$INDEX_NAME")
[[ -n "$FULL_REBUILD" ]] && EMBED_OPTS+=("$FULL_REBUILD")
[[ -n "$DRY_RUN" ]]      && EMBED_OPTS+=("$DRY_RUN")

echo ""
echo "================================================================"
echo "ステップ3: FTS5インデックス更新 (pm_embed.py)"
[[ -n "$INDEX_NAME" ]]   && echo "  インデックス : $INDEX_NAME"
[[ -n "$FULL_REBUILD" ]] && echo "  full-rebuild : on"
[[ -n "$DRY_RUN" ]]      && echo "  dry-run      : on"
echo "================================================================"

"$PYTHON3" "$SCRIPT_DIR/pm_embed.py" "${EMBED_OPTS[@]}"

echo ""
echo "✓ pm_box_update.sh 完了"
