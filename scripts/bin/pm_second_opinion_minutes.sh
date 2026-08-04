#!/usr/bin/env bash
# pm_second_opinion_minutes.sh
#
# 議事録経路への第2系統（独立系統）差分検査バッチ（pm_screen.py
# --second-opinion-minutes）を定期実行するラッパー。
# 議事録DB・pm.db の action_items/decisions は一切書き換えない（読み取りのみ）。
# 所見は pm.db の triage_second_opinion に記録される（レビューは
# pm_screen.py --list-findings / --mark-reviewed、滞留検査は
# pm_selfcheck.py の second_opinion_findings_stale が担う）。
#
# このバッチと録音経路（pm_from_recording.sh）の役割分担（2026-08 追加）:
#   - 録音経路（Console / 手動）は pm_from_recording.sh の末尾（Step 6）で、
#     処理した会議だけを --meeting-stem で絞り込んで即時検査する。所見が出る
#     までの遅延をなくし、人が Console で議事録を確認しているタイミングで
#     出すのが狙い。
#   - この週1バッチは「取りこぼしの掃除」を担う。直接インポート経路
#     （pm_minutes_import.py を単体で使う場合）、過去分、録音経路側で
#     インラインの第2系統検査自体が失敗・タイムアウトした会議はここでしか
#     拾えない。
#   - 両方の経路が同じ会議を検査しても所見は二重にならない。記録前に同じ会議・
#     同じ読み手（kind）の既存所見と突合し、重複は記録せず件数をログに出す
#     （2026-08-04 追加。読み手の抽出は再現しないため、同じ会議を複数回読ませて
#     検出を積み増す使い方が有効で、その前提として重複排除が要る）。
#     再現性そのものを測るときは --no-dedup-existing を付ける。
#
# --reader both にする理由: kind=minutes_extraction_recall（kimi-k3。
# recall チェック専用の読み手）と kind=minutes_extraction（R8対策の第2系統、
# Llama-4-Scout）は別系統・別目的であり、どちらか一方だけでは相手の
# 検出対象を取りこぼす（docs/pm_screen.py の --reader ヘッダコメント参照）。
#
# --limit 5 の根拠: kimi-k3 は1会議あたり数分〜十数分かかる（実測）。
# 5会議で概ね30〜60分程度に収まる見積り。
#
# **cron への登録はこのコミットでは行わない**（crontab の変更は PM 作業）。
# 登録例（週1回を推奨。会議は週に数本で、直近30日で37件のため日次は過剰。
# 日曜3:00 JST 想定）:
#   0 3 * * 0 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/bin/pm_second_opinion_minutes.sh
#
# Usage:
#   bash scripts/bin/pm_second_opinion_minutes.sh

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
    PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$ARCH" == "x86_64" ]]; then
    PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
    echo "Unknown architecture: $ARCH"; exit 1
fi

# net_guard（外向き通信の allow-list、docs/security-architecture.md §4.7 層1）を
# enforce にする（他の cron ラッパーと同じ既定。tests/selfcheck/test_cron_env_contract.py
# の契約検査対象）。退避が必要なときは ARGUS_NETGUARD=warn を付けて実行する。
export ARGUS_NETGUARD="${ARGUS_NETGUARD:-enforce}"

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# scripts/pm_screen.py（旧パスの symlink）経由で起動すること。pm_screen.py は
# Path(__file__).parent（resolve なし）で同階層の cli_utils.py/db_utils.py の
# symlink を前提に import しており、実体パス（quality/pm_screen.py）を直接
# 起動すると ModuleNotFoundError になる（tests/selfcheck/test_cli_help_smoke.py
# のヘッダコメント参照）。
PM_SCREEN="$SCRIPT_DIR/pm_screen.py"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/pm_second_opinion_minutes.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "======== pm_second_opinion_minutes.sh 開始: $(date '+%Y-%m-%d %H:%M:%S') ========"

[[ -f "$HOME/.secrets/rikyu_token.sh" ]] && source "$HOME/.secrets/rikyu_token.sh"
[[ -f "$HOME/.secrets/localLLM.sh" ]] && source "$HOME/.secrets/localLLM.sh"
[[ -f "$HOME/.secrets/rivault_tokens.sh" ]] && source "$HOME/.secrets/rivault_tokens.sh"

# 1実行が数十分かかるため、多重起動を防ぐ（pm_argus_patrol.sh と同じ形）。
LOCKFILE="$REPO_ROOT/data/.pm_second_opinion_minutes.lock"
flock -n "$LOCKFILE" \
    "$PYTHON3" -u "$PM_SCREEN" --second-opinion-minutes --reader both --limit 5
STATUS=$?

echo "======== pm_second_opinion_minutes.sh 完了: $(date '+%Y-%m-%d %H:%M:%S') (exit=$STATUS) ========"
exit "$STATUS"
