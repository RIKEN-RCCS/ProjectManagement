#!/bin/bash
# Argus Patrol Agent — cron から30分間隔で呼ばれる巡回ラッパー

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    . "$HOME/.venv_aarch64/bin/activate"
elif [[ "$_arch" == "x86_64" ]]; then
    . "$HOME/.venv_x86_64/bin/activate"
else
    echo "Unknown architecture: $_arch"; exit 1
fi

BASEDIR="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement"
LOGFILE="$BASEDIR/logs/pm_argus_patrol.log"
LOCKFILE="$BASEDIR/data/.pm_argus_patrol.lock"

# cron 実行時に box CLI (Node 製) が見つかるよう PATH を補う
# （自動クローズ後の XLSX 再エクスポート = xlsx_sync 巻き戻り防止の生命線。
#   これが無いと再エクスポートが FileNotFoundError で毎回失敗する）
export PATH="$HOME/.nvm_arm64/versions/node/v20.19.5/bin:$PATH"
command -v box >/dev/null 2>&1 || echo "[WARN] box CLI が PATH に見つかりません（nvm の node バージョン変更を確認）" >&2

source "$HOME/.secrets/slack_tokens.sh"
source "$HOME/.secrets/rivault_tokens.sh"
source "$HOME/.secrets/localLLM.sh"

# LLM 判定で1回の巡回が長引いた場合に次回起動と重ならないようにする
exec flock -n "$LOCKFILE" \
    python3 -u "$BASEDIR/scripts/argus/pm_argus_patrol.py" >> "$LOGFILE" 2>&1
