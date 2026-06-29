"""narrate.py — Argus TTS/ナレーション動画生成層

PM分析ロジック（brief/risk/draft/today/transcribe）は argus.pm_argus に移動済み。
本モジュールは PPTX/PDF スライドに音声ナレーションを付けた mp4 を生成し
Slack に投稿する機能群のみを提供する。

依存方向: pm_argus → narrate（TTS helpers を呼ぶ）。逆方向なし。

公開 API:
    _NarrateSession  — セッション状態 dataclass
    _narrate_sessions, _narrate_lock  — セッション管理グローバル
    _run_narrate, _run_narrate_build, _run_narrate_cancel  — Slack ハンドラ
    _post_argus_voice, _post_argus_video  — Slack 投稿ヘルパ
    _post_today_voice  — today コマンド用音声投稿
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logger = logging.getLogger("pm_argus")


# --------------------------------------------------------------------------- #
# /argus-narrate セッション管理
# --------------------------------------------------------------------------- #

class _NarrateSession:
    thread_ts: str
    channel_id: str
    filename: str
    work_dir: Path
    slides: list
    narrations: list[str]
    lang: str
    command: dict
    phase: str
    iteration: int

    def __init__(
        self,
        thread_ts: str,
        channel_id: str,
        filename: str,
        work_dir: Path,
        slides: list | None = None,
        narrations: list[str] | None = None,
        lang: str = "ja",
        command: dict | None = None,
        phase: str = "draft",
        iteration: int = 0,
    ) -> None:
        self.thread_ts = thread_ts
        self.channel_id = channel_id
        self.filename = filename
        self.work_dir = work_dir
        self.slides = slides if slides is not None else []
        self.narrations = narrations if narrations is not None else []
        self.lang = lang
        self.command = command if command is not None else {}
        self.phase = phase
        self.iteration = iteration


_narrate_sessions: dict[str, _NarrateSession] = {}     # thread_ts → session
_narrate_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Slack 投稿ヘルパ（TTS / 動画）
# --------------------------------------------------------------------------- #

def _post_argus_voice(
    command: dict,
    *,
    kind: str,
    today: str,
    result_md: str,
    summarize_mode: str = "auto",
    title: str,
    enable_env: str = "",
) -> None:
    """argus-* コマンドの本文を音声合成し、コマンドを叩いたチャンネルにアップロードする。

    DM (conversations_open) に投稿すると Slack 上は "App" セクションに隔離され
    視認性が悪いため、コマンドを実行したチャンネル (command.channel_id) に
    chat_postMessage / files_upload_v2 でそのまま投稿する。
    テキスト本文は ephemeral だが、音声 mp3 はチャンネル全員に見える点に注意。

    失敗してもコマンド全体を落とさないよう例外は内側で握りつぶす。
    """
    if enable_env and os.environ.get(enable_env, "1") == "0":
        logger.info(f"[argus-{kind}] voice: {enable_env}=0 によりスキップ")
        return

    user_id = command.get("user_id") or ""
    channel_id = command.get("channel_id") or ""
    if not channel_id:
        logger.warning(f"[argus-{kind}] voice: channel_id 不明、スキップ")
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.warning(f"[argus-{kind}] voice: SLACK_BOT_TOKEN 未設定、スキップ")
        return

    try:
        import pm_tts
        from slack_sdk import WebClient
    except ImportError as exc:
        logger.warning(f"[argus-{kind}] voice: import 失敗 ({exc})、スキップ")
        return

    mp3_path = Path(f"/tmp/argus_{kind}_{today}_{user_id or 'anon'}.mp3")
    speaker_id = pm_tts.DEFAULT_SPEAKER
    try:
        logger.info(f"[argus-{kind}] voice: 要約・合成を開始 mode={summarize_mode}")
        pm_tts.synthesize_markdown(
            result_md,
            mp3_path,
            speaker=speaker_id,
            summarize=True,
            summarize_mode=summarize_mode,
            quiet=True,
        )
        logger.info(f"[argus-{kind}] voice: mp3 生成完了 size={mp3_path.stat().st_size} bytes")

        client = WebClient(token=bot_token)
        credit = pm_tts.credit_line(speaker_id)
        initial_comment = (
            ":sound: 音声版（要約・短縮）です。\n"
            f"_{credit}_\n"
            "削除する場合はこのメッセージに :wastebasket: リアクションを付けてください。"
        )

        upload_resp = client.files_upload_v2(
            channel=channel_id,
            file=str(mp3_path),
            filename=mp3_path.name,
            title=title,
            initial_comment=initial_comment,
        )

        # アップロード履歴を記録（reaction_added の対象判定に使う）
        try:
            import voice_uploads
            file_id, message_ts = _extract_share_ts(upload_resp, channel_id)
            if file_id and not message_ts:
                import time as _time
                for delay in (0.5, 1.0, 2.0):
                    _time.sleep(delay)
                    try:
                        info = client.files_info(file=file_id)
                    except Exception as exc:
                        logger.debug(f"[argus-{kind}] voice: files_info 失敗 {exc}")
                        continue
                    file_obj = info.get("file") or {}
                    _, message_ts = _extract_share_ts({"files": [file_obj]}, channel_id)
                    if message_ts:
                        break
            if file_id and message_ts:
                voice_uploads.record_upload(
                    message_ts=message_ts,
                    channel_id=channel_id,
                    file_id=file_id,
                    user_id=user_id,
                    kind=kind,
                    title=title,
                )
                logger.info(f"[argus-{kind}] voice: 履歴記録 file_id={file_id} ts={message_ts}")
            else:
                logger.warning(
                    f"[argus-{kind}] voice: file_id / message_ts を取得できず履歴未記録 "
                    f"(file_id={file_id!r} message_ts={message_ts!r})"
                )
        except Exception as exc:
            logger.warning(f"[argus-{kind}] voice: 履歴記録失敗 {exc}")

        logger.info(f"[argus-{kind}] voice: チャンネルアップロード完了 ch={channel_id}")
    except Exception as exc:
        logger.exception(f"[argus-{kind}] voice: 失敗 {exc}")
    finally:
        try:
            if mp3_path.exists():
                mp3_path.unlink()
        except Exception:
            pass


def _post_argus_video(
    command: dict,
    *,
    kind: str,
    mp4_path: Path,
    title: str,
    initial_comment: str,
) -> None:
    """argus-* コマンドが生成した mp4 をコマンド実行チャンネルに投稿する。

    _post_argus_voice の動画版。voice_uploads.db に kind を記録し、
    :wastebasket: リアクションや /argus-delete スレッド一括削除の対象にする。
    """
    user_id = command.get("user_id") or ""
    channel_id = command.get("channel_id") or ""
    if not channel_id:
        logger.warning(f"[argus-{kind}] video: channel_id 不明、スキップ")
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.warning(f"[argus-{kind}] video: SLACK_BOT_TOKEN 未設定、スキップ")
        return

    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        logger.warning(f"[argus-{kind}] video: import 失敗 ({exc})、スキップ")
        return

    try:
        client = WebClient(token=bot_token)
        upload_resp = client.files_upload_v2(
            channel=channel_id,
            file=str(mp4_path),
            filename=mp4_path.name,
            title=title,
            initial_comment=initial_comment,
        )

        try:
            import voice_uploads
            file_id, message_ts = _extract_share_ts(upload_resp, channel_id)
            # files_upload_v2 のレスポンスに shares が入らないことがあるため、
            # file_id だけ取れて message_ts が空なら files_info でフォールバック取得。
            # それでも取れない場合 (shares 反映が遅延) は再試行してから諦める。
            if file_id and not message_ts:
                import time as _time
                for delay in (0.5, 1.0, 2.0):
                    _time.sleep(delay)
                    try:
                        info = client.files_info(file=file_id)
                    except Exception as exc:
                        logger.debug(f"[argus-{kind}] video: files_info 失敗 {exc}")
                        continue
                    file_obj = info.get("file") or {}
                    _, message_ts = _extract_share_ts({"files": [file_obj]}, channel_id)
                    if message_ts:
                        break
            if file_id and message_ts:
                voice_uploads.record_upload(
                    message_ts=message_ts,
                    channel_id=channel_id,
                    file_id=file_id,
                    user_id=user_id,
                    kind=kind,
                    title=title,
                )
                logger.info(f"[argus-{kind}] video: 履歴記録 file_id={file_id} ts={message_ts}")
            else:
                logger.warning(
                    f"[argus-{kind}] video: file_id / message_ts を取得できず履歴未記録 "
                    f"(file_id={file_id!r} message_ts={message_ts!r})"
                )
        except Exception as exc:
            logger.warning(f"[argus-{kind}] video: 履歴記録失敗 {exc}")

        logger.info(f"[argus-{kind}] video: チャンネルアップロード完了 ch={channel_id}")
    except Exception as exc:
        logger.exception(f"[argus-{kind}] video: 失敗 {exc}")


def _extract_share_ts(upload_resp: dict, channel_id: str) -> tuple[str, str]:
    """files_upload_v2 / files_info のレスポンスから (file_id, message_ts) を取り出す。

    取れない場合は空文字を返す。message_ts は files.shares.{public,private}.{channel_id}
    にあるため、レスポンスの形が違っても両方を試す。
    """
    file_id = ""
    message_ts = ""
    files_field = upload_resp.get("files") or []
    if not files_field:
        return file_id, message_ts
    f0 = files_field[0]
    file_id = f0.get("id") or f0.get("file", {}).get("id", "")
    shares = f0.get("shares") or {}
    for visibility in ("public", "private"):
        visible = shares.get(visibility) or {}
        if channel_id in visible:
            ts_list = visible[channel_id]
            if ts_list:
                message_ts = ts_list[0].get("ts", "")
                if message_ts:
                    return file_id, message_ts
    return file_id, message_ts


def _post_today_voice(command: dict, today: str, result_md: str) -> None:
    """argus-today 用の薄いラッパ（後方互換）。"""
    _post_argus_voice(
        command,
        kind="today",
        today=today,
        result_md=result_md,
        summarize_mode="auto",
        title=f"Argus 今日の活動サマリー (音声版) {today}",
        enable_env="ARGUS_TODAY_VOICE",
    )


# --------------------------------------------------------------------------- #
# /argus-narrate: PPTX/PDF → 要約読み上げ付き mp4
# Phase1: 原稿生成 → スレッドに投稿（ユーザーが修正できるようにする）
# Phase2: 「動画を生成」ボタン押下 → 最新原稿で TTS + mp4 化
# --------------------------------------------------------------------------- #

# ハイフン (-), em-dash (—), en-dash (–) いずれも許容。
# 「スライド」「Slide」「slide」など日英どちらの表記でも拾う。
_NARRATE_SLIDE_HEADER_RE = re.compile(
    r"^\s*[-—–]{2,}\s*(?:スライド|slide)\s*(\d+)\s*[-—–]{2,}\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _format_narration_message(narrations: list[str], lang: str = "ja") -> str:
    """narration 一覧を「--- スライド N ---」または「--- Slide N ---」見出し付きに整形。"""
    label = "Slide" if lang == "en" else "スライド"
    parts: list[str] = []
    for i, narration in enumerate(narrations, 1):
        parts.append(f"--- {label} {i} ---\n{narration.strip()}")
    return "\n\n".join(parts)


def _parse_narration_message(
    text: str, expected_count: int, originals: list[str],
) -> tuple[list[str], int] | None:
    """ユーザー返信から修正済み narration を抽出して originals にマージする。

    - 見出しは日英・ダッシュ種別を許容（_NARRATE_SLIDE_HEADER_RE 参照）
    - 一部スライドだけの編集も許容（残りは originals のまま）
    - 有効な編集が 1 件以上あれば (merged_narrations, edited_count) を返す
    - 1 件もマッチしなければ None
    """
    if not text:
        return None
    matches = list(_NARRATE_SLIDE_HEADER_RE.finditer(text))
    if not matches:
        return None
    found: dict[int, str] = {}
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        if idx < 1 or idx > expected_count:
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            found[idx] = body
    if not found:
        return None
    merged = list(originals)
    for idx, body in found.items():
        merged[idx - 1] = body
    return merged, len(found)


def _narrate_action_blocks(thread_ts: str) -> list[dict]:
    """「動画を生成」「キャンセル」ボタンの Block Kit ペイロード。"""
    return [
        {
            "type": "actions",
            "block_id": f"argus_narrate_actions_{thread_ts}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "argus_narrate_build",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": ":movie_camera: 動画を生成"},
                    "value": thread_ts,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "動画を生成しますか?"},
                        "text": {"type": "mrkdwn", "text": "現在の原稿（修正があればスレッド最新返信）で TTS + mp4 を作ります。"},
                        "confirm": {"type": "plain_text", "text": "生成する"},
                        "deny": {"type": "plain_text", "text": "戻る"},
                    },
                },
                {
                    "type": "button",
                    "action_id": "argus_narrate_cancel",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "終了"},
                    "value": thread_ts,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "編集セッションを終了しますか?"},
                        "text": {"type": "mrkdwn", "text": "セッションを閉じ、中間ファイル (work_dir) を削除します。投稿済みの動画はそのまま残ります。"},
                        "confirm": {"type": "plain_text", "text": "終了する"},
                        "deny": {"type": "plain_text", "text": "戻る"},
                    },
                },
            ],
        },
    ]


def _run_narrate(respond, command):
    """Slack /argus-narrate の Phase1 (原稿生成 → スレッド投稿)。

    アップロード済み PPTX/PDF を取得して LLM で各スライドの narration を生成し、
    その原稿をコマンド実行チャンネルのスレッドに投稿する。ユーザーが原稿を
    確認・必要なら同形式でスレッド返信して修正したのち、「動画を生成」ボタンを
    押すと _run_narrate_build (Phase2) に進む。
    """
    text = (command.get("text") or "").strip()
    text = text.strip("*_`~'\"「」​‌‍﻿")

    lang = "ja"
    lang_m = re.search(r"--lang\s+(ja|en)\b", text)
    if lang_m:
        lang = lang_m.group(1)
        text = re.sub(r"--lang\s+(ja|en)\b", "", text).strip()

    filename = text
    if filename and not Path(filename).suffix:
        filename += ".pptx"
    channel_id = command.get("channel_id", "")

    if not filename:
        respond(
            text=(
                "ファイル名を指定してください。\n"
                "例: `/argus-narrate slides.pptx` / `/argus-narrate handout.pdf`\n"
                "英語ナレーションにする場合は `--lang en` を付けてください。\n"
                "例: `/argus-narrate slides.pptx --lang en`"
            ),
            response_type="ephemeral",
            replace_original=True,
        )
        return

    suffix = Path(filename).suffix.lower()
    if suffix not in (".pptx", ".pdf"):
        respond(
            text=f":warning: 対応形式は .pptx / .pdf です（指定: `{filename}`）",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        respond(
            text=":warning: SLACK_BOT_TOKEN が設定されていません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    try:
        from slack_sdk import WebClient
        bot_client = WebClient(token=bot_token)
    except ImportError:
        respond(
            text=":warning: slack_sdk がインストールされていません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    try:
        listing = bot_client.files_list(channel=channel_id, types="all")
        files = listing.get("files", [])
    except Exception as exc:
        respond(
            text=f":warning: files_list 失敗: {exc}",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    matched = [f for f in files if f.get("name") == filename]
    if not matched:
        respond(
            text=f":warning: `{filename}` がこのチャンネルに見つかりません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    url = matched[0].get("url_private_download")
    if not url:
        respond(
            text=f":warning: `{filename}` のダウンロードURLが取得できません。",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # スレッド親メッセージは「原稿レビュー用ヘッダ」1 行のみ。
    # 進捗は chat_update で上書きしてチャンネルへの追加投稿を増やさない。
    try:
        progress = bot_client.chat_postMessage(
            channel=channel_id,
            text=f":scroll: `{filename}` のナレーション原稿（生成中）",
        )
        thread_ts = progress["ts"]
    except Exception as exc:
        respond(
            text=f":warning: Slack 投稿失敗: {exc}",
            response_type="ephemeral",
            replace_original=True,
        )
        return

    # 原稿生成中はセッション登録だけ先行 (Phase2 と排他)
    work_dir = Path(tempfile.mkdtemp(prefix="argus_narrate_"))
    session = _NarrateSession(
        thread_ts=thread_ts, channel_id=channel_id, filename=filename,
        work_dir=work_dir, lang=lang, command=dict(command), phase="draft",
    )
    with _narrate_lock:
        _narrate_sessions[thread_ts] = session

    try:
        import requests as _req
        src_path = work_dir / filename
        dl = _req.get(
            url,
            headers={"Authorization": f"Bearer {bot_token}"},
            stream=True, timeout=300,
        )
        dl.raise_for_status()
        with open(src_path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)

        try:
            from build_slide_video import prepare_slides, summarize_all_slides
        except ImportError as exc:
            raise RuntimeError(f"build_slide_video の import 失敗: {exc}") from exc

        logger.info(f"[argus-narrate] Phase1 開始: {filename}")
        slides = prepare_slides(src_path, work_dir)
        narrations = summarize_all_slides(slides, lang=lang, quiet=True)
        logger.info(f"[argus-narrate] 原稿生成完了: {len(narrations)} 枚")

        session.slides = slides
        session.narrations = narrations

        narration_text = _format_narration_message(narrations, lang=lang)
        # 1) スレッド: 原稿本文（修正は同形式で全件返信、ガイド文は親へ）
        bot_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=narration_text,
        )
        # 2) スレッド: 動画生成 / キャンセル ボタンのみ（テキストは fallback）
        bot_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text="動画を生成しますか?",
            blocks=_narrate_action_blocks(thread_ts),
        )
        # 3) チャンネル親メッセージを更新（追加投稿はしない）
        try:
            bot_client.chat_update(
                channel=channel_id, ts=thread_ts,
                text=(
                    f":scroll: `{filename}` のナレーション原稿（{len(narrations)} 枚）— "
                    "スレッドの原稿を確認し、修正したいスライドだけ同形式で返信、"
                    "「動画を生成」で何度でも作り直せます（編集を終えたら「終了」）"
                ),
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[argus-narrate] Phase1 エラー")
        try:
            bot_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=f":warning: 原稿生成に失敗しました: {exc}",
            )
        except Exception:
            pass
        respond(
            text=f":warning: 原稿生成エラー: {exc}",
            response_type="ephemeral",
            replace_original=True,
        )
        # 失敗時はセッションごと破棄
        with _narrate_lock:
            _narrate_sessions.pop(thread_ts, None)
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_narrate_build(thread_ts: str, user_id: str) -> None:
    """Phase2: スレッドの最新返信を読み込み、修正済み narration で動画を生成。

    ボタンハンドラ (pm_qa_server.py) から呼ばれる。Slack への通知も内部で完結する。
    """
    with _narrate_lock:
        session = _narrate_sessions.get(thread_ts)
        if session is None:
            return
        if session.phase != "draft":
            return  # 多重押下を無視
        session.phase = "rendering"

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.warning("[argus-narrate] Phase2: SLACK_BOT_TOKEN 未設定")
        return
    try:
        from slack_sdk import WebClient
        bot_client = WebClient(token=bot_token)
    except ImportError:
        logger.warning("[argus-narrate] Phase2: slack_sdk なし")
        return

    channel_id = session.channel_id
    filename = session.filename
    expected = len(session.slides)

    # スレッドから最新の修正済み narration を拾う
    final_narrations = list(session.narrations)
    edited = False
    edited_count = 0
    try:
        replies = bot_client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
        bot_user_id = None
        try:
            auth = bot_client.auth_test()
            bot_user_id = auth.get("user_id")
        except Exception:
            pass
        candidate_msgs: list[dict] = []
        for msg in reversed(replies.get("messages", []) or []):
            if msg.get("ts") == thread_ts:
                continue
            if bot_user_id and msg.get("user") == bot_user_id:
                continue
            if msg.get("bot_id"):
                continue
            candidate_msgs.append(msg)
        for msg in candidate_msgs:
            parsed = _parse_narration_message(msg.get("text") or "", expected, session.narrations)
            if parsed is not None:
                final_narrations, edited_count = parsed
                edited = True
                logger.info(f"[argus-narrate] Phase2: ユーザー修正 {edited_count}/{expected} 枚を採用 (ts={msg.get('ts')})")
                break
        if not edited and candidate_msgs:
            latest = candidate_msgs[0]
            preview = (latest.get("text") or "")[:240].replace("\n", " | ")
            logger.warning(
                f"[argus-narrate] Phase2: ユーザー返信が見出し形式と合致せず元原稿を使用 "
                f"(返信件数={len(candidate_msgs)}, latest_ts={latest.get('ts')}, preview={preview!r})"
            )
    except Exception as exc:
        logger.warning(f"[argus-narrate] Phase2: スレッド取得失敗 {exc}")

    work_dir = session.work_dir
    try:
        from build_slide_video import render_video

        iteration = session.iteration + 1
        # 同名上書きにすると Slack 側で「同じ mp4」と扱われる可能性があるので回ごとに名前を分ける
        mp4_path = work_dir / f"{Path(filename).stem}.narrate.r{iteration}.mp4"
        logger.info(f"[argus-narrate] Phase2 開始 r{iteration}: {filename} (edited={edited})")
        render_video(
            session.slides, final_narrations, mp4_path, work_dir,
            lang=session.lang, quiet=True,
        )
        logger.info(
            f"[argus-narrate] mp4 生成完了 r{iteration} size={mp4_path.stat().st_size} bytes"
        )

        try:
            import pm_tts
            credit = pm_tts.credit_line(pm_tts.DEFAULT_SPEAKER)
        except Exception:
            credit = ""
        round_note = f" (Round {iteration})"
        initial_comment = (
            f":movie_camera: スライド要約動画{round_note}です。\n"
            + (f"_{credit}_\n" if credit else "")
            + "原稿を直したい場合はこのスレッドに修正版を返信し、再度「動画を生成」を押してください。\n"
            + "完了したらスレッドの「終了」ボタンで編集セッションを閉じてください。"
        )
        _post_argus_video(
            session.command,
            kind="narrate",
            mp4_path=mp4_path,
            title=f"{Path(filename).stem} 要約動画 r{iteration}",
            initial_comment=initial_comment,
        )
        # セッションを次のラウンドに繰り越す: narrations を「今回採用した版」で更新し、
        # 次回 reply は前回採用版に対して差分マージされる。
        with _narrate_lock:
            session.narrations = final_narrations
            session.iteration = iteration
            session.phase = "draft"
        try:
            if edited:
                note = f"（直近 {edited_count}/{expected} 枚を修正）"
            else:
                note = ""
            bot_client.chat_update(
                channel=channel_id, ts=thread_ts,
                text=(
                    f":white_check_mark: `{filename}` Round {iteration} 完了{note} — "
                    "原稿を再修正してもう一度「動画を生成」、または「終了」で締めてください"
                ),
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("[argus-narrate] Phase2 エラー")
        try:
            bot_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=f":warning: 動画生成に失敗しました: {exc}",
            )
        except Exception:
            pass
        # 失敗時はリトライできるよう draft に戻すだけでセッションは残す
        with _narrate_lock:
            sess = _narrate_sessions.get(thread_ts)
            if sess is not None:
                sess.phase = "draft"


def _run_narrate_cancel(thread_ts: str, user_id: str) -> None:
    """編集セッションを終了し、work_dir を破棄して親メッセージを更新する。"""
    with _narrate_lock:
        session = _narrate_sessions.pop(thread_ts, None)
    if session is None:
        return
    shutil.rmtree(session.work_dir, ignore_errors=True)
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        return
    try:
        from slack_sdk import WebClient
        rounds = session.iteration
        suffix = f"（{rounds} 回生成）" if rounds else "（動画は生成されませんでした）"
        WebClient(token=bot_token).chat_update(
            channel=session.channel_id, ts=thread_ts,
            text=f":checkered_flag: `{session.filename}` のナレーション編集を終了しました{suffix}",
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
