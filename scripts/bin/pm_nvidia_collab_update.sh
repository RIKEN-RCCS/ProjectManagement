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
LOG_FILE="${REPO_ROOT}/logs/app_status_report.log"
mkdir -p "${REPO_ROOT}/logs"

# エグゼクティブサマリー生成用に、各アプリの日本語レポートを一時保存しておく
SUMMARY_WORKDIR=$(mktemp -d "/tmp/argus_exec_summary_XXXXXXXX")
trap 'rm -rf "$SUMMARY_WORKDIR"' EXIT

# ── 調査対象アプリ一覧 ──
APPS=(
  "GENESIS|GENESIS"
  "LQCD-DWF-HMC|LQCD-DWF-HMC"
  "SCALE-LETKF|SCALE-LETKF"
  "E-Wave|E-Wave"
  "SALMON|SALMON"
  "FrontFlow/blue|FrontFlowblue"
)
#APPS=("SCALE-LETKF|SCALE-LETKF")

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting per-app investigation (${#APPS[@]} apps)..." >> "$LOG_FILE"

for APP_ENTRY in "${APPS[@]}"; do
  IFS='|' read -r QUERY_NAME FILE_NAME <<< "$APP_ENTRY"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Investigating ${QUERY_NAME} ===" >> "$LOG_FILE"

  TMP_NATURE=$(mktemp "/tmp/argus_nature_${FILE_NAME}_XXXXXXXX.md")
  TMP_OUTPUT=$(mktemp "/tmp/argus_report_${FILE_NAME}_XXXXXXXX.md")

  # ── Pass 1: アプリケーションの性質調査 ──
  # 評価状況・GPU化進捗・ベンダー協業は調べず、計算科学的な特性のみを収集する
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Pass 1] ${QUERY_NAME} の性質調査中..." >> "$LOG_FILE"
  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 \
    "${REPO_ROOT}/scripts/argus/pm_argus_agent.py" \
    --investigate "${QUERY_NAME} はどのようなアプリケーションか。\
計算科学的な性質（解くべき問題・支配方程式・計算カーネル・メモリアクセスパターン・\
通信パターン・典型的なボトルネック・並列化の方式等）だけを調べてください。\
アプリケーションの性質のみを簡潔に箇条書きで500文字程度で出力してください。\
その他のコデザインとの連携などの阿久里ケーションの性質とは関係のない項目は出力しないでください。"\
    --no-intent-header \
    --max-steps 5 \
    --since 2024-01-01 \
    2>> "$LOG_FILE" \
    > "$TMP_NATURE"

  # ── Pass 2: 詳細分析（Pass 1 の結果を背景情報として注入）──
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Pass 2] ${QUERY_NAME} の詳細分析中..." >> "$LOG_FILE"
  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 \
    "${REPO_ROOT}/scripts/argus/pm_argus_agent.py" \
    --investigate "${QUERY_NAME} の\
GPU化・性能評価・ベンダー協業・アーキテクチャ連携の進捗を整理してレポートとしてまとめてください。\
集められた事実に基づいた分析を実施し、推測は含めないでください。\
作業・タスクが「完了した」「達成した」と記述する場合は、完了を示す具体的な証拠（完了報告・確認メッセージ・日付・担当者等）の出典を引用すること。\
証拠が見つからない場合は「進行中」「未確認」と記述し、完了と推測してはならない。\
マイルストーンとの連携、アクションアイテムの消化状況は未整備のためレポートに含めないでください。\
レポートのMarkdown構造は以下を厳守すること：\
冒頭1行目は「# ${QUERY_NAME}」（アプリ名のみ、他の前置きは書かない）。\
メインセクションは「## 1. 〜」「## 2. 〜」のように1から連番の番号付き見出し。\
サブセクションは「### 〜」（番号なし）。\
表は使わずコンパクトにまとめること。"\
    --context-file "$TMP_NATURE" \
    --no-intent-header \
    --max-steps 15 \
    --since 2026-03-01 \
    2>> "$LOG_FILE" \
    > "$TMP_OUTPUT"

#検索結果に他のアプリ名が含まれている場合はその部分を無視し、${QUERY_NAME} に直接言及している内容のみを根拠として使うこと。\
#${QUERY_NAME} 以外のアプリケーション（GENESIS・LQCD-DWF-HMC・SCALE-LETKF・E-Wave・SALMON・FrontFlow/blue・FFVHC-ACE 等）の情報は含めないでください。\
#方針変更・合意の撤回・新しい決定事項（ツール不使用の合意・計画断念・入力取得完了等）についても積極的に検索し、最新の状態をレポートに反映すること。\
  rm -f "$TMP_NATURE"

  # エグゼクティブサマリー生成用に日本語レポートを保全（Box アップロード後に削除されるため）
  cp "$TMP_OUTPUT" "${SUMMARY_WORKDIR}/${FILE_NAME}.md"

  # Box にアップロード（日本語版）
  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 -c "
import sys; sys.path.insert(0, '${REPO_ROOT}/scripts')
from argus.output_tools import box_upload_file
result = box_upload_file('$TMP_OUTPUT', filename='argus_report_${FILE_NAME}_$(date +%Y-%m-%d).md')
print('JP:', result)
" 2>> "$LOG_FILE"

  # 英訳 → Box アップロード（失敗しても日本語版には影響しない）
  TMP_OUTPUT_EN="${TMP_OUTPUT%.*}_EN.${TMP_OUTPUT##*.}"
  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 -c "
import sys; sys.path.insert(0, '${REPO_ROOT}/scripts')
from argus.output_tools import box_upload_file, translate_markdown_jp_to_en
try:
    with open('$TMP_OUTPUT') as f:
        jp_content = f.read()
    en_content = translate_markdown_jp_to_en(jp_content)
    with open('$TMP_OUTPUT_EN', 'w') as f:
        f.write(en_content)
    result = box_upload_file('$TMP_OUTPUT_EN', filename='argus_report_${FILE_NAME}_$(date +%Y-%m-%d)_EN.md')
    print('EN:', result)
except Exception as e:
    print(f'EN translation failed (non-fatal): {e}')
" 2>> "$LOG_FILE"

  rm -f "$TMP_OUTPUT" "$TMP_OUTPUT_EN"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Done ${FILE_NAME} ===" >> "$LOG_FILE"
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] All ${#APPS[@]} applications completed." >> "$LOG_FILE"

# ── 全アプリ横断のエグゼクティブサマリー PPTX（日英）を生成し Box にアップロード ──
# 個別アプリのレポート生成が全て成功していても、この最終ステップの失敗で
# set -e により全体が異常終了しないようガードする（レポート本体は既に投稿済みのため）。
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Building executive summary PPTX ===" >> "$LOG_FILE"
SUMMARY_INPUTS=()
for APP_ENTRY in "${APPS[@]}"; do
  IFS='|' read -r QUERY_NAME FILE_NAME <<< "$APP_ENTRY"
  MD_PATH="${SUMMARY_WORKDIR}/${FILE_NAME}.md"
  [ -f "$MD_PATH" ] && SUMMARY_INPUTS+=("$MD_PATH")
done

if [ "${#SUMMARY_INPUTS[@]}" -gt 0 ]; then
  PYTHONPATH="${REPO_ROOT}/scripts" \
    ~/.venv_aarch64/bin/python3 \
    "${REPO_ROOT}/scripts/reporting/pm_exec_summary.py" \
    "${SUMMARY_INPUTS[@]}" \
    --lang both --to-box \
    --title "FugakuNEXT アプリ評価 エグゼクティブサマリー" \
    >> "$LOG_FILE" 2>&1 \
    || echo "[WARN] $(date '+%Y-%m-%d %H:%M:%S') executive summary 生成失敗（各アプリレポート本体は成功済み）" >> "$LOG_FILE"
else
  echo "[WARN] $(date '+%Y-%m-%d %H:%M:%S') エグゼクティブサマリー用の入力レポートが1件もありません、スキップ" >> "$LOG_FILE"
fi
