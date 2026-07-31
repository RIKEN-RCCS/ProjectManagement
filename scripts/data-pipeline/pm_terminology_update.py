#!/usr/bin/env python3
"""pm_terminology_update.py — pm.db/terminology テーブルを各種ソースから更新する

ソース優先順位:
  1. slide_ocr 出力（terminology.txt: 1行1語の固有名詞リスト）— ground truth
  2. pm.db の decisions / action_items に登場するアプリ名・人名
  3. qa_index.db の chunks（FTS5 索引）から高頻度の固有名詞候補
  4. CRON で定期実行 or pm_ingest.py 完了後フックとして使用

使い方:
  python3 scripts/data-pipeline/pm_terminology_update.py [options]

Options:
  --slide-terms FILE   slide_ocr が出力した terminology.txt（1行1語）
  --meeting-kind KIND  会議種別タグ（meeting_kinds カラム）
  --from-pm-db         pm.db の decisions/actions から用語を抽出（デフォルト: 有効）
  --no-pm-db           pm.db からの抽出を無効化
  --db PATH            pm.db のパス（デフォルト: data/pm.db）
  --goals PATH         goals.yaml のパス（デフォルト: goals.yaml。テスト等で実ファイルを
                       読ませたくない場合は架空パスを明示指定する）
  --no-encrypt         平文モード
  --dry-run            DB 更新せずに抽出結果を表示

環境変数:
  PM_DB_KEY            SQLCipher 暗号化鍵
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from utils.terminology import add_term, load_all_terms

# --------------------------------------------------------------------------- #
# ソース別の用語抽出
# --------------------------------------------------------------------------- #

def extract_from_slide_terms(slide_terms_path: str | Path) -> list[tuple[str, str, list[str]]]:
    """slide_ocr の terminology.txt から (term, category, aliases) のリストを返す。"""
    path = Path(slide_terms_path)
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if not term:
            continue
        # アプリ名らしい（英数字・スラッシュ・ハイフン主体）なら app、それ以外は unknown
        category = "app" if re.match(r"^[A-Za-z0-9/\-_.]+$", term) else "unknown"
        terms.append((term, category, []))
    return terms


# 公開 OSS ツール名のみを直書きする（プロジェクト固有のアプリ名は _load_dynamic_app_pattern()
# 経由で pm.db.terminology から動的に読み込む。直書きしない理由は同関数の docstring 参照）。
_APP_PATTERN = re.compile(
    r"\b(Benchpark|OpenOnDemand|Spack|Ramble|"
    r"vLLM|bge-m3|PyAnnote|Whisper)\b",
    re.IGNORECASE,
)
_PERSON_PATTERN = re.compile(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:氏|さん|博士|教授|研究員)")

# マッチ文字列（小文字化）→ 正規形のマッピング
_APP_CANONICAL: dict[str, str] = {
    "benchpark": "Benchpark",
    "openondemand": "OpenOnDemand",
    "spack": "Spack",
    "ramble": "Ramble",
    "vllm": "vLLM",
    "bge-m3": "bge-m3",
    "pyannotate": "PyAnnote",
    "pyanote": "PyAnnote",
    "whisper": "Whisper",
}


def _canonical_app(matched: str) -> str:
    """マッチした文字列を正規形に正規化する。_APP_CANONICAL に未登録なら警告してそのまま返す。"""
    key = matched.lower()
    if key not in _APP_CANONICAL:
        print(f"[WARN] _APP_CANONICAL に未登録: {matched!r} — _APP_CANONICAL への追加を検討してください", file=sys.stderr)
    return _APP_CANONICAL.get(key, matched)


def _load_dynamic_app_pattern(db_path: Path) -> tuple[re.Pattern | None, dict[str, str]]:
    """pm.db.terminology（category='app'）からプロジェクト固有のアプリ名を動的に読み込み、
    追加の抽出パターンと正規形マッピングを構築する。

    プロジェクト固有のアプリ名を _APP_PATTERN / _APP_CANONICAL に直書きしないための経路
    （terminology-curation Skill 参照）。terminology テーブルに category='app' の登録が
    無い場合は WARN を出し、この経路での抽出をスキップする（silent degrade しない）。
    """
    terms = [t for t in load_all_terms(db_path=db_path) if t.get("category") == "app"]
    if not terms:
        print(
            "[WARN] terminology テーブルに category='app' の登録がありません — "
            "pm.db からのアプリ名抽出（動的分）をスキップします。"
            "slide_ocr 経由の登録または utils/terminology.py: add_term() での手動登録を検討してください",
            file=sys.stderr,
        )
        return None, {}

    canonical: dict[str, str] = {}
    alternatives: list[str] = []
    for t in terms:
        term = t["term"]
        canonical[term.lower()] = term
        alternatives.append(re.escape(term))
        if t.get("aliases"):
            try:
                aliases = json.loads(t["aliases"])
            except Exception:
                aliases = []
            for alias in aliases:
                canonical[alias.lower()] = term
                alternatives.append(re.escape(alias))

    if not alternatives:
        return None, {}
    # \b は使わない: Python の Unicode 対応 \b は日本語も「単語文字」とみなすため、
    # 「の」+ ASCII 固有名詞のように日本語に囲まれた表記（decisions/action_items の
    # 実文でよくある形）で境界が成立しない。ASCII 英数字のみを境界判定対象にする。
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(" + "|".join(alternatives) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return pattern, canonical


def extract_from_pm_db(db_path: Path, no_encrypt: bool = False) -> list[tuple[str, str, list[str]]]:
    """pm.db の decisions・action_items テキストから固有名詞候補を抽出する。"""
    from db_utils import open_db
    if not db_path.exists():
        return []
    try:
        conn = open_db(db_path, encrypt=not no_encrypt)
    except Exception as e:
        print(f"[WARN] pm.db 接続エラー: {e}", file=sys.stderr)
        return []

    try:
        rows = conn.execute(
            "SELECT content FROM decisions WHERE deleted=0 UNION ALL "
            "SELECT content FROM action_items WHERE deleted=0"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    dynamic_pattern, dynamic_canonical = _load_dynamic_app_pattern(db_path)

    term_counts: dict[str, int] = {}
    for row in rows:
        text = row[0] or ""
        for m in _APP_PATTERN.finditer(text):
            canonical = _canonical_app(m.group(0))
            term_counts[canonical] = term_counts.get(canonical, 0) + 1
        if dynamic_pattern:
            for m in dynamic_pattern.finditer(text):
                canonical = dynamic_canonical.get(m.group(0).lower(), m.group(0))
                term_counts[canonical] = term_counts.get(canonical, 0) + 1

    results = []
    for term, count in sorted(term_counts.items(), key=lambda x: x[1], reverse=True):
        if count >= 2:
            results.append((term, "app", []))
    return results


def extract_from_goals(goals_path: Path) -> list[tuple[str, str, list[str]]]:
    """goals.yaml のマイルストーン名・目標名から用語を抽出する。

    goals_path は呼び出し元（main()）が --goals で明示指定したパスをそのまま渡す。
    ここで REPO_ROOT を使って固定パスを組み立てない: --db に架空パスを渡した
    --dry-run でも本関数だけが実 goals.yaml（機密ファイル）を読みに行ってしまう
    事故が過去に発生したため。
    """
    if not goals_path.exists():
        return []
    try:
        import yaml
        with open(goals_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"[INFO] goals.yaml 読み込みスキップ: {e}", file=sys.stderr)
        return []

    results: list[tuple[str, str, list[str]]] = []
    if not isinstance(data, dict):
        return results

    # goals.yaml のキー構造: goals[].name, goals[].milestones[].name
    for goal in data.get("goals", []):
        name = goal.get("name") or goal.get("title") or ""
        if name and len(name) > 3:
            results.append((name, "milestone", []))
        for ms in goal.get("milestones", []):
            ms_name = ms.get("name") or ms.get("title") or ""
            if ms_name and len(ms_name) > 3:
                results.append((ms_name, "milestone", []))
    return results


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #

def _cmd_list(db_path: Path, no_encrypt: bool) -> None:
    """terminology テーブルの現在の内容を一覧表示する。"""
    from utils.terminology import load_all_terms
    terms = load_all_terms(db_path=db_path)
    if not terms:
        print("(terminology テーブルは空です)")
        return
    print(f"{'用語':<40} {'カテゴリ':<12} {'頻度':>4}  別表記")
    print("-" * 80)
    for t in terms:
        aliases = ""
        if t.get("aliases"):
            try:
                a = json.loads(t["aliases"])
                if a:
                    aliases = ", ".join(a)
            except Exception:
                pass
        print(f"{t['term']:<40} {t['category'] or '':<12} {t.get('frequency') or 1:>4}  {aliases}")
    print(f"\n合計 {len(terms)} 語")


def _cmd_delete(terms_to_delete: list[str], db_path: Path, no_encrypt: bool) -> None:
    """指定した term を terminology テーブルから削除する。"""
    from utils.terminology import _open_pm
    conn = _open_pm(db_path)
    if conn is None:
        print("[ERROR] DB に接続できませんでした", file=sys.stderr)
        return
    deleted = 0
    try:
        for term in terms_to_delete:
            cur = conn.execute("DELETE FROM terminology WHERE term = ?", (term,))
            if cur.rowcount:
                print(f"[削除] {term}")
                deleted += 1
            else:
                print(f"[WARN] 見つかりません: {term}")
        conn.commit()
    finally:
        conn.close()
    print(f"[完了] {deleted}/{len(terms_to_delete)} 語を削除しました")


def main() -> None:
    parser = argparse.ArgumentParser(description="pm.db/terminology テーブルを更新する")
    parser.add_argument("--slide-terms", help="slide_ocr 出力の terminology.txt パス")
    parser.add_argument("--meeting-kind", help="会議種別タグ")
    parser.add_argument("--from-pm-db", action="store_true", default=True,
                        help="pm.db の decisions/actions から用語抽出（デフォルト: 有効）")
    parser.add_argument("--no-pm-db", action="store_true", help="pm.db からの抽出を無効化")
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "pm.db"), help="pm.db パス")
    parser.add_argument("--goals", default=str(REPO_ROOT / "goals.yaml"),
                        help="goals.yaml パス（--dry-run 等で実ファイルを読ませたくない場合に"
                             "架空パスを明示指定する）")
    parser.add_argument("--no-encrypt", action="store_true", help="平文モード")
    parser.add_argument("--dry-run", action="store_true", help="DB 更新せずに抽出結果を表示")
    parser.add_argument("--list", action="store_true", help="現在 DB に登録されている用語を一覧表示して終了")
    parser.add_argument("--delete-term", metavar="TERM", nargs="+", help="指定した用語を DB から削除して終了")
    args = parser.parse_args()

    db_path = Path(args.db)
    meeting_kind = args.meeting_kind

    if args.list:
        _cmd_list(db_path, args.no_encrypt)
        return

    if args.delete_term:
        _cmd_delete(args.delete_term, db_path, args.no_encrypt)
        return

    all_terms: list[tuple[str, str, list[str]]] = []

    # slide_ocr ソース
    if args.slide_terms:
        slide_terms = extract_from_slide_terms(args.slide_terms)
        print(f"[INFO] slide_terms から {len(slide_terms)} 語を抽出")
        all_terms.extend(slide_terms)

    # pm.db ソース
    use_pm_db = args.from_pm_db and not args.no_pm_db
    if use_pm_db:
        pm_terms = extract_from_pm_db(db_path, no_encrypt=args.no_encrypt)
        print(f"[INFO] pm.db から {len(pm_terms)} 語を抽出")
        all_terms.extend(pm_terms)

    # goals.yaml ソース
    goal_terms = extract_from_goals(Path(args.goals))
    if goal_terms:
        print(f"[INFO] goals.yaml から {len(goal_terms)} 語を抽出")
        all_terms.extend(goal_terms)

    # 重複除去（term ベース）
    seen: set[str] = set()
    deduped: list[tuple[str, str, list[str]]] = []
    for term, cat, aliases in all_terms:
        if term not in seen:
            seen.add(term)
            deduped.append((term, cat, aliases))

    print(f"[INFO] 合計 {len(deduped)} 語（重複除去後）")

    if args.dry_run:
        for term, cat, aliases in deduped:
            print(f"  [{cat}] {term}" + (f" ({aliases})" if aliases else ""))
        return

    success = 0
    for term, category, aliases in deduped:
        try:
            add_term(
                term=term,
                category=category,
                aliases=aliases if aliases else None,
                source="pm_terminology_update",
                meeting_kind=meeting_kind,
                db_path=db_path,
            )
            success += 1
        except Exception as e:
            print(f"[WARN] add_term 失敗 ({term}): {e}", file=sys.stderr)

    print(f"[完了] {success}/{len(deduped)} 語を terminology テーブルに upsert しました")


if __name__ == "__main__":
    main()
