#!/bin/bash
#SBATCH --nodes=1
#SBATCH --time=24:00:00

# 複数ファイルを1ジョブで順次処理する
# Usage: bash trans.sh file1.mp4 [file2.mp4 ...] [--skip SECONDS] [--meeting-name NAME]
#
# --skip SECONDS       全ファイルの冒頭をスキップ
# --meeting-name NAME  指定すると文字起こし後に pm.db へ直接インポートし .md を削除（推奨）
#                      省略すると従来通り .md ファイルを残す（セキュリティリスクあり）
# --held-at YYYY-MM-DD --meeting-name と併用。省略時はファイル名の GMT タイムスタンプを JST 変換して使用
# 例: bash trans.sh a.mp4 b.mp4
#     bash trans.sh a.mp4 --skip 30 --meeting-name Leader_Meeting
#     bash trans.sh a.mp4 --meeting-name Leader_Meeting --held-at 2026-03-10  # 日付を明示上書き
#
# パーティション選択: ai-l40s に空きがあれば優先、次に qc-gh200、
# どちらも混雑していれば ai-l40s に投入する。

# ============================================================
# 投入モード: SLURM外（ログインノード等）から実行された場合
# ============================================================
if [[ -z "${SLURM_JOB_ID}" ]]; then

  # sinfo で idle/mix ノードが存在するか確認
  has_available_nodes() {
    sinfo -p "$1" --noheader -o "%t" 2>/dev/null | grep -qE "^(idle|mix)$"
  }

  if has_available_nodes "ai-l40s"; then
    PARTITION="ai-l40s"
    EXTRA_OPTS="--gpus=1"
    echo "[INFO] ai-l40s に空きあり → ai-l40s に投入します"
  elif has_available_nodes "qc-gh200"; then
    PARTITION="qc-gh200"
    EXTRA_OPTS=""
    echo "[INFO] ai-l40s は空きなし、qc-gh200 に空きあり → qc-gh200 に投入します"
  else
    PARTITION="ai-l40s"
    EXTRA_OPTS="--gpus=1"
    echo "[INFO] 両パーティションが混雑 → デフォルトの ai-l40s に投入します"
  fi

  # shellcheck disable=SC2086
  sbatch --partition="$PARTITION" $EXTRA_OPTS "$0" "$@"
  exit $?
fi

# ============================================================
# ジョブ実行モード: SLURM ジョブとして実行された場合
# ============================================================

WHISPER_VAD=/lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/whisper_vad.py

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

# 引数パース: --skip N / --meeting-name NAME を抽出し、残りをファイルリストとする
SKIP_SECONDS=""
MEETING_NAME=""
HELD_AT=""
FILES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip)         SKIP_SECONDS="$2"; shift 2 ;;
    --meeting-name) MEETING_NAME="$2"; shift 2 ;;
    --held-at)      HELD_AT="$2";      shift 2 ;;
    *)              FILES+=("$1"); shift ;;
  esac
done

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "Usage: bash trans.sh file1.mp4 [file2.mp4 ...] [--skip SECONDS] [--meeting-name NAME] [--held-at YYYY-MM-DD]"
  exit 1
fi

if [[ -z "$MEETING_NAME" ]]; then
  echo "[WARN] --meeting-name が未指定です。文字起こし結果が .md ファイルとして平文で残ります。"
  echo "[WARN]   推奨: --meeting-name Leader_Meeting 等を指定すると pm.db に直接保存し .md を削除します。"
fi

echo "処理対象: ${#FILES[@]} ファイル"

# ジョブ共通の作業ディレクトリ
WORKDIR=/tmp/.work_${SLURM_JOB_ID}
mkdir -p "$WORKDIR"

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

  if [[ $STATUS -eq 0 ]]; then
    echo "完了: $BASENAME.md"
    SUCCESS=$((SUCCESS + 1))

    if [[ -n "$MEETING_NAME" ]]; then
      if [[ -n "$HELD_AT" ]]; then
        DATE_TO_USE="$HELD_AT"
      else
        # ファイル名の GMT タイムスタンプ（例: GMT20260302-032528）を JST に変換
        GMT_DATE=$(basename "$INPUT_ABS" | grep -oP '(?<=GMT)\d{8}')
        GMT_TIME=$(basename "$INPUT_ABS" | grep -oP '(?<=GMT\d{8}-)\d{6}')
        if [[ -n "$GMT_DATE" && -n "$GMT_TIME" ]]; then
          UTC_STR="${GMT_DATE:0:4}-${GMT_DATE:4:2}-${GMT_DATE:6:2} ${GMT_TIME:0:2}:${GMT_TIME:2:2}:${GMT_TIME:4:2}"
          DATE_TO_USE=$(date -d "$UTC_STR UTC + 9 hours" +%Y-%m-%d 2>/dev/null)
        fi
        if [[ -z "$DATE_TO_USE" ]]; then
          DATE_TO_USE=$(date +%Y-%m-%d)
          echo "[INFO] ファイル名から日付を取得できませんでした。本日の日付を使用: $DATE_TO_USE"
        else
          echo "[INFO] GMT タイムスタンプを JST に変換: $DATE_TO_USE"
        fi
      fi

      SCRIPT_DIR=$(dirname "$(realpath "$WHISPER_VAD")")
#     VENV_PYTHON=~/.venv_x86_64/bin/python3
      PM_IMPORT="$SCRIPT_DIR/pm_meeting_import.py"
      PM_DB="$SCRIPT_DIR/../data/pm.db"

      echo "[INFO] pm.db へインポート中: $MEETING_NAME ($DATE_TO_USE)"
      "$PYTHON3" "$PM_IMPORT" "$BASENAME.md" \
        --meeting-name "$MEETING_NAME" \
        --held-at "$DATE_TO_USE" \
        --db "$PM_DB"

      if [[ $? -eq 0 ]]; then
        rm -f "$BASENAME.md"
        echo "[INFO] 文字起こし結果を pm.db に保存し、$BASENAME.md を削除しました"
      else
        echo "[WARN] pm.db へのインポートに失敗しました。$BASENAME.md は保持されています"
      fi
    fi
  else
    echo "失敗 (exit=$STATUS): $INPUT_ABS"
    FAIL=$((FAIL + 1))
  fi
done

rm -rf "$WORKDIR"

echo ""
echo "=============================="
echo "全処理完了: 成功=$SUCCESS 失敗=$FAIL"
echo "=============================="
