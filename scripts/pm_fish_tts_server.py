#!/usr/bin/env python3
"""fish-speech API サーバの起動ラッパー。

pm_daemon.sh から `bash scripts/pm_daemon.sh start fish` で起動する。

FISH_SPEECH_REPO が未設定の場合、このスクリプトの親ディレクトリの隣にある
fish-speech ディレクトリを自動検出する（../fish-speech）。

環境変数:
    FISH_SPEECH_REPO         fish-speech リポジトリのパス
                             （未設定時は ../fish-speech を自動検出）
    FISH_TTS_HOST            listen アドレス（デフォルト: 0.0.0.0:8080）
    FISH_LLAMA_CHECKPOINT    LLaMA チェックポイントのパス
                             （デフォルト: {FISH_SPEECH_REPO}/checkpoints/s2-pro）
    FISH_DECODER_CHECKPOINT  Decoder チェックポイントのパス
                             （デフォルト: {FISH_LLAMA_CHECKPOINT}/codec.pth）
    FISH_DECODER_CONFIG      Decoder 設定名（デフォルト: modded_dac_vq）
    FISH_DEVICE              推論デバイス（デフォルト: cuda）
    FISH_HALF                fp16 を使用（デフォルト: 0）
    FISH_COMPILE             コンパイル有効化（デフォルト: 0）
    FISH_WORKERS             ワーカー数（デフォルト: 1）

Usage:
    bash scripts/pm_daemon.sh start fish
    bash scripts/pm_daemon.sh stop fish
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# このスクリプトの隣 (scripts/) → その親 (repo root) → 隣の fish-speech
_DEFAULT_FISH_REPO = Path(__file__).resolve().parent.parent.parent / "fish-speech"


def main() -> int:
    repo = os.environ.get("FISH_SPEECH_REPO", "")
    if not repo:
        if _DEFAULT_FISH_REPO.is_dir():
            repo = str(_DEFAULT_FISH_REPO)
            print(f"[INFO] FISH_SPEECH_REPO 未設定: {repo} を使用します", file=sys.stderr)
        else:
            print(
                "[ERROR] FISH_SPEECH_REPO 環境変数が設定されていません。\n"
                "例: export FISH_SPEECH_REPO=/path/to/fish-speech",
                file=sys.stderr,
            )
            return 1

    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        print(f"[ERROR] FISH_SPEECH_REPO が存在しません: {repo_path}", file=sys.stderr)
        return 1

    api_server = repo_path / "tools" / "api_server.py"
    if not api_server.is_file():
        print(f"[ERROR] api_server.py が見つかりません: {api_server}", file=sys.stderr)
        return 1

    listen = os.environ.get("FISH_TTS_HOST", "0.0.0.0:8080").replace("http://", "")
    llama_ckpt = os.environ.get(
        "FISH_LLAMA_CHECKPOINT",
        str(repo_path / "checkpoints" / "s2-pro"),
    )
    decoder_ckpt = os.environ.get(
        "FISH_DECODER_CHECKPOINT",
        str(Path(llama_ckpt) / "codec.pth"),
    )
    decoder_config = os.environ.get("FISH_DECODER_CONFIG", "modded_dac_vq")
    device = os.environ.get("FISH_DEVICE", "cuda")
    workers = os.environ.get("FISH_WORKERS", "1")
    half = os.environ.get("FISH_HALF", "0") == "1"
    compile_flag = os.environ.get("FISH_COMPILE", "0") == "1"

    # PYTHONPATH に fish-speech リポジトリを追加（tools/ が import できるよう）
    pythonpath = os.environ.get("PYTHONPATH", "")
    parts = [str(repo_path)] + ([pythonpath] if pythonpath else [])
    os.environ["PYTHONPATH"] = ":".join(parts)

    cmd = [
        sys.executable,
        str(api_server),
        "--listen", listen,
        "--llama-checkpoint-path", llama_ckpt,
        "--decoder-checkpoint-path", decoder_ckpt,
        "--decoder-config-name", decoder_config,
        "--device", device,
        "--workers", workers,
    ]
    if half:
        cmd.append("--half")
    if compile_flag:
        cmd.append("--compile")

    print(f"[INFO] fish-speech API サーバを起動します: {' '.join(cmd)}", file=sys.stderr)

    # exec で置き換え（このプロセスの PID が pm_daemon.sh の PID ファイルに記録される）
    os.execv(sys.executable, cmd)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
