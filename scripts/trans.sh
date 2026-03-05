#!/bin/sh
#SBATCH -p ai-l40s
#SBATCH --nodes=1
#SBATCH --gpus=1
#SBATCH --time=24:00:00

# 複数ファイルを1ジョブで順次処理する
# Usage: sbatch trans.sh file1.mp4 [file2.mp4 ...] [--skip SECONDS]
#
# --skip SECONDS を付けると全ファイルの冒頭をスキップ
# 例: sbatch trans.sh a.mp4 b.mp4 c.mp4
#     sbatch trans.sh a.mp4 b.mp4 --skip 30

WHISPER_VAD=/lvs0/dne1/rccs-nghpcadu/hikaru.inoue/MCP/slack/scripts/whisper_vad.py

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
  SIFFILE1=/lvs0/rccs-sdt/hikaru.inoue/cpu_aarch64/singularity/whisper.sif
elif [[ "$ARCH" == "x86_64" ]]; then
  SIFFILE1=/lvs0/rccs-sdt/hikaru.inoue/cpu_amd64/singularity/whisper.sif
else
  echo "Unknown architecture: $ARCH"; exit 1
fi

export SINGULARITY_BIND=/lvs0

# 引数パース: --skip N を抽出し、残りをファイルリストとする
SKIP_SECONDS=""
FILES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip) SKIP_SECONDS="$2"; shift 2 ;;
    *)      FILES+=("$1"); shift ;;
  esac
done

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "Usage: sbatch trans.sh file1.mp4 [file2.mp4 ...] [--skip SECONDS]"
  exit 1
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
export HUGGING_FACE_TOKEN="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"
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
