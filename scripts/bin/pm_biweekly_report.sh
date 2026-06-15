#!/bin/sh

VENV_PYTHON="$HOME/.venv_$(uname -m)/bin/python3"

$VENV_PYTHON scripts/pm_biweekly_report.py --filter "リーダー会議系" --filter "HPCアプリケーションWG系" --filter  "ベンチマークWG系" --filter "アプリケーション開発エリア系" --filter "リーダー会議" --filter "HPCアプリケーションWG" --filter "ベンチマークWG" --filter "アプリケーション開発エリア"
