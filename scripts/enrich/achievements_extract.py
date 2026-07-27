#!/usr/bin/env python3
"""
achievements_extract.py — アプリ別「完了実績」の検索・LLM抽出共有モジュール

pm.db（qa_index.db 経由の pm 系索引）から、アプリが実際に完了・達成した
マイルストームを検索・抽出する。以下の2箇所から共有利用される:
  - scripts/reporting/pm_exec_summary.py（エグゼクティブサマリー completed 列）
  - scripts/ingest/achievements.py（実績台帳 populator）

元実装は pm_exec_summary.py の _retrieve_completed_candidates /
_extract_completed_from_search / _COMPLETED_PROMPT にあったものを移設・拡張した。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm

# 検索対象 index（マイルストーン発言は pm に含まれる）
_SEARCH_INDEX = "pm"
_DEFAULT_K = 20
# ledger 投入用途では完了実績を網羅的に拾いたいため、pm_exec_summary の
# completed 列（最大5件）より広めに抽出する。
_MAX_ACHIEVEMENTS = 10

_ACHIEVEMENTS_PROMPT = """以下は「{app}」に関する検索結果（プロジェクト全期間）です。
この中から、{app} が**実際に完了・達成した重要なマイルストーン**を最大{max_items}件抽出してください。

## 抽出ルール
- 対象: GPU移植/CUDA・OpenACC対応、性能測定、ベンチマーク収録、OSS・GitHub公開、EEA登録、実機評価 等の**実績**。
- 検索結果が「〜済み/公開/実施/測定完了/対応済み」等、実績として記述している事項を採用してよい（明示的な完了報告メッセージが無くても可）。ただし検索結果に無い推測は書かない（捏造禁止）。
- 資料作成・会議記録・「〜に言及」「対応を記録」等の手続き的メモは除外し、実質的な成果のみを対象とする。
- {app} 以外のアプリの実績は含めない。該当が無ければ空配列 [] とする。
- title は名詞止めの短い一句（40字以内）。
- category は実績の種別（例: GPU移植, 性能測定, OSS公開, EEA登録, 実機評価 等）。不明なら空文字。
- achieved_on は分かる範囲で YYYY-MM または YYYY-MM-DD 形式（不明なら空文字 ""）。
- evidence_ref には**根拠とした情報源の出典ラベル**（各候補行頭の `[出典: …]` に示されるチャンネル名/議事録名/文書名）を入れる。年月ではなく情報源を入れること。日付は achieved_on に入れる。
- evidence_quote は根拠となる原文を、**句点「。」など文の自然な区切りで終わる完全な形**で抜粋する。語や文の途中で終えない（尻切れ厳禁）。候補テキストがその箇所で途切れている場合は、途切れる前の**最後の完全な文**までにする。250字以内。
- confidence は、明示的な日付・完了報告・公開URL等の裏付けがあれば "high"、状況証拠のみの推測なら "low" とする。
- **同一の実績を複数回書かない**。検索結果に同じ事柄への言及が複数回現れても、1実績1エントリにまとめる。「（再報告）」「再掲」「再度」等を付けた重複エントリは出さない。
- 出力は JSON のみ。前置き・説明・コードフェンス外テキスト禁止。
- **必ず妥当な JSON にすること**。各文字列値の中に半角二重引用符( " )・改行・タブ・生の制御文字を含めない。
  原文に二重引用符 "…" がある場合は「…」に置き換え、値は必ず1行に収める。
{known_titles_section}
## 出力フォーマット（例）
{{"achievements": [
  {{"title": "OpenACC版をGitHub公開", "category": "OSS公開", "achieved_on": "2025-12",
    "evidence_ref": "22_ベンチマークwg / Slackメッセージ (2025-12-03)", "evidence_quote": "OpenACC版をGitHubにて公開した", "confidence": "high"}}
]}}

## 検索結果
---
{candidates}
"""

_KNOWN_TITLES_SECTION_TEMPLATE = """
## 既に台帳に記録済みの実績（重複禁止）
以下は既に記録済みです。これらと同一または言い換え・粒度違いにすぎない実績は**出力しないでください**。検索結果の中に、以下に無い**新規の**完了実績があればそれだけを出力してください。新規が無ければ空配列 [] を返してください。
{titles}
"""


def _build_known_titles_section(known_titles: list[str] | None) -> str:
    if not known_titles:
        return ""
    titles_text = "\n".join(f"- {t}" for t in known_titles)
    return _KNOWN_TITLES_SECTION_TEMPLATE.format(titles=titles_text)


def _extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"\{[\s\S]+\}", text)
        if not m:
            raise ValueError(f"JSON not found:\n{text[:300]}")
        raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # LLM 出力の頻出崩れに耐性を持たせる:
        #   strict=False で文字列値内の生改行・タブ等の制御文字を許容し、
        #   "Unterminated string" 系の失敗を救済する。
        return json.loads(raw, strict=False)


def retrieve_candidates(app_name: str, k: int = _DEFAULT_K) -> list[str] | None:
    """アプリ名で recency 非適用のハイブリッド検索を行い、実績候補チャンク本文を返す。

    DB/検索が使えない場合は None（呼び出し側がフォールバックする）。
    プロジェクト全期間を対象にするため since_date は指定しない。
    """
    try:
        from argus.mcp_tools import _QA_INDEX
        from argus.retrieval import retrieve_chunks_hybrid
        if not _QA_INDEX.exists():
            return None
        try:
            from argus.pm_qa_server import _format_source_label
        except Exception:
            _format_source_label = None
        query = (
            f"{app_name} がこれまでに完了・達成した実績・マイルストーン: "
            f"GPU移植 CUDA対応 OpenACC対応 性能測定 ベンチマーク収録 OSS公開 "
            f"GitHub公開 EEA登録 実機評価 リリース"
        )
        chunks = retrieve_chunks_hybrid(
            query, _QA_INDEX, k=k,
            index_name=_SEARCH_INDEX, since_date=None,
        )
        if not chunks:
            return None
        out = []
        for c in chunks:
            try:
                label = _format_source_label(c) if _format_source_label else None
            except Exception:
                label = None
            if label is None:
                label = c.get("source_ref") or c.get("source_type") or "?"
            content = (c.get("content") or "").strip().replace("\n", " ")[:1500]
            if content:
                out.append(f"[出典: {label}] {content}")
        return out or None
    except Exception as e:  # noqa: BLE001 — 検索失敗でも全体は止めない
        print(f"[WARN] {app_name}: 実績候補検索失敗: {e}", file=sys.stderr)
        return None


# title 先頭のアプリ名重複除去に許す区切り文字（0〜2文字）。
_TITLE_APP_PREFIX_SEP_RE = r"[の:：/・\-を\s]"


def _strip_app_name_prefix(title: str, app_name: str) -> str:
    """title が app_name（大文字小文字無視）で始まる場合、先頭の app 名と直後の
    区切り文字（`の`、`:`、`：`、`/`、`・`、空白、`-`、`を` のいずれか0〜2文字）を除去する。

    除去後の残りが4文字未満になる場合は「app_name」単体のような title を
    空にしないよう元の title を維持する。app_name が空の場合は正規化をスキップする。
    抽出時（LLM候補のサニタイズ時）のみに適用し、既存DB行は変更しない。
    """
    if not app_name or not title:
        return title
    pattern = re.compile(
        r"^" + re.escape(app_name) + _TITLE_APP_PREFIX_SEP_RE + r"{0,2}",
        re.IGNORECASE,
    )
    m = pattern.match(title)
    if not m:
        return title
    stripped = title[m.end():]
    if len(stripped) < 4:
        return title
    return stripped


def _sanitize_achievement(app_name: str, raw: dict) -> dict | None:
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    title = _strip_app_name_prefix(title, app_name)
    if len(title) > 40:
        title = title[:39] + "…"
    confidence = str(raw.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "low"):
        confidence = "low"
    return {
        "app": app_name,
        "title": title,
        "category": str(raw.get("category") or "").strip(),
        "achieved_on": str(raw.get("achieved_on") or "").strip(),
        "evidence_ref": str(raw.get("evidence_ref") or "").strip(),
        "evidence_quote": str(raw.get("evidence_quote") or "").strip(),
        "confidence": confidence,
    }


def extract_achievements(app_name: str, known_titles: list[str] | None = None) -> list[dict]:
    """検索候補を LLM で凝縮し、構造化された実績 dict のリストを返す。

    known_titles を渡すと、既に台帳に記録済みの実績をプロンプトに明示し、
    それらと同一・言い換え・粒度違いの実績を出力しないよう指示する
    （台帳認識/ledger-aware 抽出。embedding では拾いにくい言い換えも
    LLM の意味理解で抑止し、populator の再実行をほぼ冪等にする）。
    None（または空リスト）の場合は従来通り全件抽出する
    （pm_exec_summary.py 経由の extract_completed_titles 等、後方互換のため）。

    候補が無い/LLM・JSON解析が失敗した場合は空リストを返す（捏造しない）。
    """
    candidates = retrieve_candidates(app_name)
    if not candidates:
        return []

    prompt = _ACHIEVEMENTS_PROMPT.format(
        app=app_name, max_items=_MAX_ACHIEVEMENTS,
        known_titles_section=_build_known_titles_section(known_titles),
        candidates="\n\n".join(candidates),
    )
    for attempt in range(3):
        try:
            raw = call_argus_llm(prompt, timeout=240, max_tokens=4096, temperature=0.0)
            data = _extract_json(raw)
            items = data.get("achievements", [])
            if not isinstance(items, list):
                continue
            result = []
            for item in items[:_MAX_ACHIEVEMENTS]:
                if not isinstance(item, dict):
                    continue
                sanitized = _sanitize_achievement(app_name, item)
                if sanitized:
                    result.append(sanitized)
            return result
        except Exception as e:  # noqa: BLE001 — 1件の失敗で全体を止めない
            print(f"[WARN] {app_name}: 実績抽出失敗 (試行{attempt + 1}/2): {e}", file=sys.stderr)
    return []


def read_confirmed_titles(
    app_name: str, limit: int | None = None, db_path: str | Path | None = None
) -> list[str]:
    """achievements テーブルから status='confirmed' の実績 title を返す。

    achieved_on 昇順（時系列）で全件取得する。limit=None（既定）なら全件、
    limit 指定時は末尾（直近 limit 件）を時系列のまま切り出す。
    各要素は title に日付があれば '(YYYY-MM)' を付す（既存の completed 列書式に合わせる）。
    DB/テーブルが無い・鍵が無い等は空リスト（呼び出し側がフォールバック）。
    """
    from db_utils import open_pm_db

    path = Path(db_path) if db_path else _REPO_ROOT / "data" / "pm.db"
    if not path.exists():
        return []
    try:
        conn = open_pm_db(path)
    except SystemExit:
        return []
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT title, achieved_on FROM achievements "
            "WHERE app=? AND status='confirmed' AND COALESCE(deleted,0)=0 "
            "ORDER BY achieved_on",
            (app_name,),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]  # 直近 limit 件を時系列のまま切り出す

    titles = []
    for row in rows:
        title = str(row["title"] or "").strip()
        if not title:
            continue
        achieved_on = str(row["achieved_on"] or "").strip()
        if achieved_on:
            ym = achieved_on[:7]  # YYYY-MM に丸める
            if f"({ym})" not in title and "(" not in title:
                title = f"{title} ({ym})"
        titles.append(title)
    return titles


_CONDENSE_PROMPT = """以下は「{app}」の完了実績一覧（時系列）です。
経営層向けサマリーの「完了したこと」欄に載せる項目を最大{max_items}件に凝縮してください。

## 選定の優先順位（厳守）
1. **性能評価の実績を最優先**: 性能測定の実施・結果（絶対性能・対富岳比・GPU世代間比較・
   スケーラビリティ等）、実機評価、ベンチマーク測定。入力に存在する限り、枠の過半を
   この種別で埋めること。
2. 次点: 契約・合意・ソース/データ提供完了などの協業上の節目。
3. GPU移植・OSS公開・EEA登録・レシピ/ベンチマーク収録などの作成・登録系は、
   枠が余る場合に代表的な1〜2件のみ。

## 抽出ルール
- 出力は入力にある実績のみを対象とする。入力に無い実績を捏造しない。
- 各項目は名詞止めの短い一句（50字以内）で、日付 '(YYYY-MM)' を保持する。
- 類似・同系統の実績（例: 同時期の契約条件合意が複数件）は1件に統合してよい。
  統合した場合、日付は代表日付、または範囲 '(2025-09〜2025-12)' を付す。
- 出力は必ず**時系列順**（古い→新しい）に並べる。
- 出力は JSON のみ。前置き・説明・コードフェンス外テキスト禁止。

## 出力フォーマット（例）
{{"condensed": ["契約条件合意 (2025-09〜2025-12)", "OpenACC版をGitHub公開 (2025-12)"]}}

## 完了実績一覧
---
{numbered_titles}
"""


def condense_confirmed_titles(app_name: str, titles: list[str], max_items: int = 5) -> list[str]:
    """confirmed 実績 title のリストを LLM で max_items 件に凝縮する。

    件数が max_items 以下ならそのまま返す（LLM を呼ばない）。件数超過時は
    全件を LLM に渡し、直近 max_items 件を機械的に選ぶのではなく重要な実績を
    時系列で残しつつ凝縮する（古い重要な合意を落とさないため）。
    LLM 失敗・JSON 不正・空リストの場合は titles[-max_items:]（直近 max_items 件）に
    フォールバックする。
    """
    if len(titles) <= max_items:
        return titles

    numbered_titles = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
    prompt = _CONDENSE_PROMPT.format(
        app=app_name, max_items=max_items, numbered_titles=numbered_titles,
    )
    for attempt in range(3):
        try:
            raw = call_argus_llm(prompt, timeout=240, max_tokens=2048, temperature=0.0)
            data = _extract_json(raw)
            items = data.get("condensed", [])
            if not isinstance(items, list):
                continue
            result = []
            for item in items[:max_items]:
                s = str(item).strip()
                if s:
                    result.append(s)
            if result:
                return result
        except Exception as e:  # noqa: BLE001 — 1回の失敗で全体を止めない
            print(f"[WARN] {app_name}: 実績凝縮失敗 (試行{attempt + 1}/3): {e}", file=sys.stderr)
    print(f"[WARN] {app_name}: 実績凝縮フォールバック（直近{max_items}件を使用）", file=sys.stderr)
    return titles[-max_items:]


def extract_completed_titles(app_name: str) -> list[str] | None:
    """後方互換: pm_exec_summary.py の completed 列向けに、title（+日付）の
    文字列リストを最大5件返す。抽出結果が無ければ None。
    """
    achievements = extract_achievements(app_name)
    if not achievements:
        return None
    titles = []
    for a in achievements[:5]:
        title = a["title"]
        if a["achieved_on"]:
            title = f"{title}({a['achieved_on']})"
        titles.append(title)
    return titles or None
