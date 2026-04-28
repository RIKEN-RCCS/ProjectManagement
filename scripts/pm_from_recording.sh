#!/bin/bash
# pm_from_recording.sh
#
# 録音ファイルを文字起こし → generate_minutes_local.py で議事録生成 → pm.db インポート
#
# Usage:
#   bash scripts/pm_from_recording.sh file1.mp4 [file2.mp4 ...] [options]
#
# Options:
#   --skip SECONDS       全ファイルの冒頭をスキップ
#   --meeting-name NAME  議事録DB・pm.db に保存する会議種別名（推奨）
#                        省略すると .md ファイルが平文で残る（セキュリティリスクあり）
#   --held-at YYYY-MM-DD 開催日（省略時はファイル名の GMT タイムスタンプを JST 変換して使用）
#   --db PATH            pm.db のパス（省略時はデフォルト）
#   --vtt PATH           Zoom VTT ファイル（省略時は同日の VTT を data/ から自動検出）
#
# 例:
#   bash scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting
#   bash scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --skip 30 --meeting-name Leader_Meeting
#   bash scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting --held-at 2026-03-10

set -euo pipefail

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
  SIFFILE1=/lvs0/rccs-sdt/hikaru.inoue/cpu_aarch64/singularity/whisper.sif
  PYTHON3=/home/users/hikaru.inoue/.venv_aarch64/bin/python3
elif [[ "$ARCH" == "x86_64" ]]; then
  SIFFILE1=/lvs0/rccs-sdt/hikaru.inoue/cpu_amd64/singularity/whisper.sif
  PYTHON3=/home/users/hikaru.inoue/.venv_x86_64/bin/python3
else
  echo "Unknown architecture: $ARCH"; exit 1
fi

export SINGULARITY_BIND=/lvs0

export OPENAI_API_BASE="http://localhost:8000/v1"
export OPENAI_API_KEY="dummy"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
WHISPER_VAD="$SCRIPT_DIR/whisper_vad.py"
PM_MINUTES_IMPORT="$SCRIPT_DIR/pm_minutes_import.py"
PM_MINUTES_TO_PM="$SCRIPT_DIR/pm_minutes_to_pm.py"
GENERATE_MINUTES_LOCAL="$SCRIPT_DIR/generate_minutes_local.py"

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
SKIP_SECONDS=""
MEETING_NAME=""
HELD_AT=""
DB_PATH=""
VTT_FILE_ARG=""
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip)         SKIP_SECONDS="$2"; shift 2 ;;
    --meeting-name) MEETING_NAME="$2"; shift 2 ;;
    --held-at)      HELD_AT="$2";      shift 2 ;;
    --db)           DB_PATH="$2";      shift 2 ;;
    --vtt)          VTT_FILE_ARG="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *)              FILES+=("$1"); shift ;;
  esac
done

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "Usage: bash scripts/pm_from_recording.sh file1.mp4 [file2.mp4 ...] [--skip SECONDS] [--meeting-name NAME] [--held-at YYYY-MM-DD]"
  exit 1
fi

if [[ -z "$MEETING_NAME" ]]; then
  echo "[WARN] --meeting-name が未指定です。文字起こし結果が .md ファイルとして平文で残ります。"
  echo "[WARN]   推奨: --meeting-name Leader_Meeting 等を指定すると pm.db に直接保存し .md を削除します。"
fi

echo "処理対象: ${#FILES[@]} ファイル"

# --------------------------------------------------------------------------- #
# 作業ディレクトリ
# --------------------------------------------------------------------------- #
WORKDIR=/tmp/.work_$$
mkdir -p "$WORKDIR"
trap 'rm -rf "$WORKDIR"' EXIT

. ~/.secrets/hf_tokens.sh

SUCCESS=0
FAIL=0

for INPUT_FILE in "${FILES[@]}"; do
  INPUT_ABS=$(realpath "$INPUT_FILE")
  EXT=${INPUT_ABS##*.}
  BASENAME=${INPUT_ABS%.*}
  TMP_FILE="$WORKDIR/input.$EXT"
  WAV_FILE="$WORKDIR/input.wav"

  echo ""
  echo "=============================="
  echo "処理開始: $INPUT_ABS"
  echo "=============================="

  cat << EOF > "$WORKDIR/run.sh"
. /.venv/bin/activate
export HUGGING_FACE_TOKEN="${HUGGING_FACE_TOKEN:?HUGGING_FACE_TOKEN 環境変数が設定されていません}"
export HF_HOME="$WORKDIR/hf_cache"

if [ -n "$SKIP_SECONDS" ]; then
  ffmpeg -y -ss $SKIP_SECONDS -i $INPUT_ABS -c copy $TMP_FILE
else
  ffmpeg -y -i $INPUT_ABS -c copy $TMP_FILE
fi
ffmpeg -y -i $TMP_FILE -ac 1 -ar 16000 -vn -af "highpass=f=1000" -sample_fmt s16 $WAV_FILE
ffprobe -v error -show_format -show_streams -i $WAV_FILE
python3 $WHISPER_VAD $WAV_FILE $BASENAME.md
EOF

  time singularity run --nv "$SIFFILE1" sh "$WORKDIR/run.sh"
  STATUS=$?

  rm -f "$TMP_FILE" "$WAV_FILE" "$WORKDIR/run.sh"

  if [[ $STATUS -ne 0 ]]; then
    echo "失敗 (exit=$STATUS): $INPUT_ABS"
    FAIL=$((FAIL + 1))
    continue
  fi

  echo "文字起こし完了: $BASENAME.md"
  SUCCESS=$((SUCCESS + 1))

  if [[ -z "$MEETING_NAME" ]]; then
    continue
  fi

  # --------------------------------------------------------------------------- #
  # 開催日の決定
  # --------------------------------------------------------------------------- #
  if [[ -n "$HELD_AT" ]]; then
    DATE_TO_USE="$HELD_AT"
  else
    GMT_DATE=$(basename "$INPUT_ABS" | grep -oP '(?<=GMT)\d{8}' || true)
    GMT_TIME=$(basename "$INPUT_ABS" | grep -oP '(?<=GMT\d{8}-)\d{6}' || true)
    if [[ -n "$GMT_DATE" && -n "$GMT_TIME" ]]; then
      UTC_STR="${GMT_DATE:0:4}-${GMT_DATE:4:2}-${GMT_DATE:6:2} ${GMT_TIME:0:2}:${GMT_TIME:2:2}:${GMT_TIME:4:2}"
      DATE_TO_USE=$(date -d "$UTC_STR UTC + 9 hours" +%Y-%m-%d 2>/dev/null || true)
    fi
    if [[ -z "${DATE_TO_USE:-}" ]]; then
      DATE_TO_USE=$(date +%Y-%m-%d)
      echo "[INFO] ファイル名から日付を取得できませんでした。本日の日付を使用: $DATE_TO_USE"
    else
      echo "[INFO] GMT タイムスタンプを JST に変換: $DATE_TO_USE"
    fi
  fi

  # --------------------------------------------------------------------------- #
  # VTT ファイルの検出（--vtt 指定 > {stem}.transcript.vtt > {stem}.vtt）
  # 例: 2026-04-28_Leader_Meeting.m4a → .transcript.vtt → .vtt の順で検索
  # --------------------------------------------------------------------------- #
  VTT_FILE=""
  if [[ -n "$VTT_FILE_ARG" ]]; then
    if [[ -f "$VTT_FILE_ARG" ]]; then
      VTT_FILE="$VTT_FILE_ARG"
      echo "[INFO] VTT ファイル（引数指定）: $VTT_FILE"
    else
      echo "[WARN] 指定された VTT ファイルが見つかりません: $VTT_FILE_ARG"
    fi
  else
    INPUT_DIR=$(dirname "$INPUT_ABS")
    INPUT_STEM=$(basename "$INPUT_ABS" | sed 's/\.[^.]*$//')
    for vtt_candidate in \
        "$INPUT_DIR/${INPUT_STEM}.transcript.vtt" "$DATA_DIR/${INPUT_STEM}.transcript.vtt" \
        "$INPUT_DIR/${INPUT_STEM}.vtt" "$DATA_DIR/${INPUT_STEM}.vtt"; do
      if [[ -f "$vtt_candidate" ]]; then
        VTT_FILE="$vtt_candidate"
        echo "[INFO] VTT ファイル検出: $VTT_FILE"
        break
      fi
    done
    if [[ -z "$VTT_FILE" ]]; then
      echo "[INFO] VTT ファイルなし（Whisper のみで担当者を推定します）"
    fi
  fi

  VTT_OPT=""
  if [[ -n "$VTT_FILE" ]]; then
    VTT_OPT="--vtt $VTT_FILE"
  fi

  # --------------------------------------------------------------------------- #
  # Step 1: generate_minutes_local.py で高品質議事録を生成
  # --------------------------------------------------------------------------- #
  echo "[INFO] generate_minutes_local.py で議事録を生成中: $MEETING_NAME ($DATE_TO_USE)"
  TMPLOG=$(mktemp)
  "$PYTHON3" "$GENERATE_MINUTES_LOCAL" "$BASENAME.md" \
    --url        "$OPENAI_API_BASE" \
    --token      "$OPENAI_API_KEY" \
    --output     "$(dirname "$BASENAME")" \
    --max-tokens 16384 \
    --multi-stage --chunk-minutes 10 \
    $VTT_OPT \
    2>&1 | tee "$TMPLOG"
  GEN_EXIT=${PIPESTATUS[0]}

  MINUTES_MD=$(grep '議事録を保存しました:' "$TMPLOG" | sed 's/.*議事録を保存しました: //')
  rm -f "$TMPLOG"

  if [[ $GEN_EXIT -ne 0 || -z "$MINUTES_MD" || ! -f "$MINUTES_MD" ]]; then
    echo "[WARN] generate_minutes_local.py が失敗しました。$BASENAME.md は保持されています"
    continue
  fi

  # --------------------------------------------------------------------------- #
  # Step 2: --no-llm で議事録DB へインポート
  # --------------------------------------------------------------------------- #
  echo "[INFO] 議事録DBへインポート中: $MEETING_NAME ($DATE_TO_USE)"
  "$PYTHON3" "$PM_MINUTES_IMPORT" "$MINUTES_MD" \
    --meeting-name "$MEETING_NAME" \
    --held-at "$DATE_TO_USE" \
    --no-llm --force

  if [[ $? -ne 0 ]]; then
    echo "[WARN] 議事録DBへのインポートに失敗しました。$MINUTES_MD は保持されています"
    continue
  fi

  # --------------------------------------------------------------------------- #
  # Step 3: pm.db へ転記
  # --------------------------------------------------------------------------- #
  echo "[INFO] pm.db へ転記中: $MEETING_NAME ($DATE_TO_USE)"
  "$PYTHON3" "$PM_MINUTES_TO_PM" \
    --meeting-name "$MEETING_NAME" \
    --since "$DATE_TO_USE" \
    --db "${DB_PATH:-data/pm.db}"

  if [[ $? -eq 0 ]]; then
    rm -f "$BASENAME.md" "$MINUTES_MD"
    echo "[INFO] 文字起こし・議事録ファイルを議事録DB・pm.db に保存し削除しました"
  else
    echo "[WARN] pm.db への転記に失敗しました。ファイルは保持されています"
  fi

done

echo ""
echo "=============================="
echo "全処理完了: 成功=$SUCCESS 失敗=$FAIL"
echo "=============================="
