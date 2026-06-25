#!/usr/bin/env python3
"""
pm_qa_server.py - Slack Slash Command QA サーバー（Socket Mode）

スラッシュコマンド（/argus-brief, /argus-risk, /argus-investigate 等）を
実行チャンネルに応じてルーティングし、ローカルLLMで処理する。

起動方法:
  source ~/.secrets/slack_tokens.sh
  export LOCAL_LLM_URL="http://localhost:8000/v1" LOCAL_LLM_TOKEN="dummy"
  python3 scripts/pm_qa_server.py

環境変数:
  SLACK_BOT_TOKEN   必須: Bot Token (xoxb-)
  SLACK_APP_TOKEN   必須: App-Level Token (xapp-)
  LOCAL_LLM_URL   必須: vLLM エンドポイント
  LOCAL_LLM_TOKEN    デフォルト: "dummy"
  （モデル名は vLLM /v1/models から自動取得）
  ARGUS_CONFIG      デフォルト: data/argus_config.yaml（旧 QA_CONFIG / qa_config.yaml にフォールバック）
"""

import logging
import os
import re
import signal
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cli_utils import call_argus_llm, load_claude_md_context
from db_utils import open_pm_db, fetch_milestone_progress, fetch_overdue_items, fetch_summary_stats
from argus.retrieval import (  # 検索層（後方互換のため * 相当を個別 import）
    TOP_K_RETRIEVE, TOP_K_RERANK,
    _RECENCY_HALF_LIFE_DAYS, _RECENCY_WEIGHT, _VECTOR_SEARCH_WEIGHT, _VECTOR_K,
    _sudachi_tokenizer, _sudachi_split_mode, _SUDACHI_TARGET_POS,
    _init_sudachi, sudachi_tokenize_query,
    sanitize_fts_query, _fts5_search, _fts_tokens_search,
    retrieve_chunks, retrieve_chunks_vector, retrieve_chunks_hybrid,
    _rrf_merge, _recency_score, _combined_score,
    extract_search_keywords, expand_query_hyde, retrieve_chunks_hyde,
    rerank_chunks,
)
from argus.pm_argus import (
    _run_brief, _run_draft, _run_risk, _run_today_only,
    _run_transcribe, _transcribe_jobs, _transcribe_lock,
)
from argus.narrate import (
    _run_narrate, _run_narrate_build, _run_narrate_cancel,
    _narrate_sessions, _narrate_lock,
)
from argus.pm_argus_agent import _run_investigate
from argus.patrol.confirm import handle_approve_close, handle_reject_close
from argus.patrol.state import PatrolState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pm_qa_server")

# SudachiPy / 検索定数 / 検索関数群は argus.retrieval に移動済み

# --- 設定 ---
# TOP_K_RETRIEVE / TOP_K_RERANK は retrieval から import 済み
MAX_TOKENS = 1024
LLM_TIMEOUT = 120

_OPENAI_BASE = os.environ.get("LOCAL_LLM_URL", "")
_OPENAI_KEY = os.environ.get("LOCAL_LLM_TOKEN", "dummy")
_LOCAL_LLM_MODEL = ""
if _OPENAI_BASE:
    try:
        from cli_utils import detect_vllm_model
        _LOCAL_LLM_MODEL = detect_vllm_model(_OPENAI_BASE)
    except Exception as _e:
        print(f"[WARN] vLLM モデル自動検出に失敗: {_e}", file=sys.stderr)

_PROJECT_CONTEXT = ""

SYSTEM_PROMPT_TEMPLATE = """\
あなたは富岳NEXTプロジェクトの情報検索アシスタントです。

【回答ルール】
- 以下の「取得した関連情報」のみを根拠として、日本語で回答してください
- 構造化データ検索結果がある場合、そこに含まれるID・担当者・期限・件数は正確に記載してください
- テキスト検索結果がある場合、出典の日付・会議名を可能な限り含めてください
- 情報が見つからない場合は「記録が見つかりません」とだけ回答してください
- 推測・創作はしないでください
- 回答全体は500字以内を目安にしてください（長い場合は要点を箇条書きに）

【プロジェクト文脈】
{project_context}
"""

# --- 設定ロード ---

_channel_index_map: dict[str, str] = {}   # channel_id → index_name
_index_db_map: dict[str, Path] = {}       # index_name → Path
_pm_db_map: dict[str, list[Path]] = {}    # index_name → [pm.db Paths]
_default_index: str = "pm"
_channel_names: dict[str, str] = {}       # channel_id → 表示名
_mention_allowed_channels: set[str] = set()  # @mention 応答を許可するチャンネル集合


def load_qa_config(config_path: Path) -> None:
    """argus_config.yaml（旧 qa_config.yaml）を読み込み、グローバルマップを初期化する。
    全インデックスは統合 qa_index.db を共有し、検索時に index_name でフィルタする。"""
    global _channel_index_map, _index_db_map, _pm_db_map, _default_index
    global _channel_names, _mention_allowed_channels

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    _default_index = cfg.get("default_index", "pm")
    _channel_index_map = cfg.get("channel_map") or {}
    _channel_names = cfg.get("channel_names") or {}
    _mention_allowed_channels = set(cfg.get("mention_allowed_channels") or [])

    qa_unified = _REPO_ROOT / "data" / "qa_index.db"
    for name, index_cfg in (cfg.get("indices") or {}).items():
        # qa_*.db への個別パス指定があっても無視し、統合 DB を使う。
        # 検索クエリが index_name でフィルタする前提。
        _index_db_map[name] = qa_unified
        pm_db_list = index_cfg.get("pm_db", [])
        _pm_db_map[name] = [_REPO_ROOT / p for p in pm_db_list]

    logger.info(f"argus_config ロード: {len(_index_db_map)} インデックス, "
                f"{len(_channel_index_map)} チャンネルマッピング, "
                f"デフォルト={_default_index}")


def resolve_index_db(channel_id: str) -> tuple[str, Path, list[Path]]:
    """チャンネルIDからインデックス名・FTS DBパス（統合）・pm.dbパスリストを返す。
    DBパスは全インデックス共通で data/qa_index.db。検索時に index_name で絞り込む。"""
    index_name = _channel_index_map.get(channel_id, _default_index)
    db_path = _index_db_map.get(index_name)
    if db_path is None:
        # 設定ロード前 / インデックス未定義時のフォールバック
        db_path = _REPO_ROOT / "data" / "qa_index.db"
    pm_db_paths = _pm_db_map.get(index_name, [])
    return index_name, db_path, pm_db_paths


# --- 検索関数群は argus.retrieval に移動済み（import でアクセス可能）---
# sanitize_fts_query, retrieve_chunks, retrieve_chunks_hybrid 等は
# retrieval import から利用する。

# --- プロンプト構築 ---

_SOURCE_TYPE_LABEL = {
    "minutes_content": "議事録本文",
    "slack_raw": "Slackメッセージ",
    "document": "資料",
    "box_document": "Box資料",
    "web": "Web記事",
}

# channel_id → 表示名は argus_config.yaml の channel_names から動的に解決する
# （実値はチャンネル機密のため source 内に持たない）。


def _format_source_label(chunk: dict) -> str:
    label = _SOURCE_TYPE_LABEL.get(chunk["source_type"], chunk["source_type"])
    source_type = chunk["source_type"]
    if source_type == "web":
        from urllib.parse import urlparse
        ref = chunk.get("source_ref") or ""
        domain = urlparse(ref).netloc.replace("www.", "") if ref else "web"
        held_at = chunk["held_at"] or "日付不明"
        return f"{domain} / {label} ({held_at})"
    if source_type == "box_document":
        # content 先頭の【folder/filename】からタイトルを抽出
        content = chunk.get("content") or ""
        title = ""
        if content.startswith("【"):
            end = content.find("】")
            if end > 0:
                title = content[1:end]
        held_at = chunk.get("held_at") or "日付不明"
        return f"{title or 'Box資料'} ({held_at})"
    db_name = chunk["source_db"].replace("minutes/", "").replace(".db", "")
    # Slack チャンネルIDを人名称に変換
    if source_type == "slack_raw":
        # サブプロセスで load_qa_config を通っていない場合の lazy load フォールバック
        if not _channel_names:
            _ensure_channel_names_loaded()
        resolved = _channel_names.get(db_name)
        if resolved is None and db_name.startswith("C"):
            logger.warning(
                "channel_names 未解決: %s (channel_names entries=%d)",
                db_name, len(_channel_names),
            )
        db_name = resolved or db_name
    held_at = chunk["held_at"] or "日付不明"
    return f"{db_name} / {label} ({held_at})"


def _ensure_channel_names_loaded() -> None:
    """`_channel_names` が空の場合、argus_config.yaml から直接 channel_names だけ読み込む。
    load_qa_config() を経由しないインポートパス（pm_argus_agent から _format_source_label のみ
    import するケース等）で表示名が空にならないためのフォールバック。"""
    global _channel_names
    if _channel_names:
        return
    cfg_path = _REPO_ROOT / "data" / "argus_config.yaml"
    if not cfg_path.exists():
        return
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        names = cfg.get("channel_names") or {}
        if isinstance(names, dict) and names:
            _channel_names = {str(k): str(v) for k, v in names.items()}
            logger.info("channel_names lazy-load: %d entries", len(_channel_names))
    except Exception as exc:
        logger.warning("channel_names lazy-load failed: %s", exc)


def format_context(chunks: list[dict]) -> str:
    lines = []
    for i, chunk in enumerate(chunks, 1):
        label = _format_source_label(chunk)
        ref = chunk.get("source_ref") or ""
        source_type = chunk.get("source_type", "")

        if source_type == "slack_raw" and ref:
            ref_str = f" | <{ref}|スレッドを開く>"
        elif source_type == "minutes_content" and ref:
            held_at = chunk.get("held_at") or ""
            ref_str = f" | {held_at} {ref}" if held_at else f" | {ref}"
        elif source_type == "web" and ref:
            ref_str = f" | <{ref}|リンク>"
        elif source_type == "box_document" and ref:
            ref_str = f" | <{ref}|Boxで開く>"
        else:
            ref_str = f" | {ref}" if ref else ""

        lines.append(f"[{i}] 出典: {label}{ref_str}")
        lines.append(f"    {chunk['content'].strip()}")
        lines.append("")
    return "\n".join(lines)


# --- Hybrid検索: Intent分類 + 構造化クエリ ---


from argus.qa_engine import (  # noqa: F401 — 後方互換のため再 export
    _CLASSIFY_PROMPT, SYSTEM_PROMPT_TEMPLATE,
    classify_intent,
    _query_action_items, _query_decisions,
    run_structured_query,
    generate_answer, format_slack_response,
    _run_qa,
)
def build_app():
    from slack_bolt import App

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.error("SLACK_BOT_TOKEN が設定されていません")
        sys.exit(1)

    app = App(token=bot_token)
    executor = ThreadPoolExecutor(max_workers=4)

    # --- Argus コマンドハンドラ ---

    @app.command("/argus-brief")
    def handle_argus_brief(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus 分析中...", response_type="ephemeral")
        executor.submit(_run_brief, respond, command)

    @app.command("/argus-today")
    def handle_argus_today(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus 今日の活動を分析中...", response_type="ephemeral")
        executor.submit(_run_today_only, respond, command)

    @app.command("/argus-draft")
    def handle_argus_draft(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()
        if not text:
            respond(
                text=(
                    "用途と件名を指定してください。\n"
                    "例: `/argus-draft agenda 次回リーダー会議`\n"
                    "用途: `agenda`(会議アジェンダ), `report`(進捗報告), `request`(確認依頼)"
                ),
                response_type="ephemeral",
            )
            return
        respond(text=":hourglass_flowing_sand: Argus 草案生成中...", response_type="ephemeral")
        executor.submit(_run_draft, respond, command)

    @app.command("/argus-risk")
    def handle_argus_risk(ack, respond, command):
        ack()
        respond(text=":hourglass_flowing_sand: Argus リスク分析中...", response_type="ephemeral")
        executor.submit(_run_risk, respond, command)

    @app.command("/argus-investigate")
    def handle_argus_investigate(ack, respond, command):
        ack()
        question = (command.get("text") or "").strip()
        if not question:
            respond(
                text=(
                    "調査内容を入力してください。\n"
                    "例: `/argus-investigate M3の遅延原因を調査して`\n"
                    "例: `/argus-investigate 先週の決定事項が実行されているか確認`"
                ),
                response_type="ephemeral",
            )
            return
        respond(text=f":mag: `{question[:80]}`", response_type="ephemeral")
        executor.submit(_run_investigate, respond, command)

    # --- Patrol Agent Block Kit ボタンハンドラ ---

    _patrol_state = PatrolState(_REPO_ROOT / "data" / "patrol_state.db")

    @app.action("patrol_approve_close")
    def on_approve_close(ack, body, client):
        ack()
        pm_paths = _pm_db_map.get(_default_index, [_REPO_ROOT / "data" / "pm.db"])
        action = (body.get("actions") or [{}])[0]
        pending_id = int(action.get("value", 0))
        pending = _patrol_state.get_pending(pending_id)
        ai_id = pending["target_id"] if pending else None

        conns = [open_pm_db(p) for p in pm_paths]
        target_conn = conns[0]
        if ai_id is not None:
            for c in conns:
                if c.execute("SELECT id FROM action_items WHERE id=?", (ai_id,)).fetchone():
                    target_conn = c
                    break
        try:
            handle_approve_close(body, client, _patrol_state, target_conn)
            target_conn.commit()
        finally:
            for c in conns:
                c.close()

    @app.action("patrol_reject_close")
    def on_reject_close(ack, body, client):
        ack()
        handle_reject_close(body, client, _patrol_state)

    def _handle_transcribe_command(ack, respond, command, example_cmd):
        """共通: 文字起こしコマンドの受付・排他制御・バックグラウンド実行。"""
        ack()
        filename = (command.get("text") or "").strip()
        if not filename:
            respond(
                text=(
                    "ファイル名を指定してください。\n"
                    f"例: `{example_cmd} GMT20260302-032528_Recording.mp4`"
                ),
                response_type="ephemeral",
            )
            return
        with _transcribe_lock:
            if _transcribe_jobs:
                running = ", ".join(
                    f"`{fname}` (ch={chid})"
                    for _, (fname, chid) in _transcribe_jobs.items()
                )
                respond(
                    text=f":warning: 現在処理中のジョブがあります。完了後に再実行してください。\n処理中: {running}",
                    response_type="ephemeral",
                )
                return
        executor.submit(_run_transcribe, respond, command)

    @app.command("/argus-transcribe")
    def handle_argus_transcribe(ack, respond, command):
        _handle_transcribe_command(ack, respond, command, "/argus-transcribe")

    @app.command("/transcribe")
    def handle_transcribe(ack, respond, command):
        _handle_transcribe_command(ack, respond, command, "/transcribe")

    @app.command("/argus-narrate")
    def handle_argus_narrate(ack, respond, command):
        ack()
        filename = (command.get("text") or "").strip()
        if not filename:
            respond(
                text=(
                    "ファイル名を指定してください。\n"
                    "例: `/argus-narrate slides.pptx` / `/argus-narrate handout.pdf`\n"
                    "英語ナレーションにする場合は `--lang en` を付けてください。\n"
                    "例: `/argus-narrate slides.pptx --lang en`"
                ),
                response_type="ephemeral",
            )
            return
        with _narrate_lock:
            if _narrate_sessions:
                running = ", ".join(
                    f"`{s.filename}` (ch={s.channel_id}, phase={s.phase})"
                    for s in _narrate_sessions.values()
                )
                respond(
                    text=f":warning: 現在処理中の要約動画ジョブがあります。完了後に再実行してください。\n処理中: {running}",
                    response_type="ephemeral",
                )
                return
        # 進捗はチャンネル親メッセージで示すので ephemeral は出さない
        executor.submit(_run_narrate, respond, command)

    @app.action("argus_narrate_build")
    def handle_argus_narrate_build(ack, body, respond):
        ack()
        try:
            thread_ts = (body.get("actions") or [{}])[0].get("value", "")
        except Exception:
            thread_ts = ""
        user_id = (body.get("user") or {}).get("id", "")
        if not thread_ts:
            return
        with _narrate_lock:
            sess = _narrate_sessions.get(thread_ts)
        if sess is None or sess.phase != "draft":
            return  # 多重押下・期限切れは静かに無視
        executor.submit(_run_narrate_build, thread_ts, user_id)

    @app.action("argus_narrate_cancel")
    def handle_argus_narrate_cancel(ack, body, respond):
        ack()
        try:
            thread_ts = (body.get("actions") or [{}])[0].get("value", "")
        except Exception:
            thread_ts = ""
        user_id = (body.get("user") or {}).get("id", "")
        if not thread_ts:
            return
        executor.submit(_run_narrate_cancel, thread_ts, user_id)

    def _delete_thread_files(client, channel_id: str, thread_ts: str) -> tuple[int, list[str]]:
        """スレッド配下の全 reply に添付された files を削除する。

        Returns: (削除件数, エラーメッセージ list)
        """
        try:
            replies = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
        except Exception as e:
            return 0, [f"スレッド取得に失敗: {e}"]

        file_ids: list[str] = []
        for msg in replies.get("messages", []) or []:
            for f in msg.get("files", []) or []:
                fid = f.get("id")
                if fid:
                    file_ids.append(fid)

        deleted = 0
        errors: list[str] = []
        for fid in file_ids:
            try:
                client.files_delete(file=fid)
                deleted += 1
            except Exception as e:
                errors.append(f"{fid}: {e}")

        # voice_uploads.db のエントリも掃除する
        try:
            sys.path.insert(0, str(_REPO_ROOT / "scripts"))
            import voice_uploads
            for fid in file_ids:
                voice_uploads.delete_record(channel_id=channel_id, message_ts=thread_ts, file_id=fid)
        except Exception as e:
            logger.debug(f"[delete] voice_uploads cleanup failed: {e}")

        return deleted, errors

    # --- デバッグ用: 受信した event を全てログ ---
    # （Slack App の Event Subscriptions に reaction_added が登録されているか
    # 切り分けるための一時フック。負担は軽微なので常時有効でも問題ない）
    @app.event({"type": "reaction_removed"})
    def handle_reaction_removed_log(event, logger=logger):
        logger.info(f"[event] reaction_removed reaction={event.get('reaction')}")

    # --- reaction_added ハンドラ: :wastebasket: で添付ファイル一括削除 ---
    # スラッシュコマンドはスレッド reply 入力欄から呼んでも thread_ts が
    # 渡らないため、Bot 投稿に🗑️リアクションを付けることで
    # 「このメッセージ（とそのスレッドの添付）を削除」を表現する。
    _DELETE_REACTIONS = {"wastebasket", "x", "no_entry_sign"}

    @app.event("reaction_added")
    def handle_reaction_delete(event, client):
        logger.info(
            f"[reaction-delete] received reaction={event.get('reaction')} "
            f"item={event.get('item')} user={event.get('user')}"
        )
        if event.get("reaction") not in _DELETE_REACTIONS:
            logger.info(f"[reaction-delete] スキップ (対象外リアクション {event.get('reaction')})")
            return
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        channel_id = item.get("channel") or ""
        message_ts = item.get("ts") or ""
        if not (channel_id and message_ts):
            return

        # 対象メッセージを取得して bot 自身の投稿か確認する。
        # （他人の投稿に誤って付けたリアクションで削除事故を起こさない）
        # conversations_history は親メッセージのみ取得できるため、
        # スレッド reply の場合は conversations_replies にフォールバックする。
        target: dict | None = None
        try:
            hist = client.conversations_history(
                channel=channel_id, latest=message_ts, inclusive=True, limit=1,
            )
            for m in hist.get("messages") or []:
                if m.get("ts") == message_ts:
                    target = m
                    break
        except Exception as e:
            logger.warning(f"[reaction-delete] history 取得失敗 ch={channel_id} ts={message_ts}: {e}")

        if target is None:
            try:
                rep = client.conversations_replies(
                    channel=channel_id, ts=message_ts, latest=message_ts,
                    inclusive=True, limit=1,
                )
                for m in rep.get("messages") or []:
                    if m.get("ts") == message_ts:
                        target = m
                        break
            except Exception as e:
                logger.warning(f"[reaction-delete] replies 取得失敗 ch={channel_id} ts={message_ts}: {e}")
                return

        if target is None:
            logger.info(f"[reaction-delete] 対象メッセージが見つからない ch={channel_id} ts={message_ts}")
            return

        bot_authored = bool(target.get("bot_id")) or target.get("subtype") == "bot_message"
        try:
            sys.path.insert(0, str(_REPO_ROOT / "scripts"))
            import voice_uploads
            recorded = voice_uploads.find_by_thread(channel_id=channel_id, message_ts=message_ts)
        except Exception as e:
            logger.debug(f"[reaction-delete] voice_uploads lookup failed: {e}")
            recorded = []

        # bot 投稿でも、voice_uploads.db に記録されている投稿でもない場合は無視
        if not (bot_authored or recorded):
            logger.info(f"[reaction-delete] 対象外 (non-bot non-recorded) ch={channel_id} ts={message_ts}")
            return

        # 親メッセージとそのスレッド配下の添付を全て削除
        thread_ts = target.get("thread_ts") or message_ts
        deleted, errors = _delete_thread_files(client, channel_id, thread_ts)

        # メッセージ本体を削除する。
        # リアクション対象が親メッセージ（thread_ts == message_ts）の場合は
        # スレッド内の全 reply も合わせて削除する。
        is_parent = (thread_ts == message_ts)
        reply_tss: list[str] = []
        if is_parent:
            try:
                cursor = None
                while True:
                    kwargs = dict(channel=channel_id, ts=thread_ts, limit=200)
                    if cursor:
                        kwargs["cursor"] = cursor
                    rep = client.conversations_replies(**kwargs)
                    for m in rep.get("messages") or []:
                        ts = m.get("ts")
                        if ts and ts != message_ts:
                            reply_tss.append(ts)
                    meta = rep.get("response_metadata") or {}
                    cursor = meta.get("next_cursor")
                    if not cursor:
                        break
            except Exception as e:
                logger.warning(f"[reaction-delete] replies 取得失敗（子削除スキップ）: {e}")

        for ts in reply_tss:
            try:
                client.chat_delete(channel=channel_id, ts=ts)
            except Exception as e:
                logger.debug(f"[reaction-delete] reply chat_delete skipped ts={ts}: {e}")

        try:
            client.chat_delete(channel=channel_id, ts=message_ts)
        except Exception as e:
            logger.debug(f"[reaction-delete] chat_delete skipped ts={message_ts}: {e}")

        if errors:
            user_id = event.get("user") or ""
            try:
                client.chat_postEphemeral(
                    channel=channel_id, user=user_id,
                    text=":warning: 削除失敗:\n" + "\n".join(f"• {e}" for e in errors[:5]),
                )
            except Exception:
                pass

    # --- app_mention ハンドラ（常駐AIとしての @mention 応答） ---
    # 許可チャンネルは argus_config.yaml の mention_allowed_channels から取得。
    # 動作確認段階ではデバッグ用 personal チャンネルのみを設定する想定。

    @app.event("app_mention")
    def handle_app_mention(event, client):
        channel_id = event.get("channel", "")
        if channel_id not in _mention_allowed_channels:
            logger.info(f"[mention] 許可外チャンネル {channel_id} からのメンションを無視")
            return

        raw_text = event.get("text", "") or ""
        # <@Uxxxx> 形式のメンションを全て除去
        question = re.sub(r"<@[UW][A-Z0-9]+>", "", raw_text).strip()
        if not question:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        user_id = event.get("user", "")
        ts_for_reply = thread_ts

        # 受付通知（スレッドに公開）
        try:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=ts_for_reply,
                text=f":mag: 調査中... `{question[:80]}`",
            )
        except Exception as e:
            logger.warning(f"[mention] 受付通知失敗: {e}")

        executor.submit(
            _run_mention_investigate,
            client, channel_id, ts_for_reply, question, user_id, event,
        )

    def _run_mention_investigate(client, channel_id, thread_ts, question, user_id, event):
        """app_mention からの調査実行。スレッドに公開返信する。

        2026-05-14: ローカル gemma4 reasoning モードに統一したため
        prefer_rivault() のラップは撤去（旧 Kimi 切替時の名残）。
        """
        _run_mention_investigate_impl(client, channel_id, thread_ts, question, user_id, event)

    def _run_mention_investigate_impl(client, channel_id, thread_ts, question, user_id, event):
        try:
            from datetime import date, timedelta
            from argus.pm_argus_agent import (
                _resolve_index_and_channels, _expand_id_references,
                AgentContext, build_seed_data, run_agent,
                _DEFAULT_SINCE_DAYS, _DATA_DIR, _MINUTES_DIR,
            )
            from utils.slack_post import _to_slack_mrkdwn

            today = date.today().isoformat()
            since_date = (date.today() - timedelta(days=_DEFAULT_SINCE_DAYS)).isoformat()
            index_db, channels, pm_db_paths, index_name = _resolve_index_and_channels(channel_id)
            conns = [open_pm_db(p) for p in pm_db_paths]

            # スレッド文脈の取り込み（メンションがスレッド内にある場合）
            thread_context = ""
            parent_ts = event.get("thread_ts")
            current_ts = event.get("ts")
            if parent_ts and parent_ts != current_ts:
                try:
                    resp = client.conversations_replies(
                        channel=channel_id, ts=parent_ts, limit=50,
                    )
                    msgs = resp.get("messages", []) or []
                    # 現在の質問メッセージ自体は除外（文脈は「過去のやり取り」）
                    past_msgs = [m for m in msgs if m.get("ts") != current_ts]

                    # ユーザーID → display_name キャッシュ
                    name_cache: dict[str, str] = {}

                    def _resolve_name(uid: str) -> str:
                        if not uid:
                            return "?"
                        if uid in name_cache:
                            return name_cache[uid]
                        try:
                            u = client.users_info(user=uid)
                            prof = (u.get("user") or {}).get("profile") or {}
                            nm = prof.get("real_name") or prof.get("display_name") or uid
                        except Exception:
                            nm = uid
                        name_cache[uid] = nm
                        return nm

                    lines = []
                    for m in past_msgs:
                        uid = m.get("user") or m.get("bot_id") or ""
                        is_bot = bool(m.get("bot_id")) or (m.get("subtype") == "bot_message")
                        speaker = "Argus" if is_bot else _resolve_name(uid)
                        # Bot が Block Kit で投稿した場合 text が空で blocks 側に本文があることがある
                        text_body = m.get("text") or ""
                        if not text_body and m.get("blocks"):
                            texts = []
                            for b in m["blocks"]:
                                t = (b.get("text") or {}).get("text") or ""
                                if t:
                                    texts.append(t)
                            text_body = "\n".join(texts)
                        text_body = text_body.replace("\n", " ")[:800]
                        lines.append(f"- **{speaker}**: {text_body}")
                    if lines:
                        thread_context = (
                            "\n\n## スレッド内の過去のやり取り（時系列、直近のメンションの手前まで）\n"
                            + "\n".join(lines)
                            + "\n\n上記はこのスレッドでの過去の会話。"
                            "直近の質問は上記を前提とした**深掘り・追質問**である可能性が高い。"
                            "まず上記のやり取りから直接答えられないかを検討し、答えられる場合はツールを呼ばずに回答する。"
                            "必要に応じて補足情報をツールで取得する。"
                        )
                except Exception as e:
                    logger.warning(f"[mention] スレッド取得失敗: {e}")

            ctx = AgentContext(
                conns=conns, today=today, since=since_date,
                no_encrypt=False, data_dir=_DATA_DIR, minutes_dir=_MINUTES_DIR,
                index_db=index_db, index_name=index_name, channels=channels,
            )

            # 実行者情報をシードに注入（search_mentions ツールに使わせる）
            user_info = ""
            if user_id:
                display_name = ""
                try:
                    u = client.users_info(user=user_id)
                    prof = (u.get("user") or {}).get("profile") or {}
                    display_name = prof.get("real_name") or prof.get("display_name") or ""
                except Exception as e:
                    logger.warning(f"[mention] users_info 失敗: {e}")
                # 姓のみ（スペース前の部分）も抽出。日本語名指し検索の補助
                name_first_token = display_name.split()[0] if display_name else ""
                user_info = (
                    f"\n\n## 実行者情報\n"
                    f"- user_id: {user_id}\n"
                    f"- 名前（display_name）: {display_name}\n"
                    f"- 姓/first token: {name_first_token}\n"
                    f"「自分」「私」「あなた宛」など一人称の参照はこのユーザーを指す。\n"
                    f"\n"
                    f"## ツール選択のガイド（重要）\n"
                    f"- **質問が一人称/名指し参照を含まない場合は search_mentions を使わないこと**。\n"
                    f"  例: 「Xのリストは？」「Yの進捗は？」→ まず search_text で本文検索する。\n"
                    f"- search_mentions は「私宛のメンションは？」「〇〇さん宛の依頼は？」等、\n"
                    f"  メンション対象が明確な質問のみで使う。\n"
                    f"- search_mentions を使う場合は user_id={user_id} と name=「{display_name}」を同時指定する\n"
                    f"  （どちらか一方では取りこぼす）。"
                )
            seed_data = build_seed_data(ctx) + user_info + thread_context

            result = run_agent(
                question=question, seed_data=seed_data, respond=None, ctx=ctx,
            )
            result = _expand_id_references(result, conns)
            for c in conns:
                c.close()

            header = f"<@{user_id}> *Argus 調査結果* ({today})\n\n" if user_id else f"*Argus 調査結果* ({today})\n\n"
            body = _to_slack_mrkdwn(header + result)
            # Slack section block は 3000 文字、chat_postMessage text は 40000 文字上限。
            # 長い出力は段落単位でチャンク分割し、複数メッセージに分けて投稿する。
            _CHUNK = 2800

            def _split(text: str, size: int) -> list[str]:
                chunks: list[str] = []
                remaining = text
                while len(remaining) > size:
                    cut = remaining.rfind("\n---", 0, size)
                    if cut < size // 2:
                        cut = remaining.rfind("\n\n", 0, size)
                    if cut < size // 2:
                        cut = remaining.rfind("\n", 0, size)
                    if cut <= 0:
                        cut = size
                    chunks.append(remaining[:cut])
                    remaining = remaining[cut:].lstrip("\n")
                if remaining:
                    chunks.append(remaining)
                return chunks

            parts = _split(body, _CHUNK)
            for i, part in enumerate(parts):
                suffix = f"\n\n（{i+1}/{len(parts)}）" if len(parts) > 1 else ""
                text_part = part + suffix
                client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts, text=text_part,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text_part}}],
                )
            logger.info(f"[mention] 完了 channel={channel_id} thread={thread_ts}")

        except Exception as e:
            logger.exception(f"[mention] エラー: {e}")
            try:
                client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts,
                    text=f":warning: 調査中にエラーが発生しました: {e}",
                )
            except Exception:
                pass

    def _shutdown(signum, frame):
        logger.info("シャットダウン中...")
        executor.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    return app, executor


def _init_common() -> None:
    """main / test-hybrid 共通の初期化処理。"""
    global _PROJECT_CONTEXT

    if _init_sudachi():
        logger.info("SudachiPy: 初期化完了（形態素解析検索を使用します）")
    else:
        logger.warning("SudachiPy: 利用不可（trigram検索のみで動作します）")

    env_config = os.environ.get("ARGUS_CONFIG") or os.environ.get("QA_CONFIG")
    if env_config:
        config_path = _REPO_ROOT / env_config
    else:
        config_path = _REPO_ROOT / "data/argus_config.yaml"
        if not config_path.exists():
            config_path = _REPO_ROOT / "data/qa_config.yaml"
    if config_path.exists():
        load_qa_config(config_path)
    else:
        logger.warning(f"argus_config.yaml が見つかりません: {config_path}")

    # 統合 qa_index.db のチャンク数を index_name 別で表示
    qa_index = _REPO_ROOT / "data" / "qa_index.db"
    if qa_index.exists():
        try:
            ic = sqlite3.connect(str(qa_index))
            counts = dict(ic.execute(
                "SELECT index_name, COUNT(*) FROM chunk_indexes GROUP BY index_name"
            ).fetchall())
            ic.close()
        except Exception as e:
            logger.warning(f"  qa_index.db クエリ失敗: {e}")
            counts = {}
        for name in _index_db_map.keys():
            n = counts.get(name, 0)
            logger.info(f"  [{name}] qa_index.db: {n} チャンク")
    else:
        logger.warning(f"  data/qa_index.db: 未構築（pm_embed.py を実行してください）")

    try:
        _PROJECT_CONTEXT = load_claude_md_context()
        logger.info(f"CLAUDE.md 文脈ロード: {len(_PROJECT_CONTEXT)} 文字")
    except Exception as e:
        logger.warning(f"CLAUDE.md ロード失敗: {e}")
        _PROJECT_CONTEXT = ""


def test_hybrid(question: str) -> None:
    """CLIテストモード: Slackデーモン不要でハイブリッド検索をテストする。"""
    _init_common()

    if not _OPENAI_BASE:
        logger.warning("LOCAL_LLM_URL が未設定です")

    index_name, index_db, _pm_dbs = resolve_index_db("")
    print(f"\n質問: {question}")
    print(f"インデックス: [{index_name}] {index_db}")
    print("-" * 60)

    # Intent分類
    intent_result = classify_intent(question)
    intent = intent_result.get("intent", "text")
    entities = intent_result.get("entities", {})
    print(f"Intent: {intent}")
    print(f"Entities: {entities}")
    print("-" * 60)

    structured_context = ""
    chunks: list[dict] = []

    if intent in ("structured", "hybrid"):
        structured_context = run_structured_query(entities)
        if structured_context:
            print(f"\n[構造化クエリ結果]\n{structured_context}")
        else:
            print("\n[構造化クエリ結果] なし")

    if intent in ("text", "hybrid") or (intent == "structured" and not structured_context):
        chunks = retrieve_chunks_hyde(question, index_db, index_name=index_name)
        print(f"\n[FTS検索] {len(chunks)} チャンク取得（HyDE拡張後）")
        chunks = rerank_chunks(question, chunks)
        print(f"[re-rank後] {len(chunks)} チャンク")
        for i, c in enumerate(chunks, 1):
            print(f"  [{i}] {_format_source_label(c)}: {c['content'][:80]}...")

    if structured_context and chunks:
        search_mode = "ハイブリッド検索"
    elif structured_context:
        search_mode = "構造化検索"
    else:
        search_mode = "テキスト検索"

    print(f"\n検索モード: {search_mode}")
    print("-" * 60)

    answer = generate_answer(question, chunks, structured_context=structured_context)
    print(f"\n[回答]\n{answer}")
    print(f"\n_（検索対象: {index_name} / {search_mode}）_")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Slack QA Server (Socket Mode)")
    parser.add_argument("--test-hybrid", metavar="QUESTION",
                        help="CLIテストモード: ハイブリッド検索をテスト（Slack不要）")
    args = parser.parse_args()

    if args.test_hybrid:
        test_hybrid(args.test_hybrid)
        return

    logger.info("pm_qa_server 起動中...")
    _init_common()

    if not _OPENAI_BASE:
        logger.warning("LOCAL_LLM_URL が未設定です（QA実行時にエラーになります）")
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.error("SLACK_BOT_TOKEN が未設定です")
    if not os.environ.get("SLACK_APP_TOKEN"):
        logger.error("SLACK_APP_TOKEN が未設定です")

    app, executor = build_app()

    from slack_bolt.adapter.socket_mode import SocketModeHandler
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        logger.error("SLACK_APP_TOKEN が未設定です")
        sys.exit(1)

    logger.info("Socket Mode で接続中... スラッシュコマンドを待機します")
    handler = SocketModeHandler(app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
