# shellcheck shell=bash
# _lib_sync_canvas.sh
#
# pm.db を更新するスクリプトの先頭で source して使う共通ヘルパ。
#
# 進捗レポートの正本は Box 上の XLSX。pm.db の更新前に
# pm_xlsx_sync.py で XLSX 上の人手編集（アクションアイテム・決定事項）
# を pm.db に取り込み、続く ingest 処理で上書きされないようにする。
#
# 利用側で以下が定義されていることを前提:
#   PYTHON3      … python3 実行ファイルパス
#   SCRIPT_DIR   … scripts/ ディレクトリの絶対パス
#
# 失敗時は警告だけ出して 0 で抜ける（cron が止まらないようにするため）。

# Canvas ID の解決順:
#   1. 環境変数 PM_REPORT_CANVAS_ID（明示上書き用）
#   2. argus_config.yaml の report.canvas_id
#   3. 解決できなければ空文字を返す
_resolve_pm_report_canvas_id() {
    if [[ -n "${PM_REPORT_CANVAS_ID:-}" ]]; then
        echo "$PM_REPORT_CANVAS_ID"
        return 0
    fi
    local cfg
    cfg="$(dirname "$SCRIPT_DIR")/data/argus_config.yaml"
    if [[ ! -f "$cfg" ]]; then
        return 0
    fi
    "$PYTHON3" - "$cfg" <<'PY' 2>/dev/null || true
import sys
try:
    import yaml
except Exception:
    sys.exit(0)
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
except Exception:
    sys.exit(0)
report = cfg.get("report") or {}
cid = report.get("canvas_id")
if isinstance(cid, str) and cid:
    print(cid)
PY
}

sync_canvas_before_pm_update() {
    # 進捗レポートが Box XLSX 化されたため、Canvas 同期ではなく
    # pm_xlsx_sync.py を呼び出して XLSX → pm.db 反映を行う。
    # 関数名は呼び出し側との互換のためそのまま残す。
    local db="$1"
    if [[ "${PM_CANVAS_SYNC_DONE:-0}" == "1" ]]; then
        return 0
    fi
    echo "================================================================"
    echo "ステップ0: XLSX → pm.db 同期 (pm_xlsx_sync.py)"
    echo "  db        : $db"
    echo "================================================================"
    if ! "$PYTHON3" "$SCRIPT_DIR/reporting/pm_xlsx_sync.py" --db "$db"; then
        echo "[WARN] pm_xlsx_sync.py が失敗しました。Box XLSX 上の編集が pm.db に" \
             "反映されていない可能性がありますが、続行します。" >&2
    fi
    export PM_CANVAS_SYNC_DONE=1
    return 0
}
