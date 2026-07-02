#!/usr/bin/env python3
"""
pm_exec_summary.py

複数アプリの評価レポート（Markdown）を「完了したこと / これからやること /
ベンダーとの連携状況」の3カテゴリに凝縮し、全アプリを1枚のPowerPointに
まとめたエグゼクティブサマリーを生成する。

pm_nvidia_collab_update.sh がアプリ単位で生成した argus_report_*.md を
入力として想定（先頭行 "# <アプリ名>" からアプリ名を取得）。

Usage:
    python3 scripts/pm_exec_summary.py report1.md report2.md ... \
        --lang both --to-box \
        --title "FugakuNEXT アプリ評価 エグゼクティブサマリー"

Options:
    --lang {ja,en,both}   生成する言語（デフォルト: both）
    --title TEXT          スライドタイトル（日本語。英語版は自動翻訳）
    --date YYYY-MM-DD      表示日付（省略時は当日）
    --to-box              Box にアップロードする（省略時はローカル保存のみ）
    --out-dir DIR          ローカル出力先ディレクトリ（デフォルト: /tmp）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.llm import call_argus_llm
from utils.pptx_theme import (
    DARK,
    GRAY,
    ICE,
    NAVY,
    TEAL,
    WHITE,
    add_bg,
    add_bullets,
    add_rect,
    add_text,
    blank_slide,
    make_presentation,
)

_CATEGORIES = ("completed", "next", "vendor")
_CATEGORY_LABELS_JA = {
    "completed": "完了したこと",
    "next": "これからやること",
    "vendor": "ベンダーとの連携状況",
}
_CATEGORY_LABELS_EN = {
    "completed": "Completed",
    "next": "Next Steps",
    "vendor": "Vendor Collaboration",
}
_MAX_ITEMS = 3
_MAX_CHARS = 40

_EXTRACT_PROMPT = """以下は「{app}」というアプリケーションの評価状況レポートです。
このレポートを、経営層向けエグゼクティブサマリーの3カテゴリに凝縮してください。

## カテゴリ
- completed: 完了したこと（証拠のある事項のみ。完了報告・確認メッセージ・日付等の裏付けがない
  「進行中」「未確認」の事項は completed に含めず next に回すこと）
- next: これからやること（未完了の作業・次のマイルストーン）
- vendor: ベンダー（NVIDIA・富士通等）との連携状況（協業内容・依存事項・連絡待ち等）

## 制約
- 各カテゴリ最大 {max_items} 項目
- 各項目は {max_chars} 文字以内（日本語は全角、英数字は半角でカウント）に要約すること
- レポートに記載の無い推測は書かない。該当情報が無いカテゴリは空配列 [] とする
- 出力は JSON のみ。前置き・説明・コードフェンス外のテキストは書かない

## 出力フォーマット（例）
{{"completed": ["GPU移植完了", "初期性能測定完了"], "next": ["大規模実行検証"], "vendor": ["NVIDIAとの週次MTG継続中"]}}

## レポート本文
---
{report}
"""

_TRANSLATE_SYSTEM = (
    "You are a professional translator. Translate the Japanese text values in "
    "the given JSON into natural, fluent English. Keep the JSON keys and "
    "structure exactly the same. Keep application names, proper nouns, and "
    "technical terms (e.g. GPU, NVIDIA, GENESIS) in their original form. "
    "Output JSON only, no commentary."
)


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found:\n{text[:300]}")


def _empty_buckets() -> dict:
    return {c: [] for c in _CATEGORIES}


def _sanitize_buckets(raw: dict) -> dict:
    out = _empty_buckets()
    for cat in _CATEGORIES:
        items = raw.get(cat) or []
        if not isinstance(items, list):
            continue
        cleaned = []
        for it in items[:_MAX_ITEMS]:
            s = str(it).strip()
            if not s:
                continue
            if len(s) > _MAX_CHARS:
                s = s[: _MAX_CHARS - 1] + "…"
            cleaned.append(s)
        out[cat] = cleaned
    return out


def app_name_from_report(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line:
            break
    return path.stem


def extract_buckets(app_name: str, md_text: str) -> dict:
    """アプリ1件のレポート全文からエグゼクティブサマリー3カテゴリを抽出する。

    LLM/JSONエラー時は空バケット（該当セル空欄）にフォールバックし、
    1アプリの抽出失敗で全体を落とさない。
    """
    prompt = _EXTRACT_PROMPT.format(
        app=app_name, max_items=_MAX_ITEMS, max_chars=_MAX_CHARS, report=md_text
    )
    for attempt in range(2):
        try:
            raw = call_argus_llm(prompt, timeout=180, max_tokens=1024, temperature=0.2)
            return _sanitize_buckets(extract_json(raw))
        except Exception as e:  # noqa: BLE001 — 1件の失敗で全体を止めない
            print(f"[WARN] {app_name}: 抽出失敗 (試行{attempt + 1}/2): {e}", file=sys.stderr)
    return _empty_buckets()


def translate_buckets(buckets_by_app: list[dict]) -> list[dict]:
    """全アプリ分のバケットをまとめて1回のLLM呼び出しで英訳する。

    失敗時は原文（日本語）のまま返す（英語版スライドに日本語が残るのみで、
    生成自体は失敗させない）。
    """
    payload = {b["app"]: {c: b[c] for c in _CATEGORIES} for b in buckets_by_app}
    try:
        raw = call_argus_llm(
            f"Translate this JSON:\n\n{json.dumps(payload, ensure_ascii=False)}",
            system=_TRANSLATE_SYSTEM,
            timeout=180,
            max_tokens=2048,
            temperature=0.3,
        )
        translated = extract_json(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 英訳失敗、日本語のまま使用: {e}", file=sys.stderr)
        return buckets_by_app

    out = []
    for b in buckets_by_app:
        t = translated.get(b["app"]) or {}
        merged = {"app": b["app"]}
        for c in _CATEGORIES:
            items = t.get(c)
            merged[c] = items if isinstance(items, list) and items else b[c]
        out.append(merged)
    return out


def build_deck(buckets_by_app: list[dict], *, lang: str, title: str, date_str: str):
    labels = _CATEGORY_LABELS_EN if lang == "en" else _CATEGORY_LABELS_JA
    prs = make_presentation()
    sw, sh = prs.slide_width, prs.slide_height
    slide = blank_slide(prs)
    add_bg(slide, sw, sh)

    # タイトル帯
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    add_rect(slide, 0, 0, sw, Inches(0.95), NAVY)
    add_text(slide, Inches(0.4), Inches(0.18), Inches(9.5), Inches(0.6),
              title, size=22, bold=True, color=WHITE)
    add_text(slide, sw - Inches(2.6), Inches(0.35), Inches(2.2), Inches(0.4),
              date_str, size=12, color=WHITE, align=PP_ALIGN.RIGHT)

    n_apps = len(buckets_by_app)
    left_margin = Inches(0.4)
    app_col_w = Inches(1.7)
    cat_col_w = Inches(3.55)
    header_y = Inches(1.15)
    header_h = Inches(0.4)
    row_y0 = header_y + header_h
    row_h = int((sh - row_y0 - Inches(0.15)) / max(n_apps, 1))

    # 列ヘッダ
    add_rect(slide, left_margin, header_y, app_col_w, header_h, TEAL)
    for i, cat in enumerate(_CATEGORIES):
        x = left_margin + app_col_w + i * cat_col_w
        add_rect(slide, x, header_y, cat_col_w, header_h, TEAL)
        add_text(slide, x + Inches(0.1), header_y, cat_col_w - Inches(0.2), header_h,
                  labels[cat], size=13, bold=True, color=WHITE)

    # データ行
    for row, bucket in enumerate(buckets_by_app):
        y = row_y0 + row * row_h
        bg = ICE if row % 2 == 0 else WHITE
        add_rect(slide, left_margin, y, app_col_w + cat_col_w * 3, row_h, bg)
        add_text(slide, left_margin + Inches(0.1), y + Inches(0.05),
                  app_col_w - Inches(0.2), row_h - Inches(0.1),
                  bucket["app"], size=12, bold=True, color=NAVY)
        for i, cat in enumerate(_CATEGORIES):
            x = left_margin + app_col_w + i * cat_col_w
            items = bucket[cat] or ["—"]
            add_bullets(slide, x + Inches(0.1), y + Inches(0.05),
                        cat_col_w - Inches(0.2), row_h - Inches(0.1),
                        items, size=10, color=DARK, gap=2)
        add_rect(slide, left_margin, y + row_h - Inches(0.01),
                  app_col_w + cat_col_w * 3, Inches(0.01), GRAY)

    return prs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reports", nargs="+", help="アプリ評価レポート(.md)。渡した順にグリッド行になる")
    ap.add_argument("--lang", choices=["ja", "en", "both"], default="both")
    ap.add_argument("--title", default="FugakuNEXT アプリ評価 エグゼクティブサマリー")
    ap.add_argument("--date", default="")
    ap.add_argument("--to-box", action="store_true")
    ap.add_argument("--out-dir", default="/tmp")
    args = ap.parse_args()

    date_str = args.date or date.today().isoformat()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets_by_app = []
    for report_path in args.reports:
        p = Path(report_path)
        if not p.exists():
            print(f"[WARN] レポートが見つかりません、スキップ: {p}", file=sys.stderr)
            continue
        app_name = app_name_from_report(p)
        md_text = p.read_text(encoding="utf-8", errors="ignore")
        print(f"[INFO] 抽出中: {app_name}", file=sys.stderr)
        buckets = extract_buckets(app_name, md_text)
        buckets_by_app.append({"app": app_name, **buckets})

    if not buckets_by_app:
        print("[ERROR] 有効なレポートがありません", file=sys.stderr)
        return 1

    from argus.output_tools import (
        box_upload_file,  # ここで import（--to-box 未使用時も安全に動作）
    )

    if args.lang in ("ja", "both"):
        prs_ja = build_deck(buckets_by_app, lang="ja", title=args.title, date_str=date_str)
        path_ja = out_dir / f"executive_summary_{date_str}.pptx"
        prs_ja.save(str(path_ja))
        print(f"[INFO] 生成完了 (JP): {path_ja}", file=sys.stderr)
        if args.to_box:
            print(box_upload_file(path_ja, filename=path_ja.name), file=sys.stderr)

    if args.lang in ("en", "both"):
        buckets_en = translate_buckets(buckets_by_app)
        title_en = "FugakuNEXT Application Assessment — Executive Summary"
        prs_en = build_deck(buckets_en, lang="en", title=title_en, date_str=date_str)
        path_en = out_dir / f"executive_summary_{date_str}_EN.pptx"
        prs_en.save(str(path_en))
        print(f"[INFO] 生成完了 (EN): {path_en}", file=sys.stderr)
        if args.to_box:
            print(box_upload_file(path_en, filename=path_en.name), file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
