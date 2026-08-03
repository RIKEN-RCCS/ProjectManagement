#!/bin/bash

# argus_config.yaml のチャンネル定義に基づく日次 Slack 取得・pm.db 抽出
#
# 2026-05-17: action_items / decisions の管理を pm.db に一本化。
# pm-hpc.db / pm-pmo.db / pm-personal.db への分岐は廃止し、すべてのチャンネルを
# pm.db に投入する。FTS5 検索インデックス（qa_pm-hpc.db 等）の分割は継続するが、
# その更新は末尾の pm_embed.py が argus_config.yaml の channels 定義に従って行う。
#
# チャンネル一覧の出典は argus_config.yaml の indices.pm-all.channels。

_arch="$(uname -m)"
if [[ "$_arch" == "aarch64" ]]; then
    . "$HOME/.venv_aarch64/bin/activate"
elif [[ "$_arch" == "x86_64" ]]; then
    . "$HOME/.venv_x86_64/bin/activate"
else
    echo "Unknown architecture: $_arch"; exit 1
fi

# cron 実行時に box CLI (Node 製) が見つかるよう PATH を補う
export PATH="$HOME/.nvm_arm64/versions/node/v20.19.5/bin:$PATH"
command -v box >/dev/null 2>&1 || echo "[WARN] box CLI が PATH に見つかりません（nvm の node バージョン変更を確認）" >&2

# net_guard（外向き通信の allow-list、docs/security-architecture.md §4.7 層1）を
# enforce にする。2026-08-02 の観測修正後、cron 5 本すべてで宛先が記録されるように
# なり deny 0 件を確認した上で展開した。退避が必要なときは
# ARGUS_NETGUARD=warn を付けて実行する。
# ここで export しておくことで、子プロセスの pm_from_slack.sh にも継承される。
export ARGUS_NETGUARD="${ARGUS_NETGUARD:-enforce}"

BASEDIR="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement"
LOGFILE="${BASEDIR}/logs/pm_from_slack_daily_$(date +%Y%m%d_%H%M%S).log"
RUN="bash ${BASEDIR}/scripts/bin/pm_from_slack.sh"
DB="${BASEDIR}/data/pm.db"

touch $LOGFILE

# Canvas → pm.db 同期はループ前に一度だけ実施する。
# pm_from_slack.sh 側にも同期処理が入っているが、PM_CANVAS_SYNC_DONE=1 を
# export しておくことで子プロセスでの重複実行をスキップする。
. ~/.secrets/slack_tokens.sh
[ -f ~/.secrets/rivault_tokens.sh ] && . ~/.secrets/rivault_tokens.sh
# EMBED_API_BASE 等（ローカル embedding サービング）。ingest/slack.py が embed を使う
[ -f ~/.secrets/localLLM.sh ] && . ~/.secrets/localLLM.sh
SCRIPT_DIR_DAILY="${BASEDIR}/scripts"
PYTHON3="${HOME}/.venv_$(uname -m)/bin/python3"
SCRIPT_DIR="$SCRIPT_DIR_DAILY" PYTHON3="$PYTHON3" \
    bash -c '. "$SCRIPT_DIR/bin/_lib_sync_canvas.sh"; sync_canvas_before_pm_update "$1"' \
    _ "$DB" >> $LOGFILE 2>&1
export PM_CANVAS_SYNC_DONE=1

# チャンネル一覧は argus_config.yaml の indices.pm-all.channels から取得する
# （グループ別コメント付きの固定リストは廃止。カテゴリ表示は channel_names を
#   参照する Web UI / レポート側に任せる）。
mapfile -t CHANNELS < <("$PYTHON3" -c "
import sys
import yaml
from pathlib import Path
cfg_path = Path('$BASEDIR/data/argus_config.yaml')
if not cfg_path.exists():
    sys.exit(1)
cfg = yaml.safe_load(cfg_path.read_text()) or {}
for ch in (cfg.get('indices', {}).get('pm-all', {}).get('channels') or []):
    print(ch)
")
if [[ ${#CHANNELS[@]} -eq 0 ]]; then
    echo "[ERROR] argus_config.yaml の indices.pm-all.channels からチャンネル一覧を取得できませんでした" >> $LOGFILE 2>&1
    exit 1
fi

for ch in "${CHANNELS[@]}"; do
    $RUN -c "$ch" --db-pm $DB >> $LOGFILE 2>&1
done

# --- FTS5 インデックス再構築 ---
cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement && "$PYTHON3" ${BASEDIR}/scripts/data-pipeline/pm_embed.py --data-dir "${BASEDIR}/data" >> $LOGFILE 2>&1
