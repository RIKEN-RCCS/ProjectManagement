#!/usr/bin/env bash
# pm_web_update.sh
#
# 外部Web情報を取得し data/web_articles.db に保存する（cronドライバ）。
# FTS5 インデックスへの組み込みは pm_document_update.sh（pm_embed.py）が行う。
#
# Usage:
#   bash scripts/pm_web_update.sh
#   bash scripts/pm_web_update.sh --source "Top500"
#   bash scripts/pm_web_update.sh --dry-run
#   bash scripts/pm_web_update.sh --full-refetch
#
# cron設定例（毎朝03:30 JST）:
#   30 3 * * * /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_web_update.sh >> /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_web_cron.log 2>&1

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

"$PYTHON3" "$SCRIPT_DIR/pm_web_fetch.py" "$@"
