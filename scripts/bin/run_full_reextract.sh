#!/bin/bash
# Full Slack re-extraction across all channels in reverse (DESC) order.
# Run sequentially because gemma4 vLLM cannot serve concurrent prompts efficiently.
#
# チャンネル一覧の出典は argus_config.yaml の indices.pm-all.channels
# （pm_from_slack_daily.sh と同じ出典）。

set -u
cd "$(dirname "$0")/.."

# プロジェクトポリシー: スクリプトは Claude API を呼んではならない。
# ローカル vLLM (gemma4) を明示指定しておき、call_claude() がフォールバックで
# Claude CLI を起動する経路を確実に塞ぐ。
if [[ -f ~/.secrets/localLLM.sh ]]; then
    source ~/.secrets/localLLM.sh
fi

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    PY="$HOME/.venv_aarch64/bin/python3"
elif [[ "$_arch" == "x86_64" ]]; then
    PY="$HOME/.venv_x86_64/bin/python3"
else
    echo "Unknown architecture: $_arch"; exit 1
fi
LOG_DIR=logs
SUMMARY_LOG="$LOG_DIR/slack_reextract_summary.log"
mkdir -p "$LOG_DIR"

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$_BASH_SELF_DIR/../.." && pwd)"

mapfile -t CHANNELS < <("$PY" -c "
import sys
import yaml
from pathlib import Path
cfg_path = Path('$REPO_ROOT/data/argus_config.yaml')
if not cfg_path.exists():
    sys.exit(1)
cfg = yaml.safe_load(cfg_path.read_text()) or {}
for ch in (cfg.get('indices', {}).get('pm-all', {}).get('channels') or []):
    print(ch)
")
if [[ ${#CHANNELS[@]} -eq 0 ]]; then
    echo "[ERROR] argus_config.yaml の indices.pm-all.channels からチャンネル一覧を取得できませんでした" >&2
    exit 1
fi

START=$(date +%s)
echo "[$(date -Iseconds)] START full re-extraction (${#CHANNELS[@]} channels)" | tee -a "$SUMMARY_LOG"

for ch in "${CHANNELS[@]}"; do
    log="$LOG_DIR/slack_reextract_${ch}.log"
    t0=$(date +%s)
    echo "[$(date -Iseconds)] BEGIN $ch -> $log" | tee -a "$SUMMARY_LOG"
    "$PY" scripts/ingest/pm_ingest.py slack \
        --slack-channel "$ch" \
        --slack-force-reextract \
        > "$log" 2>&1
    rc=$?
    t1=$(date +%s)
    echo "[$(date -Iseconds)] END   $ch rc=$rc elapsed=$((t1-t0))s" | tee -a "$SUMMARY_LOG"
done

END=$(date +%s)
echo "[$(date -Iseconds)] DONE total=$((END-START))s" | tee -a "$SUMMARY_LOG"
