#!/usr/bin/env bash
# auto_recording_import.sh
#
# meetings/ ディレクトリの m4a ファイルを検出し、recording_to_pm.sh に自動投入する。
# cron で定期実行することを想定（1時間に1回など）。
#
# ファイル名形式: YYYY-MM-DD_{meeting-name}.m4a
#   - YYYY-MM-DD  → --held-at に渡す（開催日）
#   - {meeting-name} → --meeting-name に渡す。docs/project.md「会議の種類と頻度」の
#                      いずれかに一致しない場合はスキップ
#
# 有効な全ファイルを1つの sbatch ジョブにまとめて投入する。
# 投入済みファイルは meetings/processing/ に移動して再投入を防ぐ。
# ログは data/auto_recording_import.log に追記する。

set -euo pipefail

. ~/.secrets/hf_tokens.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement
MEETINGS_DIR="$REPO_ROOT/data"
PROCESSING_DIR="$MEETINGS_DIR/processing"
LOG_FILE="$REPO_ROOT/data/auto_recording_import.log"
VENV_PYTHON="$HOME/.venv_x86_64/bin/python3"

# --------------------------------------------------------------------------- #
# 引数解析
# --------------------------------------------------------------------------- #
SLACK_CHANNEL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--channel) SLACK_CHANNEL="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# --------------------------------------------------------------------------- #
# 有効な会議名（docs/project.md「会議の種類と頻度」に基づく）
# SubWG_Meeting は SubWG1_Meeting / SubWG3_Meeting 等の番号付きも許容する
# --------------------------------------------------------------------------- #
is_valid_meeting_name() {
    local name="$1"
    case "$name" in
        Leader_Meeting|Block1_Meeting|Block2_Meeting|SubWG_Meeting|\
        BenchmarkWG_Meeting|Co-design_Review_Meeting)
            return 0 ;;
        SubWG[0-9]*_Meeting)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# --------------------------------------------------------------------------- #
# meeting-name → pm.db パスのマッピング
#   Leader_Meeting / Co-design_Review_Meeting → pm.db
#   Block1/Block2/SubWG系                     → pm-hpc.db
#   BenchmarkWG_Meeting                        → pm-bmt.db
# --------------------------------------------------------------------------- #
get_pm_db() {
    local name="$1"
    case "$name" in
        Leader_Meeting|Co-design_Review_Meeting)
            echo "$REPO_ROOT/data/pm.db" ;;
        Block1_Meeting|Block2_Meeting|SubWG_Meeting|SubWG[0-9]*_Meeting)
            echo "$REPO_ROOT/data/pm-hpc.db" ;;
        BenchmarkWG_Meeting)
            echo "$REPO_ROOT/data/pm-bmt.db" ;;
        *)
            echo "$REPO_ROOT/data/pm.db" ;;
    esac
}

# --------------------------------------------------------------------------- #
# ログ出力
# --------------------------------------------------------------------------- #
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# --------------------------------------------------------------------------- #
# 議事録DBへのインポート済みチェック
# 戻り値: 0=インポート済み, 1=未インポート
# --------------------------------------------------------------------------- #
is_already_imported() {
    local held_at="$1"
    local meeting_name="$2"
    local db="$REPO_ROOT/data/minutes/${meeting_name}.db"

    [[ ! -f "$db" ]] && return 1

    CHECK_DB="$db" CHECK_HELD_AT="$held_at" CHECK_SCRIPTS="$SCRIPT_DIR" \
    "$VENV_PYTHON" -c "
import os, sys
sys.path.insert(0, os.environ['CHECK_SCRIPTS'])
from db_utils import open_db, is_encrypted
db      = os.environ['CHECK_DB']
held_at = os.environ['CHECK_HELD_AT']
try:
    conn = open_db(db, encrypt=is_encrypted(db))
    row  = conn.execute('SELECT 1 FROM instances WHERE held_at=?', (held_at,)).fetchone()
    conn.close()
    sys.exit(0 if row else 1)
except Exception as e:
    print(f'[WARN] DB確認中にエラー: {e}', file=sys.stderr)
    sys.exit(1)
" 2>>"$LOG_FILE"
}

# --------------------------------------------------------------------------- #
# Slack投稿済みチェック（指定チャンネルへの投稿が存在するか）
# 戻り値: 0=投稿済み, 1=未投稿
# --------------------------------------------------------------------------- #
is_already_posted_to_slack() {
    local held_at="$1"
    local meeting_name="$2"
    local channel_id="$3"
    local db="$REPO_ROOT/data/minutes/${meeting_name}.db"

    [[ ! -f "$db" ]] && return 1

    CHECK_DB="$db" CHECK_HELD_AT="$held_at" CHECK_CHANNEL="$channel_id" CHECK_SCRIPTS="$SCRIPT_DIR" \
    "$VENV_PYTHON" -c "
import os, sys
sys.path.insert(0, os.environ['CHECK_SCRIPTS'])
from db_utils import open_db, is_encrypted
db         = os.environ['CHECK_DB']
held_at    = os.environ['CHECK_HELD_AT']
channel_id = os.environ['CHECK_CHANNEL']
try:
    conn = open_db(db, encrypt=is_encrypted(db))
    row  = conn.execute(
        'SELECT 1 FROM instances WHERE held_at=? AND slack_channel_id=? AND slack_file_permalink IS NOT NULL',
        (held_at, channel_id)).fetchone()
    conn.close()
    sys.exit(0 if row else 1)
except Exception as e:
    print(f'[WARN] DB確認中にエラー: {e}', file=sys.stderr)
    sys.exit(1)
" 2>>"$LOG_FILE"
}

# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
mkdir -p "$PROCESSING_DIR"

log "=== auto_recording_import 開始 ==="

shopt -s nullglob
m4a_files=("$MEETINGS_DIR"/*.m4a)
shopt -u nullglob

if [[ ${#m4a_files[@]} -eq 0 ]]; then
    log "対象ファイルなし（meetings/*.m4a が存在しません）"
    log "=== 完了 ==="
    exit 0
fi

# --------------------------------------------------------------------------- #
# ファイルを検証し投入リストを構築
# --------------------------------------------------------------------------- #
declare -a BATCH_FILES=()
declare -a BATCH_HELD_AT=()
declare -a BATCH_NAMES=()
declare -a BATCH_DBS=()
skipped=0

for m4a_file in "${m4a_files[@]}"; do
    filename=$(basename "$m4a_file")
    name="${filename%.m4a}"

    # 形式チェック: YYYY-MM-DD_{meeting-name}
    if [[ ! "$name" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})_(.+)$ ]]; then
        log "[SKIP] 形式不一致（YYYY-MM-DD_{meeting-name}.m4a ではない）: $filename"
        skipped=$((skipped + 1))
        continue
    fi

    held_at="${BASH_REMATCH[1]}"
    meeting_name="${BASH_REMATCH[2]}"

    # 会議名の有効性チェック
    if ! is_valid_meeting_name "$meeting_name"; then
        log "[SKIP] 未知の会議名: '$meeting_name': $filename"
        skipped=$((skipped + 1))
        continue
    fi

    # 議事録DBへのインポート済みチェック
    if is_already_imported "$held_at" "$meeting_name"; then
        # Slackへの投稿が必要か確認
        if [[ -n "$SLACK_CHANNEL" ]] && ! is_already_posted_to_slack "$held_at" "$meeting_name" "$SLACK_CHANNEL"; then
            log "[SLACK] 議事録DBインポート済み・Slack未投稿 → 直接投稿: $filename"
            # GPU不要のため sbatch を介さず直接実行
            # shellcheck disable=SC1090
            source ~/.secrets/slack_tokens.sh 2>>"$LOG_FILE" || true
            # チャンネル単位で未投稿と確認済みのため --force を渡す
            # （pm_minutes_import.py の posted_to_slack_at チェックをバイパス）
            "$VENV_PYTHON" "$SCRIPT_DIR/pm_minutes_import.py" \
                --post-to-slack --meeting-name "$meeting_name" --held-at "$held_at" \
                -c "$SLACK_CHANNEL" --force 2>&1 | tee -a "$LOG_FILE"
        else
            log "[SKIP] 議事録DBにインポート済み（held_at=${held_at}, meeting=${meeting_name}）: $filename"
        fi
        skipped=$((skipped + 1))
        continue
    fi

    # processing/ に移動（再投入防止）
    dest="$PROCESSING_DIR/$filename"
    mv "$m4a_file" "$dest"
    pm_db=$(get_pm_db "$meeting_name")
    log "[QUEUE] $filename → held_at=$held_at, meeting_name=$meeting_name, db=$(basename "$pm_db")"

    BATCH_FILES+=("$dest")
    BATCH_HELD_AT+=("$held_at")
    BATCH_NAMES+=("$meeting_name")
    BATCH_DBS+=("$pm_db")
done

if [[ ${#BATCH_FILES[@]} -eq 0 ]]; then
    log "投入対象なし（スキップ=$skipped 件）"
    log "=== 完了 ==="
    exit 0
fi

# --------------------------------------------------------------------------- #
# パーティション選択
# --------------------------------------------------------------------------- #
has_available_nodes() {
    sinfo -p "$1" --noheader -o "%t" 2>/dev/null | grep -qE "^(idle|mix)$"
}

if has_available_nodes "ai-h100l-pu"; then
    PARTITION="ai-h100l-pu"
    EXTRA_OPTS=""
    TIME_LIMIT="00:30:00"
    log "[INFO] ai-h100l-pu に空きあり → ai-h100l-pu に投入"
elif has_available_nodes "ai-l40s"; then
    PARTITION="ai-l40s"
    EXTRA_OPTS="--gpus=1"
    TIME_LIMIT="24:00:00"
    log "[INFO] ai-h100l-pu は空きなし、ai-l40s に空きあり → ai-l40s に投入"
elif has_available_nodes "qc-gh200"; then
    PARTITION="qc-gh200"
    EXTRA_OPTS=""
    TIME_LIMIT="24:00:00"
    log "[INFO] ai-h100l-pu/ai-l40s は空きなし、qc-gh200 に空きあり → qc-gh200 に投入"
else
    PARTITION="ai-h100l-pu"
    EXTRA_OPTS=""
    TIME_LIMIT="00:30:00"
    log "[INFO] 全パーティション混雑 → デフォルト ai-h100l-pu に投入"
fi

# --------------------------------------------------------------------------- #
# 一時バッチスクリプトを生成（全ファイルを1ジョブで順次処理）
# SLURM_JOB_ID が設定された環境で recording_to_pm.sh を呼ぶと処理モードに入る
# --------------------------------------------------------------------------- #
BATCH_SCRIPT=$(mktemp /tmp/auto_batch_XXXXXX.sh)
{
    echo "#!/bin/bash"
    echo "#SBATCH --nodes=1"
    echo "#SBATCH --time=${TIME_LIMIT}"
    echo ""
    if [[ -n "$SLACK_CHANNEL" ]]; then
        echo ". ~/.secrets/slack_tokens.sh"
        echo ""
    fi
    for i in "${!BATCH_FILES[@]}"; do
        printf "bash '%s/recording_to_pm.sh' '%s' --held-at '%s' --meeting-name '%s' --db '%s'\n" \
            "$SCRIPT_DIR" "${BATCH_FILES[$i]}" "${BATCH_HELD_AT[$i]}" "${BATCH_NAMES[$i]}" "${BATCH_DBS[$i]}"
        if [[ -n "$SLACK_CHANNEL" ]]; then
            printf "if [[ \$? -eq 0 ]]; then\n"
            printf "  '%s' '%s/pm_minutes_import.py' --post-to-slack --meeting-name '%s' --held-at '%s' -c '%s'\n" \
                "$VENV_PYTHON" "$SCRIPT_DIR" "${BATCH_NAMES[$i]}" "${BATCH_HELD_AT[$i]}" "$SLACK_CHANNEL"
            printf "fi\n"
        fi
        echo ""
    done
} > "$BATCH_SCRIPT"

# --------------------------------------------------------------------------- #
# sbatch 投入
# --------------------------------------------------------------------------- #
file_list=$(printf "'%s' " "${BATCH_FILES[@]}")
log "[SUBMIT] ${#BATCH_FILES[@]} 件を1ジョブで投入: $file_list"

# shellcheck disable=SC2086
JOB_OUTPUT=$(sbatch --partition="$PARTITION" $EXTRA_OPTS "$BATCH_SCRIPT" 2>&1)
log "$JOB_OUTPUT"

rm -f "$BATCH_SCRIPT"

log "=== 完了: 投入=${#BATCH_FILES[@]} 件, スキップ=$skipped 件 ==="
