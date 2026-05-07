#!/usr/bin/env python3
"""
pm_document_content.py — BOXドキュメント本文取込みスクリプト

BOXフォルダからファイルを取得し、各形式をMarkdownに変換して
box_docs.db に保存する。pm_embed.py で FTS5 索引化すれば
/argus-investigate や enrich_items.py でドキュメント内容を検索できる。

使い方:
  # BOXフォルダを走査してファイル一覧を登録
  python3 scripts/pm_document_content.py --scan

  # 登録済みファイルの本文を抽出
  python3 scripts/pm_document_content.py --convert

  # 走査＋変換を一括実行
  python3 scripts/pm_document_content.py --scan --convert

  # 特定ソースのみ
  python3 scripts/pm_document_content.py --scan --source "アプリケーション開発エリア"

  # 特定ファイルのみ変換
  python3 scripts/pm_document_content.py --convert --box-file-id 123456

  # 特定形式のみ
  python3 scripts/pm_document_content.py --convert --type pptx

  # 確認のみ
  python3 scripts/pm_document_content.py --scan --dry-run
  python3 scripts/pm_document_content.py --convert --dry-run

  # 再変換
  python3 scripts/pm_document_content.py --convert --force

  # 一覧表示
  python3 scripts/pm_document_content.py --list
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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from db_utils import open_db

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
    registered_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doc_content (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    box_file_id    TEXT NOT NULL UNIQUE,
    content_md     TEXT NOT NULL,
    content_hash   TEXT,
    page_count     INTEGER,
    char_count     INTEGER,
    convert_method TEXT,
    extracted_at   TEXT NOT NULL
);
"""


_JST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    for sub_id, sub_path in folders:
        _list_box_folder_inner(sub_id, sub_path, out, recursive=recursive, exclude_folders=exclude_folders)


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

def convert_to_markdown(file_path: Path, fmt: str) -> tuple[str, str]:
    """ファイルを Markdown に変換する。(content_md, convert_method) を返す。"""
    converters = {
        "md": _convert_md,
        "txt": _convert_md,
        "docx": _convert_docx,
        "xlsx": _convert_xlsx,
        "pptx": _convert_pptx,
        "pdf": _convert_pdf,
        "boxnote": _convert_boxnote,
    }
    converter = converters.get(fmt)
    if not converter:
        return "", "unsupported"
    return converter(file_path)


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


def _convert_pdf(path: Path) -> tuple[str, str]:
    text = _pdftotext(path)
    if text and len(text.strip()) > 100:
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
    """BOX Note JSON からテキストを再帰的に抽出する。"""
    parts: list[str] = []

    def _walk(node):
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            if "text" in node:
                parts.append(str(node["text"]))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LibreOffice HTML conversion
# ---------------------------------------------------------------------------

def _libreoffice_to_html_to_md(path: Path, convert_filter: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result = subprocess.run(
                ["libreoffice", "--headless", "--convert-to", convert_filter,
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

def _pdftotext(path: Path) -> str | None:
    try:
        text = subprocess.check_output(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            timeout=60, text=True,
        )
        return text.strip()
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


def _convert_via_multimodal(path: Path) -> str | None:
    """PDF を画像化して gemma4 マルチモーダルで各ページをOCRする。"""
    base_url = os.environ.get("OPENAI_API_BASE")
    if not base_url:
        logger.warning("    マルチモーダルOCRスキップ: OPENAI_API_BASE 未設定")
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

        pages: list[str] = []
        failed_pages: list[int] = []
        for i, img_path in enumerate(images, 1):
            logger.info(f"    マルチモーダルOCR: ページ {i}/{len(images)}")
            md = _ocr_image(img_path, base_url)
            if not md:
                import time as _time
                for attempt in range(1, 3):
                    logger.info(f"    リトライ {attempt}/2: ページ {i}/{len(images)} (30秒待機)")
                    _time.sleep(30)
                    md = _ocr_image(img_path, base_url)
                    if md:
                        break
            if md:
                pages.append(md)
            else:
                failed_pages.append(i)
                pages.append(f"[OCR失敗: ページ {i}/{len(images)}]")
                logger.warning(f"    ページ {i}/{len(images)}: リトライ後もOCR失敗 — スキップ")

        if failed_pages:
            logger.warning(f"    OCR失敗ページ: {failed_pages} ({len(failed_pages)}/{len(images)}ページ)")

        return "\n\n---\n\n".join(pages) if pages else None


def _to_pdf(path: Path, tmpdir: Path) -> Path | None:
    try:
        subprocess.check_call(
            ["libreoffice", "--headless", "--convert-to", "pdf",
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


def _ocr_image(img_path: Path, base_url: str) -> str | None:
    import requests

    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    api_key = os.environ.get("OPENAI_API_KEY", "dummy")
    from cli_utils import detect_vllm_model
    try:
        model = os.environ.get("OPENAI_MODEL") or detect_vllm_model(base_url)
    except Exception:
        return None

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": SLIDE_OCR_PROMPT},
            ],
        }],
        "max_tokens": 4096,
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
        text = resp.json()["choices"][0]["message"]["content"]
        from cli_utils import strip_think_blocks
        return strip_think_blocks(text)
    except Exception as e:
        logger.warning(f"    マルチモーダルOCR失敗: {e}")
        return None


# ---------------------------------------------------------------------------
# Scan: register BOX files
# ---------------------------------------------------------------------------

def scan_sources(
    conn: sqlite3.Connection,
    config: dict,
    *,
    source_filter: str | None = None,
    dry_run: bool = False,
    log=print,
) -> int:
    """box_sources.yaml の全ソースを走査して box_files に登録する。"""
    sources = config.get("sources") or []
    if not sources:
        log("[WARN] box_sources.yaml に sources が定義されていません")
        return 0

    total = 0
    for src in sources:
        if not src.get("enabled", True):
            continue
        name = src.get("name", "")
        if source_filter and name != source_filter:
            continue

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

        log(f"\n[SCAN] {name} (folder_id={folder_id}, recursive={recursive})")
        if excl_folders:
            log(f"  exclude_folders: {excl_folders}")
        if excl_patterns:
            log(f"  exclude_patterns: {excl_patterns}")

        files = list_box_folder(folder_id, recursive=recursive, exclude_folders=excl_folders)
        log(f"  BOXファイル数: {len(files)}")

        pdf_stems: set[str] = set()
        for f in files:
            if f["file_format"] == "pdf":
                stem = Path(f["name"]).stem
                key = f"{f['folder_path']}/{stem}" if f["folder_path"] else stem
                pdf_stems.add(unicodedata.normalize("NFC", key))

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
                log(f"  [DRY] {f['file_format']:6s} {f['box_file_id']:12s} {f['folder_path']}/{f['name']}")
                registered += 1
                continue

            conn.execute(
                """INSERT INTO box_files
                   (box_file_id, box_folder_id, name, file_format, size_bytes,
                    modified_at, folder_path, index_name, source_name, registered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(box_file_id) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    modified_at = excluded.modified_at,
                    folder_path = excluded.folder_path,
                    index_name = excluded.index_name""",
                (
                    f["box_file_id"], f["box_folder_id"], f["name"],
                    f["file_format"], f["size_bytes"], f["modified_at"],
                    f["folder_path"], index_names_json, name, _now_iso(),
                ),
            )
            registered += 1

        if not dry_run:
            conn.commit()
        log(f"  登録: {registered} 件")
        total += registered

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
) -> dict:
    """ワーカープロセスで1ファイルを変換する。convert_files() から並列呼び出しされる。"""
    fid = file_info["box_file_id"]
    name = file_info["name"]
    fmt = file_info["file_format"]
    content_hash_old = file_info.get("content_hash")

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
                return {"status": "skipped", "file_id": fid, "name": name}

            logger.info(f"    変換中... (pid={pid})")
            content_md, method = convert_to_markdown(file_path, fmt)

            if not content_md.strip():
                logger.warning(f"    [WARN] 変換結果が空 (method={method}, pid={pid})")
                return {"status": "conversion_failed", "file_id": fid, "name": name}

            char_count = len(content_md)
            page_count = content_md.count("---\n") + 1 if method == "multimodal_ocr" else None

            for retry in range(3):
                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO doc_content
                           (box_file_id, content_md, content_hash, page_count,
                            char_count, convert_method, extracted_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (fid, content_md, content_hash, page_count, char_count, method, _now_iso()),
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
    if not force:
        where_parts.append("dc.box_file_id IS NULL")

    where = " AND ".join(where_parts)
    rows = conn.execute(
        f"""SELECT bf.box_file_id, bf.name, bf.file_format, bf.size_bytes,
                   bf.folder_path, bf.index_name, bf.modified_at,
                   dc.content_hash
            FROM box_files bf
            LEFT JOIN doc_content dc ON bf.box_file_id = dc.box_file_id
            WHERE {where}
            ORDER BY bf.name""",
        params,
    ).fetchall()

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
                [(f, str(db_path), no_encrypt, force) for f in file_infos],
            )
    else:
        results = [convert_single_file(f, str(db_path), no_encrypt, force) for f in file_infos]

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

    if not os.environ.get("OPENAI_API_BASE"):
        os.environ["OPENAI_API_BASE"] = "http://localhost:8000/v1"
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "dummy"

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
            log=log,
        )

    conn.close()
    if output_file:
        output_file.close()


if __name__ == "__main__":
    main()
