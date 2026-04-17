#!/usr/bin/env python3
"""
canvas_utils.py

Slack Canvas 操作の共通ユーティリティ。
複数スクリプトで重複していた Canvas 投稿・セクション削除・テキスト整形を一元管理する。
"""

import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# --------------------------------------------------------------------------- #
# Slack クライアント初期化
# --------------------------------------------------------------------------- #

def get_slack_client() -> WebClient:
    """SLACK_USER_TOKEN チェック付きで WebClient を返す"""
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        print("ERROR: SLACK_USER_TOKEN を設定してください", file=sys.stderr)
        sys.exit(1)
    return WebClient(token=token)


# --------------------------------------------------------------------------- #
# Canvas 向けテキスト整形
# --------------------------------------------------------------------------- #

def sanitize_for_canvas(text: str) -> str:
    """Canvas 向け Markdown 変換（特殊文字置換・見出しレベル正規化・URL クリッカブル化）"""
    replacements = {
        # ダッシュ・ハイフン類
        "\u2013": "-", "\u2014": "-", "\u2015": "-",
        "\u2212": "-", "\u2011": "-", "\u2010": "-",
        # 波ダッシュ・チルダ類
        "\uff5e": "-", "\u301c": "-",
        # 全角括弧
        "\uff08": "(", "\uff09": ")",
        # 全角記号
        "\uff0c": ",", "\uff0e": ".", "\uff01": "!",
        "\uff1a": ":", "\uff1b": ";", "\uff1f": "?",
        # 引用符類
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u300c": '"', "\u300d": '"', "\u300e": '"', "\u300f": '"',
        # 矢印類
        "\u2192": "->", "\u2190": "<-", "\u2194": "<->",
        "\u21d2": "=>", "\u21d0": "<=", "\u21d4": "<=>",
        "\u25b6": ">", "\u25c0": "<",
        # 点・中黒
        "\u30fb": ".", "\u2022": "-", "\u2023": "-",
        "\u25cf": "-", "\u25cb": "-", "\u2027": ".",
        # スペース類
        "\u3000": " ", "\u00a0": " ",
        # その他よく出る記号
        "\u2026": "...", "\u22ef": "...",
        "\u00d7": "x", "\u00f7": "/",
        "\u2605": "*", "\u2606": "*",
        "\u2713": "OK", "\u2714": "OK", "\u2715": "NG", "\u2716": "NG",
        "\u25a0": "-", "\u25a1": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # 裸のURLを <URL> 形式でラップしてクリッカブルにする（既にラップ済みはスキップ）
    text = re.sub(r"(?<![<(\[])https?://[^\s<>）」\]]+[^\s<>）」\].,;:!?、。]",
                  lambda m: f"<{m.group(0)}>", text)

    # Markdownリンク構文でない単体の [テキスト] をエスケープ（Canvasが "Unsupported target for link" エラーになるのを防ぐ）
    # [text](url) や [text][ref] の形はそのまま残し、単体の [...] のみ \[...\] に変換する
    text = re.sub(r"\[([^\]]*)\](?!\s*[\(\[])", r"\\[\1\\]", text)

    # h4以下の見出しはh3に統一（Canvasで未サポート）
    text = re.sub(r"^#{4,6}\s+", "### ", text, flags=re.MULTILINE)
    # インデントされた番号リストをリストに変換
    text = re.sub(r"^(\s+)\d+\.\s+", r"\1- ", text, flags=re.MULTILINE)
    # ブロッククオート内のリスト項目からブロッククオートを除去
    # (Slack Canvas は blockquote 内の List ブロックをサポートしない)
    text = re.sub(r"^> (-|\*|\d+\.)\s+", r"\1 ", text, flags=re.MULTILINE)

    # 上記で対処できなかった非ASCII・非日本語の特殊記号を除去
    def keep_char(c: str) -> str:
        cp = ord(c)
        if 0x20 <= cp <= 0x7E:
            return c
        if c in ("\n", "\t"):
            return c
        if 0x3000 <= cp <= 0x9FFF:
            return c
        if 0xF900 <= cp <= 0xFAFF:
            return c
        if 0xFF00 <= cp <= 0xFFEF:
            return c
        if 0x00C0 <= cp <= 0x024F:
            return c
        return ""

    text = "".join(keep_char(c) for c in text)

    # 連続する空行を1行に圧縮
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 裸のURLを <URL> 形式に変換してクリッカブルにする
    url_pattern = r'(?<!\[)(?<!\()(?<!\<)https?://[^\s\)>\]]+(?!\))(?!\>)'
    def replace_url(match: re.Match) -> str:
        url = match.group(0).rstrip(".,;:!?、。")
        return f"<{url}>"
    text = re.sub(url_pattern, replace_url, text)

    return text


# --------------------------------------------------------------------------- #
# Canvas セクション削除
# --------------------------------------------------------------------------- #

_PAT_TAG_WITH_ID = re.compile(
    r"<(h[1-6]|p|div|ul|ol|li|blockquote|pre|hr|table|tbody|thead|tr|td|th)\b[^>]*\sid=['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_PAT_DATA_BLOCK = re.compile(r'data-block-id=["\']([^"\']+)["\']')
_PAT_DATA_SEC   = re.compile(r'data-section-id=["\']([^"\']+)["\']')

_DELETE_MAX_WORKERS = 8   # 並列スレッド数（16以上は削除失敗が増えるため8が上限）
_DELETE_MAX_RETRY   = 3   # 429 時の最大リトライ回数


def _collect_section_ids(client: WebClient, canvas_id: str) -> list[str]:
    """url_private HTML から全セクション ID を収集する"""
    token = os.getenv("SLACK_USER_TOKEN", "")
    try:
        resp = client.files_info(file=canvas_id)
        file_info = resp.get("file", {})
        url = file_info.get("url_private") or file_info.get("url_private_download", "")
        if not url:
            return []
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] url_private 取得失敗: {e}", file=sys.stderr)
        return []

    seen: set[str] = set()
    ids: list[str] = []
    for m in _PAT_TAG_WITH_ID.finditer(html):
        tag, sid = m.group(1).lower(), m.group(2)
        if sid in seen:
            continue
        seen.add(sid)
        ids.append(sid)
    for pat in [_PAT_DATA_BLOCK, _PAT_DATA_SEC]:
        for m in pat.finditer(html):
            sid = m.group(1)
            if sid not in seen:
                seen.add(sid)
                ids.append(sid)
    return ids


def _delete_one(token: str, canvas_id: str, sid: str) -> None:
    """1セクションを削除する。429 は Retry-After を待ってリトライ。"""
    c = WebClient(token=token)
    for _ in range(_DELETE_MAX_RETRY):
        try:
            c.canvases_edit(
                canvas_id=canvas_id,
                changes=[{"operation": "delete", "section_id": sid}],
            )
            return
        except SlackApiError as e:
            if e.response.get("error") == "ratelimited":
                wait = int(e.response.headers.get("Retry-After", 5))
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"rate limit retry exhausted: {sid}")


def _delete_sections_parallel(token: str, canvas_id: str,
                               section_ids: list[str]) -> tuple[int, list[str]]:
    """section_ids を MAX_WORKERS 並列で削除する。(ok件数, 失敗IDリスト) を返す。"""
    ok = 0
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=_DELETE_MAX_WORKERS) as pool:
        futures = {pool.submit(_delete_one, token, canvas_id, sid): sid
                   for sid in section_ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                future.result()
                ok += 1
            except SlackApiError as e:
                print(f"[WARN] {sid} 削除失敗: {e.response.get('error')}", file=sys.stderr)
                failed.append(sid)
            except Exception as e:
                print(f"[WARN] {sid} 削除失敗: {e}", file=sys.stderr)
                failed.append(sid)
    return ok, failed


def _delete_sections_sequential(token: str, canvas_id: str,
                                 section_ids: list[str],
                                 delay: float = 1.0) -> tuple[int, list[str]]:
    """失敗セクションを1件ずつ順次リトライする。(ok件数, 依然失敗のIDリスト) を返す。"""
    ok = 0
    still_failed: list[str] = []
    for sid in section_ids:
        time.sleep(delay)
        try:
            _delete_one(token, canvas_id, sid)
            ok += 1
        except SlackApiError as e:
            print(f"[WARN] {sid} 再試行も失敗: {e.response.get('error')}", file=sys.stderr)
            still_failed.append(sid)
        except Exception as e:
            print(f"[WARN] {sid} 再試行も失敗: {e}", file=sys.stderr)
            still_failed.append(sid)
    return ok, still_failed


# --------------------------------------------------------------------------- #
# Canvas 投稿
# --------------------------------------------------------------------------- #

def post_to_canvas(canvas_id: str, content: str) -> None:
    """Canvas の既存コンテンツを全削除し、新コンテンツを先頭に挿入する"""
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        print("ERROR: SLACK_USER_TOKEN を設定してください", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Canvas投稿コンテンツ: {len(content)} 文字")
    client = WebClient(token=token)

    try:
        # Step 1: url_private HTML から全セクション ID を収集して削除
        section_ids = _collect_section_ids(client, canvas_id)
        if section_ids:
            total = len(section_ids)
            print(f"[INFO] 既存セクション {total} 件を削除中...")
            ok, failed_ids = _delete_sections_parallel(token, canvas_id, section_ids)
            if failed_ids:
                print(f"[INFO] 失敗 {len(failed_ids)} 件を順次リトライ中...")
                retry_ok, still_failed = _delete_sections_sequential(token, canvas_id, failed_ids)
                ok += retry_ok
                fail = len(still_failed)
            else:
                fail = 0
            print(f"[INFO] 削除完了: {ok}件成功 / {fail}件失敗")

        # Step 2: 新コンテンツを先頭に挿入
        client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{
                "operation": "insert_at_start",
                "document_content": {"type": "markdown", "markdown": content},
            }],
        )
        print(f"✓ Canvas 更新成功: {canvas_id}")
    except SlackApiError as e:
        print(f"Slack API エラー: {e.response['error']}", file=sys.stderr)
        print(f"レスポンス詳細: {e.response}", file=sys.stderr)
        sys.exit(1)
