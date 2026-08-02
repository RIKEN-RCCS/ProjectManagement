#!/usr/bin/env bash
# pm_selfcheck.sh
#
# pm_selfcheck.py（データ不変条件の読み取り専用検査）を定期実行するラッパー。
# DB書き込みは行わない。違反を検出した場合は非0で終了する
# （cron のメール通知で気づけるようにするため、set -e は使わずそのまま伝搬する）。
#
# Usage:
#   bash scripts/bin/pm_selfcheck.sh
#
# cron登録例（毎朝06:30 JST、月〜金）:
#   30 6 * * 1-5 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/bin/pm_selfcheck.sh

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

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/pm_selfcheck.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "======== pm_selfcheck.sh 開始: $(date '+%Y-%m-%d %H:%M:%S') ========"

# モデル pin のドリフト検査（model_pin_drift）がエンドポイントへ到達するために必要。
# source しないと常に判定不能になり、検査が入っているのに何も見ていない状態になる。
[[ -f "$HOME/.secrets/rikyu_token.sh" ]] && source "$HOME/.secrets/rikyu_token.sh"
[[ -f "$HOME/.secrets/localLLM.sh" ]] && source "$HOME/.secrets/localLLM.sh"
[[ -f "$HOME/.secrets/rivault_tokens.sh" ]] && source "$HOME/.secrets/rivault_tokens.sh"

"$PYTHON3" "$SCRIPT_DIR/quality/pm_selfcheck.py" --days 7 "$@"
STATUS=$?

echo "======== pm_selfcheck.sh 完了: $(date '+%Y-%m-%d %H:%M:%S') (exit=$STATUS) ========"
exit "$STATUS"
