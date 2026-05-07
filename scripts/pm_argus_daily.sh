#!/bin/bash

# <CHANNEL_ID> | 20_アプリケーション開発エリア
# <CHANNEL_ID> | 20_1_リーダ会議メンバ
# <CHANNEL_ID> | 21_hpcアプリケーションwg
# <CHANNEL_ID> | 21_1_hpcアプリケーションwg_ブロック1
# <CHANNEL_ID> | 21_2_hpcアプリケーションwg_ブロック2
# <CHANNEL_ID> | 22_ベンチマークwg
# <CHANNEL_ID> | 23_benchmark_framework
# <CHANNEL_ID> | 24_ai-hpc-application
# <CHANNEL_ID> | 03_次世代計算基盤部門定例会議メンバ
# <CHANNEL_ID> | personal

. /home/users/hikaru.inoue/.venv_aarch64/bin/activate

LOGFILE="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_argus_daily_$(date +%Y%m%d_%H%M%S).log"

touch $LOGFILE

source /home/users/hikaru.inoue/.secrets/slack_tokens.sh
source /home/users/hikaru.inoue/.secrets/rivault_tokens.sh

python3 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/argus/pm_argus.py --brief-to-canvas --canvas-id <CANVAS_ID> >> $LOGFILE 2>&1
python3 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/argus/pm_argus.py --risk --canvas-id <CANVAS_ID> >> $LOGFILE 2>&1
