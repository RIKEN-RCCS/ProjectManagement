#!/usr/bin/env bash
# pm_selfcheck.sh
#
# pm_selfcheck.py（データ不変条件の読み取り専用検査）を定期実行するラッパー。
# DB書き込みは行わない。違反を検出した場合は非0で終了する
# （cron のメール通知で気づけるようにするため、set -e は使わずそのまま伝搬する）。
#
# Usage:
#   bash scripts/bin/pm_selfcheck.sh
#
# cron登録例（毎朝06:30 JST、月〜金）:
#   30 6 * * 1-5 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/bin/pm_selfcheck.sh

ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
    PYTHON3="$HOME/.venv_aarch64/bin/python3"
elif [[ "$ARCH" == "x86_64" ]]; then
    PYTHON3="$HOME/.venv_x86_64/bin/python3"
else
    echo "Unknown architecture: $ARCH"; exit 1
fi

# net_guard（外向き通信の allow-list、docs/security-architecture.md §4.7 層1）を
# enforce にする。2026-08-02 の観測修正後、cron 5 本すべてで宛先が記録されるように
# なり deny 0 件を確認した上で展開した。退避が必要なときは
# ARGUS_NETGUARD=warn を付けて実行する。
export ARGUS_NETGUARD="${ARGUS_NETGUARD:-enforce}"

_BASH_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$_BASH_SELF_DIR")" == "bin" ]]; then
  SCRIPT_DIR="$(cd "$_BASH_SELF_DIR/.." && pwd)"
else
  SCRIPT_DIR="$_BASH_SELF_DIR"
fi
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# 外部アンカー（config/anchors/tool_call_anchor.jsonl）を専用ブランチ `anchors`
# へ commit+push する（docs/security-architecture.md §4.4）。
#
# **git の低レベルコマンド（plumbing）のみを使う** — `git add`/`git commit`/
# `git checkout` は作業ツリー・インデックスを汚し main 上の開発者の作業と
# 衝突するため使わない。ここで使う hash-object/mktree/commit-tree/update-ref/
# push はどれも作業ツリー・インデックス・HEAD（main）に一切触れない。
#
# アンカーファイルの内容が前回 publish 時の commit と同じであれば no-op
# （commit も push もしない）。push が失敗した場合は ERROR を出し、非0を返す
# （push できなければアンカーは外部化されておらず、存在しないのと同じ）。
publish_anchor_branch() {
    local anchor_file="$1"
    local blob tree old_tree commit rows date_str

    if [[ ! -f "$anchor_file" ]]; then
        echo "[INFO] anchor_publish: アンカーファイルが見当たりません（$anchor_file）。publish をスキップします"
        return 0
    fi

    blob=$(git -C "$REPO_ROOT" hash-object -w "$anchor_file") || {
        echo "ERROR: anchor_publish: git hash-object に失敗しました"
        return 1
    }
    tree=$(printf '100644 blob %s\ttool_call_anchor.jsonl\n' "$blob" | git -C "$REPO_ROOT" mktree) || {
        echo "ERROR: anchor_publish: git mktree に失敗しました"
        return 1
    }

    old_tree=""
    if git -C "$REPO_ROOT" rev-parse --verify --quiet refs/heads/anchors >/dev/null 2>&1; then
        old_tree=$(git -C "$REPO_ROOT" rev-parse --quiet 'refs/heads/anchors^{tree}' 2>/dev/null)
    fi

    if [[ -n "$old_tree" && "$old_tree" == "$tree" ]]; then
        echo "OK: anchor_publish: アンカーの内容が前回 publish 時と同じです。commit/push をスキップします"
        return 0
    fi

    rows=$(wc -l < "$anchor_file" | tr -d '[:space:]')
    date_str=$(date -u '+%Y-%m-%d')

    if [[ -n "$old_tree" ]]; then
        commit=$(git -C "$REPO_ROOT" commit-tree "$tree" -p refs/heads/anchors -m "anchor: ${date_str} rows=${rows}")
    else
        commit=$(git -C "$REPO_ROOT" commit-tree "$tree" -m "anchor: ${date_str} rows=${rows}")
    fi
    if [[ -z "$commit" ]]; then
        echo "ERROR: anchor_publish: git commit-tree に失敗しました"
        return 1
    fi

    if ! git -C "$REPO_ROOT" update-ref refs/heads/anchors "$commit"; then
        echo "ERROR: anchor_publish: git update-ref refs/heads/anchors に失敗しました"
        return 1
    fi

    if ! git -C "$REPO_ROOT" push origin anchors:anchors; then
        echo "ERROR: anchor_publish: git push origin anchors:anchors に失敗しました"
        echo "ERROR: anchor_publish: push されていないアンカーは外部化されておらず、アンカーとして機能していません"
        return 1
    fi

    echo "OK: anchor_publish: anchors ブランチへ commit+push しました rows=${rows} commit=${commit}"
    return 0
}

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/pm_selfcheck.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "======== pm_selfcheck.sh 開始: $(date '+%Y-%m-%d %H:%M:%S') ========"

# モデル pin のドリフト検査（model_pin_drift）がエンドポイントへ到達するために必要。
# source しないと常に判定不能になり、検査が入っているのに何も見ていない状態になる。
[[ -f "$HOME/.secrets/rikyu_token.sh" ]] && source "$HOME/.secrets/rikyu_token.sh"
[[ -f "$HOME/.secrets/localLLM.sh" ]] && source "$HOME/.secrets/localLLM.sh"
[[ -f "$HOME/.secrets/rivault_tokens.sh" ]] && source "$HOME/.secrets/rivault_tokens.sh"

"$PYTHON3" "$SCRIPT_DIR/quality/pm_selfcheck.py" --days 7 "$@"
STATUS=$?

# 外部アンカー（§4.4）の記録は検査の成否によらず行う。検査が壊れているときこそ
# 「連鎖の頭が動いているか」の記録が要るため、STATUS が非0でもここは実行する。
"$PYTHON3" "$SCRIPT_DIR/quality/pm_selfcheck.py" --emit-anchor

# アンカーを anchors ブランチへ commit+push する。これも検査の成否によらず行う。
publish_anchor_branch "$REPO_ROOT/config/anchors/tool_call_anchor.jsonl"
PUBLISH_STATUS=$?

# 検査本体の違反判定を上書きしない。検査がすでに非0（違反あり）ならそれを優先し、
# 検査が0（違反なし）だった場合にのみ publish の失敗を STATUS へ反映する。
if [[ "$STATUS" -eq 0 && "$PUBLISH_STATUS" -ne 0 ]]; then
    STATUS="$PUBLISH_STATUS"
fi

echo "======== pm_selfcheck.sh 完了: $(date '+%Y-%m-%d %H:%M:%S') (exit=$STATUS) ========"
exit "$STATUS"
