#!/usr/bin/env python3
"""
pm_box_relevance.py

box_docs.db.box_files に対し、本文（doc_content.content_md）の冒頭を
ローカルLLMで読み取り、relevance (core/related/noise/unknown) を判定する
スクリーニングツール。判定結果は box_files.relevance に保存され、
pm_embed.py が relevance='noise' のファイルを索引から除外する。

relevance:
  core    — 富岳NEXTプロジェクトの本質的ナレッジ（設計資料・公式報告書・意思決定資料等）
  related — 関連するが本質ではない（補助資料・参考情報・過去事例等）
  noise   — プロジェクトと無関係 / 索引化するとノイズになる（雑談添付・個人メモ等）
  unknown — 判定不能（情報不足）

**誤りの非対称性**: core/related の誤りは検索すればいずれ見つかるが、noise 判定は
索引から落とすため二度と検索結果に出てこない。「出なかったこと」には気づけないので、
noise の誤判定だけが**永久に不可視の欠落**を作る。再審査の対象を noise に絞るのは
このため（--recheck-noise）。

Usage:
  # 本文ベースでLLM判定（未判定のみ）
  python3 scripts/pm_box_relevance.py --judge

  # 全件再判定 / 特定 index_name のみ
  python3 scripts/pm_box_relevance.py --judge --force
  python3 scripts/pm_box_relevance.py --judge --index-name pm

  # 既存の noise 判定を読み手に再審査させる（relevance は上書きせず記録のみ）
  python3 scripts/pm_box_relevance.py --recheck-noise --reader k3 --limit 50
  python3 scripts/pm_box_relevance.py --recheck-noise --reader k3 --dry-run

  # CSVにエクスポート（精査用、noise を先頭に）
  python3 scripts/pm_box_relevance.py --export --output screen.csv

  # 精査後のCSVをDBに反映（この経路の更新は relevance_source='human' になる）
  python3 scripts/pm_box_relevance.py --import screen.csv

  # relevance分布を集計
  python3 scripts/pm_box_relevance.py --stats

**人手の修正の保護**: `--import` で入った判定は `relevance_source='human'` を立て、
`--judge --force` の再判定対象から外す（消すには `--force-human` の明示が要る）。
人間の最終判断を LLM が黙って上書きしないための列で、議事録側の human_kept
（scripts/ingest/minutes.py）と同じ考え方。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli_utils import add_no_encrypt_arg, call_argus_llm
from db_utils import open_db, open_pm_db

# flag_sensitive_terms / _load_second_opinion_config は scripts/ingest/slack.py 側の実装を
# そのまま使う（重複させない）。ingest → data-pipeline 方向の依存は無いため循環importにはならない。
from ingest.slack import (
    SecondOpinionOnHold,
    _load_second_opinion_config,
    flag_sensitive_terms,
)

# second_opinion_hold / _hold_message は**関数内で import する**。モジュール先頭で
# 取り込むと `pbr.second_opinion_hold` が `ingest.slack.second_opinion_hold` とは別の
# 名前になり、テストが `ingest.slack` 側を差し替えてもこちらに効かなくなる
# （同じ罠を test_cap_stops_further_calls_and_warns のコメントが記録している）。
# 保留の宣言元は ingest/slack.py ただ1つ、という不変条件を名前解決でも保つ。

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
BOX_DOCS_DB = DATA_DIR / "box_docs.db"
PM_DB = DATA_DIR / "pm.db"

VALID_RELEVANCE = {"core", "related", "noise", "unknown"}
BATCH_SIZE = 5  # 本文を渡すので少なめ
CONTENT_PREVIEW_CHARS = 2500

JUDGE_PROMPT = """あなたは「富岳NEXT」プロジェクト（次世代スーパーコンピュータ開発）の
ナレッジマネジメント担当です。Box に格納されたドキュメントの本文冒頭を見て、
RAG検索インデックスに残すべきか判定してください。

# プロジェクト文脈
富岳NEXTは理研・富士通・NVIDIA連携による次世代AI-HPCシステム。アプリケーション開発エリア
（HPCアプリケーションWG・ベンチマークWG）のプロジェクトマネジメントを支援している。
本質的ナレッジ = 設計方針・技術仕様・意思決定・議事録・公式報告書・開発成果・ベンチマーク結果等。

# 判定カテゴリ
- core    : プロジェクトの本質的ナレッジ。設計資料・公式報告書・意思決定資料・議事録・技術仕様
- related : 関連するが本質ではない。参考資料・過去事例・外部文献・補助資料
- noise   : 索引化すべきでない。雑談添付・個人メモ・関係ない資料・壊れたファイル・情報不足で意味不明
- unknown : 本文が空・抽出失敗・短すぎて判定不能

# 出力形式
各ドキュメントに対し次の JSON 配列を出力（順序は入力と同じ）:
[
  {{"box_file_id": "<id>", "relevance": "core|related|noise|unknown", "reason": "<1行の根拠>"}},
  ...
]

# 入力ドキュメント
{documents}

JSON配列のみ出力。コードブロック記法不要。"""


def ensure_relevance_source_column(conn) -> None:
    """box_files.relevance_source を後付けする（'human' / 'llm' / NULL=由来不明）。

    既存行は NULL のまま残す。**NULL を 'llm' とみなさない** — 実際には CSV 経由で
    人間が直した行が混じっている可能性があり、遡って区別する手段が無いため。
    「分からない」を「LLM が付けた」に丸めると、保護すべき行を保護しないまま
    「保護済み」と表示してしまう。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(box_files)")}
    if "relevance_source" not in cols:
        conn.execute("ALTER TABLE box_files ADD COLUMN relevance_source TEXT")
        conn.commit()


def format_doc_for_prompt(row) -> str:
    name = row["name"] or "(名前なし)"
    folder = row["folder_path"] or ""
    content = (row["content_md"] or "").strip()[:CONTENT_PREVIEW_CHARS]
    parts = [f"=== box_file_id={row['box_file_id']} ==="]
    parts.append(f"path: {folder}/{name}" if folder else f"name: {name}")
    if row["file_format"]:
        parts.append(f"format: {row['file_format']}")
    if content:
        parts.append(f"本文(冒頭{CONTENT_PREVIEW_CHARS}字):\n{content}")
    else:
        parts.append("(本文なし)")
    return "\n".join(parts)


def judge_batch(rows: list, logger) -> dict[str, tuple[str, str]]:
    """Returns {box_file_id: (relevance, reason)}."""
    if not rows:
        return {}
    doc_lines = "\n\n".join(format_doc_for_prompt(r) for r in rows)
    prompt = JUDGE_PROMPT.format(documents=doc_lines)
    try:
        result = call_argus_llm(prompt, max_tokens=2048, timeout=300)
    except Exception as e:
        logger.error(f"LLMエラー: {e}")
        return {}

    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```\w*\n?", "", result)
        result = re.sub(r"\n?```$", "", result)

    parsed = None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", result, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(parsed, list):
        logger.error(f"JSONパース失敗: {result[:200]}")
        return {}

    out: dict[str, tuple[str, str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("box_file_id") or "").strip()
        if not fid:
            continue
        rel = str(item.get("relevance", "unknown")).lower().strip()
        if rel not in VALID_RELEVANCE:
            rel = "unknown"
        reason = str(item.get("reason", ""))[:300]
        out[fid] = (rel, reason)
    return out


# --------------------------------------------------------------------------- #
# 第2系統（独立系統）による差分検査 — R8 / Phase 4
# --------------------------------------------------------------------------- #
#
# noise 判定のうち、フラグ語が立ったものだけを対象にする。
# **索引から落とす判定（noise）だけが欠落を作る** — core/related の誤りは検索すれば
# いずれ見つかるが、noise と判定されたドキュメントは二度と検索結果に出てこない。
# そのため第2系統も noise 判定の再審査に絞り、主系統が noise で第2系統が
# core/related と判定したときだけ不一致として記録する（自動では relevance を上書きしない）。


# --reader オプション: 「読み手」を切り替える（scripts/quality/pm_screen.py と同じ設計）。
#
#   second（既定） — R8 対策の第2系統。**出自が主系統と独立していること**が本質で、
#     能力ではなく供給元の分散が目的。現在は保留中（config/sensitive_terms.yaml の
#     second_opinion.on_hold）。
#   k3            — kimi-k3 を read-only の recall チェック役として使う。
#     **これは R8 対策ではない** — K3（Moonshot）は主系統の glm/DeepSeek/Qwen と
#     同じ「主要モデルが一系統に寄っている」構図の外には出られない。狙いは
#     出自の分散ではなく、K3 の読解・網羅（recall）の高さを
#     「誤って noise に落とされた文書の発掘」に使うことだけ。
#
# kind を分けるのは、後から「R8 対策として何件検査したか」と
# 「品質検査として何件検査したか」を混ぜないため。
_READER_KIND = {
    "second": "box_relevance",
    "k3": "box_relevance_recall",
}


def _resolve_readers(reader: str) -> list[str]:
    """--reader の値（second/k3/both）から実行する読み手のリストを返す。"""
    if reader == "both":
        return ["second", "k3"]
    return [reader]


def second_opinion_box_verdict(
    doc_text: str, *, model: str | None = None, route: str = "second",
) -> tuple[str, str]:
    """読み手にドキュメント1件の relevance を単独で判定させる。

    既存 JUDGE_PROMPT は複数件バッチ・主モデル向けに調整されたプロンプトのため
    流用しない（scripts/ingest/slack.py の second_opinion_extraction と同じ理由:
    別モデルにそのまま当てると差がプロンプト適合度の差になってしまう）。

    route: "second"（R8 対策の第2系統、call_rivault）/ "k3"（recall チェック、
    call_local_llm）。上の _READER_KIND のコメント参照。
    """
    cfg_root = _load_second_opinion_config()
    prompt = (
        "次のドキュメントが、富岳NEXTプロジェクトの検索インデックスに残すべきかを判定してください。\n"
        "判定は core（本質的ナレッジ）/ related（関連あり）/ noise（無関係、索引化不要）/ "
        "unknown（判定不能）のいずれか1語だけを1行目に出力し、\n"
        "2行目に理由を1文で書いてください。\n\n"
        f"{doc_text}\n"
    )

    if route == "k3":
        model = model or (cfg_root.get("quality_reader") or {}).get("model")
        from utils.llm import _token_for_base, call_local_llm, load_llm_secrets

        # call_argus_llm と違い call_local_llm は secrets を自分で読まない。
        # ここで読まないと、シェルで source していない実行（cron・Console 経由）で
        # 全行が「LOCAL_LLM_URL 未設定」で落ちる。
        load_llm_secrets()
        base_url = os.environ.get("LOCAL_LLM_URL")
        if not base_url:
            raise RuntimeError("LOCAL_LLM_URL 未設定（~/.secrets/localLLM.sh を確認）")
        # **K3 は thinking を無効化できない**（think=False を渡しても効かない）。
        # thinking だけで数千トークン消費するため、第2系統向けの max_tokens=256 を
        # そのまま流用すると**本文に到達する前に打ち切られ、verdict が全部 unknown に
        # 落ちる**（しかも unknown は正常値に見えるので壊れたことに気づけない）。
        # 議事録の読み手で実測した 16384 / 600 秒に合わせる。
        # **あるモデル向けに調整した値を別のモデルへそのまま適用してはいけない。**
        raw = call_local_llm(
            prompt, model=model, base_url=base_url,
            api_key=_token_for_base(base_url),
            max_tokens=16384, timeout=600, think=False,
        )
    else:
        # 保留中の第2系統をここで止める（backstop。呼び出し側は second_opinion_hold()
        # を見て呼ぶ前に飛ばすこと）。**空の結果を返さず例外にする** —
        # 「0 件の不一致」と「検査していない」を混同させないため。
        from ingest.slack import _hold_message, second_opinion_hold

        hold = second_opinion_hold()
        if hold:
            raise SecondOpinionOnHold(_hold_message(hold))
        model = model or (cfg_root.get("second_opinion") or {}).get("model")
        from utils.llm import call_rivault

        raw = call_rivault(prompt, model=model, max_tokens=256, timeout=120)

    return _parse_box_verdict(raw), (raw or "")[:500]


def _parse_box_verdict(raw: str | None) -> str:
    """読み手の応答から core/related/noise/unknown を取り出す。

    先頭行だけを見ると、K3 のように前置きを書くモデルで取りこぼす。**判定語を含む
    最初の行**を探し、どの行にも無ければ unknown を返す（判定不能を noise に
    寄せない — 索引から落とす方向の誤りは不可視の欠落を作るため）。
    """
    for line in (raw or "").strip().splitlines():
        low = line.lower()
        for cand in ("core", "related", "noise", "unknown"):
            if cand in low:
                return cand
    return "unknown"


def _open_pm_db_for_second_opinion():
    """記録先の pm.db 接続を開く。存在しなければ None（第2系統の記録をスキップ）。

    pm.db は常に暗号化前提で開く。呼び出し元の `--no-encrypt` は box_docs.db を
    平文で扱うための CLI フラグであり、そのまま pm.db に流用すると、暗号化済みの
    pm.db を平文接続で開いた壊れた接続を掴んでしまい、第2系統の記録が全部
    落ちる（box_docs.db だけを平文で扱う運用は想定されるが、pm.db は本番では
    常に暗号化されている前提のため区別する）。
    """
    if not PM_DB.exists():
        return None
    return open_pm_db(PM_DB)


def apply_second_opinion_box_relevance(
    batch: list, verdicts: dict[str, tuple[str, str]], *,
    conn_pm=None, log=None, state: dict | None = None, reader: str = "second",
) -> list[dict]:
    """判定済みバッチのうち、主系統が noise と判定した行に読み手を当てる。

    box_files.relevance は一切上書きしない。主系統 noise × 読み手 core/related の
    ときだけ不一致として `triage_second_opinion`（pm.db）に記録する。

    **この経路が見るのは「今まさに判定した行」だけ**である。既に noise で確定済みの
    行は --judge を何度回してもここを通らない。既存の山を掘り返すのは
    `--recheck-noise`（cmd_recheck_noise）の役目。

    戻り値: 不一致だった項目一覧（テスト・ログ確認用）。
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if os.environ.get("ARGUS_SECOND_OPINION", "1").strip() not in ("1", "true", "yes"):
        return []
    readers = _resolve_readers(reader)
    # 第2系統が保留中なら LLM を呼ばない（ingest/slack.py の second_opinion_hold が
    # 単一の宣言元）。**黙って飛ばさない** — 記録が無いことを「不一致なし」と
    # 読まれないよう、必ず WARN を出す。
    from ingest.slack import _hold_message, second_opinion_hold

    hold = second_opinion_hold()
    if hold and "second" in readers:
        log(f"[WARN] {_hold_message(hold)} Box relevance の差分検査（reader=second）は行いません")
        readers = [r for r in readers if r != "second"]
        if not readers:
            return []

    cfg = (_load_second_opinion_config().get("second_opinion") or {})
    cap = int(cfg.get("max_flagged_per_run") or 30)
    state = state if state is not None else {}
    model = cfg.get("model") or ""

    disagreements: list[dict] = []
    for row in batch:
        fid = row["box_file_id"]
        v = verdicts.get(fid)
        if not v or v[0] != "noise":
            continue

        doc_text = format_doc_for_prompt(row)
        terms = flag_sensitive_terms(doc_text)
        if not terms:
            continue

        count = state.get("count", 0)
        if count >= cap:
            if not state.get("cap_warned"):
                log(f"[WARN] 第2系統(box relevance) の実行回数が上限 {cap} 件に達しました。"
                    "これ以降の noise 判定には第2系統を当てません"
                    "（黙って打ち切ると「全部見た」と誤読されるため明示します）")
                state["cap_warned"] = True
            continue
        state["count"] = count + 1

        for r in readers:
            reader_model = _reader_model(r, cfg_root=_load_second_opinion_config(), default=model)
            try:
                second, raw = second_opinion_box_verdict(doc_text, route=r)
            except Exception as e:
                log(f"[WARN] 読み手({r}, box relevance) の呼び出しに失敗（この行はスキップ）: {e}")
                continue

            if second not in ("core", "related"):
                continue

            content = f"{row['name'] or ''} (box_file_id={fid})"
            disagreements.append(
                {"box_file_id": fid, "primary": "noise", "second": second, "reader": r}
            )
            if conn_pm is not None:
                try:
                    from db_utils import record_second_opinion
                    record_second_opinion(
                        conn_pm, kind=_READER_KIND[r], content=content,
                        primary_verdict="noise", second_verdict=second,
                        flagged_terms=terms, model=reader_model, raw=raw,
                    )
                except Exception as e:
                    log(f"[WARN] 読み手({r}, box relevance) の結果を記録できませんでした"
                        f"（判定は継続）: {e}")

    if disagreements:
        log(f"[SECOND-OPINION] Box relevance: noise 判定のうち読み手との不一致 "
            f"{len(disagreements)} 件を検出。**relevance は上書きしない** — "
            "pm.db の triage_second_opinion を確認してください")
    return disagreements


def _reader_model(reader: str, *, cfg_root: dict, default: str = "") -> str:
    """読み手ごとのモデル名（記録用）。実際の解決は second_opinion_box_verdict 側。"""
    if reader == "k3":
        return (cfg_root.get("quality_reader") or {}).get("model") or ""
    return (cfg_root.get("second_opinion") or {}).get("model") or default


_FID_IN_CONTENT_RE = re.compile(r"box_file_id=(\d+)")


def already_rechecked_fids(conn_pm, kind: str) -> set[str]:
    """既に同じ kind で再審査済みの box_file_id 集合。

    実行のたびに同じ先頭 N 件を舐め直すと、山の奥（=まだ誰も見ていない noise）に
    永久に到達しない。記録済みを飛ばして**続きから**進めるための下敷き。
    """
    if conn_pm is None:
        return set()
    from db_utils import table_exists

    if not table_exists(conn_pm, "triage_second_opinion"):
        return set()
    out: set[str] = set()
    for row in conn_pm.execute(
        "SELECT content_head FROM triage_second_opinion WHERE kind=?", (kind,)
    ):
        m = _FID_IN_CONTENT_RE.search(row[0] or "")
        if m:
            out.add(m.group(1))
    return out


def cmd_recheck_noise(args, logger) -> None:
    """既存の noise 判定を読み手に再審査させ、記録だけ残す（relevance は上書きしない）。

    --judge に同梱した差分検査は「今まさに判定した行」しか見ないため、既に noise で
    確定している山（本番では 1,600 件超）は何度 --judge を回しても再審査されない。
    **索引から落とされた文書は検索に出てこないので、掘り返す入口が無ければ
    誤判定は永久に不可視のまま**になる。ここがその入口。

    relevance を書き換えないのは、読み手の判定を人手のレビュー無しで正とみなさない
    ため（記録先は pm.db の triage_second_opinion。Console の「所見」タブで読める）。
    """
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return

    from ingest.slack import _hold_message, second_opinion_hold

    readers = _resolve_readers(args.reader)
    hold = second_opinion_hold()
    if hold and "second" in readers:
        print(f"[WARN] {_hold_message(hold)}")
        print("[WARN] --reader の second（R8 対策の第2系統）を外して実行します"
              "（記録されないのは『欠落が無い』からではなく『検査していない』からです）")
        readers = [r for r in readers if r != "second"]
        if not readers:
            print("[ERROR] 実行できる読み手がありません"
                  "（--reader k3 を指定すれば K3 の recall チェックだけを回せます）")
            return
    if "k3" in readers:
        print("[INFO] --reader k3: kimi-k3 は読み手（recall 確認）専用です。"
              "**R8 の第2系統ではありません** — 出自は主系統と同じ側にあります")

    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    conn_pm = None
    try:
        where = ["bf.relevance = 'noise'", "dc.content_md IS NOT NULL"]
        if args.index_name:
            where.append(f"bf.index_name LIKE '%\"{args.index_name}\"%'")
        rows = conn.execute(
            "SELECT bf.box_file_id, bf.name, bf.folder_path, bf.file_format,"
            " dc.content_md"
            " FROM box_files bf JOIN doc_content dc"
            " ON bf.box_file_id = dc.box_file_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY bf.box_file_id"
        ).fetchall()
        if not rows:
            print("再審査対象の noise 判定はありません")
            return

        conn_pm = None if args.dry_run else _open_pm_db_for_second_opinion()
        # dry-run では pm.db を開かないので「済み」を引けない。件数の見積もりが
        # 実際より多く出ることを明示する（少なく見せて安心させない）。
        checked: dict[str, set[str]] = {
            r: already_rechecked_fids(conn_pm, _READER_KIND[r]) for r in readers
        }

        targets: list[tuple] = []
        for row in rows:
            if any(row["box_file_id"] not in checked[r] for r in readers):
                targets.append(row)
            if len(targets) >= args.limit:
                break

        n_skipped = sum(len(checked[r]) for r in readers)
        print(f"noise 判定 {len(rows)} 件中、未審査の先頭 {len(targets)} 件を対象"
              f"（--limit {args.limit}、記録済みスキップ 延べ {n_skipped} 件、reader={args.reader}）")
        if len(rows) > len(targets) + n_skipped:
            print(f"[INFO] 残り {len(rows) - len(targets) - n_skipped} 件は今回の対象外です。"
                  "同じコマンドを繰り返すと続きから進みます"
                  "（黙って打ち切ると「全部見た」と誤読されるため明示します）")
        print(f"推定LLM呼び出し回数: {len(targets)} 件 × 読み手{len(readers)}種 "
              f"= 最大 {len(targets) * len(readers)} 回")

        if args.dry_run:
            print("[INFO] --dry-run のためLLM呼び出し・DB書き込みは行いません"
                  "（pm.db を開かないため、記録済みのスキップは反映されていません）")
            for row in targets[:20]:
                print(f"    {row['box_file_id']} {(row['name'] or '')[:60]}")
            return

        from db_utils import record_second_opinion

        cfg_root = _load_second_opinion_config()
        n_called = 0
        n_failed = 0
        n_disagree = 0
        per_reader: dict[str, dict[str, int]] = {r: {} for r in readers}

        for i, row in enumerate(targets, 1):
            fid = row["box_file_id"]
            doc_text = format_doc_for_prompt(row)
            # 第2系統（R8）はフラグ語で対象を絞るが、recall チェックは絞らない。
            # 狙いが違う — R8 は「特定の話題が狙って落とされていないか」、こちらは
            # 「読み落としで落ちていないか」。後者に話題の絞り込みを持ち込むと、
            # フラグ語を含まない大多数が検査対象から外れてしまう。
            terms = flag_sensitive_terms(doc_text)
            content = f"{row['name'] or ''} (box_file_id={fid})"

            for r in readers:
                if fid in checked[r]:
                    continue
                try:
                    verdict, raw = second_opinion_box_verdict(doc_text, route=r)
                    n_called += 1
                except Exception as e:
                    n_failed += 1
                    print(f"  [WARN] {fid}: 読み手({r}) の呼び出しに失敗（この行はスキップ）: {e}")
                    continue

                per_reader[r][verdict] = per_reader[r].get(verdict, 0) + 1
                if verdict in ("core", "related"):
                    n_disagree += 1
                    print(f"  [不一致] {fid} noise → {verdict} ({r}): {(row['name'] or '')[:50]}")

                # **一致・不一致の両方を記録する** — 不一致だけ残すと「何件中の
                # 不一致か」が分からず、率として読めない。
                try:
                    record_second_opinion(
                        conn_pm, kind=_READER_KIND[r], content=content,
                        primary_verdict="noise", second_verdict=verdict,
                        flagged_terms=terms,
                        model=_reader_model(r, cfg_root=cfg_root), raw=raw,
                    )
                    conn_pm.commit()
                except Exception as e:
                    print(f"  [WARN] {fid}: 読み手({r}) の結果を記録できませんでした: {e}")

            if i % 10 == 0:
                print(f"  [{i}/{len(targets)}] 進行中（不一致 {n_disagree} 件）")

        print(f"\n完了: LLM呼び出し {n_called} 回（失敗 {n_failed} 回）、"
              f"noise を覆した判定 {n_disagree} 件")
        for r in readers:
            dist = ", ".join(f"{k}={v}" for k, v in sorted(per_reader[r].items()))
            print(f"  reader={r}: {dist or '(記録なし)'}")
        if n_called:
            print(f"  noise 誤判定率（読み手基準）: {n_disagree}/{n_called} "
                  f"= {n_disagree / n_called:.1%}")
        print("**box_files.relevance は上書きしていません** — "
              "pm.db の triage_second_opinion / Console の「所見」タブで確認し、"
              "戻す場合は --export → 編集 → --import を使ってください")
    finally:
        if conn_pm is not None:
            conn_pm.close()
        conn.close()


def cmd_judge(args, logger) -> None:
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return

    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    conn_pm = None
    try:
        ensure_relevance_source_column(conn)
        where = ["dc.content_md IS NOT NULL"]
        rejudge = getattr(args, "rejudge_relevance", None)
        if rejudge:
            # 特定の判定値の行だけを再判定する。実行単位の事故（1回の実行で
            # 全件が同じ値になる）から復旧するための入口で、健全な判定を
            # 巻き込まずに済ませるためにある。--force とは独立に効く。
            where.append("bf.relevance = ?")
        elif not args.force:
            where.append("(bf.relevance IS NULL OR bf.relevance = '')")
        # 人手で直した行は --force でも再判定しない。**人間の最終判断を LLM が
        # 黙って上書きしない**（議事録側の human_kept と同じ原則）。消すには
        # --force-human の明示が要る。
        if not args.force_human:
            where.append("COALESCE(bf.relevance_source,'') != 'human'")
        if args.index_name:
            where.append(f"bf.index_name LIKE '%\"{args.index_name}\"%'")
        where_sql = " AND ".join(where)

        if args.force_human:
            print("[WARN] --force-human: 人手で修正した relevance も LLM の判定で"
                  "上書きします（人間の最終判断が消えます）")
        else:
            n_protected = conn.execute(
                "SELECT COUNT(*) FROM box_files WHERE relevance_source='human'"
            ).fetchone()[0]
            if n_protected:
                print(f"[INFO] 人手修正 {n_protected} 件は再判定対象から除外します"
                      "（上書きするには --force-human）")

        rows = conn.execute(
            f"SELECT bf.box_file_id, bf.name, bf.folder_path, bf.file_format,"
            f" dc.content_md"
            f" FROM box_files bf JOIN doc_content dc"
            f" ON bf.box_file_id = dc.box_file_id"
            f" WHERE {where_sql}"
            f" ORDER BY bf.box_file_id",
            ([rejudge] if rejudge else []),
        ).fetchall()

        if not rows:
            print("判定対象なし")
            return

        if rejudge:
            print(f"[INFO] --rejudge-relevance {rejudge}: 現在 {rejudge} の行のみ再判定します"
                  "（他の判定値は触りません）")
        print(f"判定対象: {len(rows)} 件")
        now = datetime.now().isoformat()
        total_updated = 0
        processed = 0

        # 第2系統（R8 / Phase 4）: dry-run では box_files.relevance も pm.db への記録も
        # 一切書き込まないため、pm.db 接続自体を開かない。
        conn_pm = None if args.dry_run else _open_pm_db_for_second_opinion()
        second_opinion_state: dict = {}

        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start : start + BATCH_SIZE]
            verdicts = judge_batch(batch, logger)
            processed += len(batch)
            print(f"  [{processed}/{len(rows)}] バッチ処理 (判定: {len(verdicts)}/{len(batch)})")

            if args.dry_run:
                for r in batch:
                    v = verdicts.get(r["box_file_id"])
                    if v:
                        print(f"    {r['box_file_id']} {v[0]:7s} {(r['name'] or '')[:50]} — {v[1][:60]}")
                continue

            for fid, (rel, reason) in verdicts.items():
                conn.execute(
                    "UPDATE box_files SET relevance=?, relevance_reason=?,"
                    " relevance_judged_at=?, relevance_source='llm' WHERE box_file_id=?",
                    (rel, reason, now, fid),
                )
                total_updated += 1
            conn.commit()

            try:
                apply_second_opinion_box_relevance(
                    batch, verdicts, conn_pm=conn_pm, log=print,
                    state=second_opinion_state, reader=args.reader,
                )
            except Exception as e:
                # apply_second_opinion_box_relevance() 内で保護されているのは LLM 呼び出しと
                # 記録だけで、設定読み込み・format_doc_for_prompt・flag_sensitive_terms は
                # 素通りする。ここで受けずに伝播させるとバッチループごと落ち、残りバッチの
                # relevance 判定（主系統）まで失われるため、ここで受けて処理を続行する。
                print(
                    "[WARN] 第2系統(box relevance) の適用に失敗"
                    f"（主系統の判定結果は影響を受けません）: {e}"
                )

        print(f"\n完了: {total_updated} 件更新" + (" (dry-run)" if args.dry_run else ""))
    finally:
        if conn_pm is not None:
            conn_pm.close()
        conn.close()


def cmd_export(args, logger) -> None:
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return
    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    ensure_relevance_source_column(conn)

    where = ""
    params: list = []
    if args.index_name:
        where = "WHERE index_name LIKE ?"
        params = [f'%"{args.index_name}"%']
    rows = conn.execute(
        f"SELECT box_file_id, name, folder_path, file_format, modified_at,"
        f" index_name, source_name, relevance, relevance_reason, relevance_source"
        f" FROM box_files {where} ORDER BY relevance, name", params
    ).fetchall()
    conn.close()

    out_rows = []
    for r in rows:
        out_rows.append({
            "box_file_id": r["box_file_id"],
            "relevance": r["relevance"] or "",
            "final_relevance": r["relevance"] or "",
            "relevance_reason": r["relevance_reason"] or "",
            "relevance_source": r["relevance_source"] or "",
            "name": r["name"] or "",
            "folder_path": r["folder_path"] or "",
            "file_format": r["file_format"] or "",
            "modified_at": r["modified_at"] or "",
            "index_name": r["index_name"] or "",
            "source_name": r["source_name"] or "",
        })

    order = {"noise": 0, "unknown": 1, "": 2, "related": 3, "core": 4}
    out_rows.sort(key=lambda x: (order.get(x["relevance"], 9), x["name"]))

    fields = ["box_file_id", "relevance", "final_relevance", "relevance_reason",
              "relevance_source", "name", "folder_path", "file_format", "modified_at",
              "index_name", "source_name"]
    out_path = Path(args.output)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)
    print(f"書き出し完了: {out_path} ({len(out_rows)} 行)")


_INVALID_FID_RE = re.compile(r"[^0-9]")


def _looks_like_box_file_id(s: str) -> bool:
    """純粋に数字のみで構成された box_file_id か（Excel指数表記を弾く）。"""
    return bool(s) and _INVALID_FID_RE.search(s) is None


def cmd_import(args, logger) -> None:
    in_path = Path(args.import_csv)
    if not in_path.exists():
        print(f"ファイルなし: {in_path}")
        sys.exit(1)

    rows: list[dict] = []
    with open(in_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            final = (row.get("final_relevance") or "").strip().lower()
            if final not in VALID_RELEVANCE:
                continue
            rows.append({
                "box_file_id": (row.get("box_file_id") or "").strip(),
                "name": (row.get("name") or "").strip(),
                "folder_path": (row.get("folder_path") or "").strip(),
                "final": final,
            })

    if not rows:
        print("有効な更新行なし")
        return

    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    ensure_relevance_source_column(conn)
    now = datetime.now().isoformat()
    changed = 0
    skipped = 0
    fid_lookup_failed = 0

    for row in rows:
        fid = row["box_file_id"]
        existing = None
        if _looks_like_box_file_id(fid):
            existing = conn.execute(
                "SELECT box_file_id, relevance FROM box_files WHERE box_file_id=?", (fid,)
            ).fetchone()

        # box_file_id でマッチしないとき (folder_path, name) で逆引き
        if existing is None and row["name"]:
            cands = conn.execute(
                "SELECT box_file_id, relevance FROM box_files"
                " WHERE name=? AND COALESCE(folder_path,'')=?",
                (row["name"], row["folder_path"]),
            ).fetchall()
            if len(cands) == 1:
                existing = cands[0]
            elif len(cands) > 1:
                logger.warning(
                    f"name+folder_path で複数候補 ({len(cands)} 件): {row['folder_path']}/{row['name']}"
                )
                fid_lookup_failed += 1
                continue

        if existing is None:
            fid_lookup_failed += 1
            continue

        if existing["relevance"] == row["final"]:
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [DRY] {existing['box_file_id']} {existing['relevance']} → {row['final']}")
        else:
            # この経路の更新は人手の判断。relevance_source='human' を立てて
            # --judge --force の再判定対象から外す（--force-human でのみ覆せる）。
            conn.execute(
                "UPDATE box_files SET relevance=?, relevance_judged_at=?,"
                " relevance_source='human' WHERE box_file_id=?",
                (row["final"], now, existing["box_file_id"]),
            )
        changed += 1

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"完了: {changed} 件更新"
          f"（変化なし {skipped}, 行特定不能 {fid_lookup_failed}）"
          + (" (dry-run)" if args.dry_run else ""))


def cmd_stats(args, logger) -> None:
    if not BOX_DOCS_DB.exists():
        print(f"box_docs.db が存在しません: {BOX_DOCS_DB}")
        return
    conn = open_db(BOX_DOCS_DB, encrypt=not args.no_encrypt)
    ensure_relevance_source_column(conn)
    counts = {"core": 0, "related": 0, "noise": 0, "unknown": 0, None: 0}
    for r in conn.execute("SELECT relevance, COUNT(*) FROM box_files GROUP BY relevance"):
        counts[r[0]] = r[1]
    total = sum(counts.values())
    src = {r[0]: r[1] for r in conn.execute(
        "SELECT COALESCE(relevance_source,'(由来不明)'), COUNT(*)"
        " FROM box_files GROUP BY 1"
    )}
    conn.close()
    print(f"core    : {counts['core']:>6d}")
    print(f"related : {counts['related']:>6d}")
    print(f"noise   : {counts['noise']:>6d}")
    print(f"unknown : {counts['unknown']:>6d}")
    print(f"未判定  : {counts[None]:>6d}")
    print(f"合計    : {total:>6d}")
    print("\n判定の由来:")
    for k in sorted(src):
        print(f"  {k:<10s}: {src[k]:>6d}")
    if src.get("(由来不明)"):
        print("  ※ 由来不明 = relevance_source 列の追加より前に判定された行。"
              "人手修正が混じっていても区別できないため保護対象にならない")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="box_docs.db のドキュメントを本文ベースで relevance 判定・精査"
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--judge", action="store_true", help="LLMで relevance を判定")
    g.add_argument("--recheck-noise", action="store_true",
                   help="既存の noise 判定を読み手に再審査させる（relevance は上書きしない）")
    g.add_argument("--export", action="store_true", help="CSV にエクスポート")
    g.add_argument("--import", dest="import_csv", metavar="PATH", help="CSV をインポート")
    g.add_argument("--stats", action="store_true", help="relevance 分布を集計")

    parser.add_argument("--index-name", default=None, help="特定インデックスのみ")
    parser.add_argument("--force", action="store_true", help="判定済みも再判定（--judge）")
    parser.add_argument("--rejudge-relevance", choices=list(VALID_RELEVANCE), default=None,
                        help="現在この判定値の行だけを再判定する（--judge）。"
                             "実行単位の事故からの復旧用")
    parser.add_argument("--force-human", action="store_true",
                        help="人手修正（relevance_source='human'）も再判定対象にする（--judge）")
    parser.add_argument("--reader", choices=["second", "k3", "both"], default="second",
                        help="読み手。second=R8対策の第2系統（既定・現在保留中）、"
                             "k3=kimi-k3 による recall チェック（R8 対策ではない）")
    parser.add_argument("--limit", type=int, default=50,
                        help="--recheck-noise の1回あたり対象件数（既定 50）")
    parser.add_argument("--output", default="docs_screen.csv", help="--export の出力先")
    parser.add_argument("--dry-run", action="store_true", help="DB更新なし")
    add_no_encrypt_arg(parser)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("pm_box_relevance")

    if args.judge:
        cmd_judge(args, logger)
    elif args.recheck_noise:
        cmd_recheck_noise(args, logger)
    elif args.export:
        cmd_export(args, logger)
    elif args.import_csv:
        cmd_import(args, logger)
    elif args.stats:
        cmd_stats(args, logger)


if __name__ == "__main__":
    main()
