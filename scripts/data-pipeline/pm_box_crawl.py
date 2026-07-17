#!/usr/bin/env python3
"""
pm_box_crawl.py — BOXドキュメント本文取込みスクリプト

BOXフォルダからファイルを取得し、各形式をMarkdownに変換して
box_docs.db に保存する。pm_embed.py で FTS5 索引化すれば
/argus-investigate や enrich_items.py でドキュメント内容を検索できる。

使い方:
  # BOXフォルダを走査してファイル一覧を登録
  python3 scripts/pm_box_crawl.py --scan

  # 登録済みファイルの本文を抽出
  python3 scripts/pm_box_crawl.py --convert

  # 走査＋変換を一括実行
  python3 scripts/pm_box_crawl.py --scan --convert

  # 特定ソースのみ
  python3 scripts/pm_box_crawl.py --scan --source "アプリケーション開発エリア"

  # 特定ファイルのみ変換
  python3 scripts/pm_box_crawl.py --convert --box-file-id 123456

  # 特定形式のみ
  python3 scripts/pm_box_crawl.py --convert --type pptx

  # 確認のみ
  python3 scripts/pm_box_crawl.py --scan --dry-run
  python3 scripts/pm_box_crawl.py --convert --dry-run

  # 再変換
  python3 scripts/pm_box_crawl.py --convert --force

  # 一覧表示
  python3 scripts/pm_box_crawl.py --list
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import open_db

# 暗号化DB (sqlcipher3) 接続時の ALTER TABLE 「列が既に存在」エラーは
# sqlite3.OperationalError のサブクラスではないため、別途 import して
# tuple で拾えるようにする（db_utils.py と同じ二重サポートパターン）。
try:
    from sqlcipher3 import dbapi2 as _sqlcipher3
    _OPERATIONAL_ERRORS = (sqlite3.OperationalError, _sqlcipher3.OperationalError)
except ImportError:
    _OPERATIONAL_ERRORS = (sqlite3.OperationalError,)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {"pptx", "xlsx", "docx", "pdf", "md", "boxnote", "txt"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS box_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    box_file_id    TEXT NOT NULL UNIQUE,
    box_folder_id  TEXT NOT NULL,
    name           TEXT NOT NULL,
    file_format    TEXT,
    size_bytes     INTEGER,
    modified_at    TEXT,
    folder_path    TEXT,
    index_name     TEXT,
    source_name    TEXT,
    registered_at  TEXT NOT NULL,
    relevance          TEXT,
    relevance_reason   TEXT,
    relevance_judged_at TEXT
);

CREATE TABLE IF NOT EXISTS doc_content (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    box_file_id         TEXT NOT NULL UNIQUE,
    content_md          TEXT NOT NULL,
    content_hash        TEXT,
    page_count          INTEGER,
    char_count          INTEGER,
    convert_method      TEXT,
    extracted_at        TEXT NOT NULL,
    source_modified_at  TEXT
);
"""


_JST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_jst(iso_str: str | None) -> str:
    """BOX の ISO8601 UTC 日時を JST 文字列に変換する。"""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:10] if len(iso_str) >= 10 else iso_str


def _open_box_docs_db(db_path: Path, *, no_encrypt: bool = False) -> sqlite3.Connection:
    conn = open_db(db_path, encrypt=not no_encrypt)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    # relevance 3列の移行（CREATE TABLE IF NOT EXISTS は既存テーブルに列を
    # 追加しないため、この列が無い旧DBでも SELECT bf.relevance が落ちないようにする）
    # source_modified_at の移行（既存DBの doc_content には無いため同様に追加。
    # Box側更新検知＝bf.modified_at と dc.source_modified_at の等値比較に使う）
    for col_def in (
        "ALTER TABLE box_files ADD COLUMN relevance TEXT",
        "ALTER TABLE box_files ADD COLUMN relevance_reason TEXT",
        "ALTER TABLE box_files ADD COLUMN relevance_judged_at TEXT",
        "ALTER TABLE doc_content ADD COLUMN source_modified_at TEXT",
    ):
        try:
            conn.execute(col_def)
        except _OPERATIONAL_ERRORS:
            pass  # 既存DBに列がある場合は no-op
    conn.commit()
    return conn


def _file_extension(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext


# ---------------------------------------------------------------------------
# BOX CLI helpers
# ---------------------------------------------------------------------------

def list_box_folder(
    folder_id: str, *, recursive: bool = True,
    exclude_folders: list[str] | None = None,
) -> list[dict]:
    """BOX CLI で指定フォルダのファイル一覧を取得する（再帰対応）。"""
    files: list[dict] = []
    _list_box_folder_inner(
        folder_id, "", files, recursive=recursive,
        exclude_folders=exclude_folders or [],
    )
    return files


def _list_box_folder_inner(
    folder_id: str, parent_path: str, out: list[dict],
    *, recursive: bool, exclude_folders: list[str],
):
    display_path = parent_path or "(root)"
    logger.info(f"  📂 {display_path} (folder_id={folder_id}) を取得中...")
    try:
        raw = subprocess.check_output(
            ["box", "folders:items", folder_id, "--json",
             "--fields", "name,type,size,content_modified_at,parent"],
            text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(f"BOX folder {folder_id} の取得に失敗: {e}")
        return
    except subprocess.TimeoutExpired:
        logger.warning(f"BOX folder {folder_id} の取得がタイムアウト")
        return

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"BOX folder {folder_id} のJSONパースに失敗")
        return

    folders = []
    file_count = 0
    for item in items:
        item_type = item.get("type", "")
        item_id = str(item.get("id", ""))
        name = item.get("name", "")

        if item_type == "folder" and recursive:
            if any(fnmatch.fnmatch(name, pat) for pat in exclude_folders):
                logger.info(f"    [EXCLUDE] フォルダ除外: {name}")
                continue
            sub_path = f"{parent_path}/{name}" if parent_path else name
            folders.append((item_id, sub_path))
        elif item_type == "file":
            ext = _file_extension(name)
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            out.append({
                "box_file_id": item_id,
                "box_folder_id": str(item.get("parent", {}).get("id", "")),
                "name": name,
                "file_format": ext,
                "size_bytes": item.get("size", 0),
                "modified_at": _to_jst(item.get("content_modified_at")),
                "folder_path": parent_path,
            })
            file_count += 1

    logger.info(f"    → ファイル {file_count} 件, サブフォルダ {len(folders)} 件 (累計 {len(out)} 件)")

    # Phase 1: サブフォルダ取得を並列化（ThreadPoolExecutor）
    if folders:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    _list_box_folder_inner, sub_id, sub_path, out,
                    recursive=recursive, exclude_folders=exclude_folders
                ): sub_path
                for sub_id, sub_path in folders
            }
            for future in as_completed(futures):
                sub_path = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"[WARN] サブフォルダ取得失敗 {sub_path}: {e}")


def download_box_file(box_file_id: str, dest_dir: Path) -> Path | None:
    """BOX CLI でファイルをダウンロードする。"""
    logger.info(f"    ダウンロード中... (file_id={box_file_id})")
    try:
        raw = subprocess.check_output(
            ["box", "files:get", box_file_id, "--json"],
            text=True, timeout=30,
        )
        info = json.loads(raw)
        name = info.get("name", f"file_{box_file_id}")
    except Exception:
        name = f"file_{box_file_id}"

    dest = dest_dir / name
    try:
        subprocess.check_call(
            ["box", "files:download", box_file_id,
             "--destination", str(dest_dir), "--overwrite"],
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"BOX file {box_file_id} のダウンロードに失敗: {e}")
        return None

    if dest.exists():
        logger.info(f"    ダウンロード完了: {name}")
        return dest
    candidates = list(dest_dir.glob("*"))
    return candidates[0] if candidates else None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Format conversion
# ---------------------------------------------------------------------------

def _is_encrypted_office(path: Path) -> bool:
    """OOXML (pptx/docx/xlsx) がパスワード暗号化されているかをマジックバイトで判定。
    通常の OOXML は ZIP (PK\\x03\\x04) で始まるが、暗号化されたものは
    OLE compound document (D0 CF 11 E0) として暗号化パッケージをラップする。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\xd0\xcf\x11\xe0"
    except Exception:
        return False


def _is_encrypted_pdf(path: Path) -> bool:
    """PDF がパスワード暗号化されているかを trailer の `/Encrypt` で判定。

    PDF の trailer dictionary はファイル末尾にあり、暗号化 PDF はそこに
    `/Encrypt` 参照を必ず持つ。末尾 64KB に絞ることで本文中の偶然一致
    （フォント辞書名等）を避ける。
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read()
        return b"/Encrypt" in tail
    except Exception:
        return False


def convert_to_markdown(
    file_path: Path, fmt: str,
    *,
    verbalize_figures: bool = False,
    fig_max_pages: int | None = None,
    fig_endpoints: list[str] | None = None,
) -> tuple[str, str]:
    """ファイルを Markdown に変換する。(content_md, convert_method) を返す。"""
    if fmt in ("pptx", "docx", "xlsx") and _is_encrypted_office(file_path):
        return "[ENCRYPTED — password-protected file, cannot extract content]", "encrypted"

    if fmt == "pdf":
        content, method = _convert_pdf(
            file_path,
            verbalize_figures=verbalize_figures,
            fig_max_pages=fig_max_pages,
            endpoints=fig_endpoints,
        )
    else:
        converters = {
            "md": _convert_md,
            "txt": _convert_md,
            "docx": _convert_docx,
            "xlsx": _convert_xlsx,
            "pptx": _convert_pptx,
            "boxnote": _convert_boxnote,
        }
        converter = converters.get(fmt)
        if not converter:
            return "", "unsupported"
        content, method = converter(file_path)

    # PDF の /Encrypt は権限制限（コピー・印刷禁止等）のみでオープンパスワード無し、
    # というケースが多く（政府系公開PDFに頻出）、pdftotext は空パスワードで
    # 自動復号し普通に本文を取れる。実際に抽出できたかで判定し、事前に
    # ブロックしない（本文が空だった場合のみ暗号化を理由として明示する）。
    if fmt == "pdf" and not content.strip() and _is_encrypted_pdf(file_path):
        return "[ENCRYPTED — password-protected PDF, cannot extract content]", "encrypted"
    return content, method


def _convert_md(path: Path) -> tuple[str, str]:
    return path.read_text(encoding="utf-8", errors="replace"), "direct"


def _convert_docx(path: Path) -> tuple[str, str]:
    # 最初にXHTML形式で試行（従来の形式）
    md = _libreoffice_to_html_to_md(path, "html:XHTML Writer File:UTF8")
    if md:
        return md, "libreoffice_html_xhtml"

    # XHTML失敗時は標準HTML形式で再試行（埋め込みデータが多いファイル対応）
    logger.info(f"    XHTML変換失敗、標準HTML形式で再試行: {path.name}")
    md = _libreoffice_to_html_to_md(path, "html:HTML")
    if md:
        return md, "libreoffice_html_standard"

    return "", "failed"


def _convert_xlsx(path: Path) -> tuple[str, str]:
    # 最初にHTML(StarCalc)UTF8形式で試行
    md = _libreoffice_to_html_to_md(path, "html:HTML (StarCalc):UTF8")
    if md:
        return md, "libreoffice_html_starcalc_utf8"

    # StarCalc UTF8失敗時は標準HTML形式で再試行（埋め込みデータが多いファイル対応）
    logger.info(f"    StarCalc UTF8変換失敗、標準HTML形式で再試行: {path.name}")
    md = _libreoffice_to_html_to_md(path, "html:HTML")
    if md:
        return md, "libreoffice_html_standard"

    return "", "failed"


def _convert_pptx(path: Path) -> tuple[str, str]:
    md = _convert_via_multimodal(path)
    if md:
        return md, "multimodal_ocr"
    md = _libreoffice_to_html_to_md(path, "html")
    if md:
        return md, "libreoffice_html"
    return "", "failed"


def _convert_pdf(
    path: Path,
    *,
    verbalize_figures: bool = False,
    fig_max_pages: int | None = None,
    endpoints: list[str] | None = None,
) -> tuple[str, str]:
    text = _pdftotext(path)
    if text and len(text.strip()) > 100:
        if verbalize_figures:
            eps = endpoints if endpoints is not None else get_ocr_endpoints()
            if eps:
                raw_text = _pdftotext(path, strip=False)
                merged, matched, all_ok = _merge_pdftotext_with_figures(
                    path, text, raw_text, eps, fig_max_pages
                )
                if matched:
                    method = "pdftotext+figures" if all_ok else "pdftotext+figures_partial"
                    return merged, method
        return text, "pdftotext"
    md = _convert_via_multimodal(path)
    if md:
        return md, "multimodal_ocr"
    return text or "", "pdftotext"


def _convert_boxnote(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        text = _extract_boxnote_text(data)
        if text:
            return text, "boxnote_json"
    except Exception:
        pass
    return path.read_text(encoding="utf-8", errors="replace"), "direct"


def _extract_boxnote_text(data: dict) -> str:
    """BOX Note JSON (ProseMirror) から本文テキストのみを抽出する。

    BoxNote の構造:
      { "type": "doc", "content": [ {"type": "paragraph", "content": [ {"type":"text","text":"本文"} ] }, ... ] }

    - type="text" のノードの "text" フィールドのみを本文として採用
    - paragraph/heading/list_item/bullet_list/ordered_list の境界で改行を挿入
    - author_id・mark・attrs 等の構造メタは無視
    """
    parts: list[str] = []

    _BLOCK_TYPES = {
        "paragraph", "heading", "list_item",
        "bullet_list", "ordered_list", "blockquote", "code_block",
    }

    def _walk(node):
        if isinstance(node, dict):
            node_type = node.get("type")
            # テキストノード: text フィールドのみを本文として採用
            if node_type == "text":
                txt = node.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
                return  # text ノードは葉なのでこれ以上降りない
            # ProseMirror ブロックノード: content を再帰、終わりに改行
            if "content" in node and isinstance(node["content"], list):
                for child in node["content"]:
                    _walk(child)
                if node_type in _BLOCK_TYPES:
                    parts.append("\n")
                return
            # ラッパー（{"doc": {...}} や {"version":..., "doc":{...}} 等）:
            # dict / list の値のみ降りる（文字列・数値・真偽値はメタなので無視）
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        # str / int / その他は無視（構造メタの値を拾わない）

    _walk(data)
    # 連続改行を圧縮
    import re as _re
    text = "".join(parts)
    text = _re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ---------------------------------------------------------------------------
# LibreOffice HTML conversion
# ---------------------------------------------------------------------------

def _libreoffice_to_html_to_md(path: Path, convert_filter: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # 並列ワーカー間で LibreOffice の user profile を競合させないため、
        # 呼び出しごとに専用 UserInstallation を割り当てる（無いと多重起動が silent fail する）
        lo_profile = f"file://{tmpdir}/lo_profile"
        try:
            result = subprocess.run(
                ["libreoffice", f"-env:UserInstallation={lo_profile}",
                 "--headless", "--convert-to", convert_filter,
                 "--outdir", tmpdir, str(path)],
                timeout=300,  # 120秒 → 300秒に延長（複雑なdocxファイル対応）
                capture_output=True,  # stdout/stderrをキャプチャ
                text=True,
            )
            if result.returncode != 0:
                logger.warning(
                    f"LibreOffice変換失敗 (code={result.returncode}): {path.name}\n"
                    f"STDOUT: {result.stdout[:500]}\n"
                    f"STDERR: {result.stderr[:500]}"
                )
                return None
        except subprocess.TimeoutExpired:
            logger.warning(f"LibreOffice変換タイムアウト (300秒): {path.name}")
            return None
        except FileNotFoundError:
            logger.error("LibreOffice実行ファイルが見つかりません")
            return None

        html_files = list(Path(tmpdir).glob("*.html")) + list(Path(tmpdir).glob("*.htm"))
        if not html_files:
            logger.warning(f"LibreOffice変換後にHTMLファイルが生成されませんでした: {path.name}")
            return None

        html = html_files[0].read_text(encoding="utf-8", errors="replace")
        md = html_to_markdown(html)

        # 変換結果の妥当性チェック
        if len(md.strip()) < 50:
            logger.warning(f"LibreOffice変換結果が短すぎます ({len(md)}文字): {path.name}")
            return None

        return md


def html_to_markdown(html: str) -> str:
    """LibreOffice 出力の HTML を正規表現で Markdown に変換する。"""
    text = html
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    for i in range(6, 0, -1):
        text = re.sub(
            rf"<h{i}[^>]*>(.*?)</h{i}>",
            lambda m, lv=i: f"\n{'#' * lv} {m.group(1).strip()}\n",
            text, flags=re.DOTALL,
        )

    text = re.sub(r"<(b|strong)[^>]*>(.*?)</\1>", r"**\2**", text, flags=re.DOTALL)
    text = re.sub(r"<(i|em)[^>]*>(.*?)</\1>", r"*\2*", text, flags=re.DOTALL)

    text = _convert_html_tables(text)

    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
    text = re.sub(r"</?[ou]l[^>]*>", "\n", text)

    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL)
    text = re.sub(r"<div[^>]*>(.*?)</div>", r"\1\n", text, flags=re.DOTALL)

    text = re.sub(r"<[^>]+>", "", text)

    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)

    return text.strip()


def _convert_html_tables(html: str) -> str:
    """HTML <table> を Markdown テーブルに変換する。"""
    def _table_to_md(m):
        table_html = m.group(0)
        rows: list[list[str]] = []
        for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr.group(1), re.DOTALL)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        max_cols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < max_cols:
                r.append("")
        lines = []
        lines.append("| " + " | ".join(rows[0]) + " |")
        lines.append("|" + "|".join(["---"] * max_cols) + "|")
        for r in rows[1:]:
            lines.append("| " + " | ".join(r) + " |")
        return "\n" + "\n".join(lines) + "\n"

    return re.sub(r"<table[^>]*>.*?</table>", _table_to_md, html, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# pdftotext
# ---------------------------------------------------------------------------

def _pdftotext(path: Path, *, strip: bool = True) -> str | None:
    """pdftotext で PDF からテキストを抽出する。

    strip=False の場合は生出力（ページ末尾の `\\f` を含む）をそのまま返す。
    ページ単位のマージ処理（`_merge_pdftotext_with_figures`）はこちらを使う。
    """
    try:
        text = subprocess.check_output(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            timeout=60, text=True,
        )
        return text.strip() if strip else text
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Multimodal (gemma4) conversion for PPTX / scanned PDF
# ---------------------------------------------------------------------------

SLIDE_OCR_PROMPT = """この画像はプレゼンテーションのスライドまたはドキュメントの1ページです。
以下の内容をMarkdown形式で書き出してください:
1. タイトルがあれば見出し（## 見出し）
2. 箇条書き・本文テキスト
3. 表があればMarkdownテーブル形式
4. 図表・グラフは [図: 説明] 形式で記述
日本語はそのまま保持してください。Markdownのみ出力してください。"""

FIGURE_VERBALIZE_PROMPT = """この画像は報告書の1ページです。本文テキストの段落（通常の文章）は無視し、
図・グラフ・ダイアグラム・表などの視覚情報のみを言語化してください。

数値・内容をできるだけ具体的に読み取り、書き出してください:
- 棒グラフ／折れ線グラフ／散布図: 各系列の**データ点の値**（軸の目盛りから読める範囲で）、
  最大値・最小値・ピーク、系列間の差や比率、増減の幅。軸の範囲・単位・目盛り間隔。
- 表: 主要な行・列と**セルの値**を転記する。全セルが多すぎる場合は重要行・代表値・
  合計/平均などを優先し、省略した旨を明記する。列見出し・行見出しは保持する。
- 円グラフ／割合図: 各区分のラベルと**パーセンテージ/実数**。
- 模式図／フロー図: ノード名・順序・分岐・矢印の意味・注記の数値。
- 図中の注釈・吹き出し・凡例・脚注にある数値やラベルも拾う。

各図について次の観点も記述してください:
- 図の種別（棒グラフ／折れ線グラフ／散布図／円グラフ／フロー図／模式図／表 等）
- タイトル
- 軸ラベルと単位、系列名

定性的な要約（傾向・結論・示唆）は、上記の数値を書き出した**後に**簡潔に添えてください
（数値が主、要約は従）。

読み取れない・つぶれて判別不能な値は推測で埋めず「（判読不可）」と明記してください。
推測で補う場合のみ「（推測）」と明記してください。
軸目盛りや図中に明示されていない数値を正確な値として書かないでください。目盛りから
読み取る場合は概数・範囲（例: 約50〜60）で表記してください。

ロゴ・ヘッダー/フッター・ページ番号・装飾要素・本文テキストの段落は無視してください。
図・グラフが1つも無いページの場合は、他の内容を一切書かず「図なし」とだけ返してください。

出力は図ごとに `[図: ...]` ブロックで記述してください（複数の図がある場合はブロックを複数に分けてください）。"""

# 図言語化は数値・セル値まで詳細に書き出すため、標準の SLIDE_OCR_PROMPT より
# 出力が長くなりやすい。表が多いページで出力が途中で切れないよう専用の上限を設ける。
FIGURE_VERBALIZE_MAX_TOKENS = 8192


def _convert_via_multimodal(path: Path) -> str | None:
    """PDF を画像化してマルチモーダルLLMで各ページをOCRする。"""
    endpoints = get_ocr_endpoints()
    if not endpoints:
        logger.warning("    マルチモーダルOCRスキップ: LOCAL_LLM_URL 未設定")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = path
        if path.suffix.lower() != ".pdf":
            pdf_path = _to_pdf(path, Path(tmpdir))
            if not pdf_path:
                logger.warning("    マルチモーダルOCRスキップ: PDF変換失敗")
                return None

        images = _pdf_to_images(pdf_path, Path(tmpdir))
        if not images:
            logger.warning("    マルチモーダルOCRスキップ: 画像化失敗")
            return None

        import time as _time
        pages: list[str] = []
        failed_pages: list[int] = []
        for i, img_path in enumerate(images, 1):
            logger.info(f"    マルチモーダルOCR: ページ {i}/{len(images)}")
            md = None
            for ep in endpoints:
                md = _ocr_image(img_path, ep)
                if md:
                    break
                if ep != endpoints[-1]:
                    logger.warning(f"    {ep} 失敗 → フォールバック: {endpoints[endpoints.index(ep) + 1]}")
            if not md and len(endpoints) == 1:
                # フォールバック先がない場合のみ待機リトライ
                for attempt in range(1, 3):
                    logger.info(f"    リトライ {attempt}/2: ページ {i}/{len(images)} (30秒待機)")
                    _time.sleep(30)
                    md = _ocr_image(img_path, endpoints[0])
                    if md:
                        break
            if md:
                pages.append(md)
            else:
                failed_pages.append(i)
                pages.append(f"[OCR失敗: ページ {i}/{len(images)}]")
                logger.warning(f"    ページ {i}/{len(images)}: 全エンドポイントでOCR失敗 — スキップ")

        if failed_pages:
            logger.warning(f"    OCR失敗ページ: {failed_pages} ({len(failed_pages)}/{len(images)}ページ)")

        if len(failed_pages) == len(images):
            # 全ページ失敗時は "[OCR失敗]" プレースホルダのみの文字列を「成功」として
            # 返さない。呼び出し元（_convert_pptx/_convert_pdf）はこれを非Noneとみなすと
            # libreoffice等の代替経路へフォールバックせず、失敗プレースホルダのみが
            # box_docs.db に保存されてしまう。
            return None
        return "\n\n---\n\n".join(pages) if pages else None


def _is_no_figure(text: str) -> bool:
    """マルチモーダルLLMの「図なし」応答を判定する（句読点の揺れを許容）。"""
    stripped = text.strip().rstrip("。.")
    return stripped in ("", "図なし")


# ページ並列OCRのワーカー上限。Pool(workers) 既定2〜4プロセスの中でさらに
# スレッドを増やすとネストした並列度が飽和し 429/timeout を誘発するため抑える。
_FIGURE_OCR_MAX_WORKERS = 4


def _verbalize_figures(
    pdf_path: Path,
    endpoints: list[str],
    max_pages: int | None = None,
    logger: logging.Logger | None = None,
) -> tuple[list[str], list[int]]:
    """PDF の各ページを画像化し、図・グラフ等の視覚情報のみをマルチモーダルLLMで言語化する。

    戻り値: (ページ順のテキストリスト, リトライ後もOCRに失敗したページ番号のリスト(1始まり))。
    本文のみのページ・「図なし」は空文字に正規化する。全エンドポイントで失敗した
    ページは「図なし」と区別するため一旦 None として扱い、1回だけ30秒待機して
    リトライする。リトライ後もなお失敗したページは失敗ページリストに含め、
    テキストとしては空文字を返す（呼び出し元が convert_method を
    "pdftotext+figures_partial" にするための判定に使う）。
    endpoints が空、画像化に失敗した場合は空リストを返す（例外は投げない）。
    """
    log = logger or globals()["logger"]
    if not endpoints:
        return [], []

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            images = _pdf_to_images(pdf_path, Path(tmpdir))
            if not images:
                log.warning("    図言語化スキップ: 画像化失敗")
                return [], []

            if max_pages is not None and len(images) > max_pages:
                log.info(
                    f"    図言語化: 先頭{max_pages}ページのみ対象"
                    f"（全{len(images)}ページ中、超過分は言語化しません）"
                )
                images = images[:max_pages]

            def _one(idx_img: tuple[int, Path]) -> tuple[int, str | None]:
                idx, img_path = idx_img
                for ep in endpoints:
                    text = ocr_slide_image(
                        img_path, ep, prompt=FIGURE_VERBALIZE_PROMPT,
                        max_tokens=FIGURE_VERBALIZE_MAX_TOKENS,
                    )
                    if text:
                        return idx, text.strip()
                return idx, None

            raw_results: list[str | None] = [None] * len(images)
            workers = max(1, min(_FIGURE_OCR_MAX_WORKERS, len(images)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for idx, text in pool.map(_one, enumerate(images)):
                    raw_results[idx] = text

            failed_indices = [i for i, t in enumerate(raw_results) if t is None]
            if failed_indices:
                log.warning(
                    f"    図言語化: {len(failed_indices)}ページでOCR失敗、"
                    f"30秒待機してリトライします: {[i + 1 for i in failed_indices]}"
                )
                time.sleep(30)
                for i in failed_indices:
                    _, text = _one((i, images[i]))
                    raw_results[i] = text

            failed_pages = [i + 1 for i, t in enumerate(raw_results) if t is None]
            if failed_pages:
                log.warning(f"    図言語化: リトライ後も失敗が残ったページ: {failed_pages}")

            results = [
                "" if (t is None or _is_no_figure(t)) else t
                for t in raw_results
            ]
            return results, failed_pages
    except Exception as e:
        log.warning(f"    図言語化スキップ: 予期しないエラー ({e})")
        return [], []


def _merge_pdftotext_with_figures(
    path: Path,
    text: str,
    raw_text: str | None,
    endpoints: list[str],
    max_pages: int | None,
) -> tuple[str, bool, bool]:
    """pdftotext 本文と図言語化結果をページ単位でマージする。

    戻り値: (マージ後テキスト, 図が1つでも得られたか, 全ページOCRに成功したか)。
    図が1つも得られなければ元の本文をそのまま返す。

    ページ分割は strip 前の生出力 (raw_text) で行う。pdftotext (`-layout`) は
    各ページ末尾に必ず `\\f` (form feed) を1個出力するため、
    `raw_text.split("\\f")[:-1]` が正確にページ数と一致する（strip 済みの
    text で分割すると、先頭/末尾に本文の無いページがある場合にページ数が
    ずれて別ページの図説明が誤って別ページ本文にマージされる）。raw_text が
    取得できない、または `\\f` で終わらない想定外の出力の場合は strip 済み
    text の分割にフォールバックする。ページ数が一致しない場合は本文末尾に
    まとめて追記する（fig_max_pages 使用時など）。
    """
    figures, failed_pages = _verbalize_figures(path, endpoints, max_pages=max_pages, logger=logger)
    all_ok = not failed_pages
    if not any(figures):
        return text, False, all_ok

    text_pages = None
    if raw_text and raw_text.endswith("\f"):
        text_pages = raw_text.split("\f")[:-1]
    if text_pages is None:
        text_pages = text.split("\f")

    if len(text_pages) == len(figures):
        merged_pages = []
        for page_text, fig_text in zip(text_pages, figures, strict=True):
            if fig_text:
                merged_pages.append(f"{page_text.rstrip()}\n\n{fig_text}\n")
            else:
                merged_pages.append(page_text)
        merged = "\f".join(merged_pages).strip()
        return merged, True, all_ok

    logger.warning(
        f"    図言語化: ページ数不一致（本文{len(text_pages)}ページ, "
        f"図言語化{len(figures)}ページ）— 末尾にまとめて追記します"
    )
    fig_blocks = "\n\n".join(f for f in figures if f)
    merged = f"{text}\n\n## 図・グラフ（OCR言語化）\n\n{fig_blocks}"
    return merged, True, all_ok


def _to_pdf(path: Path, tmpdir: Path) -> Path | None:
    # 並列ワーカー間で LibreOffice の user profile を競合させないため、
    # 呼び出しごとに専用 UserInstallation を割り当てる
    lo_profile = f"file://{tmpdir}/lo_profile"
    try:
        subprocess.check_call(
            ["libreoffice", f"-env:UserInstallation={lo_profile}",
             "--headless", "--convert-to", "pdf",
             "--outdir", str(tmpdir), str(path)],
            timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    pdfs = list(tmpdir.glob("*.pdf"))
    return pdfs[0] if pdfs else None


def _pdf_to_images(pdf_path: Path, tmpdir: Path) -> list[Path]:
    """PDF を画像（PNG）に変換し、画像ファイルのリストを返す。"""
    prefix = tmpdir / "slide"
    try:
        subprocess.check_call(
            ["pdftoppm", "-png", "-r", "150", str(pdf_path), str(prefix)],
            timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    images = sorted(tmpdir.glob("slide*.png"))
    if images:
        return images

    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        imgs = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=150)
            img_path = tmpdir / f"page_{i:04d}.png"
            pix.save(str(img_path))
            imgs.append(img_path)
        doc.close()
        return imgs
    except ImportError:
        pass

    return []


def ocr_slide_image(
    img_path: Path, base_url: str, prompt: str | None = None,
    max_tokens: int = 4096,
) -> str | None:
    """スライド/ドキュメント画像をマルチモーダルLLMでOCRしMarkdownを返す。

    他モジュール（recording/slide_ocr.py 等）からも利用する公開API。
    """
    return _ocr_image(img_path, base_url, prompt=prompt, max_tokens=max_tokens)


def _ocr_image(
    img_path: Path, base_url: str, prompt: str | None = None,
    max_tokens: int = 4096,
) -> str | None:
    import requests

    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    # base_url が RiVault を指している場合のみ RiVault のトークン・モデルを使う。
    # 通常は LOCAL_LLM_URL (ローカル vLLM, gemma-4 等) でローカルマルチモーダル。
    from cli_utils import detect_vllm_model
    rivault_url = os.environ.get("RIVAULT_URL", "").rstrip("/")
    if rivault_url and base_url.rstrip("/") == rivault_url:
        api_key = os.environ.get("RIVAULT_TOKEN", "dummy")
        model = os.environ.get("RIVAULT_OCR_MODEL", "").strip()
    else:
        api_key = os.environ.get("LOCAL_LLM_TOKEN", "dummy")
        # OCR は専用の LOCAL_OCR_MODEL を優先する（テキスト生成モデル LOCAL_LLM_MODEL が
        # マルチモーダル非対応な場合に備える。RiVault 側の RIVAULT_OCR_MODEL と対称）。
        model = (os.environ.get("LOCAL_OCR_MODEL", "").strip()
                 or os.environ.get("LOCAL_LLM_MODEL", "").strip())
    if not model:
        try:
            model = detect_vllm_model(base_url)
        except Exception:
            return None

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt or SLIDE_OCR_PROMPT},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    url = base_url.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"].get("content")
        if not text:
            logger.warning("    マルチモーダルOCR応答が空です（content未設定）")
            return None
        from cli_utils import strip_think_blocks
        return strip_think_blocks(text)
    except Exception as e:
        logger.warning(f"    マルチモーダルOCR失敗: {e}")
        return None


def get_ocr_endpoints() -> list[str]:
    """マルチモーダルOCR用エンドポイントURLをルーティング優先度順に返す。

    LOCAL_LLM_URL → RIVAULT_URL の順。それぞれ対応する OCR 専用モデル
    （LOCAL_OCR_MODEL / RIVAULT_OCR_MODEL）が設定されている場合のみ追加する
    （テキスト専用モデルへの誤送信を防ぐため）。
    """
    from cli_utils import load_llm_secrets
    load_llm_secrets()
    endpoints: list[str] = []
    local_url = os.environ.get("LOCAL_LLM_URL", "").strip().rstrip("/")
    if local_url and os.environ.get("LOCAL_OCR_MODEL", "").strip():
        endpoints.append(local_url)
    rivault_url = os.environ.get("RIVAULT_URL", "").strip().rstrip("/")
    if rivault_url and os.environ.get("RIVAULT_OCR_MODEL", "").strip():
        if rivault_url not in endpoints:
            endpoints.append(rivault_url)
    return endpoints


# ---------------------------------------------------------------------------
# Scan: register BOX files
# ---------------------------------------------------------------------------

def _scan_single_source(
    src: dict,
    db_path: Path,
    no_encrypt: bool,
    dry_run: bool = False,
    log=print,
) -> tuple[int, str]:
    """
    単一ソースをスキャンする（ThreadPoolExecutor で並列実行用）。
    各スレッドで独立した DB 接続を使用。
    """
    # スレッドごとに独立した DB 接続を開く（暗号化対応）
    from db_utils import open_db
    src_conn = open_db(db_path, encrypt=not no_encrypt)
    src_conn.execute("PRAGMA journal_mode=WAL")
    src_conn.execute("PRAGMA busy_timeout=5000")

    try:
        name = src.get("name", "")
        folder_id = str(src["folder_id"])
        index_names = src.get("index_names") or [src.get("index_name", "pm")]
        if isinstance(index_names, str):
            index_names = [index_names]
        index_names_json = json.dumps(index_names)
        recursive = src.get("recursive", True)
        extensions = set(src.get("extensions", list(SUPPORTED_EXTENSIONS)))
        max_size = src.get("max_file_size_mb", 50) * 1024 * 1024
        excl_folders = src.get("exclude_folders") or []
        excl_patterns = src.get("exclude_patterns") or []

        log(f"\n[SCAN] {name} (folder_id={folder_id})")

        files = list_box_folder(folder_id, recursive=recursive, exclude_folders=excl_folders)
        log(f"  BOXファイル数: {len(files)}")

        # PDF stem セットを構築
        pdf_stems: set[str] = set()
        for f in files:
            if f["file_format"] == "pdf":
                stem = Path(f["name"]).stem
                key = f"{f['folder_path']}/{stem}" if f["folder_path"] else stem
                pdf_stems.add(unicodedata.normalize("NFC", key))

        # Phase 3: Batch insert の準備
        records = []
        registered = 0

        for f in files:
            if f["file_format"] not in extensions:
                continue
            if f["file_format"] in ("pptx", "docx"):
                stem = Path(f["name"]).stem
                key = f"{f['folder_path']}/{stem}" if f["folder_path"] else stem
                if unicodedata.normalize("NFC", key) in pdf_stems:
                    log(f"  [SKIP] {f['name']} (同名PDFあり)")
                    continue
            if f["size_bytes"] and f["size_bytes"] > max_size:
                log(f"  [SKIP] {f['name']} ({f['size_bytes']/(1024*1024):.1f}MB > {max_size/(1024*1024):.0f}MB)")
                continue
            full_path = f"{f['folder_path']}/{f['name']}" if f["folder_path"] else f["name"]
            if any(fnmatch.fnmatch(full_path, pat) for pat in excl_patterns):
                log(f"  [EXCLUDE] {full_path}")
                continue

            if dry_run:
                log(f"  [DRY] {f['file_format']:6s} {f['box_file_id']:12s} {full_path}")
                registered += 1
            else:
                records.append((
                    f["box_file_id"], f["box_folder_id"], f["name"],
                    f["file_format"], f["size_bytes"], f["modified_at"],
                    f["folder_path"], index_names_json, name, _now_iso(),
                ))
                registered += 1

        # batch insert（executemany）
        if records and not dry_run:
            src_conn.executemany(
                """INSERT INTO box_files
                   (box_file_id, box_folder_id, name, file_format, size_bytes,
                    modified_at, folder_path, index_name, source_name, registered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(box_file_id) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    modified_at = excluded.modified_at,
                    folder_path = excluded.folder_path,
                    index_name = excluded.index_name""",
                records,
            )
            src_conn.commit()

        log(f"  登録: {registered} 件")
        return registered, name

    except Exception as e:
        log(f"[ERROR] {name}: スキャン失敗: {e}")
        raise
    finally:
        src_conn.close()


def scan_sources(
    conn: sqlite3.Connection,
    config: dict,
    *,
    source_filter: str | None = None,
    dry_run: bool = False,
    log=print,
) -> int:
    """box_sources.yaml の全ソースを走査して box_files に登録する（並列化）。"""
    sources = config.get("sources") or []
    if not sources:
        log("[WARN] box_sources.yaml に sources が定義されていません")
        return 0

    # フィルタと有効化チェック
    filtered_sources = [
        src for src in sources
        if src.get("enabled", True) and (not source_filter or src.get("name") == source_filter)
    ]

    if not filtered_sources:
        log(f"[WARN] 対象ソースなし（filter={source_filter}）")
        return 0

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    total = 0

    # Phase 2: ソース処理の並列化（ThreadPoolExecutor）
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                _scan_single_source, src, db_path, False,
                dry_run=dry_run, log=log
            ): src.get("name", "unknown")
            for src in filtered_sources
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                count, src_name = future.result()
                total += count
            except Exception as e:
                log(f"[ERROR] {name}: スキャン失敗: {e}")

    _remove_duplicated_by_pdf(conn, dry_run=dry_run, log=log)
    return total


def _remove_duplicated_by_pdf(
    conn: sqlite3.Connection, *, dry_run: bool = False, log=print
):
    """DB内で同フォルダに同名PDFが存在するpptx/docxを削除する。"""
    rows = conn.execute(
        "SELECT box_file_id, name, folder_path, file_format FROM box_files"
    ).fetchall()

    pdf_stems: set[str] = set()
    for r in rows:
        if r["file_format"] == "pdf":
            stem = Path(r["name"]).stem
            key = f"{r['folder_path']}/{stem}" if r["folder_path"] else stem
            pdf_stems.add(unicodedata.normalize("NFC", key))

    to_remove: list[tuple[str, str]] = []
    for r in rows:
        if r["file_format"] in ("pptx", "docx"):
            stem = Path(r["name"]).stem
            key = f"{r['folder_path']}/{stem}" if r["folder_path"] else stem
            if unicodedata.normalize("NFC", key) in pdf_stems:
                to_remove.append((r["box_file_id"], r["name"]))

    if not to_remove:
        return

    log(f"\n  [CLEANUP] 同名PDFありで除去: {len(to_remove)} 件")
    for fid, name in to_remove:
        log(f"    削除: {name} (id={fid})")
        if not dry_run:
            conn.execute("DELETE FROM doc_content WHERE box_file_id = ?", (fid,))
            conn.execute("DELETE FROM box_files WHERE box_file_id = ?", (fid,))

    if not dry_run:
        conn.commit()


# ---------------------------------------------------------------------------
# Convert: extract content
# ---------------------------------------------------------------------------

def convert_single_file(
    file_info: dict,
    db_path: str,
    no_encrypt: bool,
    force: bool,
    verbalize_figures: bool = False,
    fig_max_pages: int | None = None,
    fig_endpoints: list[str] | None = None,
) -> dict:
    """ワーカープロセスで1ファイルを変換する。convert_files() から並列呼び出しされる。

    verbalize_figures は relevance='core' の pdf のみに適用する（呼び出し側で
    file_info["relevance"] を見て判定、ここでは二重にガードする）。

    --figures 未指定でも、既存 doc_content.convert_method に 'figures' を含む
    （＝過去に図言語化済みの）core PDF がBox側更新で再変換される場合は、図言語化を
    自動的に維持する（cron 夜間更新で --figures フラグを付けなくても剥がれない
    ようにするため）。この「維持」発火時のみ OCR エンドポイントをワーカー内で
    遅延解決する。エンドポイントが解決できない場合は図言語化なしで上書きすると
    既存の図説明が黙って失われるため、再変換自体をスキップし次回リトライに委ねる。
    relevance が core から外れていた場合は維持せず text-only に落とす。
    """
    fid = file_info["box_file_id"]
    name = file_info["name"]
    fmt = file_info["file_format"]
    content_hash_old = file_info.get("content_hash")
    existing_method = file_info.get("convert_method") or ""
    new_modified_at = file_info.get("modified_at")

    pid = os.getpid()
    logger.info(f"  [{fmt}] {name} (id={fid}, pid={pid})")

    conn = _open_box_docs_db(Path(db_path), no_encrypt=no_encrypt)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = download_box_file(fid, Path(tmpdir))
            if not file_path:
                logger.warning(f"    [WARN] ダウンロード失敗 (pid={pid})")
                return {"status": "download_failed", "file_id": fid, "name": name}

            content_hash = _file_sha256(file_path)
            if not force and content_hash_old == content_hash:
                logger.info(f"    [SKIP] ハッシュ変更なし (pid={pid})")
                # Box側のメタデータ(modified_at)だけ変わって内容は不変だったケース。
                # 比較基準(source_modified_at)を更新しないと、このファイルは
                # 次回以降も「更新検知」に引っかかり続け毎晩ダウンロードされてしまう。
                if new_modified_at is not None:
                    conn.execute(
                        "UPDATE doc_content SET source_modified_at = ? WHERE box_file_id = ?",
                        (new_modified_at, fid),
                    )
                    conn.commit()
                return {"status": "skipped", "file_id": fid, "name": name}

            is_core_pdf = fmt == "pdf" and file_info.get("relevance") == "core"
            had_figures = "figures" in existing_method
            maintain_figures = (not verbalize_figures) and had_figures and fmt == "pdf"

            want_figures = False
            endpoints = fig_endpoints
            if verbalize_figures and is_core_pdf:
                want_figures = True
            elif maintain_figures:
                if is_core_pdf:
                    endpoints = fig_endpoints if fig_endpoints is not None else get_ocr_endpoints()
                    if endpoints:
                        want_figures = True
                    else:
                        logger.warning(
                            f"    [SKIP] figures 維持不可（OCRエンドポイント未設定/停止）"
                            f"のため再変換をスキップ、次回リトライ (pid={pid})"
                        )
                        return {"status": "skipped_figures_unavailable", "file_id": fid, "name": name}
                else:
                    logger.info(f"    relevance 降格のため figures 維持を停止し text-only へ変換 (pid={pid})")

            logger.info(f"    変換中... (pid={pid})")
            content_md, method = convert_to_markdown(
                file_path, fmt,
                verbalize_figures=want_figures,
                fig_max_pages=fig_max_pages,
                fig_endpoints=endpoints,
            )

            if method == "encrypted":
                logger.warning(f"    [SKIP] 暗号化ファイル (pid={pid})")
                # placeholder row を書き込み、以降の再変換ループでスキップさせる
            elif not content_md.strip():
                logger.warning(f"    [WARN] 変換結果が空 (method={method}, pid={pid})")
                return {"status": "conversion_failed", "file_id": fid, "name": name}

            char_count = len(content_md)
            page_count = content_md.count("---\n") + 1 if method == "multimodal_ocr" else None

            for retry in range(3):
                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO doc_content
                           (box_file_id, content_md, content_hash, page_count,
                            char_count, convert_method, extracted_at, source_modified_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (fid, content_md, content_hash, page_count, char_count, method,
                         _now_iso(), new_modified_at),
                    )
                    conn.commit()
                    logger.info(f"    完了: {char_count}文字, method={method} (pid={pid})")
                    return {"status": "success", "file_id": fid, "name": name, "chars": char_count}
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and retry < 2:
                        logger.warning(f"    DB locked, リトライ {retry+1}/2 (pid={pid})")
                        time.sleep(0.5 * (retry + 1))
                    else:
                        logger.error(f"    DB書き込み失敗: {e} (pid={pid})")
                        return {"status": "db_error", "file_id": fid, "name": name, "error": str(e)}
    finally:
        conn.close()

    return {"status": "unknown_error", "file_id": fid, "name": name}


def convert_files(
    conn: sqlite3.Connection,
    *,
    source_filter: str | None = None,
    box_file_id_filter: str | None = None,
    type_filter: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    workers: int = 2,
    no_encrypt: bool = False,
    db_path: Path | None = None,
    figures: bool = False,
    fig_max_pages: int | None = None,
    fig_endpoints: list[str] | None = None,
    log=print,
) -> int:
    """登録済みファイルをダウンロード・変換して doc_content に保存する。"""
    where_parts = ["1=1"]
    params: list = []

    if source_filter:
        where_parts.append("bf.source_name = ?")
        params.append(source_filter)
    if box_file_id_filter:
        where_parts.append("bf.box_file_id = ?")
        params.append(box_file_id_filter)
    if type_filter:
        where_parts.append("bf.file_format = ?")
        params.append(type_filter)
    # Box側更新検知句: 未変換(dc無し)、または既に source_modified_at が記録済みで
    # かつ bf.modified_at と食い違う(＝Boxで更新された)行を対象に含める。
    # source_modified_at が NULL（移行直後の既存行等、比較基準が未確立）の行は
    # 「更新扱いにしない」ことで --force 無しの初回実行時の全件雪崩DLを防ぐ
    # （基準は --force 実行時、または初回の hash 一致スキップ時に確立される）。
    update_detect_clause = (
        "(dc.box_file_id IS NULL OR "
        "(dc.source_modified_at IS NOT NULL AND bf.modified_at IS NOT NULL "
        "AND bf.modified_at != dc.source_modified_at))"
    )
    if not force:
        where_parts.append(update_detect_clause)

    where = " AND ".join(where_parts)
    rows = conn.execute(
        f"""SELECT bf.box_file_id, bf.name, bf.file_format, bf.size_bytes,
                   bf.folder_path, bf.index_name, bf.modified_at,
                   bf.relevance, dc.content_hash, dc.convert_method
            FROM box_files bf
            LEFT JOIN doc_content dc ON bf.box_file_id = dc.box_file_id
            WHERE {where}
            ORDER BY bf.name""",
        params,
    ).fetchall()

    if figures and not force:
        # --force 未指定の場合、既に変換済みの core PDF は上の WHERE で除外され
        # 図言語化の対象から漏れる。無言で漏らさず案内する。
        already_where_parts = [p for p in where_parts if p != update_detect_clause]
        already_where_parts += [
            "bf.file_format = 'pdf'", "bf.relevance = 'core'",
            "dc.box_file_id IS NOT NULL",
            "(dc.convert_method IS NULL OR dc.convert_method NOT LIKE '%figures%'"
            " OR dc.convert_method LIKE '%figures_partial%')",
        ]
        already_where = " AND ".join(already_where_parts)
        pending = conn.execute(
            f"""SELECT COUNT(*) FROM box_files bf
                LEFT JOIN doc_content dc ON bf.box_file_id = dc.box_file_id
                WHERE {already_where}""",
            params,
        ).fetchone()[0]
        if pending:
            log(
                f"[INFO] --figures 指定ですが、既に変換済みの core PDF が {pending} 件あります。"
                f"図言語化を追加するには --force を併用してください。"
            )

    if not rows:
        log("[INFO] 変換対象なし")
        return 0

    log(f"[CONVERT] 対象: {len(rows)} 件, workers={workers}")

    if dry_run:
        for row in rows:
            log(f"  [DRY] [{row['file_format']}] {row['name']} (id={row['box_file_id']})")
        return len(rows)

    if db_path is None:
        log("[ERROR] db_path が指定されていません")
        return 0

    file_infos = [dict(r) for r in rows]

    if workers > 1 and len(file_infos) > 1:
        with Pool(workers) as pool:
            results = pool.starmap(
                convert_single_file,
                [(f, str(db_path), no_encrypt, force, figures, fig_max_pages, fig_endpoints) for f in file_infos],
            )
    else:
        results = [
            convert_single_file(f, str(db_path), no_encrypt, force, figures, fig_max_pages, fig_endpoints)
            for f in file_infos
        ]

    converted = sum(1 for r in results if r["status"] == "success")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = [r for r in results if r["status"] not in ("success", "skipped")]

    if failed:
        log(f"\n[WARN] 変換失敗: {len(failed)} 件")
        for r in failed:
            log(f"  {r['file_id']} ({r['name']}): {r['status']}")

    log(f"\n完了: 変換 {converted}, スキップ {skipped}, 失敗 {len(failed)} / 合計 {len(rows)} 件")
    return converted


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def list_files(conn: sqlite3.Connection, *, source_filter: str | None = None, log=print):
    where = "WHERE bf.source_name = ?" if source_filter else ""
    params = [source_filter] if source_filter else []

    rows = conn.execute(
        f"""SELECT bf.box_file_id, bf.name, bf.file_format, bf.size_bytes,
                   bf.folder_path, bf.source_name, bf.index_name,
                   bf.modified_at,
                   dc.char_count, dc.convert_method, dc.extracted_at
            FROM box_files bf
            LEFT JOIN doc_content dc ON bf.box_file_id = dc.box_file_id
            {where}
            ORDER BY bf.source_name, bf.folder_path, bf.name""",
        params,
    ).fetchall()

    if not rows:
        log("登録ファイルなし")
        return

    current_source = None
    total = len(rows)
    converted = sum(1 for r in rows if r["char_count"])

    log(f"登録: {total} 件, 変換済: {converted} 件\n")

    for r in rows:
        if r["source_name"] != current_source:
            current_source = r["source_name"]
            log(f"--- {current_source} ---")

        status = f"{r['char_count']}文字 ({r['convert_method']})" if r["char_count"] else "未変換"
        size_mb = f"{r['size_bytes']/(1024*1024):.1f}MB" if r["size_bytes"] else "?MB"
        mod = _to_jst(r["modified_at"]) if r["modified_at"] else ""
        fid = r["box_file_id"]
        log(f"  {fid} [{r['file_format']:6s}] {mod:16s} {size_mb:>8s} {status:30s} {r['folder_path']}/{r['name']}")


# ---------------------------------------------------------------------------
# Debug: test file conversion with detailed logging
# ---------------------------------------------------------------------------

def debug_convert_file(file_path: Path, *, log=print):
    """特定ファイルの変換をデバッグモードで実行（LibreOffice出力を詳細表示）"""
    if not file_path.exists():
        log(f"[ERROR] ファイルが見つかりません: {file_path}")
        return

    log(f"\n=== デバッグ変換: {file_path.name} ===")
    log(f"ファイルサイズ: {file_path.stat().st_size / 1024 / 1024:.2f} MB")
    log(f"拡張子: {file_path.suffix}")

    if file_path.suffix.lower() == ".docx":
        log("\n--- LibreOffice変換 ---")
        md = _libreoffice_to_html_to_md(file_path, "html:XHTML Writer File:UTF8")
        if md:
            log(f"✓ 成功 ({len(md)} 文字)")
            log(f"プレビュー:\n{md[:500]}...\n")
        else:
            log("✗ 失敗（詳細はログを確認してください）\n")
    elif file_path.suffix.lower() == ".xlsx":
        log("\n--- LibreOffice Excel変換 ---")
        md = _libreoffice_to_html_to_md(file_path, "html:HTML (StarCalc):UTF8")
        if md:
            log(f"✓ 成功 ({len(md)} 文字)")
            log(f"プレビュー:\n{md[:500]}...\n")
        else:
            log("✗ 失敗（詳細はログを確認してください）\n")
    elif file_path.suffix.lower() == ".pptx":
        log("\n--- LibreOffice PowerPoint変換 ---")
        md = _libreoffice_to_html_to_md(file_path, "html")
        if md:
            log(f"✓ 成功 ({len(md)} 文字)")
            log(f"プレビュー:\n{md[:500]}...\n")
        else:
            log("✗ 失敗（詳細はログを確認してください）\n")
    elif file_path.suffix.lower() == ".pdf":
        log("\n--- PDF テキスト抽出 ---")
        text = _pdftotext(file_path)
        if text:
            log(f"✓ 成功 ({len(text)} 文字)")
            log(f"プレビュー:\n{text[:500]}...\n")
        else:
            log("✗ 失敗（詳細はログを確認してください）\n")
    else:
        log(f"[ERROR] サポートされていない形式: {file_path.suffix}")


# ---------------------------------------------------------------------------
# Show: display converted content
# ---------------------------------------------------------------------------

def show_file(conn: sqlite3.Connection, box_file_id: str, *, log=print):
    """変換済みMarkdownの内容を表示する。"""
    row = conn.execute(
        """SELECT bf.name, bf.file_format, bf.folder_path, bf.size_bytes,
                  bf.modified_at, bf.index_name, bf.source_name,
                  dc.content_md, dc.char_count, dc.page_count,
                  dc.convert_method, dc.extracted_at, dc.content_hash
           FROM box_files bf
           LEFT JOIN doc_content dc ON bf.box_file_id = dc.box_file_id
           WHERE bf.box_file_id = ?""",
        (box_file_id,),
    ).fetchone()

    if not row:
        log(f"[ERROR] box_file_id={box_file_id} が見つかりません")
        return

    log(f"ファイル名   : {row['name']}")
    log(f"フォルダ     : {row['folder_path']}")
    log(f"形式         : {row['file_format']}")
    size_mb = f"{row['size_bytes']/(1024*1024):.1f}MB" if row["size_bytes"] else "不明"
    log(f"サイズ       : {size_mb}")
    log(f"BOX更新日時  : {row['modified_at'] or '不明'}")
    log(f"インデックス : {row['index_name']}")
    log(f"ソース       : {row['source_name']}")
    log("")

    if not row["content_md"]:
        log("[未変換]")
        return

    log(f"変換方法     : {row['convert_method']}")
    log(f"文字数       : {row['char_count']}")
    if row["page_count"]:
        log(f"ページ数     : {row['page_count']}")
    log(f"変換日時     : {row['extracted_at']}")
    log(f"ハッシュ     : {row['content_hash'][:16]}...")
    log("")
    log("=" * 72)
    log(row["content_md"])


# ---------------------------------------------------------------------------
# Remove: delete registered files
# ---------------------------------------------------------------------------

def remove_files(
    conn: sqlite3.Connection,
    *,
    box_file_id_filter: str | None = None,
    folder_pattern: str | None = None,
    dry_run: bool = False,
    log=print,
) -> int:
    """box_files + doc_content から登録済みファイルを削除する。"""
    if not box_file_id_filter and not folder_pattern:
        log("[ERROR] --box-file-id または --folder-pattern を指定してください")
        return 0

    if box_file_id_filter:
        rows = conn.execute(
            "SELECT box_file_id, name, folder_path FROM box_files WHERE box_file_id = ?",
            (box_file_id_filter,),
        ).fetchall()
    else:
        all_rows = conn.execute(
            "SELECT box_file_id, name, folder_path FROM box_files"
        ).fetchall()
        rows = []
        for r in all_rows:
            full_path = f"{r['folder_path']}/{r['name']}" if r["folder_path"] else r["name"]
            if fnmatch.fnmatch(full_path, folder_pattern):
                rows.append(r)

    if not rows:
        log("[INFO] 削除対象なし")
        return 0

    log(f"[REMOVE] 削除対象: {len(rows)} 件")
    for r in rows:
        log(f"  {r['box_file_id']} {r['folder_path']}/{r['name']}")

    if dry_run:
        log("[DRY-RUN] 削除は行いません")
        return len(rows)

    ids = [r["box_file_id"] for r in rows]
    for fid in ids:
        conn.execute("DELETE FROM doc_content WHERE box_file_id = ?", (fid,))
        conn.execute("DELETE FROM box_files WHERE box_file_id = ?", (fid,))
    conn.commit()
    log(f"削除完了: {len(ids)} 件")
    return len(ids)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="BOXドキュメントの本文をMarkdownに変換してDBに保存する"
    )
    parser.add_argument("--scan", action="store_true",
                        help="BOXフォルダを走査してファイル一覧を登録")
    parser.add_argument("--convert", action="store_true",
                        help="登録済みファイルの本文を抽出・変換")
    parser.add_argument("--list", action="store_true",
                        help="登録・変換状況を一覧表示")
    parser.add_argument("--show", metavar="BOX_FILE_ID",
                        help="指定ファイルの変換済みMarkdownを表示")
    parser.add_argument("--remove", action="store_true",
                        help="登録済みファイルを削除（--box-file-id or --folder-pattern で対象指定）")
    parser.add_argument("--source", help="特定ソースのみ対象（box_sources.yaml の name）")
    parser.add_argument("--box-file-id", help="特定BOXファイルIDのみ対象")
    parser.add_argument("--folder-pattern", help="フォルダパスのfnmatchパターン（--remove用、例: 'アーカイブ/*'）")
    parser.add_argument("--type", help="特定形式のみ変換（pptx/xlsx/docx/pdf/md）")
    parser.add_argument("--force", action="store_true",
                        help="変換済みファイルも再変換")
    parser.add_argument("--figures", action="store_true",
                        help="relevance='core' のPDFに対し図・グラフをマルチモーダルOCRで言語化して本文にマージする")
    parser.add_argument("--figures-max-pages", type=int, default=None,
                        help="図言語化を行う先頭ページ数の上限（未指定なら全ページ）")
    parser.add_argument("--workers", type=int, default=2,
                        help="並列処理数（デフォルト: 2、1で順次実行、最大4推奨）")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB保存なし・確認のみ")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="平文モード")
    parser.add_argument("--db", default="data/box_docs.db",
                        help="box_docs.db のパス")
    parser.add_argument("--config", default="data/box_sources.yaml",
                        help="設定ファイルのパス")
    parser.add_argument("--output", help="ログをファイルにも保存")
    parser.add_argument("--debug-convert", metavar="PATH",
                        help="特定ファイルの変換をデバッグモードで実行（LibreOffice出力を詳細表示）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = _REPO_ROOT / db_path
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _REPO_ROOT / config_path

    output_file = None
    if args.output:
        output_file = open(args.output, "w", encoding="utf-8")

    def log(msg: str):
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    conn = _open_box_docs_db(db_path, no_encrypt=args.no_encrypt)

    if not args.scan and not args.convert and not args.list and not args.remove and not args.show and not args.debug_convert:
        log("[ERROR] --scan, --convert, --list, --show, --remove, --debug-convert のいずれかを指定してください")
        conn.close()
        sys.exit(1)

    if args.figures and not args.convert:
        log("[WARN] --figures は --convert と併用しないと効果がありません")

    if args.debug_convert:
        debug_convert_file(Path(args.debug_convert), log=log)
        conn.close()
        return

    if args.show:
        show_file(conn, args.show, log=log)
        conn.close()
        return

    if args.list:
        list_files(conn, source_filter=args.source, log=log)
        conn.close()
        return

    if args.remove:
        remove_files(
            conn,
            box_file_id_filter=args.box_file_id,
            folder_pattern=args.folder_pattern,
            dry_run=args.dry_run,
            log=log,
        )
        conn.close()
        return

    if args.scan:
        if not config_path.exists():
            log(f"[ERROR] 設定ファイルが見つかりません: {config_path}")
            conn.close()
            sys.exit(1)
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        if args.dry_run:
            log("[DRY-RUN] DB保存は行いません")
        scan_sources(
            conn, config,
            source_filter=args.source,
            dry_run=args.dry_run,
            log=log,
        )

    if args.convert:
        if args.dry_run:
            log("\n[DRY-RUN] ダウンロード・変換は行いません")
        fig_endpoints = None
        if args.figures:
            fig_endpoints = get_ocr_endpoints()
            if not fig_endpoints:
                log("[WARN] --figures 指定ですが LOCAL_OCR_MODEL/RIVAULT_OCR_MODEL 未設定のため図言語化スキップ")
        convert_files(
            conn,
            source_filter=args.source,
            box_file_id_filter=args.box_file_id,
            type_filter=args.type,
            force=args.force,
            dry_run=args.dry_run,
            workers=args.workers,
            no_encrypt=args.no_encrypt,
            db_path=db_path,
            figures=args.figures,
            fig_max_pages=args.figures_max_pages,
            fig_endpoints=fig_endpoints,
            log=log,
        )

    conn.close()
    if output_file:
        output_file.close()


if __name__ == "__main__":
    main()
