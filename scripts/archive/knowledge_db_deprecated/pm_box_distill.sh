#!/bin/bash

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    . "$HOME/.venv_aarch64/bin/activate"
elif [[ "$_arch" == "x86_64" ]]; then
    . "$HOME/.venv_x86_64/bin/activate"
else
    echo "Unknown architecture: $_arch"; exit 1
fi

set -euo pipefail

. $HOME/.secrets/rivault_tokens.sh

YESTERDAY=$(date -d 'yesterday' +%Y-%m-%d)
LOG=/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_box_distill_cron.log

python /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_box_distill.py \
  --source all \
  --since "$YESTERDAY" \
  >> "$LOG" 2>&1
