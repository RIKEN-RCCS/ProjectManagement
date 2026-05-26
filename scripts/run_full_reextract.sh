#!/bin/bash
# Full Slack re-extraction across all 56 channels in reverse (DESC) order.
# Run sequentially because gemma4 vLLM cannot serve concurrent prompts efficiently.

set -u
cd "$(dirname "$0")/.."

# プロジェクトポリシー: スクリプトは Claude API を呼んではならない。
# ローカル vLLM (gemma4) を明示指定しておき、call_claude() がフォールバックで
# Claude CLI を起動する経路を確実に塞ぐ。
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://localhost:8000/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-dummy}"

PY=~/.venv_aarch64/bin/python3
LOG_DIR=logs
SUMMARY_LOG="$LOG_DIR/slack_reextract_summary.log"
mkdir -p "$LOG_DIR"

CHANNELS=(
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID> <CHANNEL_ID>
    <CHANNEL_ID>
)

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
