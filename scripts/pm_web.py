#!/usr/bin/env python3
"""
pm_web.py — pm.db ローカル編集 Web UI（NiceGUI + AG Grid）

起動方法:
    source ~/.secrets/slack_tokens.sh
    bash scripts/pm_web_start.sh

環境変数:
    PM_WEB_PORT     ポート番号（デフォルト: 8501）
    PM_WEB_DB       DBパス（デフォルト: data/pm.db）
    PM_WEB_NO_ENCRYPT  1 を設定すると平文モード
"""

# NOTE: pm_web.py は非推奨です。現用の Web UI は pm_api.py（FastAPI）です。
import json
import os
import sys
from pathlib import Path

from fastapi import Request
from nicegui import ui

sys.path.insert(0, str(Path(__file__).parent))
from web_utils import (
    scan_pm_dbs, get_conn as _get_conn_raw, audit as _audit,
    load_milestones, load_action_items,
    load_decisions, load_minutes_content, do_save_action_items, do_save_decisions,
)

# --------------------------------------------------------------------------- #
# 設定（環境変数で上書き可）
# --------------------------------------------------------------------------- #
_REPO = Path(__file__).parent.parent
PORT       = int(os.environ.get("PM_WEB_PORT", 8501))
DB_PATH    = Path(os.environ.get("PM_WEB_DB", _REPO / "data" / "pm.db"))
NO_ENCRYPT = os.environ.get("PM_WEB_NO_ENCRYPT", "").lower() in ("1", "true", "yes")

def get_conn(db_path: Path | None = None):
    """モジュールレベルの DB_PATH/NO_ENCRYPT を使って接続を返す薄いラッパー。"""
    return _get_conn_raw(db_path or DB_PATH, no_encrypt=NO_ENCRYPT)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

@ui.page("/")
def page():
    state = {"conn": get_conn(), "db_path": str(DB_PATH)}
    milestone_map = {"current": load_milestones(state["conn"])}

    ui.page_title("PM DB Editor")

    ui.query("body > .q-layout").classes("flex flex-col h-screen")
    ui.query(".q-page-container").classes("flex flex-col flex-1 overflow-hidden")
    ui.query(".q-page").classes("flex flex-col flex-1 overflow-hidden")

    with ui.header(elevated=True).classes("bg-blue-800 text-white px-4 py-2 flex-none"):
        ui.label("PM DB Editor").classes("text-xl font-bold")

    ui.add_head_html("""
    <style>
      body, .nicegui-content { height: 100vh; overflow: hidden; }
      .q-tab-panels { flex: 1; min-height: 0; }
      .q-tab-panels > .q-panel { height: 100%; overflow-y: auto; }
    </style>
    """)

    # --- DB切り替え ---
    db_candidates = scan_pm_dbs()
    # 現在のDB_PATHが候補に含まれなければ先頭に追加
    if str(DB_PATH) not in db_candidates:
        db_candidates.insert(0, str(DB_PATH))

    with ui.row().classes("items-center gap-2 px-4 py-1 flex-none bg-gray-100"):
        sel_db = ui.select(
            options={p: Path(p).name for p in db_candidates},
            value=str(DB_PATH),
            label="DB",
        ).classes("w-64")

        def switch_db():
            p = Path(sel_db.value)
            if not p.exists():
                ui.notify(f"ファイルが見つかりません: {p}", type="negative")
                return
            try:
                new_conn = get_conn(p)
            except Exception as e:
                ui.notify(f"DB接続エラー: {e}", type="negative")
                return
            state["conn"] = new_conn
            state["db_path"] = str(p)
            milestone_map["current"] = load_milestones(new_conn)
            ms_keys = ["すべて"] + list(milestone_map["current"].keys())
            sel_ms.options = ms_keys
            sel_ms.value = "すべて"
            sel_ms.update()
            # マイルストーン選択肢をグリッドにも反映
            for col_def in _ai_ref["grid"].options["columnDefs"]:
                if col_def["field"] == "milestone_id":
                    col_def["cellEditorParams"]["values"] = [""] + list(milestone_map["current"].keys())
            ui.notify(f"DB を切り替えました: {p.name}", type="positive")
            refresh_ai()
            refresh_dec()

        sel_db.on("update:model-value", lambda: switch_db())

    with ui.tabs().classes("w-full flex-none") as tabs:
        tab_ai  = ui.tab("アクションアイテム")
        tab_dec = ui.tab("決定事項")

    with ui.tab_panels(tabs, value=tab_ai).classes("w-full flex-1").style("min-height: 0"):

        # ================================================================
        # アクションアイテム
        # ================================================================
        with ui.tab_panel(tab_ai).classes("flex flex-col p-2"):

            # --- フィルター ---
            with ui.row().classes("items-end gap-4 mb-1 flex-none"):
                sel_status = ui.select(
                    ["open", "closed", "すべて"], value="open", label="ステータス"
                ).classes("w-32")
                sel_ms = ui.select(
                    ["すべて"] + list(milestone_map["current"].keys()), value="すべて", label="マイルストーン"
                ).classes("w-44")
                sel_del_ai = ui.select(
                    ["非削除", "削除のみ", "すべて"], value="非削除", label="削除"
                ).classes("w-28")
                inp_since = ui.input(label="発生日（以降）YYYY-MM-DD").classes("w-44")
                ui.button("検索", icon="search", on_click=lambda: refresh_ai()).props("flat")

            ai_df = {"current": load_action_items(state["conn"], "open", "すべて", None)}
            _ai_ref = {}  # グリッドへの前方参照用

            # --- 保存・操作ボタン（グリッドの上・スクロール対象外） ---
            async def save_ai():
                await _ai_ref["grid"].run_grid_method("stopEditing")
                rows = await _ai_ref["grid"].get_client_data()
                n, conflicts = do_save_action_items(state["conn"], ai_df["current"], rows)
                if n > 0:
                    ui.notify(f"{n} フィールドを更新しました", type="positive")
                if conflicts:
                    lines = [f"ID:{c['id']} [{c['field']}] あなた: {c['yours']!r}  /  DB現在値: {c['db']!r}"
                             for c in conflicts]
                    ui.notify("競合のため保存できなかった変更があります（他ユーザーが先に更新）:\n" + "\n".join(lines),
                              type="warning", multi_line=True, close_button=True, timeout=0)
                if n == 0 and not conflicts:
                    ui.notify("変更はありませんでした", type="info")
                refresh_ai()

            def refresh_ai():
                ai_df["current"] = load_action_items(
                    state["conn"], sel_status.value, sel_ms.value,
                    inp_since.value or None, sel_del_ai.value,
                )
                _ai_ref["grid"].options["rowData"] = ai_df["current"].to_dict("records")
                _ai_ref["grid"].update()
                # トグル状態をリセット
                _ai_bulk["done"] = False
                _ai_bulk["deleted"] = False
                if "done" in _ai_btn: _ai_btn["done"].props("flat")
                if "del"  in _ai_btn: _ai_btn["del"].props("flat")

            _ai_bulk = {"done": False, "deleted": False}
            _ai_btn  = {}  # 前方参照用

            async def bulk_done_ai():
                _ai_bulk["done"] = not _ai_bulk["done"]
                val = _ai_bulk["done"]
                rows = await _ai_ref["grid"].get_client_data()
                for r in rows:
                    r["done"] = val
                _ai_ref["grid"].options["rowData"] = rows
                _ai_ref["grid"].update()
                _ai_btn["done"].props("color=primary" if val else "flat")
                ui.notify("全件を完了にしました（保存で確定）" if val else "全件の完了を解除しました（保存で確定）", type="info")

            async def bulk_del_ai():
                _ai_bulk["deleted"] = not _ai_bulk["deleted"]
                val = _ai_bulk["deleted"]
                rows = await _ai_ref["grid"].get_client_data()
                for r in rows:
                    r["deleted"] = val
                _ai_ref["grid"].options["rowData"] = rows
                _ai_ref["grid"].update()
                _ai_btn["del"].props("color=negative" if val else "flat")
                ui.notify("全件を削除にしました（保存で確定）" if val else "全件の削除を解除しました（保存で確定）", type="info")

            with ui.row().classes("gap-2 mb-1 flex-none items-center"):
                ui.button("保存", icon="save", on_click=save_ai, color="primary")
                _ai_btn["del"]  = ui.button("全件削除", on_click=bulk_del_ai).props("flat")
                _ai_btn["done"] = ui.button("全件完了", on_click=bulk_done_ai).props("flat")
                ui.button("再読み込み", icon="refresh", on_click=refresh_ai).props("flat")
                ui.button("＋ 新規追加", icon="add", on_click=lambda: add_ai_dialog.open()).props("outline")

            # フィルター変更で自動更新
            sel_status.on("update:model-value", lambda: refresh_ai())
            sel_ms.on("update:model-value",     lambda: refresh_ai())
            sel_del_ai.on("update:model-value", lambda: refresh_ai())

            # --- グリッド（残り高さを使用） ---
            _ai_ref["grid"] = ui.aggrid({
                "defaultColDef": {
                    "editable":    True,
                    "resizable":   True,
                    "sortable":    True,
                    "filter":      True,
                    "wrapText":    True,
                    "autoHeight":  True,
                },
                "columnDefs": [
                    {"field": "deleted",      "headerName": "削除",  "width": 50, "pinned": "left",
                     "cellRenderer": "agCheckboxCellRenderer",
                     "cellEditor": "agCheckboxCellEditor",
                     "cellRendererParams": {"disabled": False}},
                    {"field": "id",           "headerName": "ID",  "editable": False, "width": 50, "pinned": "left"},
                    {"field": "content",      "headerName": "内容",   "width": 380},
                    {"field": "assignee",     "headerName": "担当者", "width": 120},
                    {"field": "due_date",     "headerName": "期限",   "width": 110},
                    {"field": "milestone_id", "headerName": "MS",    "width": 60,
                     "cellEditor": "agSelectCellEditor",
                     "cellEditorParams": {"values": [""] + list(milestone_map["current"].keys())}},
                    {"field": "done",         "headerName": "完了",   "width": 80,
                     "cellRenderer": "agCheckboxCellRenderer",
                     "cellEditor": "agCheckboxCellEditor",
                     "cellRendererParams": {"disabled": False}},
                    {"field": "note",         "headerName": "対応状況", "width": 280},
                    {"field": "extracted_at", "headerName": "発生日",  "editable": False, "width": 110},
                    {"field": "source",       "headerName": "出典",   "editable": False, "width": 110,
                     ":cellRenderer": """(params) => {
                       const src = params.value || '';
                       const ref = (params.data || {}).source_ref || '';
                       const mid = (params.data || {}).meeting_id || '';
                       const s = 'cursor:pointer;color:#1565c0;text-decoration:underline';
                       if (src === 'slack' && ref) {
                         return '<span style="' + s + '">Slack</span>';
                       }
                       if (src === 'meeting') {
                         return '<span style="' + s + '">minutes</span>';
                       }
                       return src;
                     }"""},
                    {"field": "source_ref",   "headerName": "source_ref",   "hide": True},
                    {"field": "meeting_id",   "headerName": "meeting_id",   "hide": True},
                    {"field": "meeting_kind", "headerName": "meeting_kind", "hide": True},
                ],
                "rowData": ai_df["current"].to_dict("records"),
                "domLayout": "autoHeight",
                "stopEditingWhenCellsLoseFocus": True,
                "singleClickEdit": True,
            }).classes("w-full")

            async def open_minutes_for(meeting_id: str, kind: str = ""):
                import urllib.parse
                params = urllib.parse.urlencode({"id": meeting_id, "kind": kind})
                url = f"/minutes?{params}"
                await ui.run_javascript(
                    f"window.open({json.dumps(url)}, '_blank',"
                    f" 'width=960,height=780,scrollbars=yes,resizable=yes')"
                )

            async def on_ai_cell_clicked(e):
                args = e.args if isinstance(e.args, dict) else {}
                col = args.get("colId", "")
                row = args.get("data") or {}
                src = row.get("source", "")
                if col != "source":
                    return
                if src == "slack":
                    ref = row.get("source_ref", "")
                    if ref:
                        await ui.run_javascript(f"window.open({json.dumps(ref)}, '_blank')")
                elif src == "meeting":
                    mid = row.get("meeting_id", "")
                    kind = row.get("meeting_kind", "")  # meetings テーブルから解決済みの kind
                    if mid:
                        await open_minutes_for(mid, kind)

            _ai_ref["grid"].on("cellClicked", on_ai_cell_clicked)

            # --- 新規追加ダイアログ ---
            with ui.dialog() as add_ai_dialog, ui.card().classes("w-[600px]"):
                ui.label("新規アクションアイテム").classes("text-lg font-bold mb-2")
                f_content  = ui.textarea("内容 *").classes("w-full")
                with ui.row().classes("w-full gap-2"):
                    f_assignee = ui.input("担当者").classes("flex-1")
                    f_due      = ui.input("期限 (YYYY-MM-DD)").classes("flex-1")
                with ui.row().classes("w-full gap-2"):
                    f_ms     = ui.select([""] + list(milestone_map["current"].keys()), value="", label="マイルストーン").classes("flex-1")
                    f_status = ui.select(["open", "closed"], value="open", label="ステータス").classes("flex-1")
                f_note = ui.input("対応状況").classes("w-full")
                f_src  = ui.input("出典 (任意)").classes("w-full")

                def do_add_ai():
                    if not f_content.value.strip():
                        ui.notify("内容は必須です", type="negative"); return
                    state["conn"].execute(
                        "INSERT INTO action_items"
                        " (content,assignee,due_date,milestone_id,status,note,source,source_ref,extracted_at)"
                        " VALUES(?,?,?,?,?,?,'manual',?,?)",
                        (f_content.value.strip(), _nv(f_assignee.value), _nv(f_due.value),
                         _nv(f_ms.value), f_status.value, _nv(f_note.value), _nv(f_src.value),
                         datetime.now(timezone.utc).isoformat()),
                    )
                    state["conn"].commit()
                    ui.notify("追加しました", type="positive")
                    add_ai_dialog.close()
                    refresh_ai()

                with ui.row().classes("justify-end gap-2 mt-3"):
                    ui.button("キャンセル", on_click=add_ai_dialog.close).props("flat")
                    ui.button("追加", on_click=do_add_ai, color="primary")

        # ================================================================
        # 決定事項
        # ================================================================
        with ui.tab_panel(tab_dec).classes("flex flex-col p-2"):

            with ui.row().classes("items-end gap-4 mb-1 flex-none"):
                sel_del_dec = ui.select(
                    ["非削除", "削除のみ", "すべて"], value="非削除", label="削除"
                ).classes("w-28")
                inp_dsince = ui.input(label="発生日（以降）YYYY-MM-DD").classes("w-44")
                ui.button("検索", icon="search", on_click=lambda: refresh_dec()).props("flat")

            dec_df = {"current": load_decisions(state["conn"], "すべて", None)}
            _dec_ref = {}  # グリッドへの前方参照用

            # --- 保存・操作ボタン（グリッドの上・スクロール対象外） ---
            async def save_dec():
                await _dec_ref["grid"].run_grid_method("stopEditing")
                rows = await _dec_ref["grid"].get_client_data()
                n, conflicts = do_save_decisions(state["conn"], dec_df["current"], rows)
                if n > 0:
                    ui.notify(f"{n} フィールドを更新しました", type="positive")
                if conflicts:
                    lines = [f"ID:{c['id']} [{c['field']}] あなた: {c['yours']!r}  /  DB現在値: {c['db']!r}"
                             for c in conflicts]
                    ui.notify("競合のため保存できなかった変更があります（他ユーザーが先に更新）:\n" + "\n".join(lines),
                              type="warning", multi_line=True, close_button=True, timeout=0)
                if n == 0 and not conflicts:
                    ui.notify("変更はありませんでした", type="info")
                refresh_dec()

            async def ack_all():
                conn = state["conn"]
                now = datetime.now(timezone.utc).isoformat()
                unacked = dec_df["current"][dec_df["current"]["acknowledged_at"] == ""]
                for dec_id in unacked["id"].tolist():
                    _audit(conn, "decisions", int(dec_id), "acknowledged_at", None, now)
                    conn.execute("UPDATE decisions SET acknowledged_at=? WHERE id=?", (now, int(dec_id)))
                conn.commit()
                ui.notify(f"{len(unacked)} 件を確認済みにしました", type="positive")
                refresh_dec()

            def refresh_dec():
                dec_df["current"] = load_decisions(
                    state["conn"], "すべて", inp_dsince.value or None, sel_del_dec.value,
                )
                _dec_ref["grid"].options["rowData"] = dec_df["current"].to_dict("records")
                _dec_ref["grid"].update()

            with ui.row().classes("gap-2 mb-1 flex-none items-center"):
                ui.button("保存",         icon="save",    on_click=save_dec,  color="primary")
                ui.button("未確認を一括確認", icon="check_all", on_click=ack_all).props("flat")
                ui.button("再読み込み",   icon="refresh", on_click=refresh_dec).props("flat")
                ui.button("＋ 新規追加",  icon="add",     on_click=lambda: add_dec_dialog.open()).props("outline")

            sel_del_dec.on("update:model-value", lambda: refresh_dec())

            # --- グリッド（残り高さを使用） ---
            _dec_ref["grid"] = ui.aggrid({
                "defaultColDef": {
                    "editable":   True,
                    "resizable":  True,
                    "sortable":   True,
                    "filter":     True,
                    "wrapText":   True,
                    "autoHeight": True,
                },
                "columnDefs": [
                    {"field": "deleted",         "headerName": "削除",  "width": 50, "pinned": "left",
                     "cellRenderer": "agCheckboxCellRenderer",
                     "cellEditor": "agCheckboxCellEditor",
                     "cellRendererParams": {"disabled": False}},
                    {"field": "id",              "headerName": "ID",    "editable": False, "width": 50, "pinned": "left"},
                    {"field": "content",         "headerName": "内容",    "width": 500},


                    {"field": "extracted_at",    "headerName": "発生日",  "editable": False, "width": 110},
                    {"field": "source",          "headerName": "出典",   "editable": False, "width": 110,
                     ":cellRenderer": """(params) => {
                       const src = params.value || '';
                       const ref = (params.data || {}).source_ref || '';
                       const s = 'cursor:pointer;color:#1565c0;text-decoration:underline';
                       if (src === 'slack' && ref) {
                         return '<span style="' + s + '">Slack</span>';
                       }
                       if (src === 'meeting') {
                         return '<span style="' + s + '">minutes</span>';
                       }
                       return src;
                     }"""},
                    {"field": "source_ref", "headerName": "source_ref", "hide": True},
                ],
                "rowData": dec_df["current"].to_dict("records"),
                "domLayout": "autoHeight",
                "stopEditingWhenCellsLoseFocus": True,
                "singleClickEdit": True,
            }).classes("w-full")

            async def on_dec_cell_clicked(e):
                args = e.args if isinstance(e.args, dict) else {}
                col = args.get("colId", "")
                row = args.get("data") or {}
                src = row.get("source", "")
                if col != "source":
                    return
                if src == "slack":
                    ref = row.get("source_ref", "")
                    if ref:
                        await ui.run_javascript(f"window.open({json.dumps(ref)}, '_blank')")
                elif src == "meeting":
                    ref = row.get("source_ref", "")
                    if ref:
                        # source_ref is a file path like ".../meetings/2026-01-05_Leader_Meeting.md"
                        mid = Path(ref).stem      # "2026-01-05_Leader_Meeting"
                        # kind: stem[11:] → "Leader_Meeting"（決定事項は YYYY-MM-DD_Kind 形式が前提）
                        kind = mid[11:] if len(mid) > 11 else ""
                        await open_minutes_for(mid, kind)

            _dec_ref["grid"].on("cellClicked", on_dec_cell_clicked)

            with ui.dialog() as add_dec_dialog, ui.card().classes("w-[600px]"):
                ui.label("新規決定事項").classes("text-lg font-bold mb-2")
                fd_content = ui.textarea("内容 *").classes("w-full")
                with ui.row().classes("w-full gap-2"):
                    fd_date = ui.input("決定日 (YYYY-MM-DD)",
                                       value=datetime.now().strftime("%Y-%m-%d")).classes("flex-1")
                    fd_src  = ui.input("出典 (任意)").classes("flex-1")

                def do_add_dec():
                    if not fd_content.value.strip():
                        ui.notify("内容は必須です", type="negative"); return
                    state["conn"].execute(
                        "INSERT INTO decisions (content,decided_at,source,source_ref,extracted_at)"
                        " VALUES(?,?,'manual',?,?)",
                        (fd_content.value.strip(), _nv(fd_date.value),
                         _nv(fd_src.value), datetime.now(timezone.utc).isoformat()),
                    )
                    state["conn"].commit()
                    ui.notify("追加しました", type="positive")
                    add_dec_dialog.close()
                    refresh_dec()

                with ui.row().classes("justify-end gap-2 mt-3"):
                    ui.button("キャンセル", on_click=add_dec_dialog.close).props("flat")
                    ui.button("追加", on_click=do_add_dec, color="primary")




@ui.page("/minutes")
def minutes_page(request: Request):
    """議事録ポップアップ表示ページ。クエリパラメータ: id=meeting_id&kind=KindName"""
    meeting_id = request.query_params.get("id", "")
    kind       = request.query_params.get("kind", "")

    ui.page_title(f"議事録: {meeting_id or '?'}")
    ui.add_head_html("""
    <style>
      body { margin: 0; padding: 0; }
      .minutes-body { padding: 16px 24px; font-family: sans-serif; }
      .minutes-body h1,h2,h3 { margin-top: 1.2em; }
    </style>
    """)

    with ui.column().classes("w-full minutes-body"):
        with ui.row().classes("items-center gap-3 mb-3"):
            ui.label(f"📄 {meeting_id}").classes("text-xl font-bold flex-1")
            if kind:
                ui.badge(kind, color="blue")
            ui.button(icon="close", on_click=lambda: ui.run_javascript("window.close()")).props("flat round dense")

        if not meeting_id:
            ui.label("（meeting_id が指定されていません）").classes("text-red-500")
            return

        md = load_minutes_content(meeting_id, no_encrypt=NO_ENCRYPT, kind=kind)
        ui.markdown(md).classes("w-full")


ui.run(port=PORT, reload=False, title="PM DB Editor", show=False)
