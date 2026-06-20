"""output_tools.py — Box / Slack / Canvas 出力操作の実装本体

pm_mcp_server.py（MCP ツール）と pm_qa_server.py（Slack Bot コマンド）の
両方から import して使われる。ユーザー確認は呼び出し元の責務。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("pm_argus_output")

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
_ARGUS_CONFIG = _REPO_ROOT / "data" / "argus_config.yaml"


# =========================================================================== #
#  設定解決
# =========================================================================== #

def _load_argus_config() -> dict:
    if not _ARGUS_CONFIG.exists():
        return {}
    import yaml
    with open(_ARGUS_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_box_folder_id() -> str:
    """Box アップロード先 folder_id を解決する。

    優先順位: 環境変数 PM_BOX_FOLDER_ID > argus_config.yaml の box.upload_folder_id
    """
    env_val = os.environ.get("PM_BOX_FOLDER_ID")
    if env_val:
        return env_val
    cfg = _load_argus_config()
    box_cfg = cfg.get("box") or {}
    fid = box_cfg.get("upload_folder_id") or ""
    if isinstance(fid, str) and fid.strip():
        return fid.strip()
    return ""


def _get_slack_bot_client():
    """SLACK_BOT_TOKEN から WebClient を生成。未設定なら None。"""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None
    from slack_sdk import WebClient
    return WebClient(token=token)


# =========================================================================== #
#  Box: ファイルアップロード
# =========================================================================== #

def box_upload_file(
    local_path: str | Path,
    filename: str | None = None,
    folder_id: str | None = None,
) -> str:
    """ローカルファイルを Box にアップロードし、共有リンクを返す。

    Args:
        local_path: アップロードするローカルファイルのパス
        filename: Box 上のファイル名（省略時は local_path のベース名）
        folder_id: アップロード先 folder_id（省略時は自動解決）

    Returns:
        成功時: "アップロード完了: {filename}\n  file_id: ...\n  共有リンク: ..."
        失敗時: "エラー: ..."
    """
    from utils.box_cli import box_upload_or_version, box_get_or_create_shared_link

    path = Path(local_path)
    if not path.exists():
        return f"エラー: ファイルが見つかりません: {local_path}"
    if not path.is_file():
        return f"エラー: パスがファイルではありません: {local_path}"

    fname = filename or path.name

    if not folder_id:
        folder_id = resolve_box_folder_id()
    if not folder_id:
        return (
            "エラー: Box アップロード先 folder_id が設定されていません。\n"
            "環境変数 PM_BOX_FOLDER_ID、または argus_config.yaml の "
            "`box.upload_folder_id` を設定してください。"
        )

    def _log(msg: str) -> None:
        logger.info("[box_upload] %s", msg)

    try:
        file_id = box_upload_or_version(path, folder_id, fname, log=_log)
        url = box_get_or_create_shared_link(file_id, log=_log)
        return (
            f"アップロード完了: {fname}\n"
            f"  file_id: {file_id}\n"
            f"  共有リンク: {url}"
        )
    except Exception as e:
        logger.exception("[box_upload] アップロード失敗")
        return f"エラー: Box アップロードに失敗しました — {e}"


# =========================================================================== #
#  Slack: メッセージ投稿
# =========================================================================== #

def slack_post_message(
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> str:
    """Slack チャンネルにメッセージを投稿する。

    Args:
        channel: 投稿先チャンネル ID
        text: Markdown 形式の本文（Slack mrkdwn に自動変換）
        thread_ts: スレッド返信先の ts（省略時は新規投稿）

    Returns:
        成功時: "メッセージを投稿しました: {channel}\n  タイムスタンプ: ..."
        失敗時: "エラー: ..."
    """
    client = _get_slack_bot_client()
    if not client:
        return "エラー: SLACK_BOT_TOKEN が設定されていません。"

    from utils.slack_post import _to_slack_mrkdwn, _split_mrkdwn_to_blocks

    mrkdwn = _to_slack_mrkdwn(text)
    blocks = _split_mrkdwn_to_blocks(mrkdwn)
    kwargs: dict = {"channel": channel, "blocks": blocks, "text": mrkdwn[:40000]}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    try:
        resp = client.chat_postMessage(**kwargs)
        ts = resp.get("ts", "")
        return f"メッセージを投稿しました: {channel}\n  タイムスタンプ: {ts}"
    except Exception as e:
        logger.exception("[slack_post] 投稿失敗")
        return f"エラー: Slack 投稿に失敗しました — {e}"


# =========================================================================== #
#  Slack Canvas: コンテンツ更新
# =========================================================================== #

def canvas_post_content(
    canvas_id: str,
    content: str,
) -> str:
    """Slack Canvas の内容を置き換える（既存は全削除→新コンテンツ挿入）。

    Args:
        canvas_id: Canvas ID（F で始まる文字列）
        content: Markdown 形式の新しいコンテンツ

    Returns:
        成功時: "Canvas を更新しました: {canvas_id}\n  更新サイズ: ..."
        失敗時: "エラー: ..."
    """
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "エラー: SLACK_USER_TOKEN が設定されていません。"

    from utils.canvas_utils import sanitize_for_canvas, post_to_canvas

    sanitized = sanitize_for_canvas(content)
    try:
        post_to_canvas(canvas_id, sanitized)
        return f"Canvas を更新しました: {canvas_id}\n  更新サイズ: {len(sanitized)} 文字"
    except Exception as e:
        logger.exception("[canvas_post] Canvas 更新失敗")
        return f"エラー: Canvas 更新に失敗しました — {e}"
