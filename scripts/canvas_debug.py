#!/usr/bin/env python3
"""
canvas_debug.py

Slack Canvas の詳細状態を表示するデバッグ用スクリプト。
Canvas に投稿しても既存内容が残ってしまう場合の原因調査に使う。

canvases_sections_lookup でセクションが見つからない場合でも、
url_private でダウンロードした HTML からセクション ID を抽出して削除できる。

Usage:
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --show-raw
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --delete-all

Options:
    --canvas-id ID     対象 Canvas ID（必須）
    --show-raw         files.info の生レスポンスと url_private 全文を表示
    --delete-all       コンテンツを削除して Canvas を空にする（h1 タイトルは保持）
    --include-title    --delete-all 時に h1 タイトルも削除対象にする
    --yes              --delete-all の確認プロンプトをスキップ
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
    r"<(h[1-6]|p|div|ul|ol|li|blockquote|pre|hr)\b[^>]*\sid=['\"]([^'\"]+)['\"]",
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
    ok = fail = 0
    for sid in section_ids:
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
    return ok, fail


# --------------------------------------------------------------------------- #
# メイン出力
# --------------------------------------------------------------------------- #

def run(canvas_id: str, show_raw: bool, delete_all: bool,
        include_title: bool, yes: bool, out) -> None:
    client = get_client()

    def p(*args, **kwargs):
        print(*args, **kwargs)
        if out:
            print(*args, file=out, **kwargs)

    p(_sep(f"Canvas Debug: {canvas_id}"))
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
            p("  ─ 先頭 500 文字 ─")
            p(raw_content[:500])
            if len(raw_content) > 500:
                p(f"  … （残り {len(raw_content) - 500} 文字）")
            if show_raw:
                p()
                p("  ─ 全文 ─")
                p(raw_content)
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


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Slack Canvas の詳細状態を表示するデバッグツール"
    )
    parser.add_argument("--canvas-id", required=True, help="対象 Canvas ID")
    parser.add_argument("--show-raw", action="store_true",
                        help="files.info 生レスポンスと url_private 全文を表示")
    parser.add_argument("--delete-all", action="store_true",
                        help="発見した全セクションを削除してCanvasのコンテンツを空にする（h1タイトルは保持）")
    parser.add_argument("--include-title", action="store_true",
                        help="--delete-all 時に h1 タイトルも削除対象にする")
    parser.add_argument("--yes", action="store_true",
                        help="--delete-all の確認プロンプトをスキップ")
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="結果をファイルにも保存")
    args = parser.parse_args()

    out = None
    if args.output:
        out = open(args.output, "w", encoding="utf-8")

    try:
        run(args.canvas_id, args.show_raw, args.delete_all,
            args.include_title, args.yes, out)
    finally:
        if out:
            out.close()
            print(f"✓ {args.output} に保存しました")


if __name__ == "__main__":
    main()
