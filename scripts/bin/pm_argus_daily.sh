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

# Claude 設定から claude_code ルート向け環境変数を読み出し
if [ -f ~/.claude/settings.json ]; then
  export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-$(python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json'))['env']['ANTHROPIC_BASE_URL'])" 2>/dev/null)}"
  export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$(python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json'))['env']['ANTHROPIC_AUTH_TOKEN'])" 2>/dev/null)}"
fi

BASEDIR="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement"
python3 "$BASEDIR/scripts/argus/pm_argus.py" --brief-to-canvas --canvas-id F0ATCN7E2D9 >> $LOGFILE 2>&1
python3 "$BASEDIR/scripts/argus/pm_argus.py" --risk --canvas-id F0ATN63JQV7 >> $LOGFILE 2>&1
