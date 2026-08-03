#!/bin/bash

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    . "$HOME/.venv_aarch64/bin/activate"
elif [[ "$_arch" == "x86_64" ]]; then
    . "$HOME/.venv_x86_64/bin/activate"
else
    echo "Unknown architecture: $_arch"; exit 1
fi

# net_guard（外向き通信の allow-list、docs/security-architecture.md §4.7 層1）を
# enforce にする。2026-08-02 の観測修正後、cron 5 本すべてで宛先が記録されるように
# なり deny 0 件を確認した上で展開した。退避が必要なときは
# ARGUS_NETGUARD=warn を付けて実行する。
export ARGUS_NETGUARD="${ARGUS_NETGUARD:-enforce}"

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
