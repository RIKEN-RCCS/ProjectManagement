#!/usr/bin/env python3
"""
cli_utils.py

PM支援スクリプト共通の CLI ユーティリティ。
argparse ヘルパー関数・make_logger() を提供する。
"""

import argparse
import os
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# argparse ヘルパー
# --------------------------------------------------------------------------- #

def add_output_arg(parser: argparse.ArgumentParser) -> None:
    """--output PATH を parser に追加する"""
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="出力をファイルにも保存")


def add_no_encrypt_arg(parser: argparse.ArgumentParser) -> None:
    """--no-encrypt を parser に追加する"""
    parser.add_argument("--no-encrypt", action="store_true",
                        help="DBを暗号化しない（平文モード）")


def add_dry_run_arg(parser: argparse.ArgumentParser) -> None:
    """--dry-run を parser に追加する"""
    parser.add_argument("--dry-run", action="store_true",
                        help="DB保存なし・結果を標準出力のみ")


def add_since_arg(parser: argparse.ArgumentParser, help_suffix: str = "") -> None:
    """--since YYYY-MM-DD を parser に追加する"""
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help=f"この日付以降のデータのみ対象{help_suffix}")


def add_db_arg(parser: argparse.ArgumentParser, default: str = "data/pm.db") -> None:
    """--db PATH を parser に追加する"""
    parser.add_argument("--db", default=None, metavar="PATH",
                        help=f"pm.db のパス（デフォルト: {default}）")


def add_filter_arg(parser: argparse.ArgumentParser) -> None:
    """--filter PRESET を parser に追加する（複数指定可）"""
    parser.add_argument(
        "--filter", action="append", default=None, metavar="PRESET",
        help="argus_config.yaml の filter_presets 名でチャンネル・議事録を絞り込む（複数指定可）",
    )


def resolve_filter_presets(
    filter_names: list[str] | None,
    config_path: Path | str = "data/argus_config.yaml",
) -> tuple[list[str], list[str]]:
    """filter_presets からチャンネルIDと議事録種別を解決する。

    Returns
    -------
    (channel_ids, meeting_kinds)
        filter_names が None/空の場合は ([], []) を返す（フィルタなし）。
    """
    if not filter_names:
        return [], []

    import yaml  # type: ignore

    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent.parent / cfg_path
    if not cfg_path.exists():
        print(f"[WARN] argus_config.yaml が見つかりません: {cfg_path}", file=sys.stderr)
        return [], []

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    presets = cfg.get("filter_presets") or {}
    ch_presets = presets.get("channels") or {}
    mk_presets = presets.get("meeting_kinds") or {}

    channel_ids: list[str] = []
    meeting_kinds: list[str] = []

    for name in filter_names:
        found = False
        if name in ch_presets:
            channel_ids.extend(ch_presets[name].get("values") or [])
            found = True
        if name in mk_presets:
            meeting_kinds.extend(mk_presets[name].get("values") or [])
            found = True
        if not found:
            available = sorted(set(list(ch_presets.keys()) + list(mk_presets.keys())))
            print(f"[ERROR] filter_presets に '{name}' が見つかりません。"
                  f" 利用可能: {', '.join(available)}", file=sys.stderr)
            sys.exit(1)

    return list(dict.fromkeys(channel_ids)), list(dict.fromkeys(meeting_kinds))


def _resolve_name_section(
    section_key: str,
    config_path: Path | str = "data/argus_config.yaml",
) -> dict[str, str]:
    """argus_config.yaml の指定セクション（`channel_names` / `user_names` 等）を dict で返す。
    yaml が無い・空・キー不在のいずれでも空 dict を返す（呼び出し側でフォールバック）。
    """
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent.parent / cfg_path
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    sec = cfg.get(section_key) or {}
    if not isinstance(sec, dict):
        return {}
    return {str(k): str(v) for k, v in sec.items() if v}


def resolve_user_names(
    config_path: Path | str = "data/argus_config.yaml",
) -> dict[str, str]:
    """argus_config.yaml の `user_names:` セクションから user_id → 表示名 dict を返す。"""
    return _resolve_name_section("user_names", config_path)


def resolve_channel_names(
    config_path: Path | str = "data/argus_config.yaml",
) -> dict[str, str]:
    """argus_config.yaml の `channel_names:` セクションから channel_id → 表示名 dict を返す。"""
    return _resolve_name_section("channel_names", config_path)


def resolve_report_canvas_id(
    fallback: str | None = None,
    config_path: Path | str = "data/argus_config.yaml",
) -> str | None:
    """pm_report / pm_sync_canvas が扱う Canvas ID を解決する。

    優先順位: 環境変数 PM_REPORT_CANVAS_ID > argus_config.yaml の report.canvas_id > fallback
    """
    env_val = os.environ.get("PM_REPORT_CANVAS_ID")
    if env_val:
        return env_val
    try:
        import yaml  # type: ignore
    except Exception:
        return fallback
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent.parent / cfg_path
    if not cfg_path.exists():
        return fallback
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return fallback
    report = cfg.get("report") or {}
    cid = report.get("canvas_id")
    if isinstance(cid, str) and cid:
        return cid
    return fallback


# --------------------------------------------------------------------------- #
# ロガーユーティリティ
# --------------------------------------------------------------------------- #

def make_logger(output_path: str | None):
    """
    (log, close) のタプルを返す。

    Parameters
    ----------
    output_path : str | None
        ファイルに出力する場合はパス文字列。None なら標準出力のみ。

    Returns
    -------
    log : Callable[[str], None]
        print(msg) + output_file.write(msg + "\\n") を行う関数
    close : Callable[[], None]
        output_file を閉じる関数（output_path が None の場合は何もしない）
    """
    output_file = open(output_path, "w", encoding="utf-8") if output_path else None

    def log(msg: str = "") -> None:
        print(msg)
        if output_file:
            output_file.write(msg + "\n")

    def close() -> None:
        if output_file:
            output_file.close()

    return log, close


# --------------------------------------------------------------------------- #
# LLM 呼び出し（utils.llm に移動済み — 後方互換のため再 export）
# --------------------------------------------------------------------------- #
from utils.llm import (  # noqa: E402, F401
    _call_local_llm_inner,
    call_argus_llm,
    call_local_llm,
    call_rivault,
    detect_vllm_model,
    strip_think_blocks,
)

# --------------------------------------------------------------------------- #
# CLAUDE.md ローダー
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_claude_md_context() -> str:
    """ローカルLLM向けプロジェクト文脈を返す。generate_minutes_local.py より移植。

    docs/project.md から「ステークホルダー・主なプロジェクト参加者・プロジェクト固有の用語・
    会議の種類」の各セクションを抽出する。docs/project.md が存在しない場合は CLAUDE.md に
    フォールバックする。Claude CLI は CLAUDE.md を自動ロードするが、ローカルLLMはしないため
    このコンテキストをプロンプトに明示的に埋め込む必要がある。
    """
    _SECTION_PAT = re.compile(
        r"^###\s+(ステークホルダー|主なプロジェクト参加者|会議の種類)"
    )
    project_md = _REPO_ROOT / "docs" / "project.md"
    claude_md  = _REPO_ROOT / "CLAUDE.md"

    if project_md.exists():
        content = project_md.read_text(encoding="utf-8")
        sections, capture = [], False
        for line in content.splitlines():
            if _SECTION_PAT.match(line):
                capture = True
            if capture:
                sections.append(line)
        return "\n".join(sections) if sections else content

    # フォールバック: CLAUDE.md から抽出
    if not claude_md.exists():
        return ""
    content = claude_md.read_text(encoding="utf-8")
    sections, capture = [], False
    for line in content.splitlines():
        if _SECTION_PAT.match(line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)
    return "\n".join(sections) if sections else content[:3000]


def load_codesign_context() -> str:
    """docs/project.md の `### コデザイン項目` セクション本文を返す。

    富岳NEXT のシステム仕様選択肢（ノード構成・メモリ階層・スケールアウト NW・
    GPU 世代等）はコデザイン項目セクションに記載されており、機密情報のため
    Claude が直接読めない。Argus Agent などローカル LLM が文脈として利用するため、
    スクリプトから直接ファイルを読んでプロンプトに埋め込む。

    セクションが存在しない / ファイルが存在しない場合は空文字列を返す。
    """
    # _REPO_ROOT は scripts/ を指してしまっているので、リポジトリルートを独自計算
    repo_root = Path(__file__).resolve().parent.parent.parent
    project_md = repo_root / "docs" / "project.md"
    if not project_md.exists():
        return ""
    content = project_md.read_text(encoding="utf-8")
    lines = content.splitlines()
    out: list[str] = []
    capture = False
    for line in lines:
        if re.match(r"^###\s+コデザイン項目\s*$", line):
            capture = True
            continue
        if capture and re.match(r"^##?\s+", line):
            break
        if capture:
            out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def load_claude_md(claude_md_path: Path) -> str:
    """
    CLAUDE.md を読み込み、`@path` 参照を再帰的に展開して返す。

    Claude Code は `@docs/project.md` のような行を自動的に展開するが、
    スクリプトがファイルを直接読む場合は展開されない。
    本関数はその差異を吸収し、参照先ファイルの内容をインラインに結合する。
    """
    if not claude_md_path.exists():
        return ""
    base_dir = claude_md_path.parent
    return _expand_at_refs(claude_md_path.read_text(encoding="utf-8"), base_dir, depth=0)


def _expand_at_refs(text: str, base_dir: Path, depth: int) -> str:
    if depth > 5:  # 循環参照ガード
        return text
    lines = []
    for line in text.splitlines():
        m = re.match(r"^@(.+)$", line.strip())
        if m:
            ref_path = base_dir / m.group(1).strip()
            if ref_path.exists():
                included = _expand_at_refs(
                    ref_path.read_text(encoding="utf-8"), ref_path.parent, depth + 1
                )
                lines.append(included)
            # 参照先が存在しない場合はその行をスキップ
        else:
            lines.append(line)
    return "\n".join(lines)


def retrieve_knowledge_for_extraction(
    query_text: str,
    qa_db_path: Path | None = None,
    top_k: int = 5,
    since_days: int = 90,
    logger=None,
    index_name: str = "pm-all",
) -> str:
    """
    抽出処理用のナレッジ検索。
    統合 FTS5 インデックス (data/qa_index.db) から関連する過去議論・決定事項を取得し、
    プロンプト注入用のフォーマット済みテキストを返す。

    Args:
        query_text: 検索クエリ（Slackスレッド本文 or 議事録本文）
        qa_db_path: 統合 FTS5 DB（None なら data/qa_index.db）
        top_k: 返却する最大チャンク数（デフォルト5）
        since_days: 検索対象を直近N日以内に限定（デフォルト90日）
        logger: ロガー（省略時は標準出力）
        index_name: chunk_indexes でフィルタする論理 index 名（デフォルト pm-all）

    Returns:
        フォーマット済みナレッジテキスト。検索失敗時は空文字列。
    """
    import logging
    from datetime import datetime, timedelta

    if logger is None:
        logger = logging.getLogger(__name__)

    # デフォルトDB: data/qa_index.db
    if qa_db_path is None:
        repo_root = Path(__file__).resolve().parent.parent
        qa_db_path = repo_root / "data" / "qa_index.db"

    # FTS5インデックス未構築時はスキップ
    if not qa_db_path.exists():
        logger.debug(f"ナレッジDB未構築: {qa_db_path}（スキップ）")
        return ""

    try:
        # scripts/ を sys.path に追加（argus/pm_qa_server, enrich/knowledge_context の解決用）
        _scripts_dir = str(Path(__file__).resolve().parent.parent)
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from argus.retrieval import rerank_chunks, retrieve_chunks_hyde
        from enrich.knowledge_context import extract_topic_keywords, format_context

        # トピックキーワード抽出（名詞・固有名詞のみ）
        keywords = extract_topic_keywords(query_text)
        search_query = " ".join(keywords[:15])  # 上位15個

        # 日付カットオフ計算
        cutoff_date = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")

        logger.info(f"ナレッジ検索: {cutoff_date} 以降, キーワード={search_query[:100]}, index={index_name}")

        # FTS5検索（HyDE クエリ拡張 + 日付フィルタ + index_name フィルタ）
        chunks = retrieve_chunks_hyde(
            search_query, qa_db_path, k=20,
            since_date=cutoff_date, index_name=index_name,
        )
        if not chunks:
            logger.debug("ナレッジ検索: 該当なし")
            return "（該当する過去議論なし）"

        # LLM re-ranking で上位top_k件に絞り込み
        reranked = rerank_chunks(search_query, chunks)[:top_k]

        # プロンプト注入用フォーマット
        return format_context(reranked)

    except Exception as e:
        logger.warning(f"ナレッジ検索エラー（処理継続）: {e}")
        return ""


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Whisper パーサ（utils.transcript に移動済み — 後方互換のため再 export）
# --------------------------------------------------------------------------- #
from utils.transcript import (  # noqa: F401 — 後方互換
    _WHISPER_SEGMENT_RE,
    _parse_timestamp,
    format_whisper_transcript,
    parse_whisper_transcript,
    prepare_transcript,
)

# --------------------------------------------------------------------------- #
# パスユーティリティ
# --------------------------------------------------------------------------- #

def resolve_db_path(arg_db: str | None, default: Path) -> Path:
    """--db 引数からパスを解決する"""
    return Path(arg_db) if arg_db else default



# --------------------------------------------------------------------------- #
# VTT ユーティリティ（utils.transcript に移動済み — 後方互換のため再 export）
# --------------------------------------------------------------------------- #
from utils.transcript import (  # noqa: F401
    _COMBINED_PART_RE,
    _ts_to_sec,
    build_speaker_map,
    enrich_combined_with_vtt,
    get_speaker_summary,
    get_speaker_timeline,
    parse_vtt,
)
