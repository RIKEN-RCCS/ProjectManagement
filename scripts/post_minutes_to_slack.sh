#!/usr/bin/env bash
# post_minutes_to_slack.sh
#
# 議事録DBの内容をSlackチャンネルに投稿する（pm_minutes_import.py --post-to-slack のラッパー）。
#
# Usage:
#   bash scripts/post_minutes_to_slack.sh --meeting-name Leader_Meeting --held-at 2026-03-10 -c C08SXA4M7JT
#   bash scripts/post_minutes_to_slack.sh --meeting-name Leader_Meeting --held-at 2026-03-10 -c C08SXA4M7JT --force
#
# Options:
#   --meeting-name NAME    会議種別名（必須）
#   --held-at YYYY-MM-DD   開催日（必須）
#   -c / --channel ID      投稿先チャンネルID（必須）
#   --thread-ts TS         投稿先スレッドTS（省略: チャンネル直接投稿）
#   --force                投稿済みでも再投稿する
#   --dry-run              Slack API呼び出しなし・確認のみ
#   --no-encrypt           平文モード

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON3="${HOME}/.venv_x86_64/bin/python3"

. ~/.secrets/slack_tokens.sh

# --------------------------------------------------------------------------- #
# 引数パース
# --------------------------------------------------------------------------- #
MEETING_NAME=""
HELD_AT=""
CHANNEL=""
THREAD_TS=""
FORCE=""
DRY_RUN=""
NO_ENCRYPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --meeting-name)  MEETING_NAME="$2";          shift 2 ;;
        --held-at)       HELD_AT="$2";               shift 2 ;;
        -c|--channel)    CHANNEL="$2";               shift 2 ;;
        --thread-ts)     THREAD_TS="$2";             shift 2 ;;
        --force)         FORCE="--force";            shift   ;;
        --dry-run)       DRY_RUN="--dry-run";        shift   ;;
        --no-encrypt)    NO_ENCRYPT="--no-encrypt";  shift   ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$MEETING_NAME" ]]; then
    echo "[ERROR] --meeting-name が未指定です。" >&2; exit 1
fi
if [[ -z "$HELD_AT" ]]; then
    echo "[ERROR] --held-at が未指定です。" >&2; exit 1
fi
if [[ -z "$CHANNEL" ]]; then
    echo "[ERROR] -c / --channel が未指定です。" >&2; exit 1
fi

# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #
ARGS=(--post-to-slack --meeting-name "$MEETING_NAME" --held-at "$HELD_AT" -c "$CHANNEL")
[[ -n "$THREAD_TS" ]]  && ARGS+=(--thread-ts "$THREAD_TS")
[[ -n "$FORCE" ]]      && ARGS+=("$FORCE")
[[ -n "$DRY_RUN" ]]    && ARGS+=("$DRY_RUN")
[[ -n "$NO_ENCRYPT" ]] && ARGS+=("$NO_ENCRYPT")

"$PYTHON3" "$SCRIPT_DIR/pm_minutes_import.py" "${ARGS[@]}"
