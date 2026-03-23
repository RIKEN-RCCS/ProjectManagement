#!/usr/bin/env python3
"""
canvas_debug.py

Slack Canvas の詳細状態を表示するデバッグ用スクリプト。
Canvas に投稿しても既存内容が残ってしまう場合の原因調査に使う。

Usage:
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --show-raw
    python3 scripts/canvas_debug.py --canvas-id F0AAD2494VB --delete-all

Options:
    --canvas-id ID     対象 Canvas ID（必須）
    --show-raw         files.info の生レスポンスをすべて表示
    --delete-all       発見した全セクションを削除して Canvas を空にする（確認プロンプトあり）
    --yes              --delete-all の確認プロンプトをスキップ
    --output PATH      結果をファイルにも保存
"""

import argparse
import json
import os
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
# Section 収集（検索ワードを幅広く試す）
# --------------------------------------------------------------------------- #

SEARCH_TERMS = [
    # 一般的な Markdown 要素
    "|", "##", "# ", "- ", "* ", "> ",
    # 日本語キーワード
    "【", "アクション", "決定", "マイルストーン", "プロジェクト",
    "状況", "要注意", "サマリー", "進捗", "未完了",
    # 英語キーワード
    "project", "status", "action", "milestone",
    # 記号
    "!", "OK", "—", "✓",
]


def collect_all_section_ids(client: WebClient, canvas_id: str) -> dict[str, dict]:
    """
    複数の検索ワードで canvases_sections_lookup を試し、
    section_id → セクション情報の辞書を返す。
    """
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
# files.info
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
# メイン出力
# --------------------------------------------------------------------------- #

def run(canvas_id: str, show_raw: bool, delete_all: bool, yes: bool, out) -> None:
    client = get_client()

    def p(*args, **kwargs):
        print(*args, **kwargs)
        print(*args, file=out, **kwargs) if out else None

    p(_sep(f"Canvas Debug: {canvas_id}"))
    p()

    # ── 1. files.info ──────────────────────────────────────────────────────
    p(_sep("1. files.info"))
    file_info = fetch_files_info(client, canvas_id)

    if "_error" in file_info:
        p(f"  ERROR: {file_info['_error']}")
    else:
        important_keys = [
            "id", "name", "title", "filetype", "pretty_type",
            "created", "updated", "size",
            "url_private", "url_private_download",
            "has_more", "content_type",
        ]
        for k in important_keys:
            if k in file_info:
                val = file_info[k]
                if k in ("url_private", "url_private_download"):
                    val = str(val)[:80] + ("…" if len(str(val)) > 80 else "")
                p(f"  {k}: {val}")
        if show_raw:
            p()
            p("  [raw files.info response]")
            p(json.dumps(file_info, ensure_ascii=False, indent=2))

    p()

    # ── 2. url_private ダウンロード ────────────────────────────────────────
    p(_sep("2. url_private ダウンロード"))
    raw_content, url = download_url_private(file_info)
    if not url:
        p("  url_private が見つかりません")
    else:
        p(f"  URL: {url[:80]}…")
        p(f"  サイズ: {len(raw_content)} バイト")
        if raw_content and not raw_content.startswith("[download error"):
            p()
            p("  ─ 先頭 500 文字 ─")
            p(raw_content[:500])
            if len(raw_content) > 500:
                p(f"  … （残り {len(raw_content) - 500} 文字）")
        elif raw_content:
            p(f"  {raw_content}")
    p()

    # ── 3. canvases_sections_lookup ────────────────────────────────────────
    p(_sep("3. canvases_sections_lookup（全検索ワード試行）"))
    sections = collect_all_section_ids(client, canvas_id)
    p(f"  発見したセクション数: {len(sections)}")
    p()

    if sections:
        for i, (sid, sec) in enumerate(sections.items(), 1):
            content_raw = sec.get("content", "")
            content_preview = content_raw[:120].replace("\n", "↵")
            if len(content_raw) > 120:
                content_preview += "…"
            p(f"  [{i:02d}] id: {sid}")
            p(f"       type: {sec.get('type', '?')}")
            p(f"       content ({len(content_raw)} 文字): {content_preview!r}")
    else:
        p("  （セクションが見つかりませんでした）")
        p("  ※ canvases_sections_lookup は Slack の Canvas が特定の構造を持つ場合のみ機能します。")
        p("    空の Canvas や、検索ワードにマッチしないコンテンツは検出できません。")

    p()

    # ── 4. section_id ごとの検索ワード対応表 ───────────────────────────────
    if sections and show_raw:
        p(_sep("4. 各セクションが引っかかった検索ワード"))
        for sid in sections:
            matched = []
            for term in SEARCH_TERMS:
                try:
                    resp = client.canvases_sections_lookup(
                        canvas_id=canvas_id,
                        criteria={"contains_text": term},
                    )
                    ids_in_resp = [s.get("id") for s in resp.get("sections", [])]
                    if sid in ids_in_resp:
                        matched.append(repr(term))
                except SlackApiError:
                    pass
            p(f"  {sid}: {', '.join(matched) if matched else '（なし）'}")
        p()

    # ── 5. pm_report._collect_section_ids との差分 ─────────────────────────
    p(_sep("5. pm_report._collect_section_ids との比較"))
    pm_report_terms = ["|", "##", "- ", "【", "project", "アクション"]
    pm_report_found: set[str] = set()
    for term in pm_report_terms:
        try:
            resp = client.canvases_sections_lookup(
                canvas_id=canvas_id,
                criteria={"contains_text": term},
            )
            for sec in resp.get("sections", []):
                sid = sec.get("id")
                if sid:
                    pm_report_found.add(sid)
        except SlackApiError:
            pass

    all_ids = set(sections.keys())
    missed = all_ids - pm_report_found
    p(f"  pm_report が検出できるセクション数: {len(pm_report_found)} / {len(all_ids)}")
    if missed:
        p(f"  !! 削除漏れになるセクション ({len(missed)} 件):")
        for sid in missed:
            c = sections[sid].get("content", "")[:80].replace("\n", "↵")
            p(f"     {sid}: {c!r}")
    else:
        p("  （削除漏れなし）")
    p()

    # ── 6. --delete-all ────────────────────────────────────────────────────
    if delete_all:
        p(_sep("6. 全セクション削除"))
        if not sections:
            p("  削除対象のセクションがありません")
        else:
            p(f"  対象: {len(sections)} セクション")
            if not yes:
                ans = input(f"  Canvas {canvas_id} の全セクションを削除しますか？ [y/N]: ").strip().lower()
                if ans != "y":
                    p("  キャンセルしました")
                    return
            ok = 0
            fail = 0
            for sid in sections:
                try:
                    client.canvases_edit(
                        canvas_id=canvas_id,
                        changes=[{"operation": "delete", "section_id": sid}],
                    )
                    ok += 1
                except SlackApiError as e:
                    p(f"  WARN: {sid} の削除失敗: {e.response.get('error', e)}")
                    fail += 1
            p(f"  削除完了: {ok} 件成功 / {fail} 件失敗")

            # 削除後に残存確認
            remaining = collect_all_section_ids(client, canvas_id)
            if remaining:
                p(f"  !! まだ {len(remaining)} セクション残存:")
                for sid in remaining:
                    c = remaining[sid].get("content", "")[:60].replace("\n", "↵")
                    p(f"     {sid}: {c!r}")
            else:
                p("  ✓ 全セクション削除確認済み")
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
                        help="files.info 生レスポンスと検索ワード対応表を表示")
    parser.add_argument("--delete-all", action="store_true",
                        help="発見した全セクションを削除して Canvas を空にする")
    parser.add_argument("--yes", action="store_true",
                        help="--delete-all の確認プロンプトをスキップ")
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="結果をファイルにも保存")
    args = parser.parse_args()

    out = None
    if args.output:
        out = open(args.output, "w", encoding="utf-8")

    try:
        run(args.canvas_id, args.show_raw, args.delete_all, args.yes, out)
    finally:
        if out:
            out.close()
            print(f"✓ {args.output} に保存しました")


if __name__ == "__main__":
    main()
