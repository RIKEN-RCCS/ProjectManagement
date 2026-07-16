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
- evidence_ref には検索結果中の該当箇所の年月（例: 2025-12）を、evidence_quote には根拠となった原文の抜粋（100字以内）を入れる。
- confidence は、明示的な日付・完了報告・公開URL等の裏付けがあれば "high"、状況証拠のみの推測なら "low" とする。
- **同一の実績を複数回書かない**。検索結果に同じ事柄への言及が複数回現れても、1実績1エントリにまとめる。「（再報告）」「再掲」「再度」等を付けた重複エントリは出さない。
- 出力は JSON のみ。前置き・説明・コードフェンス外テキスト禁止。
{known_titles_section}
## 出力フォーマット（例）
{{"achievements": [
  {{"title": "OpenACC版をGitHub公開", "category": "OSS公開", "achieved_on": "2025-12",
    "evidence_ref": "2025-12", "evidence_quote": "OpenACC版をGitHubにて公開した", "confidence": "high"}}
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
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found:\n{text[:300]}")


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
            held = (c.get("held_at") or "")[:7]  # YYYY-MM
            content = (c.get("content") or "").strip().replace("\n", " ")[:300]
            if content:
                out.append(f"[{held}] {content}")
        return out or None
    except Exception as e:  # noqa: BLE001 — 検索失敗でも全体は止めない
        print(f"[WARN] {app_name}: 実績候補検索失敗: {e}", file=sys.stderr)
        return None


def _sanitize_achievement(app_name: str, raw: dict) -> dict | None:
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
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
    for attempt in range(2):
        try:
            raw = call_argus_llm(prompt, timeout=180, max_tokens=1536, temperature=0.0)
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


def read_confirmed_titles(app_name: str, limit: int = 5, db_path: str | Path | None = None) -> list[str]:
    """achievements テーブルから status='confirmed' の実績 title を achieved_on 昇順で返す。

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
            "ORDER BY achieved_on LIMIT ?",
            (app_name, limit),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

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
