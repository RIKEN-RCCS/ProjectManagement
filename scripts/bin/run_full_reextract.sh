#!/bin/bash
# Full Slack re-extraction across all 56 channels in reverse (DESC) order.
# Run sequentially because gemma4 vLLM cannot serve concurrent prompts efficiently.

set -u
cd "$(dirname "$0")/.."

# プロジェクトポリシー: スクリプトは Claude API を呼んではならない。
# ローカル vLLM (gemma4) を明示指定しておき、call_claude() がフォールバックで
# Claude CLI を起動する経路を確実に塞ぐ。
export LOCAL_LLM_URL="${LOCAL_LLM_URL:-http://localhost:8000/v1}"
export LOCAL_LLM_TOKEN="${LOCAL_LLM_TOKEN:-dummy}"

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

CHANNELS=(
    C0AU688SQFL C0AS2JKS200 C0A9KG036CS C0A6AC59AHM C0A5MRRP268
    C0A1H7EF324 C0A1H6PP82C C0A11R260JV C0A11QXGLKT C0A0LS4C1UN
    C0A0KEMJM29 C0A0GG4ULLT C0A07EAKKSB C09MDALKEUQ C09JMEA157E
    C09FFN6725N C09EJLFES11 C09DUURNB47 C09DMJ5P5J4 C09DMHK10C8
    C09DMHJA5MW C09D7GK0QSV C09CYEV4BV2 C09CVJK9TNC C09CUHNRW6A
    C09CUH5RTP0 C09CUH37NTG C09CUH1SSBY C09CTDXFK4J C09CS0JFVL5
    C09CPFSJG67 C09CE3C3C4X C09CE38SDFZ C09AANCC649 C099LH46K36
    C097A2P387R C096ER1A0LU C094Z4XKYGG C094CTQUXRS C094CTHUPTN
    C094C73FSKB C094ARMCHK4 C0949U7983X C0949TWGMFX C0949TUE33P
    C094715A23Y C093Y781T1V C093LP1J15G C093DQFSCRH C0936JBQVGQ
    C08SXA4M7JT C08PE3K9N72 C08MJ0NF5UZ C08M0249GRL C08M002D7TQ
    C08LSJP4R6K
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
