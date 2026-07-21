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

source "$HOME/.secrets/slack_tokens.sh"
source "$HOME/.secrets/rivault_tokens.sh"
source "$HOME/.secrets/localLLM.sh"

# LLM 判定で1回の巡回が長引いた場合に次回起動と重ならないようにする
exec flock -n "$LOCKFILE" \
    python3 -u "$BASEDIR/scripts/argus/pm_argus_patrol.py" >> "$LOGFILE" 2>&1
