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
#   --no-slide-ocr       スライドOCRを無効化（デフォルトは有効。動画にスライドなしの場合のみ使用）
#   --scene-threshold N  ffmpeg scene detect 閾値（デフォルト: 0.25）
#   --max-frames N       OCR に渡すフレーム数上限。超過時は時系列に均等間引き（デフォルト: 200）
#   --ocr-workers N      OCR 並列ワーカー数（デフォルト: 8）
#   --consensus N        Self-consistency サンプリング数（デフォルト 3。--consensus 1 で従来の単発生成に戻す）
#   --no-triage          抽出候補のトリアージ（2次審査）を無効化
#
# 環境変数:
#   ARGUS_PREFER_RIVAULT=1  LLM バックエンドを RiVault に切替（デフォルト: ローカル vLLM）
#
# 例:
#   bash scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting
#   bash scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --skip 30 --meeting-name Leader_Meeting
#   bash scripts/pm_from_recording.sh GMT20260302-032528_Recording.mp4 --meeting-name Leader_Meeting --held-at 2026-03-10

set -euo pipefail

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
  SIFFILE1=/lvs0/rccs-sdt/hikaru.inoue/cpu_aarch64/singularity/whisper.sif
  PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$ARCH" == "x86_64" ]]; then
  SIFFILE1=/lvs0/rccs-sdt/hikaru.inoue/cpu_amd64/singularity/whisper.sif
  PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
  echo "Unknown architecture: $ARCH"; exit 1
fi

export SINGULARITY_BIND=/lvs0

if [[ -f ~/.secrets/localLLM.sh ]]; then
  source ~/.secrets/localLLM.sh
fi

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/bin/ から直接実行された場合は scripts/ を SCRIPT_DIR とする
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
WHISPER_VAD="$SCRIPT_DIR/recording/whisper_vad.py"
PM_MINUTES_IMPORT="$SCRIPT_DIR/minutes/pm_minutes_import.py"
PM_INGEST="$SCRIPT_DIR/ingest/pm_ingest.py"
GENERATE_MINUTES_LOCAL="$SCRIPT_DIR/recording/generate_minutes_local.py"
PM_MINUTES_CATALOG="$SCRIPT_DIR/minutes/pm_minutes_catalog.py"
PM_MINUTES_PUBLISH="$SCRIPT_DIR/minutes/pm_minutes_publish.py"

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
SKIP_SECONDS=""
MEETING_NAME=""
HELD_AT=""
DB_PATH=""
VTT_FILE_ARG=""
SLIDE_OCR=1
SCENE_THRESHOLD="0.25"
MAX_FRAMES="200"
OCR_WORKERS="8"
CONSENSUS_N="3"
NO_TRIAGE=0
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip)         SKIP_SECONDS="$2"; shift 2 ;;
    --meeting-name) MEETING_NAME="$2"; shift 2 ;;
    --held-at)      HELD_AT="$2";      shift 2 ;;
    --db)           DB_PATH="$2";      shift 2 ;;
    --vtt)          VTT_FILE_ARG="$2"; shift 2 ;;
    --no-slide-ocr) SLIDE_OCR=0; shift ;;
    --scene-threshold) SCENE_THRESHOLD="$2"; shift 2 ;;
    --max-frames)   MAX_FRAMES="$2"; shift 2 ;;
    --ocr-workers)  OCR_WORKERS="$2"; shift 2 ;;
    --consensus)    CONSENSUS_N="$2"; shift 2 ;;
    --no-triage)    NO_TRIAGE=1; shift ;;
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

# Canvas → pm.db 同期（ループに入る前に1回。--meeting-name 未指定なら
# pm.db への転記は行わないのでスキップする）。
if [[ -n "$MEETING_NAME" ]]; then
    DB_PM_FOR_SYNC="${DB_PATH:-$REPO_ROOT/data/pm.db}"
    # shellcheck source=_lib_sync_canvas.sh
    source "$SCRIPT_DIR/bin/_lib_sync_canvas.sh"
    sync_canvas_before_pm_update "$DB_PM_FOR_SYNC"
    echo ""
fi

# --------------------------------------------------------------------------- #
# 作業ディレクトリ
# --------------------------------------------------------------------------- #
WORKDIR=/tmp/.work_$$
mkdir -p "$WORKDIR"
trap 'rm -rf "$WORKDIR"' EXIT

. ~/.secrets/hf_tokens.sh
# RiVault トークン（Self-consistency の embedding 取得・ARGUS_PREFER_RIVAULT=1 時の LLM 呼び出しに必要）
[ -f ~/.secrets/rivault_tokens.sh ] && . ~/.secrets/rivault_tokens.sh

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

  # --------------------------------------------------------------------------- #
  # スライドOCR（Whisper より前に実行して terminology.txt を initial_prompt に渡す）
  # --------------------------------------------------------------------------- #
  SLIDE_CONTEXT_FILE=""
  TERMINOLOGY_FILE=""
  if [[ "$SLIDE_OCR" == "1" && "${EXT,,}" == "mp4" ]]; then
    echo "[INFO] スライドOCR開始 (threshold=$SCENE_THRESHOLD, max_frames=$MAX_FRAMES)"
    SLIDE_WORKDIR="$WORKDIR/slides_$(date +%s)"
    mkdir -p "$SLIDE_WORKDIR"
    SLIDE_CONTEXT_FILE="$SLIDE_WORKDIR/slide_context.md"
    TERMINOLOGY_FILE="$SLIDE_WORKDIR/terminology.txt"
    if "$PYTHON3" "$SCRIPT_DIR/recording/slide_ocr.py" "$INPUT_ABS" \
        --out-dir "$SLIDE_WORKDIR" \
        --scene-threshold "$SCENE_THRESHOLD" \
        --max-frames "$MAX_FRAMES" \
        --max-workers "$OCR_WORKERS" \
        --context-out "$SLIDE_CONTEXT_FILE" \
        --terminology-out "$TERMINOLOGY_FILE" \
        --verbose; then
      if [[ -s "$SLIDE_CONTEXT_FILE" ]]; then
        echo "[INFO] スライド文脈生成: $(wc -c < "$SLIDE_CONTEXT_FILE") bytes"
      else
        SLIDE_CONTEXT_FILE=""
      fi
      if [[ ! -s "$TERMINOLOGY_FILE" ]]; then
        TERMINOLOGY_FILE=""
      fi
    else
      echo "[WARN] スライドOCR失敗。文脈・用語なしで続行します"
      SLIDE_CONTEXT_FILE=""
      TERMINOLOGY_FILE=""
    fi
  elif [[ "$SLIDE_OCR" == "1" ]]; then
    echo "[INFO] mp4 以外のためスライドOCRをスキップします (ext=$EXT)"
  fi

  # pm.db の terminology テーブルから Whisper 用語を事前取得
  # （コンテナ内 Python は sqlcipher3 非対応のため、コンテナ外の venv Python で生成する）
  PM_DB_TERMS_FILE="$WORKDIR/pm_db_terms.txt"
  if PYTHONPATH="$SCRIPT_DIR" "$PYTHON3" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from utils.terminology import load_top_k
terms = load_top_k(limit_tokens=224)
print('\n'.join(terms))
" > "$PM_DB_TERMS_FILE" 2>/dev/null && [[ -s "$PM_DB_TERMS_FILE" ]]; then
    N_DB_TERMS=$(wc -l < "$PM_DB_TERMS_FILE" | tr -d ' ')
    echo "[INFO] pm.db terminology から ${N_DB_TERMS} 語を Whisper 用に取得しました"
    if [[ -n "$TERMINOLOGY_FILE" && -s "$TERMINOLOGY_FILE" ]]; then
      cat "$PM_DB_TERMS_FILE" >> "$TERMINOLOGY_FILE"
    else
      TERMINOLOGY_FILE="$PM_DB_TERMS_FILE"
    fi
  else
    rm -f "$PM_DB_TERMS_FILE"
  fi

  WHISPER_EXTRA_OPT=""
  if [[ -n "$TERMINOLOGY_FILE" ]]; then
    WHISPER_EXTRA_OPT="--initial-prompt-extra $TERMINOLOGY_FILE"
  fi

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
python3 $WHISPER_VAD $WAV_FILE $BASENAME.md $WHISPER_EXTRA_OPT
EOF

  # GPU OOM は再実行で成功することが多い（vLLM 等の他プロセスのメモリ占有が変動するため）。
  # OOM パターンを検出した場合のみリトライする。コード起因の失敗をリトライしないように
  # OOM 以外の失敗は即座に中断する。
  MAX_RETRIES="${WHISPER_MAX_RETRIES:-3}"
  RETRY_SLEEP="${WHISPER_RETRY_SLEEP:-30}"
  RUN_LOG="$WORKDIR/whisper_run.log"
  STATUS=1
  for ((attempt = 1; attempt <= MAX_RETRIES; attempt++)); do
    echo "[INFO] Whisper 試行 $attempt / $MAX_RETRIES"
    set +e
    time singularity run --nv "$SIFFILE1" sh "$WORKDIR/run.sh" 2>&1 | tee "$RUN_LOG"
    STATUS=${PIPESTATUS[0]}
    set -e
    if [[ $STATUS -eq 0 ]]; then
      break
    fi
    if grep -qE "out of memory|CUDA out of memory|OutOfMemoryError|CUDA error: out of memory" "$RUN_LOG" \
       || [[ $STATUS -eq 137 ]]; then
      if [[ $attempt -lt $MAX_RETRIES ]]; then
        echo "[WARN] OOM 検出 (exit=$STATUS)。${RETRY_SLEEP}s 待機後にリトライします"
        sleep "$RETRY_SLEEP"
        continue
      else
        echo "[ERROR] OOM のため $MAX_RETRIES 回リトライしましたが成功しませんでした"
      fi
    else
      echo "[ERROR] OOM 以外の失敗 (exit=$STATUS)。リトライしません"
    fi
    break
  done

  rm -f "$TMP_FILE" "$WAV_FILE" "$WORKDIR/run.sh" "$RUN_LOG"

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
  # VTT ファイルの検出
  #   優先度: --vtt 指定 > {stem}.transcript.vtt > {stem}.vtt
  #   stem の派生: 元 stem / 解像度サフィックス剥がし (_3840x2160 等) /
  #               ブラウザ重複DLサフィックス剥がし ( (1), (12) 等) / 両方剥がし
  #   さらに Zoom が ".transcript (N).vtt" の形で生成するパターンにも対応
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
    INPUT_STEM_NORES=$(echo "$INPUT_STEM" | sed -E 's/_[0-9]+x[0-9]+$//')
    INPUT_STEM_NODUP=$(echo "$INPUT_STEM" | sed -E 's/ ?\([0-9]+\)$//')
    INPUT_STEM_BARE=$(echo "$INPUT_STEM_NORES" | sed -E 's/ ?\([0-9]+\)$//')

    declare -A SEEN_STEMS=()
    STEM_CANDIDATES=()
    for s in "$INPUT_STEM" "$INPUT_STEM_NORES" "$INPUT_STEM_NODUP" "$INPUT_STEM_BARE"; do
        if [[ -n "$s" && -z "${SEEN_STEMS[$s]:-}" ]]; then
            SEEN_STEMS[$s]=1
            STEM_CANDIDATES+=("$s")
        fi
    done

    VTT_CANDIDATES=()
    for stem in "${STEM_CANDIDATES[@]}"; do
        VTT_CANDIDATES+=(
            "$INPUT_DIR/${stem}.transcript.vtt" "$DATA_DIR/${stem}.transcript.vtt"
            "$INPUT_DIR/${stem}.vtt"            "$DATA_DIR/${stem}.vtt"
        )
    done
    # Zoom が "Recording (1).m4a" に対し "Recording.transcript (1).vtt" を生成するパターン
    if [[ "$INPUT_STEM" =~ ^(.+)\ (\([0-9]+\))$ ]]; then
        BASE="${BASH_REMATCH[1]}"
        PAREN="${BASH_REMATCH[2]}"
        VTT_CANDIDATES+=(
            "$INPUT_DIR/${BASE}.transcript ${PAREN}.vtt" "$DATA_DIR/${BASE}.transcript ${PAREN}.vtt"
            "$INPUT_DIR/${BASE}.transcript${PAREN}.vtt"  "$DATA_DIR/${BASE}.transcript${PAREN}.vtt"
        )
    fi

    for vtt_candidate in "${VTT_CANDIDATES[@]}"; do
      if [[ -f "$vtt_candidate" ]]; then
        VTT_FILE="$vtt_candidate"
        echo "[INFO] VTT ファイル検出: $VTT_FILE"
        break
      fi
    done
    if [[ -z "$VTT_FILE" ]]; then
      echo "[WARN] VTT ファイルなし（Whisper のみで担当者を推定します）"
      echo "[WARN]   検索した stem: ${STEM_CANDIDATES[*]}"
      echo "[WARN]   検索ディレクトリ: $INPUT_DIR, $DATA_DIR"
    fi
  fi

  VTT_OPT=""
  if [[ -n "$VTT_FILE" ]]; then
    VTT_OPT="--vtt $VTT_FILE"
  fi

  SLIDE_OPT=""
  if [[ -n "$SLIDE_CONTEXT_FILE" && -s "$SLIDE_CONTEXT_FILE" ]]; then
    SLIDE_OPT="--slide-context $SLIDE_CONTEXT_FILE"
  fi

  CONSENSUS_OPT=""
  if [[ "$CONSENSUS_N" =~ ^[0-9]+$ ]]; then
    CONSENSUS_OPT="--consensus $CONSENSUS_N"
    if [[ "$CONSENSUS_N" -ge 2 ]]; then
      echo "[INFO] Self-consistency 有効: N=$CONSENSUS_N（生成時間が baseline 比 +15〜25% 程度）"
    fi
  fi

  NO_TRIAGE_OPT=""
  if [[ "$NO_TRIAGE" -eq 1 ]]; then
    NO_TRIAGE_OPT="--no-triage"
  fi

  # --------------------------------------------------------------------------- #
  # Step 0.5: VTT × Whisper 突合修正（reconcile_transcript.py）
  #   VTT がある場合のみ実行。失敗しても元の文字起こしで続行する。
  # --------------------------------------------------------------------------- #
  if [[ -n "$VTT_FILE" ]]; then
    echo "[INFO] VTT × Whisper 突合修正中: $VTT_FILE"
    RECONCILED_MD="${BASENAME}_reconciled.md"
    SLIDE_CTX_OPT=""
    if [[ -n "$SLIDE_CONTEXT_FILE" && -s "$SLIDE_CONTEXT_FILE" ]]; then
      SLIDE_CTX_OPT="--slide-context $SLIDE_CONTEXT_FILE"
    fi
    if PYTHONPATH="$SCRIPT_DIR" "$PYTHON3" \
        "$SCRIPT_DIR/recording/reconcile_transcript.py" \
        "$BASENAME.md" \
        --vtt "$VTT_FILE" \
        --output "$RECONCILED_MD" \
        $SLIDE_CTX_OPT; then
      mv "$RECONCILED_MD" "$BASENAME.md"
      echo "[INFO] reconcile 完了: $BASENAME.md を更新しました"
    else
      echo "[WARN] reconcile 失敗（元の文字起こしをそのまま使用します）"
      rm -f "$RECONCILED_MD"
    fi
  fi

  # --------------------------------------------------------------------------- #
  # Step 1: generate_minutes_local.py で高品質議事録を生成
  # --------------------------------------------------------------------------- #
  echo "[INFO] generate_minutes_local.py で議事録を生成中: $MEETING_NAME ($DATE_TO_USE)"
  TMPLOG=$(mktemp)
  URL_OPT=""
  TOKEN_OPT=""
  [[ -n "${LOCAL_LLM_URL:-}" ]] && URL_OPT="--url $LOCAL_LLM_URL"
  [[ -n "${LOCAL_LLM_TOKEN:-}" ]] && TOKEN_OPT="--token $LOCAL_LLM_TOKEN"
  "$PYTHON3" "$GENERATE_MINUTES_LOCAL" "$BASENAME.md" \
    $URL_OPT \
    $TOKEN_OPT \
    --output     "$(dirname "$BASENAME")" \
    --max-tokens 16384 \
    --multi-stage --chunk-minutes 10 \
    $VTT_OPT \
    $SLIDE_OPT \
    $CONSENSUS_OPT \
    $NO_TRIAGE_OPT \
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
  "$PYTHON3" "$PM_INGEST" minutes \
    --minutes-name "$MEETING_NAME" \
    --since "$DATE_TO_USE" \
    --db "${DB_PATH:-data/pm.db}"

  if [[ $? -eq 0 ]]; then
    rm -f "$BASENAME.md" "$MINUTES_MD"
    echo "[INFO] 文字起こし・議事録ファイルを議事録DB・pm.db に保存し削除しました"

    # -------------------------------------------------------------------- #
    # Step 3.5: terminology テーブルを更新
    #   - slide_ocr の terminology.txt があればそれを ground truth として優先
    #   - pm.db の decisions/actions から正規表現で追加抽出
    #   - 失敗しても後続処理は継続する
    # -------------------------------------------------------------------- #
    echo "[INFO] terminology テーブル更新中..."
    TERMINOLOGY_OPTS="--meeting-kind ${MEETING_NAME} --db ${DB_PATH:-data/pm.db}"
    if [[ -n "$TERMINOLOGY_FILE" && -s "$TERMINOLOGY_FILE" ]]; then
      TERMINOLOGY_OPTS="$TERMINOLOGY_OPTS --slide-terms $TERMINOLOGY_FILE"
    fi
    PYTHONPATH="$SCRIPT_DIR" "$PYTHON3" \
      "$SCRIPT_DIR/data-pipeline/pm_terminology_update.py" \
      $TERMINOLOGY_OPTS \
      || echo "[WARN] terminology 更新失敗（後続処理は継続）"

    # -------------------------------------------------------------------- #
    # Step 4: Box 議事録アップロード + Canvas 目録更新（設定ありの場合のみ）
    # -------------------------------------------------------------------- #
    MEETING_CFG=$(python3 -c "
import sys, yaml
from pathlib import Path
root = Path('$REPO_ROOT')
cfg_path = root / 'data' / 'argus_config.yaml'
if not cfg_path.exists():
    print('0 0')
    sys.exit(0)
cfg = yaml.safe_load(cfg_path.read_text())
m = (cfg.get('meetings') or {}).get('$MEETING_NAME', {})
print(f\"{1 if m.get('box_folder_id') else 0} {1 if m.get('catalog_canvas_id') else 0}\")
" 2>/dev/null || echo "0 0")
    HAS_BOX=$(echo "$MEETING_CFG" | cut -d' ' -f1)
    HAS_CANVAS=$(echo "$MEETING_CFG" | cut -d' ' -f2)
    CATALOG_OPTS=""
    if [[ "$HAS_BOX" == "1" ]]; then
      CATALOG_OPTS="--upload"
      echo "[INFO] Box 議事録アップロード: $MEETING_NAME"
    else
      echo "[SKIP] Box 議事録アップロード: $MEETING_NAME に box_folder_id 未設定"
    fi
    if [[ "$HAS_CANVAS" == "1" ]]; then
      CATALOG_OPTS="$CATALOG_OPTS --catalog"
      echo "[INFO] Canvas 目録更新: $MEETING_NAME"
    else
      echo "[SKIP] Canvas 目録更新: $MEETING_NAME に catalog_canvas_id 未設定"
    fi
    if [[ -n "$CATALOG_OPTS" ]]; then
      echo "[INFO] pm_minutes_catalog.py 実行中..."
      "$PYTHON3" "$PM_MINUTES_CATALOG" $CATALOG_OPTS --meeting-name "$MEETING_NAME" 2>&1 | tail -3
    fi
    # Step 5: Box XLSX 更新
    echo "[INFO] Box XLSX 更新中..."
    "$PYTHON3" "$PM_MINUTES_PUBLISH" --xlsx-only 2>&1 | tail -3 || echo "[WARN] Box XLSX 更新失敗"
  else
    echo "[WARN] pm.db への転記に失敗しました。ファイルは保持されています"
  fi

done

echo ""
echo "=============================="
echo "全処理完了: 成功=$SUCCESS 失敗=$FAIL"
echo "=============================="
