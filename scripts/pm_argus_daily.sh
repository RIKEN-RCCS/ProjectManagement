#!/bin/bash

# C08M0249GRL | 20_アプリケーション開発エリア
# C08SXA4M7JT | 20_1_リーダ会議メンバ
# C08LSJP4R6K | 21_hpcアプリケーションwg
# C093DQFSCRH | 21_1_hpcアプリケーションwg_ブロック1
# C093LP1J15G | 21_2_hpcアプリケーションwg_ブロック2
# C08MJ0NF5UZ | 22_ベンチマークwg
# C096ER1A0LU | 23_benchmark_framework
# C0A6AC59AHM | 24_ai-hpc-application
# C08PE3K9N72 | 03_次世代計算基盤部門定例会議メンバ
# C0A9KG036CS | personal

. /home/users/hikaru.inoue/.venv_aarch64/bin/activate

LOGFILE="/lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/logs/pm_argus_daily_$(date +%Y%m%d_%H%M%S).log"

touch $LOGFILE

source /home/users/hikaru.inoue/.secrets/slack_tokens.sh
source /home/users/hikaru.inoue/.secrets/rivault_tokens.sh

python3 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_argus.py --brief-to-canvas --canvas-id F0ATCN7E2D9 >> $LOGFILE 2>&1
python3 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_argus.py --risk --canvas-id F0ATN63JQV7 >> $LOGFILE 2>&1
