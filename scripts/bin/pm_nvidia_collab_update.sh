#!/bin/bash
# pm_nvidia_collab_update.sh — アプリケーション評価ステータスレポート 定期更新
#
# /argus-investigate と同じ pm_argus_agent.py --investigate を使って
# アプリごとの調査を実行し、Box に Markdown レポートとして保存する。
#
# 使い方:
#   bash scripts/bin/pm_nvidia_collab_update.sh
#
# cron 登録例（毎週月曜 10:00）:
#   0 10 * * 1 cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement \
#     && bash scripts/bin/pm_nvidia_collab_update.sh >> logs/app_status_report.log 2>&1

set -euo pipefail

cd "$(dirname "$0")/../.."
REPO_ROOT=$(pwd)

# ── 環境変数 ──
source ~/.secrets/slack_tokens.sh
source ~/.secrets/pm_tokens.sh 2>/dev/null || true
source ~/.secrets/rivault_tokens.sh 2>/dev/null || true
if [ -z "${PM_DB_KEY:-}" ] && [ -f ~/.secrets/pm_db_key.txt ]; then
  export PM_DB_KEY="$(cat ~/.secrets/pm_db_key.txt)"
fi
# claude_code ルートを通すため ANTHROPIC_BASE_URL が必要
if [ -f ~/.claude/settings.json ]; then
  export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-$(python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json'))['env']['ANTHROPIC_BASE_URL'])" 2>/dev/null)}"
  export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$(python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json'))['env']['ANTHROPIC_AUTH_TOKEN'])" 2>/dev/null)}"
fi

ANTHROPIC_BASE_URL="http://localhost:8001"
ANTHROPIC_AUTH_TOKEN="dummy"

LOG_FILE="${REPO_ROOT}/logs/app_status_report.log"
mkdir -p "${REPO_ROOT}/logs"

# ── 調査対象アプリ一覧 ──
APPS=(
  "GENESIS|GENESIS"
  "LQCD-DWF-HMC|LQCD-DWF-HMC"
  "SCALE-LETKF|SCALE-LETKF"
  "E-Wave|E-Wave"
  "SALMON|SALMON"
  "FrontFlow/blue|FrontFlowblue"
)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting per-app investigation (${#APPS[@]} apps)..." >> "$LOG_FILE"

for APP_ENTRY in "${APPS[@]}"; do
  IFS='|' read -r QUERY_NAME FILE_NAME <<< "$APP_ENTRY"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Investigating ${QUERY_NAME} ===" >> "$LOG_FILE"

  TMP_OUTPUT=$(mktemp "/tmp/argus_report_${FILE_NAME}_XXXXXXXX.md")

  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 \
    "${REPO_ROOT}/scripts/argus/pm_argus_agent.py" \
    --investigate "${QUERY_NAME} の\
GPU化・性能評価・ベンダー協業・アーキテクチャ連携の進捗を整理してレポートとしてまとめてください。\
集められた事実に基づいた分析を実施し、推測は含めないでください。\
${QUERY_NAME} 以外のアプリケーションの情報は含めないでください。\
マイルストーンとの連携、アクションアイテムの消化状況は未整備のためレポートに含めないでください。\
レポートは煩雑にならないように表は使わずコンパクトにまとめてください。"\
    --max-steps 10 \
    --since 2026-03-01 \
    2>> "$LOG_FILE" \
    > "$TMP_OUTPUT"

  # Box にアップロード（ファイル名に FILE_NAME を含める）
  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 -c "
import sys; sys.path.insert(0, '${REPO_ROOT}/scripts')
from argus.output_tools import box_upload_file
with open('$TMP_OUTPUT') as f:
    content = f.read()
result = box_upload_file('$TMP_OUTPUT', filename='argus_report_${FILE_NAME}_$(date +%Y-%m-%d).md')
print(result)
" 2>> "$LOG_FILE"

  rm -f "$TMP_OUTPUT"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Done ${FILE_NAME} ===" >> "$LOG_FILE"
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] All ${#APPS[@]} applications completed." >> "$LOG_FILE"
