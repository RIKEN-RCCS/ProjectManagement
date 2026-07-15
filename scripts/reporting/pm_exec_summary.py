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
_MAX_ITEMS = {"completed": 5, "next": 3, "vendor": 3}
_MAX_CHARS = {"completed": 30, "next": 34, "vendor": 34}

# 完了列を決定的に生成するためのハイブリッド検索設定
_COMPLETED_SEARCH_INDEX = "pm"          # 検索対象 index（マイルストーン発言は pm に含まれる）
_COMPLETED_SEARCH_K = 20                 # 取得チャンク数

_EXTRACT_PROMPT = """以下は「{app}」というアプリケーションの評価状況レポートです。
このレポートを、経営層向けエグゼクティブサマリーの3カテゴリに凝縮してください。

## カテゴリ
- completed: プロジェクト推進に関わる**重要な完了マイルストーン**（移植・公開・性能測定・評価・登録・
  対応等の実績）を優先して最大5件。レポートが実績として記述している事項（〜済み / 〜公開 / 〜実施 /
  測定完了 等）は、明示的な完了報告メッセージが無くても completed に含めてよい。ただしレポートに
  記載の無い推測は書かない。各項目は名詞止めの短い一句（20字以内）。日付があれば末尾に (2025-12) の
  形で添える。資料作成・会議記録・「〜に言及」「対応を記録」等の手続き的メモは書かず、実質的な
  成果のみ。
- next: これからやること（未完了の作業・次のマイルストーン）。最大3件、各34字以内。
- vendor: ベンダー（NVIDIA・富士通等）との連携状況（協業内容・依存事項・連絡待ち等）。最大3件、
  各34字以内。

## 制約
- completed は最大5項目・各20字以内、next・vendor はそれぞれ最大3項目・各34字以内
  （日本語は全角、英数字は半角でカウント）に要約すること
- レポートに記載の無い推測は書かない。該当情報が無いカテゴリは空配列 [] とする
- 出力は JSON のみ。前置き・説明・コードフェンス外のテキストは書かない

## 出力フォーマット（例）
{{"completed": ["GPU移植完了", "初期性能測定完了(2025-12)", "OSS版を公開", "ベンチマーク収録", "EEA登録完了"], "next": ["大規模実行検証"], "vendor": ["NVIDIAと週次MTG継続"]}}

## レポート本文
---
{report}
"""

_COMPLETED_PROMPT = """以下は「{app}」に関する検索結果（プロジェクト全期間）です。
この中から、{app} が**実際に完了・達成した重要なマイルストーン**を最大{max_items}件抽出してください。

## 抽出ルール
- 対象: GPU移植/CUDA・OpenACC対応、性能測定、ベンチマーク収録、OSS・GitHub公開、EEA登録、実機評価 等の**実績**。
- 検索結果が「〜済み/公開/実施/測定完了/対応済み」等、実績として記述している事項を採用してよい（明示的な完了報告メッセージが無くても可）。ただし検索結果に無い推測は書かない。
- 各項目は名詞止めの短い一句（20字以内）。日付が分かれば末尾に (2025-12) の形で添える。
- 資料作成・会議記録・「〜に言及」「対応を記録」等の手続き的メモは書かない。実質的な成果のみ。
- {app} 以外のアプリの実績は含めない。該当が無ければ空配列 [] とする。
- 出力は JSON のみ。前置き・説明・コードフェンス外テキスト禁止。

## 出力フォーマット（例）
{{"completed": ["OpenACC版をGitHub公開(2025-12)", "富岳ベースライン性能測定", "FS_Benchmarks収録", "EEA登録完了"]}}

## 検索結果
---
{candidates}
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
        for it in items[: _MAX_ITEMS[cat]]:
            s = str(it).strip()
            if not s:
                continue
            if len(s) > _MAX_CHARS[cat]:
                s = s[: _MAX_CHARS[cat] - 1] + "…"
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
    prompt = _EXTRACT_PROMPT.format(app=app_name, report=md_text)
    for attempt in range(2):
        try:
            raw = call_argus_llm(prompt, timeout=180, max_tokens=1024, temperature=0.2)
            return _sanitize_buckets(extract_json(raw))
        except Exception as e:  # noqa: BLE001 — 1件の失敗で全体を止めない
            print(f"[WARN] {app_name}: 抽出失敗 (試行{attempt + 1}/2): {e}", file=sys.stderr)
    return _empty_buckets()


def _retrieve_completed_candidates(app_name: str) -> list[str] | None:
    """アプリ名で recency 非適用のハイブリッド検索を行い、完了実績の候補チャンク本文を返す。

    DB/検索が使えない場合は None（呼び出し側がレポート由来の completed にフォールバック）。
    プロジェクト全期間を対象にするため since_date は指定しない。
    """
    try:
        from argus.mcp_tools import _QA_INDEX
        from argus.retrieval import retrieve_chunks_hybrid
        if not _QA_INDEX.exists():
            return None
        query = (
            f"{app_name} がこれまでに完了・達成した実績・マイルストーン: "
            f"GPU移植 CUDA対応 OpenACC対応 性能測定 ベンチマーク収録 OSS公開 "
            f"GitHub公開 EEA登録 実機評価 リリース"
        )
        chunks = retrieve_chunks_hybrid(
            query, _QA_INDEX, k=_COMPLETED_SEARCH_K,
            index_name=_COMPLETED_SEARCH_INDEX, since_date=None,
        )
        if not chunks:
            return None
        out = []
        for c in chunks:
            held = (c.get("held_at") or "")[:7]  # YYYY-MM
            content = (c.get("content") or "").strip().replace("\n", " ")[:300]
            if content:
                out.append(f"[{held}] {content}")
        return out or None
    except Exception as e:  # noqa: BLE001 — 検索失敗でも全体は止めない
        print(f"[WARN] {app_name}: 完了検索失敗、レポート由来にフォールバック: {e}", file=sys.stderr)
        return None


def _extract_completed_from_search(app_name: str) -> list[str] | None:
    """検索ベースで completed バケットを生成する。失敗/該当なしは None。"""
    candidates = _retrieve_completed_candidates(app_name)
    if not candidates:
        return None
    prompt = _COMPLETED_PROMPT.format(
        app=app_name, max_items=_MAX_ITEMS["completed"],
        candidates="\n\n".join(candidates),
    )
    for attempt in range(2):
        try:
            raw = call_argus_llm(prompt, timeout=180, max_tokens=1024, temperature=0.2)
            data = extract_json(raw)
            items = _sanitize_buckets({"completed": data.get("completed", []),
                                       "next": [], "vendor": []})["completed"]
            return items or None
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] {app_name}: 完了凝縮失敗 (試行{attempt + 1}/2): {e}", file=sys.stderr)
    return None


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
            merged[c] = (
                items[: _MAX_ITEMS[c]] if isinstance(items, list) and items else b[c]
            )
        out.append(merged)
    return out


def build_deck(buckets_by_app: list[dict], *, lang: str, title: str, date_str: str):
    labels = _CATEGORY_LABELS_EN if lang == "en" else _CATEGORY_LABELS_JA
    prs = make_presentation()
    sw, sh = prs.slide_width, prs.slide_height
    slide = blank_slide(prs)
    add_bg(slide, sw, sh)

    # タイトル帯
    from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Inches

    add_rect(slide, 0, 0, sw, Inches(0.95), NAVY)
    add_text(slide, Inches(0.4), Inches(0.18), Inches(9.5), Inches(0.6),
              title, size=22, bold=True, color=WHITE)
    add_text(slide, sw - Inches(2.6), Inches(0.35), Inches(2.2), Inches(0.4),
              date_str, size=12, color=WHITE, align=PP_ALIGN.RIGHT)

    n_apps = len(buckets_by_app)
    left_margin = Inches(0.4)
    app_col_w = Inches(1.7)
    cat_widths = {"completed": Inches(4.3), "next": Inches(3.2), "vendor": Inches(3.15)}
    row_total_w = app_col_w + cat_widths["completed"] + cat_widths["next"] + cat_widths["vendor"]
    cat_x = {}
    _acc = left_margin + app_col_w
    for c in _CATEGORIES:
        cat_x[c] = _acc
        _acc += cat_widths[c]
    header_y = Inches(1.15)
    header_h = Inches(0.4)
    row_y0 = header_y + header_h
    row_h = int((sh - row_y0 - Inches(0.15)) / max(n_apps, 1))

    # 列ヘッダ
    add_rect(slide, left_margin, header_y, app_col_w, header_h, TEAL)
    for cat in _CATEGORIES:
        x = cat_x[cat]
        add_rect(slide, x, header_y, cat_widths[cat], header_h, TEAL)
        add_text(slide, x + Inches(0.1), header_y, cat_widths[cat] - Inches(0.2), header_h,
                  labels[cat], size=13, bold=True, color=WHITE)

    # データ行
    for row, bucket in enumerate(buckets_by_app):
        y = row_y0 + row * row_h
        bg = ICE if row % 2 == 0 else WHITE
        add_rect(slide, left_margin, y, row_total_w, row_h, bg)
        add_text(slide, left_margin + Inches(0.1), y + Inches(0.05),
                  app_col_w - Inches(0.2), row_h - Inches(0.1),
                  bucket["app"], size=12, bold=True, color=NAVY)
        for cat in _CATEGORIES:
            x = cat_x[cat]
            items = bucket[cat] or ["—"]
            size = 9 if cat == "completed" else 10
            tb = add_bullets(slide, x + Inches(0.1), y + Inches(0.05),
                              cat_widths[cat] - Inches(0.2), row_h - Inches(0.1),
                              items, size=size, color=DARK, gap=2)
            if cat == "completed":
                tb.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        add_rect(slide, left_margin, y + row_h - Inches(0.01),
                  row_total_w, Inches(0.01), GRAY)

    return prs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reports", nargs="+", help="アプリ評価レポート(.md)。渡した順にグリッド行になる")
    ap.add_argument("--lang", choices=["ja", "en", "both"], default="both")
    ap.add_argument("--title", default="FugakuNEXT アプリ評価 エグゼクティブサマリー")
    ap.add_argument("--date", default="")
    ap.add_argument("--to-box", action="store_true")
    ap.add_argument("--out-dir", default="/tmp")
    ap.add_argument("--no-completed-search", action="store_true",
                    help="完了列を検索ベースで生成せず、レポート md からの抽出のみを使う")
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
        buckets = extract_buckets(app_name, md_text)   # next/vendor と completed(フォールバック)
        if not args.no_completed_search:
            searched = _extract_completed_from_search(app_name)
            if searched:
                buckets["completed"] = searched
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
