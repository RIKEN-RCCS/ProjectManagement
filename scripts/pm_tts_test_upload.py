#!/usr/bin/env python3
"""音声合成 → DM アップロード → 🗑️ リアクション削除フローのテスト用 CLI。

本番の _post_argus_voice と同じ経路で 1 件のテスト mp3 を実行者の DM に投稿し、
voice_uploads.db に記録する。投稿後 Slack 上で🗑️リアクションを付けて
reaction_added ハンドラの動作を確認できる。

使い方:
    # MD ファイルを合成して投稿
    source ~/.secrets/slack_tokens.sh
    python3 scripts/pm_tts_test_upload.py --user-id U01234ABCD data/sample.md

    # 既存 mp3 をそのまま投稿（合成時間を省略してリアクション削除だけ確認したい場合）
    python3 scripts/pm_tts_test_upload.py --user-id U01234ABCD --mp3 /tmp/sample.mp3

    # 任意のチャンネル（自分の DM 以外）に投稿
    python3 scripts/pm_tts_test_upload.py --channel C0123ABCD data/sample.md

必要な環境変数:
    SLACK_BOT_TOKEN  Bot Token (xoxb-...)。通常は ~/.secrets/slack_tokens.sh で設定済み

ヒント:
    投稿後に Slack でメッセージに :wastebasket: リアクションを付けると、
    pm_qa_server.py の reaction_added ハンドラがファイルとメッセージを削除します。
    そのために Bot デーモンが起動している必要があります（scripts/pm_daemon.sh start qa）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

import pm_tts
import voice_uploads


def _resolve_dm_channel(client, user_id: str) -> str:
    resp = client.conversations_open(users=[user_id])
    return resp["channel"]["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("input", nargs="?", type=Path, help="入力 Markdown ファイル（合成対象）")
    src.add_argument("--mp3", type=Path, help="合成をスキップして既存 mp3 を投稿")
    ap.add_argument("--user-id", help="DM 投稿先の user_id (Uxxxxxxxx)。--channel を指定する場合は不要")
    ap.add_argument("--channel", help="任意の channel_id を投稿先に指定")
    ap.add_argument("--speaker", type=int, default=pm_tts.DEFAULT_SPEAKER, help=f"VOICEVOX speaker ID (default: {pm_tts.DEFAULT_SPEAKER})")
    ap.add_argument("--speed", type=float, default=pm_tts.DEFAULT_SPEED, help=f"再生速度 (default: {pm_tts.DEFAULT_SPEED})")
    ap.add_argument("--mode", choices=["auto", "minutes", "priority"], default="auto", help="--summarize 時の分割モード")
    ap.add_argument("--no-summarize", action="store_true", help="LLM 要約を行わずそのまま合成")
    ap.add_argument("--kind", default="test", help="voice_uploads.db に記録する種別ラベル (default: test)")
    args = ap.parse_args()

    if not args.user_id and not args.channel:
        print("--user-id または --channel のいずれかを指定してください", file=sys.stderr)
        return 1

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        print("SLACK_BOT_TOKEN が設定されていません。`source ~/.secrets/slack_tokens.sh` を実行してください。", file=sys.stderr)
        return 1

    try:
        from slack_sdk import WebClient
    except ImportError:
        print("slack_sdk が見つかりません。", file=sys.stderr)
        return 1

    client = WebClient(token=bot_token)

    # 投稿先チャンネル決定
    if args.channel:
        channel_id = args.channel
        print(f"投稿先: 指定チャンネル {channel_id}", file=sys.stderr)
    else:
        channel_id = _resolve_dm_channel(client, args.user_id)
        print(f"投稿先: {args.user_id} との DM ({channel_id})", file=sys.stderr)

    # mp3 を準備
    cleanup_mp3 = False
    if args.mp3:
        mp3_path = args.mp3.resolve()
        if not mp3_path.is_file():
            print(f"mp3 が見つかりません: {mp3_path}", file=sys.stderr)
            return 1
    else:
        if not args.input.is_file():
            print(f"入力 MD が見つかりません: {args.input}", file=sys.stderr)
            return 1
        mp3_path = Path(f"/tmp/pm_tts_test_{os.getpid()}.mp3")
        cleanup_mp3 = True
        print(f"合成中: {args.input} -> {mp3_path}", file=sys.stderr)
        pm_tts.synthesize_markdown(
            args.input.read_text(encoding="utf-8"),
            mp3_path,
            speaker=args.speaker,
            speed=args.speed,
            summarize=not args.no_summarize,
            summarize_mode=args.mode,
            quiet=False,
        )

    credit = pm_tts.credit_line(args.speaker)
    initial_comment = (
        ":sound: テスト音声版です。\n"
        f"_{credit}_\n"
        "削除する場合はこのメッセージに :wastebasket: リアクションを付けてください。"
    )
    title = f"pm_tts test ({mp3_path.name})"

    print(f"アップロード中: {mp3_path} -> ch={channel_id}", file=sys.stderr)
    upload_resp = client.files_upload_v2(
        channel=channel_id,
        file=str(mp3_path),
        filename=mp3_path.name,
        title=title,
        initial_comment=initial_comment,
    )

    file_id = ""
    message_ts = ""
    files_field = upload_resp.get("files") or []
    if files_field:
        f0 = files_field[0]
        file_id = f0.get("id") or f0.get("file", {}).get("id", "")
        shares = f0.get("shares") or {}
        for visibility in ("public", "private"):
            visible = shares.get(visibility) or {}
            if channel_id in visible:
                ts_list = visible[channel_id]
                if ts_list:
                    message_ts = ts_list[0].get("ts", "")
                    break

    if file_id and message_ts:
        voice_uploads.record_upload(
            message_ts=message_ts,
            channel_id=channel_id,
            file_id=file_id,
            user_id=args.user_id or "",
            kind=args.kind,
            title=title,
        )
        print(f"記録完了: file_id={file_id} message_ts={message_ts}", file=sys.stderr)
    else:
        print("warn: file_id / message_ts を取得できず、voice_uploads.db に未記録", file=sys.stderr)

    print("\nSlack で投稿メッセージに :wastebasket: リアクションを付けて削除を確認してください。")
    print("デーモンが起動していない場合は: scripts/pm_daemon.sh start qa")

    if cleanup_mp3:
        try:
            mp3_path.unlink()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
