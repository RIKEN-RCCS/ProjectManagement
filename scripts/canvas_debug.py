#!/usr/bin/env python3
"""
canvas_debug.py

Slack Canvas の詳細状態を表示するデバッグ用スクリプト。
Canvas に投稿しても既存内容が残ってしまう場合の原因調査・クリーンアップに使う。

Usage:
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --show-raw
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --delete-all
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --recreate
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --recreate --title "Summary"

Options:
    --canvas-id ID     対象 Canvas ID（必須）
    --show-raw         files.info 生レスポンスと url_private 全文を表示
    --delete-all       セクション単位でコンテンツ削除（h1 タイトルは保持）
    --include-title    --delete-all 時に h1 タイトルも削除対象にする
    --recreate         Canvas を削除して新規作成（テーブル等も完全消去）
                       !! 新しい Canvas ID が発行される。CLAUDE.md の更新が必要 !!
    --title TEXT       --recreate 時の Canvas タイトル（デフォルト: 元のタイトル）
    --yes              確認プロンプトをスキップ
    --output PATH      結果をファイルにも保存
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

REPO_ROOT = Path(__file__).resolve().parent.parent
CANVAS_MAP_PATH = REPO_ROOT / "data" / "canvas_map.json"


# --------------------------------------------------------------------------- #
# Canvas ID マップ（チャンネルID ↔ Canvas ID の永続ストア）
# --------------------------------------------------------------------------- #

def load_canvas_map() -> dict:
    """data/canvas_map.json を読み込む。存在しなければ空辞書を返す。"""
    if CANVAS_MAP_PATH.exists():
        try:
            return json.loads(CANVAS_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_canvas_map(m: dict) -> None:
    """data/canvas_map.json に書き込む。"""
    CANVAS_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANVAS_MAP_PATH.write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def map_set(channel_id: str, canvas_id: str, title: str = "") -> None:
    """チャンネルID → Canvas ID のエントリを保存する。"""
    from datetime import datetime, timezone
    m = load_canvas_map()
    m[channel_id] = {
        "canvas_id": canvas_id,
        "title": title,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_canvas_map(m)


def map_get(channel_id: str) -> str | None:
    """チャンネルID から Canvas ID を返す。未登録なら None。"""
    m = load_canvas_map()
    entry = m.get(channel_id)
    if isinstance(entry, dict):
        return entry.get("canvas_id")
    if isinstance(entry, str):
        return entry  # 旧形式互換
    return None


def cmd_list_map(p) -> None:
    """登録済みチャンネル→Canvas IDの一覧を表示する。"""
    m = load_canvas_map()
    if not m:
        p("  （登録なし）")
        p(f"  マップファイル: {CANVAS_MAP_PATH}")
        return
    p(f"  マップファイル: {CANVAS_MAP_PATH}")
    p(f"  {'チャンネルID':<20} {'Canvas ID':<16} {'更新日時':<22} タイトル")
    p(f"  {'─'*20} {'─'*16} {'─'*22} {'─'*20}")
    for ch, v in m.items():
        if isinstance(v, dict):
            cid = v.get("canvas_id", "")
            ts  = v.get("updated_at", "")
            ttl = v.get("title", "")
        else:
            cid, ts, ttl = v, "", ""
        p(f"  {ch:<20} {cid:<16} {ts:<22} {ttl}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def get_client() -> WebClient:
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        print("ERROR: SLACK_USER_TOKEN を設定してください", file=sys.stderr)
        sys.exit(1)
    return WebClient(token=token)


def _sep(title: str = "", width: int = 70) -> str:
    if title:
        pad = width - len(title) - 2
        return f"{'─' * (pad // 2)} {title} {'─' * (pad - pad // 2)}"
    return "─" * width


# --------------------------------------------------------------------------- #
# Section 収集（canvases_sections_lookup）
# --------------------------------------------------------------------------- #

SEARCH_TERMS = [
    # Markdown 要素
    "|", "##", "# ", "- ", "* ", "> ",
    # 日本語
    "【", "アクション", "決定", "マイルストーン", "プロジェクト",
    "状況", "要注意", "サマリー", "進捗", "未完了",
    # 英語
    "project", "status", "action", "milestone",
    # 記号
    "!", "OK", "—", "✓",
    # 空テーブル対策（スペース1文字・タブ）
    " ", "\t",
]


def collect_sections_via_api(client: WebClient, canvas_id: str) -> dict[str, dict]:
    """canvases_sections_lookup で section_id → セクション情報の辞書を返す"""
    found: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        try:
            resp = client.canvases_sections_lookup(
                canvas_id=canvas_id,
                criteria={"contains_text": term},
            )
            for sec in resp.get("sections", []):
                sid = sec.get("id")
                if sid and sid not in found:
                    found[sid] = sec
        except SlackApiError:
            pass
    return found


# --------------------------------------------------------------------------- #
# Section ID 抽出（url_private HTML）
# --------------------------------------------------------------------------- #

# Slack Canvas (filetype: quip) HTML に埋め込まれるセクション ID のパターン:
#   <h1 id='UeO9CAnkxT8'>          → Canvas タイトル（デフォルトで削除対象外）
#   <p  id='temp:C:UeOabc123...'>  → temp:C: プレフィックス付きID（コンテンツ）
#   data-block-id / data-section-id は別形式の Canvas で使われる場合がある
_PAT_TAG_WITH_ID = re.compile(
    r"<(h[1-6]|p|div|ul|ol|li|blockquote|pre|hr|table|tbody|thead|tr|td|th)\b[^>]*\sid=['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_PAT_DATA_BLOCK = re.compile(r'data-block-id=["\']([^"\']+)["\']')
_PAT_DATA_SEC   = re.compile(r'data-section-id=["\']([^"\']+)["\']')


def extract_section_ids_from_html(
    html: str,
    include_h1: bool = False,
) -> list[tuple[str, str]]:
    """
    url_private でダウンロードした Canvas HTML から (tag, section_id) を抽出する。
    canvases_sections_lookup が返さないセクションも含めて取得できる。

    Args:
        include_h1: True のとき h1 タグのIDも含める（デフォルト: 除外）

    戻り値: [(tag, id), ...]  tag は "h1" / "p" / "ul" 等
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for m in _PAT_TAG_WITH_ID.finditer(html):
        tag = m.group(1).lower()
        sid = m.group(2)
        if sid in seen:
            continue
        if tag == "h1" and not include_h1:
            continue
        seen.add(sid)
        results.append((tag, sid))

    for pat in [_PAT_DATA_BLOCK, _PAT_DATA_SEC]:
        for m in pat.finditer(html):
            sid = m.group(1)
            if sid not in seen:
                seen.add(sid)
                results.append(("attr", sid))

    return results


# --------------------------------------------------------------------------- #
# files.info / url_private
# --------------------------------------------------------------------------- #

def fetch_files_info(client: WebClient, canvas_id: str) -> dict:
    try:
        resp = client.files_info(file=canvas_id)
        return resp.get("file", {})
    except SlackApiError as e:
        return {"_error": e.response.get("error", str(e))}


def download_url_private(file_info: dict) -> tuple[str, str]:
    """url_private を Bearer トークンでダウンロードし (content, url) を返す"""
    token = os.getenv("SLACK_USER_TOKEN", "")
    url = file_info.get("url_private") or file_info.get("url_private_download", "")
    if not url:
        return "", ""
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as r:
            return r.read().decode("utf-8", errors="replace"), url
    except Exception as e:
        return f"[download error: {e}]", url


# --------------------------------------------------------------------------- #
# 削除
# --------------------------------------------------------------------------- #

def delete_sections(client: WebClient, canvas_id: str, section_ids: list[str],
                    p) -> tuple[int, int]:
    """
    section_ids を1件ずつ canvases_edit で削除する。
    Slack Canvas API はバッチ削除（複数 changes）を受け付けないため1件ずつ。
    進捗を10件ごとに表示する。
    """
    total = len(section_ids)
    ok = fail = 0
    for i, sid in enumerate(section_ids, 1):
        try:
            client.canvases_edit(
                canvas_id=canvas_id,
                changes=[{"operation": "delete", "section_id": sid}],
            )
            ok += 1
        except SlackApiError as e:
            err = e.response.get("error", str(e))
            p(f"  WARN: {sid} 削除失敗: {err}")
            fail += 1
        if i % 10 == 0 or i == total:
            print(f"\r  進捗: {i}/{total} 件", end="", flush=True)
    print()  # 改行
    return ok, fail


# --------------------------------------------------------------------------- #
# メイン出力
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# チャンネルタブ（ブックマーク）操作
# --------------------------------------------------------------------------- #

def get_workspace_domain(client: WebClient) -> str:
    """auth.test から workspace の URL ドメインを返す（例: fugakunextfs.slack.com）"""
    try:
        resp = client.auth_test()
        return resp.get("url", "").rstrip("/").removeprefix("https://").removeprefix("http://")
    except SlackApiError:
        return "slack.com"


def list_bookmarks(client: WebClient, channel_id: str) -> tuple[list[dict], str]:
    """
    チャンネルのブックマーク一覧を返す。
    戻り値: (bookmarks_list, error_message_or_empty)
    """
    try:
        resp = client.bookmarks_list(channel_id=channel_id)
        return resp.get("bookmarks", []), ""
    except SlackApiError as e:
        return [], e.response.get("error", str(e))


def find_canvas_bookmark(bookmarks: list[dict], canvas_id: str) -> dict | None:
    """ブックマーク一覧から canvas_id を含むものを返す。"""
    for bm in bookmarks:
        link = bm.get("link", "") or ""
        entity_id = bm.get("entity_id", "") or ""
        if canvas_id in link or canvas_id in entity_id:
            return bm
    return None


def remove_bookmark(client: WebClient, channel_id: str, bookmark_id: str) -> bool:
    """チャンネルからブックマークを削除する。成功時 True。"""
    try:
        client.bookmarks_remove(channel_id=channel_id, bookmark_id=bookmark_id)
        return True
    except SlackApiError as e:
        print(f"  WARN: ブックマーク削除失敗: {e.response.get('error', e)}")
        return False


def add_canvas_bookmark(client: WebClient, channel_id: str, canvas_id: str,
                        title: str, team_id: str, domain: str) -> bool:
    """
    新しい Canvas をチャンネルのタブ（ブックマーク）として追加する。
    canvas URL: https://{domain}/docs/{team_id}/{canvas_id}
    """
    canvas_url = f"https://{domain}/docs/{team_id}/{canvas_id}"
    try:
        client.bookmarks_add(
            channel_id=channel_id,
            title=title,
            type="link",
            link=canvas_url,
        )
        return True
    except SlackApiError as e:
        print(f"  WARN: ブックマーク追加失敗: {e.response.get('error', e)}")
        return False


def recreate_canvas(client: WebClient, old_canvas_id: str, title: str, p) -> str | None:
    """
    既存 Canvas を削除して同名の新規 Canvas を作成する。
    成功時は新しい canvas_id を返す。

    !! 新しい Canvas ID が発行されるため、CLAUDE.md 等の参照先を更新すること !!
    """
    # 削除
    try:
        client.canvases_delete(canvas_id=old_canvas_id)
        p(f"  ✓ Canvas {old_canvas_id} を削除しました")
    except SlackApiError as e:
        p(f"  ERROR: Canvas 削除失敗: {e.response.get('error', e)}")
        return None

    # 新規作成
    try:
        resp = client.canvases_create(
            title=title,
            document_content={"type": "markdown", "markdown": f"# {title}\n"},
        )
        new_id = resp.get("canvas_id") or resp.get("file", {}).get("id")
        p(f"  ✓ 新規 Canvas を作成しました")
        p(f"  新しい Canvas ID: {new_id}")
        p()
        p("  !! CLAUDE.md と canvas_report.sh 等の Canvas ID を更新してください !!")
        p(f"     旧: {old_canvas_id}")
        p(f"     新: {new_id}")
        return new_id
    except SlackApiError as e:
        p(f"  ERROR: Canvas 作成失敗: {e.response.get('error', e)}")
        return None


def run(canvas_id: str, channel_id: str | None,
        show_raw: bool, show_bookmarks: bool, delete_all: bool,
        include_title: bool, recreate: bool, force_channel_canvas: bool,
        add_tab: bool, new_title: str | None, yes: bool, out) -> None:
    client = get_client()

    def p(*args, **kwargs):
        print(*args, **kwargs)
        if out:
            print(*args, file=out, **kwargs)

    p(_sep(f"Canvas Debug: {canvas_id}"))
    p()

    # ── 0. チャンネルCanvas状態確認（-c + --show-bookmarks 時）────────────
    if channel_id and show_bookmarks:
        p(_sep("0. チャンネルCanvas状態確認"))

        # (A) conversations.info でチャンネルタブを確認
        p("  [A] conversations.info")
        try:
            ci = client.conversations_info(channel=channel_id)
            ch = ci.get("channel", {})
            props = ch.get("properties") or {}

            # properties.tabs / properties.tabz（実際のタブ一覧）
            for field in ("tabs", "tabz"):
                tabs = props.get(field) or []
                if tabs:
                    p(f"  properties.{field} ({len(tabs)} 件):")
                    for tab in tabs:
                        tab_id   = tab.get("id", "")
                        tab_type = tab.get("type", "")
                        tab_data = tab.get("data") or {}
                        tab_label = tab.get("label", "")
                        file_id  = tab_data.get("file_id", "")
                        mark = " ◀ 対象Canvas" if file_id == canvas_id else ""
                        p(f"    [{tab_id}] type={tab_type} file_id={file_id!r} label={tab_label!r}{mark}")
                else:
                    p(f"  properties.{field}: なし")

            # properties.meeting_notes（チャンネル固定Canvas）
            mn = props.get("meeting_notes")
            if mn:
                file_id = mn.get("file_id", "")
                mark = " ◀ 対象Canvas" if file_id == canvas_id else ""
                p(f"  properties.meeting_notes: file_id={file_id!r}{mark}")
            else:
                p(f"  properties.meeting_notes: なし")
        except SlackApiError as e:
            p(f"  ERROR: conversations.info 失敗: {e.response.get('error', e)}")

        p()

        # (B) bookmarks.list でブックマークタブを確認（生レスポンス含む）
        p("  [B] bookmarks.list（生レスポンス）")
        try:
            bm_resp = client.bookmarks_list(channel_id=channel_id)
            p(f"  ok: {bm_resp.get('ok')}")
            bookmarks_raw = bm_resp.get("bookmarks") or []
            p(f"  件数: {len(bookmarks_raw)}")
            for bm in bookmarks_raw:
                for k, v in sorted(bm.items()):
                    p(f"    {k}: {json.dumps(v, ensure_ascii=False)}")
                p()
            if not bookmarks_raw:
                p("  → ブックマークなし")
        except SlackApiError as e:
            p(f"  ERROR: bookmarks.list 失敗: {e.response.get('error', e)}")
            p("  → User Token に bookmarks:read スコープが必要です")
        p()

    # ── 1. files.info ──────────────────────────────────────────────────────
    p(_sep("1. files.info"))
    file_info = fetch_files_info(client, canvas_id)

    if "_error" in file_info:
        p(f"  ERROR: {file_info['_error']}")
    else:
        for k in ["id", "name", "title", "filetype", "pretty_type",
                  "created", "updated", "size",
                  "url_private", "url_private_download", "content_type"]:
            if k in file_info:
                val = str(file_info[k])
                if k in ("url_private", "url_private_download"):
                    val = val[:80] + ("…" if len(val) > 80 else "")
                p(f"  {k}: {val}")
        if show_raw:
            p()
            p("  [raw files.info response]")
            p(json.dumps(file_info, ensure_ascii=False, indent=2))
    p()

    # ── 2. url_private ダウンロード & セクション ID 抽出 ───────────────────
    p(_sep("2. url_private ダウンロード"))
    raw_content, url = download_url_private(file_info)
    # (tag, id) のリスト。h1 はデフォルト除外
    html_pairs: list[tuple[str, str]] = []

    if not url:
        p("  url_private が見つかりません")
    else:
        p(f"  URL: {url[:80]}…")
        p(f"  サイズ: {len(raw_content)} バイト")

        if raw_content and not raw_content.startswith("[download error"):
            html_pairs = extract_section_ids_from_html(
                raw_content, include_h1=include_title
            )
            # h1 は常に抽出して情報表示（削除対象か否かに関わらず）
            all_pairs_for_info = extract_section_ids_from_html(
                raw_content, include_h1=True
            )
            h1_pairs  = [(t, s) for t, s in all_pairs_for_info if t == "h1"]
            body_pairs = [(t, s) for t, s in html_pairs if t != "h1"]
            temp_pairs = [(t, s) for t, s in html_pairs if s.startswith("temp:")]
            other_pairs = [(t, s) for t, s in html_pairs
                           if not s.startswith("temp:") and t != "h1"]

            p(f"  HTML から抽出したセクション ID: {len(html_pairs)} 件"
              f"（h1 タイトルは{'含む' if include_title else '除外'}）")
            if h1_pairs:
                p(f"    h1（タイトル、{'削除対象' if include_title else '削除対象外'}）: "
                  f"{len(h1_pairs)} 件")
                for _, sid in h1_pairs:
                    p(f"      {sid}")
            p(f"    コンテンツ（h1以外の短いID）: {len(other_pairs)} 件")
            for tag, sid in other_pairs:
                p(f"      <{tag}> {sid}")
            p(f"    temp:C: ID（サブ要素）: {len(temp_pairs)} 件")
            if temp_pairs:
                for _, sid in temp_pairs[:5]:
                    p(f"      {sid}")
                if len(temp_pairs) > 5:
                    p(f"      … 他 {len(temp_pairs) - 5} 件")

            p()
            # 5KB 以下なら全文表示、それ以上は先頭のみ（--show-raw で全文強制）
            if len(raw_content) <= 5000 or show_raw:
                p(f"  ─ HTML 全文 ({len(raw_content)} バイト) ─")
                p(raw_content)
            else:
                p("  ─ 先頭 500 文字 ─")
                p(raw_content[:500])
                p(f"  … （残り {len(raw_content) - 500} 文字、--show-raw で全文表示）")
        else:
            p(f"  {raw_content}")
    p()

    html_ids = {sid for _, sid in html_pairs}

    # ── 3. canvases_sections_lookup ────────────────────────────────────────
    p(_sep("3. canvases_sections_lookup（全検索ワード試行）"))
    api_sections = collect_sections_via_api(client, canvas_id)
    p(f"  API で発見したセクション数: {len(api_sections)}")

    if api_sections:
        p()
        for i, (sid, sec) in enumerate(api_sections.items(), 1):
            content_raw = sec.get("content", "")
            preview = content_raw[:120].replace("\n", "↵")
            if len(content_raw) > 120:
                preview += "…"
            p(f"  [{i:02d}] id: {sid}")
            p(f"       type: {sec.get('type', '?')}")
            p(f"       content ({len(content_raw)} 文字): {preview!r}")
    else:
        p("  （canvases_sections_lookup ではセクションが見つかりませんでした）")
        p("  → Canvas が手動編集済みまたは quip 形式の場合、この API は機能しません。")
        p("    url_private から抽出したセクション ID を使って削除できます（セクション4参照）。")
    p()

    # ── 4. HTML vs API の差分 ──────────────────────────────────────────────
    p(_sep("4. HTML抽出 vs API検出 の比較"))
    api_ids = set(api_sections.keys())

    p(f"  HTML から抽出（h1{'含む' if include_title else '除外'}）: {len(html_ids)} 件")
    p(f"  API で検出:                                              {len(api_ids)} 件")

    only_in_html = html_ids - api_ids
    only_in_api  = api_ids - html_ids
    both         = html_ids & api_ids

    p(f"  両方に存在:    {len(both)} 件")
    if only_in_html:
        p(f"  !! HTMLのみ（APIで見えない）: {len(only_in_html)} 件  ← --delete-all で削除対象")
        for sid in sorted(only_in_html):
            p(f"     {sid}")
    if only_in_api:
        p(f"  APIのみ（HTMLにない）: {len(only_in_api)} 件")
        for sid in sorted(only_in_api):
            p(f"     {sid}")
    p()

    # ── 5. pm_report との比較 ──────────────────────────────────────────────
    p(_sep("5. pm_report._collect_section_ids との比較"))
    pm_terms = ["|", "##", "- ", "【", "project", "アクション"]
    pm_found: set[str] = set()
    for term in pm_terms:
        try:
            resp = client.canvases_sections_lookup(
                canvas_id=canvas_id,
                criteria={"contains_text": term},
            )
            for sec in resp.get("sections", []):
                sid = sec.get("id")
                if sid:
                    pm_found.add(sid)
        except SlackApiError:
            pass

    all_known = html_ids | api_ids
    pm_missed = all_known - pm_found
    p(f"  pm_report が検出できる: {len(pm_found)} / {len(all_known)} 件")
    if pm_missed:
        p(f"  !! 削除漏れになるセクション ({len(pm_missed)} 件):")
        for sid in sorted(pm_missed):
            c = api_sections.get(sid, {}).get("content", "（HTMLのみ）")[:60].replace("\n", "↵")
            p(f"     {sid}: {c!r}")
    else:
        p("  （削除漏れなし）")
    p()

    # ── 6. --delete-all ────────────────────────────────────────────────────
    if delete_all:
        p(_sep("6. コンテンツ削除"))
        target_ids = sorted(all_known)
        if not target_ids:
            p("  削除対象のセクションがありません")
            p("  ※ url_private のダウンロードに失敗している場合は手動で Canvas を空にしてください")
        else:
            h1_note = "（h1 タイトルを含む）" if include_title else "（h1 タイトルは保持）"
            p(f"  対象: {len(target_ids)} セクション {h1_note}")
            if not yes:
                ans = input(
                    f"  Canvas {canvas_id} のコンテンツを削除しますか？ [y/N]: "
                ).strip().lower()
                if ans != "y":
                    p("  キャンセルしました")
                    return
            ok, fail = delete_sections(client, canvas_id, target_ids, p)
            p(f"  削除完了: {ok} 件成功 / {fail} 件失敗")

            # 削除後に残存確認
            remaining_api = collect_sections_via_api(client, canvas_id)
            remaining_raw, _ = download_url_private(file_info)
            remaining_pairs = extract_section_ids_from_html(
                remaining_raw, include_h1=include_title
            )
            remaining_html = {s for _, s in remaining_pairs}
            remaining_all = set(remaining_api.keys()) | remaining_html

            if remaining_all:
                p(f"  !! まだ {len(remaining_all)} セクション残存:")
                for sid in sorted(remaining_all):
                    src = "HTML+API" if sid in remaining_api and sid in remaining_html \
                          else ("API" if sid in remaining_api else "HTML")
                    c = remaining_api.get(sid, {}).get("content", "")[:60].replace("\n", "↵")
                    p(f"     [{src}] {sid}: {c!r}")
            else:
                p("  ✓ コンテンツ削除確認済み")
        p()

    # ── 7. --recreate ───────────────────────────────────────────────────────
    if recreate:
        p(_sep("7. Canvas 削除 & 新規作成"))
        p("  !! 警告: 旧 Canvas ID は無効になります。canvas_map.json は自動更新されます !!")
        p()
        title = new_title or file_info.get("title") or file_info.get("name") or "Canvas"
        p(f"  対象 Canvas ID : {canvas_id}")
        p(f"  新 Canvas タイトル: {title}")

        # チャンネルタブの確認
        team_id: str = ""
        domain: str = ""
        old_tab_id: str = ""   # 削除すべき古いタブエントリのID
        # is_channel_canvas: True → canvases.create を使わず conversations.canvases.create のみ
        is_channel_canvas = force_channel_canvas  # --channel-canvas で強制
        if channel_id and not force_channel_canvas:
            # conversations.info の properties.tabs / meeting_notes でタブ検索
            try:
                ci = client.conversations_info(channel=channel_id)
                ch = ci.get("channel", {})
                props = ch.get("properties") or {}
                # (A1) properties.tabs / tabz でファイルID照合
                for field in ("tabs", "tabz"):
                    for tab in (props.get(field) or []):
                        fid = (tab.get("data") or {}).get("file_id", "")
                        if fid == canvas_id:
                            is_channel_canvas = True
                            old_tab_id = tab.get("id", "")
                            label = tab.get("label", "")
                            p(f"  チャンネルタブ検出（properties.{field}）: id={old_tab_id!r} label={label!r}")
                            p("  → 削除後に conversations.canvases.create で再作成します")
                            break
                    if is_channel_canvas:
                        break
                # (A2) properties.meeting_notes でファイルID照合
                if not is_channel_canvas:
                    mn = props.get("meeting_notes") or {}
                    if mn.get("file_id") == canvas_id:
                        is_channel_canvas = True
                        p(f"  チャンネルタブ検出（properties.meeting_notes）: file_id={canvas_id}")
                        p("  → 削除後に conversations.canvases.create で再作成します")
                if not is_channel_canvas:
                    p(f"  → properties.tabs / meeting_notes に Canvas ID {canvas_id} なし")
            except SlackApiError as e:
                err = e.response.get("error", str(e))
                if err == "missing_scope":
                    p(f"  WARN: conversations.info にスコープ不足 (channels:read または groups:read)")
                    p(f"  → チャンネルCanvasタブの場合: --channel-canvas フラグを追加してください")
                else:
                    p(f"  WARN: conversations.info 失敗: {err}")
        else:
            p("  ヒント: -c CHANNEL_ID を指定するとタブの自動付け替えが有効になります")

        if not yes:
            ans = input(
                f"  Canvas {canvas_id} を削除して再作成しますか？ [y/N]: "
            ).strip().lower()
            if ans != "y":
                p("  キャンセルしました")
                return

        if channel_id and (is_channel_canvas or add_tab):
            # ─── チャンネルタブモード ───────────────────────────────────────
            # conversations.canvases.create のみ使用（canvases.create は呼ばない）
            p()
            p("  ── チャンネルCanvasタブとして再作成 ──")

            # Step 1: 旧 Canvas ファイルを削除
            try:
                client.canvases_delete(canvas_id=canvas_id)
                p(f"  ✓ 旧 Canvas を削除しました ({canvas_id})")
            except SlackApiError as e:
                p(f"  ERROR: Canvas 削除失敗: {e.response.get('error', e)}")
                return

            # Step 2: 新 Canvas をチャンネルタブとして作成
            new_id: str | None = None
            try:
                resp = client.conversations_canvases_create(
                    channel_id=channel_id,
                    document_content={"type": "markdown", "markdown": f"# {title}\n"},
                )
                new_id = (resp.get("canvas_id")
                          or (resp.get("file") or {}).get("id")
                          or (((resp.get("channel") or {}).get("properties") or {})
                              .get("meeting_notes") or {}).get("file_id"))
                p(f"  ✓ 新 Canvas をチャンネルタブとして作成しました (ID: {new_id})")
            except SlackApiError as e:
                p(f"  ERROR: conversations.canvases.create 失敗: {e.response.get('error', e)}")
                return

            # Step 3: 旧タブエントリを削除（stale tab cleanup）
            if old_tab_id:
                # bookmarks.remove で Ct0... 形式のタブIDを削除できる場合がある
                removed = False
                try:
                    client.bookmarks_remove(
                        channel_id=channel_id,
                        bookmark_id=old_tab_id,
                    )
                    p(f"  ✓ 旧タブエントリを削除しました (tab_id={old_tab_id!r})")
                    removed = True
                except SlackApiError as e:
                    err = e.response.get("error", str(e))
                    p(f"  WARN: 旧タブ削除失敗 (bookmarks.remove → {err})")
                if not removed:
                    p(f"  　　 旧タブ (tab_id={old_tab_id!r}) は Slack UI から手動で削除してください")
                    p(f"  　　 （タブ上で右クリック → 「タブを閉じる」）")

            # Step 4: canvas_map.json 更新
            if new_id and channel_id:
                map_set(channel_id, new_id, title)
                p(f"  ✓ canvas_map.json を更新しました ({channel_id} → {new_id})")
            p(f"  ※ タブの確認: --show-bookmarks で properties.tabs を再確認してください")

        else:
            # ─── スタンドアロンモード ────────────────────────────────────────
            new_id = recreate_canvas(client, canvas_id, title, p)
            if not new_id:
                return
            if channel_id:
                map_set(channel_id, new_id, title)
                p(f"  ✓ canvas_map.json を更新しました ({channel_id} → {new_id})")
        p()


# --------------------------------------------------------------------------- #
# タブ削除（properties.tabs 直接操作の試み）
# --------------------------------------------------------------------------- #

def _cmd_remove_tab(channel_id: str, tab_id: str) -> None:
    """
    properties.tabs から特定タブエントリの削除を複数のAPIで順番に試みる。
    どれが有効かは Slack の実装次第のため、成功したものを記録する。
    """
    client = get_client()

    def p(*a, **kw): print(*a, **kw)

    p(_sep(f"タブ削除: channel={channel_id}  tab_id={tab_id}"))
    p()

    # ── 現在の properties.tabs を取得 ──────────────────────────────────────
    p("【事前確認】 現在の properties.tabs")
    try:
        ci = client.conversations_info(channel=channel_id)
        ch = ci.get("channel", {})
        props = ch.get("properties") or {}
        tabs_before = props.get("tabs") or []
        for t in tabs_before:
            mark = " ◀ 削除対象" if t.get("id") == tab_id else ""
            fid = (t.get("data") or {}).get("file_id", "")
            p(f"  [{t.get('id','')}] type={t.get('type','')} file_id={fid!r} label={t.get('label','')!r}{mark}")
        target = next((t for t in tabs_before if t.get("id") == tab_id), None)
        if not target:
            p(f"  → tab_id={tab_id!r} は properties.tabs に見当たりません（既に削除済み？）")
            return
    except SlackApiError as e:
        p(f"  ERROR: conversations.info 失敗: {e.response.get('error', e)}")
        return
    p()

    candidates = [
        # (説明, api_method, payload)
        ("bookmarks.remove",
         "bookmarks.remove",
         {"channel_id": channel_id, "bookmark_id": tab_id}),

        ("conversations.tabs.delete (tab_id)",
         "conversations.tabs.delete",
         {"channel_id": channel_id, "tab_id": tab_id}),

        ("conversations.tabs.remove (tab_id)",
         "conversations.tabs.remove",
         {"channel_id": channel_id, "tab_id": tab_id}),

        ("conversations.canvas.tab.delete",
         "conversations.canvas.tab.delete",
         {"channel_id": channel_id, "tab_id": tab_id}),

        ("admin.conversations.setProperties (tabs上書き)",
         "admin.conversations.setProperties",
         {"channel_id": channel_id,
          "properties": {"tabs": [t for t in tabs_before if t.get("id") != tab_id]}}),
    ]

    for desc, method, payload in candidates:
        p(f"── 試行: {desc}")
        try:
            resp = client.api_call(method, http_verb="POST", json=payload)
            if resp.get("ok"):
                p(f"  ✅ 成功！  ({method})")
                break
            else:
                err = resp.get("error", "unknown")
                p(f"  ❌ {err}")
        except Exception as e:
            p(f"  ❌ {e}")
        p()

    p()
    p("【事後確認】 properties.tabs")
    try:
        ci2 = client.conversations_info(channel=channel_id)
        tabs_after = (ci2.get("channel", {}).get("properties") or {}).get("tabs") or []
        for t in tabs_after:
            fid = (t.get("data") or {}).get("file_id", "")
            p(f"  [{t.get('id','')}] type={t.get('type','')} file_id={fid!r} label={t.get('label','')!r}")
        removed = not any(t.get("id") == tab_id for t in tabs_after)
        p()
        if removed:
            p(f"  ✅ tab_id={tab_id!r} が削除されました")
        else:
            p(f"  ❌ tab_id={tab_id!r} は依然として残っています")
            p(f"     Slack UI でタブを右クリック → 「タブを閉じる」で手動削除してください")
    except SlackApiError as e:
        p(f"  ERROR: conversations.info 失敗: {e.response.get('error', e)}")


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Slack Canvas の詳細状態を表示するデバッグツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Canvas 指定（--canvas-id か -c のどちらか）
    id_group = parser.add_mutually_exclusive_group()
    id_group.add_argument("--canvas-id", metavar="ID",
                          help="対象 Canvas ID を直接指定")
    id_group.add_argument("-c", "--channel", metavar="CHANNEL_ID",
                          help="チャンネルID を指定（canvas_map.json から Canvas ID を解決）")

    # マップ操作
    parser.add_argument("--list-map", action="store_true",
                        help="登録済みチャンネル→Canvas ID 一覧を表示して終了")
    parser.add_argument("--register", metavar="CANVAS_ID",
                        help="-c と組み合わせてチャンネル→Canvas ID を手動登録")

    # デバッグ・操作
    parser.add_argument("--show-raw", action="store_true",
                        help="files.info 生レスポンスと url_private 全文を表示")
    parser.add_argument("--show-bookmarks", action="store_true",
                        help="-c と組み合わせてチャンネルのブックマーク一覧を表示（タブ確認用）")
    parser.add_argument("--delete-all", action="store_true",
                        help="セクション単位でコンテンツ削除（h1 タイトルは保持）")
    parser.add_argument("--include-title", action="store_true",
                        help="--delete-all 時に h1 タイトルも削除対象にする")
    parser.add_argument("--recreate", action="store_true",
                        help="Canvas を削除して新規作成（テーブル等も完全消去）"
                             " -c 指定時は新 ID を canvas_map.json に自動保存")
    parser.add_argument("--channel-canvas", action="store_true",
                        help="--recreate 時、チャンネルCanvasタブとして再作成を強制"
                             "（conversations.info に missing_scope が出る場合に使用）")
    parser.add_argument("--add-tab", action="store_true",
                        help="--recreate 時、新 Canvas をチャンネルのブックマークタブとして追加"
                             "（元のCanvasがタブになかった場合にも新規追加する）")
    parser.add_argument("--title", default=None, metavar="TEXT",
                        help="--recreate 時の Canvas タイトル（省略時: 元のタイトルを使用）")
    parser.add_argument("--yes", action="store_true",
                        help="確認プロンプトをスキップ")
    parser.add_argument("--remove-tab", metavar="TAB_ID",
                        help="-c と組み合わせて properties.tabs のエントリを削除する"
                             "（Ct0... 形式のタブID）。複数のAPIを順番に試みる")
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="結果をファイルにも保存")
    args = parser.parse_args()

    # --list-map
    if args.list_map:
        def p(*a, **kw): print(*a, **kw)
        p(_sep("canvas_map.json"))
        cmd_list_map(p)
        return

    # --register
    if args.register:
        if not args.channel:
            print("ERROR: --register には -c CHANNEL_ID が必要です", file=sys.stderr)
            sys.exit(1)
        map_set(args.channel, args.register, args.title or "")
        print(f"✓ 登録しました: {args.channel} → {args.register}")
        print(f"  マップファイル: {CANVAS_MAP_PATH}")
        return

    # --remove-tab
    if args.remove_tab:
        if not args.channel:
            print("ERROR: --remove-tab には -c CHANNEL_ID が必要です", file=sys.stderr)
            sys.exit(1)
        _cmd_remove_tab(args.channel, args.remove_tab)
        return

    # Canvas ID を解決
    canvas_id: str | None = None
    channel_id: str | None = None

    if args.canvas_id:
        canvas_id = args.canvas_id
    elif args.channel:
        channel_id = args.channel
        canvas_id = map_get(channel_id)
        if not canvas_id:
            print(f"ERROR: チャンネル {channel_id} の Canvas ID が canvas_map.json に未登録です",
                  file=sys.stderr)
            print(f"  先に登録してください:", file=sys.stderr)
            print(f"    python3 scripts/canvas_debug.py -c {channel_id} --register CANVAS_ID",
                  file=sys.stderr)
            sys.exit(1)
        print(f"[INFO] {channel_id} → Canvas ID: {canvas_id} (canvas_map.json)")
    else:
        print("ERROR: --canvas-id または -c CHANNEL_ID が必要です", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    out = None
    if args.output:
        out = open(args.output, "w", encoding="utf-8")

    try:
        run(canvas_id, channel_id, args.show_raw, args.show_bookmarks,
            args.delete_all, args.include_title, args.recreate,
            args.channel_canvas, args.add_tab, args.title, args.yes, out)
    finally:
        if out:
            out.close()
            print(f"✓ {args.output} に保存しました")


if __name__ == "__main__":
    main()
