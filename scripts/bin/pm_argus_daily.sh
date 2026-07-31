#!/bin/bash

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    . "$HOME/.venv_aarch64/bin/activate"
elif [[ "$_arch" == "x86_64" ]]; then
    . "$HOME/.venv_x86_64/bin/activate"
else
    echo "Unknown architecture: $_arch"; exit 1
fi

LOGFILE="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_argus_daily_$(date +%Y%m%d_%H%M%S).log"

touch $LOGFILE

source "$HOME/.secrets/slack_tokens.sh"
source "$HOME/.secrets/rivault_tokens.sh"
source "$HOME/.secrets/localLLM.sh"

BASEDIR="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement"
# Canvas ID は argus_config.yaml の argus_daily.brief_canvas_id / risk_canvas_id から解決される
# （pm_argus.py の resolve_brief_canvas_id() / resolve_risk_canvas_id() 経由）
python3 "$BASEDIR/scripts/argus/pm_argus.py" --brief-to-canvas >> $LOGFILE 2>&1
python3 "$BASEDIR/scripts/argus/pm_argus.py" --risk >> $LOGFILE 2>&1
