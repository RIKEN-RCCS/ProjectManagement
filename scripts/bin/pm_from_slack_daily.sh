#!/bin/bash

# argus_config.yaml のチャンネル定義に基づく日次 Slack 取得・pm.db 抽出
#
# 2026-05-17: action_items / decisions の管理を pm.db に一本化。
# pm-hpc.db / pm-pmo.db / pm-personal.db への分岐は廃止し、すべてのチャンネルを
# pm.db に投入する。FTS5 検索インデックス（qa_pm-hpc.db 等）の分割は継続するが、
# その更新は末尾の pm_embed.py が argus_config.yaml の channels 定義に従って行う。
#
# チャンネル一覧の出典は argus_config.yaml の indices.pm-all.channels。
# グループ別コメントは見通し用で機能には影響しない。

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

BASEDIR="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement"
LOGFILE="${BASEDIR}/logs/pm_from_slack_daily_$(date +%Y%m%d_%H%M%S).log"
RUN="bash ${BASEDIR}/scripts/pm_from_slack.sh"
DB="${BASEDIR}/data/pm.db"

touch $LOGFILE

# Canvas → pm.db 同期はループ前に一度だけ実施する。
# pm_from_slack.sh 側にも同期処理が入っているが、PM_CANVAS_SYNC_DONE=1 を
# export しておくことで子プロセスでの重複実行をスキップする。
. ~/.secrets/slack_tokens.sh
[ -f ~/.secrets/rivault_tokens.sh ] && . ~/.secrets/rivault_tokens.sh
SCRIPT_DIR_DAILY="${BASEDIR}/scripts"
PYTHON3="${HOME}/.venv_$(uname -m)/bin/python3"
SCRIPT_DIR="$SCRIPT_DIR_DAILY" PYTHON3="$PYTHON3" \
    bash -c '. "$SCRIPT_DIR/_lib_sync_canvas.sh"; sync_canvas_before_pm_update "$1"' \
    _ "$DB" >> $LOGFILE 2>&1
export PM_CANVAS_SYNC_DONE=1

# --- pm / リーダー会議系 ---
$RUN -c C08M002D7TQ --db-pm $DB >> $LOGFILE 2>&1   # 00_次世代計算基盤開発部門
$RUN -c C097A2P387R --db-pm $DB >> $LOGFILE 2>&1   # 09_エリアリーダー_テクニカル
$RUN -c C08SXA4M7JT --db-pm $DB >> $LOGFILE 2>&1   # 20_1_リーダ会議メンバ

# --- HPC アプリ WG / コデザイン ---
$RUN -c C09MDALKEUQ --db-pm $DB >> $LOGFILE 2>&1   # 10-9_architecture-benchmark
$RUN -c C094C73FSKB --db-pm $DB >> $LOGFILE 2>&1   # 13_コデザイン検討会_理研内
$RUN -c C08M0249GRL --db-pm $DB >> $LOGFILE 2>&1   # 20_アプリケーション開発エリア
$RUN -c C08LSJP4R6K --db-pm $DB >> $LOGFILE 2>&1   # 21_hpcアプリケーションwg
$RUN -c C093DQFSCRH --db-pm $DB >> $LOGFILE 2>&1   # 21_1_ブロック1
$RUN -c C093LP1J15G --db-pm $DB >> $LOGFILE 2>&1   # 21_2_ブロック2
$RUN -c C0A6AC59AHM --db-pm $DB >> $LOGFILE 2>&1   # 24_ai-hpc-application
$RUN -c C0B2ATLPLAG --db-pm $DB >> $LOGFILE 2>&1   # scienceapp-ai-codevelopment

# --- ベンチマーク WG ---
$RUN -c C08MJ0NF5UZ --db-pm $DB >> $LOGFILE 2>&1   # 22_ベンチマークwg
$RUN -c C096ER1A0LU --db-pm $DB >> $LOGFILE 2>&1   # 23_benchmark_framework

# --- 富士通-理研 ---
$RUN -c C09EJLFES11 --db-pm $DB >> $LOGFILE 2>&1   # 5100_general
$RUN -c C0949TUE33P --db-pm $DB >> $LOGFILE 2>&1   # 5101_アーキテクチャ
$RUN -c C094CTHUPTN --db-pm $DB >> $LOGFILE 2>&1   # 5102_コデザイン
$RUN -c C0949TWGMFX --db-pm $DB >> $LOGFILE 2>&1   # 5103_プログラミング環境
$RUN -c C094ARMCHK4 --db-pm $DB >> $LOGFILE 2>&1   # 5104_数値計算ライブラリ
$RUN -c C094CTQUXRS --db-pm $DB >> $LOGFILE 2>&1   # 5105_通信ライブラリ
$RUN -c C094Z4XKYGG --db-pm $DB >> $LOGFILE 2>&1   # 5106_ai
$RUN -c C094715A23Y --db-pm $DB >> $LOGFILE 2>&1   # 5107_インテグレーション
$RUN -c C093Y781T1V --db-pm $DB >> $LOGFILE 2>&1   # 5108_運用システム
$RUN -c C0949U7983X --db-pm $DB >> $LOGFILE 2>&1   # 5109_ストレージ
$RUN -c C0AU688SQFL --db-pm $DB >> $LOGFILE 2>&1   # 5110_アーキ_施設
$RUN -c C0A1H6PP82C --db-pm $DB >> $LOGFILE 2>&1   # 5102_subwg3
$RUN -c C0A1H7EF324 --db-pm $DB >> $LOGFILE 2>&1   # 5102_subwg6

# --- NVIDIA-理研 ---
$RUN -c C09D7GK0QSV --db-pm $DB >> $LOGFILE 2>&1   # 6102_codesign
$RUN -c C0A07EAKKSB --db-pm $DB >> $LOGFILE 2>&1   # 6102_subwg1
$RUN -c C0A0LS4C1UN --db-pm $DB >> $LOGFILE 2>&1   # 6102_subwg2
$RUN -c C0A11QXGLKT --db-pm $DB >> $LOGFILE 2>&1   # 6102_subwg3
$RUN -c C0A0KEMJM29 --db-pm $DB >> $LOGFILE 2>&1   # 6102_subwg4
$RUN -c C0A11R260JV --db-pm $DB >> $LOGFILE 2>&1   # 6102_subwg5
$RUN -c C0A0GG4ULLT --db-pm $DB >> $LOGFILE 2>&1   # 6102_subwg6
$RUN -c C09DUURNB47 --db-pm $DB >> $LOGFILE 2>&1   # 6100_general
$RUN -c C09CUH1SSBY --db-pm $DB >> $LOGFILE 2>&1   # 6103_programming_env
$RUN -c C09CE38SDFZ --db-pm $DB >> $LOGFILE 2>&1   # 6104_numerical_library
$RUN -c C09DMHJA5MW --db-pm $DB >> $LOGFILE 2>&1   # 6105_communication
$RUN -c C09CUH37NTG --db-pm $DB >> $LOGFILE 2>&1   # 6106_ai_software
$RUN -c C09DMHK10C8 --db-pm $DB >> $LOGFILE 2>&1   # 6107_construction
$RUN -c C09CUH5RTP0 --db-pm $DB >> $LOGFILE 2>&1   # 6108_operation
$RUN -c C09CE3C3C4X --db-pm $DB >> $LOGFILE 2>&1   # 6109_storage

# --- F-N-R (3者) ---
$RUN -c C09FFN6725N --db-pm $DB >> $LOGFILE 2>&1   # 7100_general
$RUN -c C09CPFSJG67 --db-pm $DB >> $LOGFILE 2>&1   # 7101_architecture
$RUN -c C09CYEV4BV2 --db-pm $DB >> $LOGFILE 2>&1   # 7102_codesign
$RUN -c C09CUHNRW6A --db-pm $DB >> $LOGFILE 2>&1   # 7103-6_system_software
$RUN -c C09DMJ5P5J4 --db-pm $DB >> $LOGFILE 2>&1   # 7107_construction
$RUN -c C09CS0JFVL5 --db-pm $DB >> $LOGFILE 2>&1   # 7108_operation
$RUN -c C09CTDXFK4J --db-pm $DB >> $LOGFILE 2>&1   # 7109_storage

# --- PMO / 管理系 ---
$RUN -c C08PE3K9N72 --db-pm $DB >> $LOGFILE 2>&1   # 03_次世代計算基盤部門定例会議メンバ
$RUN -c C09AANCC649 --db-pm $DB >> $LOGFILE 2>&1   # 04_hpci計画推進委員会資料作成
$RUN -c C0936JBQVGQ --db-pm $DB >> $LOGFILE 2>&1   # 50_富士通_理研_admin
$RUN -c C099LH46K36 --db-pm $DB >> $LOGFILE 2>&1   # 60_nvidia_riken_admin
$RUN -c C09CVJK9TNC --db-pm $DB >> $LOGFILE 2>&1   # 70_f-n-r_admin
$RUN -c C0A5MRRP268 --db-pm $DB >> $LOGFILE 2>&1   # 91_基本設計技術報告書作成
$RUN -c C0AS2JKS200 --db-pm $DB >> $LOGFILE 2>&1   # 92_詳細設計1技術報告書作成2026
$RUN -c C09JMEA157E --db-pm $DB >> $LOGFILE 2>&1   # 92_詳細設計調達

# --- FTS5 インデックス再構築 ---
cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement && python3 ${BASEDIR}/scripts/pm_embed.py >> $LOGFILE 2>&1
