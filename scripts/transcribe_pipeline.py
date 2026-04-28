"""
transcribe_pipeline.py - Whisper文字起こし → LLM議事録生成パイプライン

Minutes/slack_bot/pipeline.py を ProjectManagement に吸収したもの。
config.py への依存を除去し、PM側の環境変数体系（OPENAI_API_BASE 等）を使用する。

環境変数:
    AUDIO_SAVE_DIR      ダウンロード・中間ファイルの保存先（デフォルト: /tmp/whisper_audio）
    OPENAI_API_BASE     vLLM エンドポイント（デフォルト: http://localhost:8000/v1）
    （モデル名は vLLM /v1/models から自動取得）
    SLACK_BOT_TOKEN     ファイルダウンロード用 Bot Token
    HUGGING_FACE_TOKEN  PyAnnote モデルダウンロード用（任意）
"""

import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent

# アーキテクチャに応じてSIFファイルパスを選択（trans.sh準拠）
_ARCH = platform.machine()
if _ARCH == "aarch64":
    SIF_FILE = Path("/lvs0/rccs-sdt/hikaru.inoue/cpu_aarch64/singularity/whisper.sif")
elif _ARCH == "x86_64":
    SIF_FILE = Path("/lvs0/rccs-sdt/hikaru.inoue/cpu_amd64/singularity/whisper.sif")
else:
    SIF_FILE = Path("/tmp/whisper.sif")  # フォールバック（実行時エラーになる）

# PyAnnoteモデルの永続キャッシュ
HF_HOME = Path.home() / ".cache" / "huggingface"


def _get_audio_save_dir() -> str:
    return os.environ.get("AUDIO_SAVE_DIR", "/tmp/whisper_audio")


def _get_vllm_api_base() -> str:
    return os.environ.get("OPENAI_API_BASE", "http://localhost:8000/v1")


def _get_vllm_model() -> str:
    from cli_utils import detect_vllm_model
    return detect_vllm_model(_get_vllm_api_base())


def _get_hugging_face_token() -> str:
    return os.environ.get("HUGGING_FACE_TOKEN", "")


def _post(client, channel_id, thread_ts, text):
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)


def _download_slack_file(url, save_path):
    """Slack ファイルをダウンロードして save_path に保存する。"""
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    dl = requests.get(
        url,
        headers={"Authorization": f"Bearer {bot_token}"},
        stream=True,
        timeout=300,
    )
    dl.raise_for_status()
    with open(save_path, "wb") as f:
        for chunk in dl.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    return save_path


def download_audio(client, channel_id, filename,
                   max_retries: int = 12, retry_delay: float = 30.0):
    """チャンネル内のファイルを検索してダウンロードし、保存パスを返す。"""
    audio_save_dir = _get_audio_save_dir()

    matched = []
    for attempt in range(1, max_retries + 1):
        response = client.files_list(channel=channel_id, types="all")
        files = response.get("files", [])
        logger.info(f"files_list (試行 {attempt}/{max_retries}): {len(files)} 件取得 (channel={channel_id})")
        for f in files:
            logger.info(f"  - name={f.get('name')!r} id={f.get('id')} created={f.get('created')}")
        matched = [f for f in files if f.get("name") == filename]
        if matched:
            break
        if attempt < max_retries:
            logger.warning(f"`{filename}` が見つかりません。{retry_delay:.0f} 秒後にリトライします... ({attempt}/{max_retries})")
            time.sleep(retry_delay)

    if not matched:
        raise FileNotFoundError(f"`{filename}` がチャンネルに見つかりませんでした。")

    url = matched[0].get("url_private_download")
    if not url:
        raise RuntimeError(f"`{filename}` のダウンロードURLが取得できませんでした。")

    os.makedirs(audio_save_dir, exist_ok=True)
    save_path = Path(audio_save_dir) / filename
    _download_slack_file(url, save_path)

    return save_path


def download_vtt(client, channel_id, audio_filename):
    """音声ファイルと同名の .vtt をチャンネルから検索・ダウンロードする。見つからなければ None。

    検索順: {stem}.transcript.vtt → {stem}.vtt（先に見つかった方を使用）
    """
    stem = Path(audio_filename).stem
    vtt_candidates = [f"{stem}.transcript.vtt", f"{stem}.vtt"]

    try:
        response = client.files_list(channel=channel_id, types="all")
        files = response.get("files", [])
        file_by_name = {f.get("name"): f for f in files}

        for vtt_filename in vtt_candidates:
            if vtt_filename not in file_by_name:
                continue
            url = file_by_name[vtt_filename].get("url_private_download")
            if not url:
                logger.warning(f"VTT ファイル `{vtt_filename}` のダウンロードURLが取得できません")
                continue
            audio_save_dir = _get_audio_save_dir()
            os.makedirs(audio_save_dir, exist_ok=True)
            save_path = Path(audio_save_dir) / vtt_filename
            _download_slack_file(url, save_path)
            logger.info(f"VTT ファイルをダウンロード: {save_path}")
            return save_path

        logger.info(f"VTT ファイルはチャンネルに存在しません（検索: {', '.join(vtt_candidates)}）")
        return None
    except Exception as e:
        logger.warning(f"VTT ダウンロードでエラー（スキップ）: {e}")
        return None


def run_whisper(audio_path):
    """Singularityコンテナ内でffmpeg変換 + whisper_vad.py を実行する。"""
    audio_save_dir = _get_audio_save_dir()
    hugging_face_token = _get_hugging_face_token()
    transcript_path = audio_path.with_suffix(".md")

    # FFmpeg 4.x → 6.x の soname マッピング（コンテナ内 FFmpeg 6.x vs torchcodec 要求 4.x）
    _FFMPEG_SHIMS = {
        "libavutil.so.56":    "/usr/lib/aarch64-linux-gnu/libavutil.so.58",
        "libavcodec.so.58":   "/usr/lib/aarch64-linux-gnu/libavcodec.so.60",
        "libavformat.so.58":  "/usr/lib/aarch64-linux-gnu/libavformat.so.60",
        "libavdevice.so.58":  "/usr/lib/aarch64-linux-gnu/libavdevice.so.60",
        "libavfilter.so.7":   "/usr/lib/aarch64-linux-gnu/libavfilter.so.9",
        "libswscale.so.5":    "/usr/lib/aarch64-linux-gnu/libswscale.so.7",
        "libswresample.so.3": "/usr/lib/aarch64-linux-gnu/libswresample.so.4",
    }
    lib_shim_dir = Path(audio_save_dir) / "lib_shim"
    lib_shim_dir.mkdir(parents=True, exist_ok=True)
    for name, target in _FFMPEG_SHIMS.items():
        symlink = lib_shim_dir / name
        if not symlink.is_symlink():
            symlink.symlink_to(target)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", dir=audio_save_dir, delete=False
    ) as f:
        run_sh = Path(f.name)
        wav_path = audio_path.with_suffix(".wav")
        f.write(f"""\
. /.venv/bin/activate
[ -f ~/.secrets/hf_tokens.sh ] && . ~/.secrets/hf_tokens.sh
export HUGGING_FACE_TOKEN="${{HUGGING_FACE_TOKEN:-{hugging_face_token}}}"
export HF_HOME="{HF_HOME}"
export LD_LIBRARY_PATH="{lib_shim_dir}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
export PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync,expandable_segments:True

ffmpeg -y -i {audio_path} -ac 1 -ar 16000 -vn -af "highpass=f=1000" -sample_fmt s16 {wav_path}
python3 {_SCRIPT_DIR}/whisper_vad.py {wav_path} {transcript_path}
""")

    env = os.environ.copy()
    env["SINGULARITY_BIND"] = "/lvs0"

    output_lines = []
    try:
        proc = subprocess.Popen(
            ["singularity", "run", "--nv", str(SIF_FILE), "sh", str(run_sh)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            logger.info("[whisper] %s", line)
            output_lines.append(line)
        proc.wait(timeout=7200)

        if proc.returncode != 0:
            tail = "\n".join(output_lines[-50:])
            raise RuntimeError(
                f"Whisperエラー (exit={proc.returncode}):\n"
                f"```{tail[-2000:]}```"
            )
    finally:
        run_sh.unlink(missing_ok=True)
        wav_path = audio_path.with_suffix(".wav")
        wav_path.unlink(missing_ok=True)

    return transcript_path


def run_minutes(transcript_path, client, channel_id, thread_ts,
                vtt_path=None):
    """generate_minutes_local.py をコンテナ外のPythonで実行し、議事録パスを返す。"""
    audio_save_dir = _get_audio_save_dir()
    vllm_api_base = _get_vllm_api_base()
    vllm_model = _get_vllm_model()

    minutes_dir = Path(audio_save_dir) / "minutes"
    minutes_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(_SCRIPT_DIR / "generate_minutes_local.py"),
           str(transcript_path),
           "--model", vllm_model,
           "--url", vllm_api_base,
           "--output", str(minutes_dir),
           "--multi-stage", "--chunk-minutes", "10",
           "--max-tokens", "16384"]
    if vtt_path:
        cmd.extend(["--vtt", str(vtt_path)])

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stderr_lines = []

    def _read_stderr():
        for line in proc.stderr:
            line = line.rstrip()
            logger.warning("[minutes] %s", line)
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    minutes_path = None
    total_chunks = None
    posted_milestones = set()

    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        logger.info("[minutes] %s", line)

        if "チャンクに分割" in line:
            try:
                total_chunks = int(line.split(":")[1].strip().split()[0])
            except Exception:
                pass
            _post(client, channel_id, thread_ts,
                  f"Stage 1 開始: 文字起こしを {total_chunks} チャンクに分割して抽出中...")

        elif "抽出完了" in line and total_chunks:
            try:
                frac = line.split("チャンク")[1].split("抽出完了")[0].strip()
                current = int(frac.split("/")[0])
                pct = current * 100 // total_chunks
                milestone = (pct // 25) * 25
                if milestone > 0 and milestone not in posted_milestones:
                    posted_milestones.add(milestone)
                    _post(client, channel_id, thread_ts,
                          f"Stage 1: チャンク抽出 {milestone}% 完了 ({current}/{total_chunks})")
            except Exception:
                pass

        elif "議事録を統合生成中" in line:
            _post(client, channel_id, thread_ts, "Stage 2: 議事内容を統合生成中...")

        elif "決定事項・アクションアイテムを生成中" in line:
            _post(client, channel_id, thread_ts, "Stage 3: 決定事項・アクションアイテムを抽出中...")

        elif line.startswith("[完了]"):
            try:
                minutes_path = Path(line.split(None, 1)[1].strip())
            except Exception:
                pass

    proc.wait(timeout=7200)
    stderr_thread.join()

    if proc.returncode != 0:
        tail = "\n".join(stderr_lines[-30:])
        raise RuntimeError(
            f"要約エラー (exit={proc.returncode}):\n"
            f"```{tail[-2000:]}```"
        )

    if minutes_path is None or not minutes_path.exists():
        raise RuntimeError("議事録ファイルのパスが取得できませんでした。\n"
                           f"stderr: {chr(10).join(stderr_lines[-10:])}")

    return minutes_path


def run_pipeline(client, channel_id, filename, thread_ts):
    """ダウンロード → 文字起こし → 議事録生成 → Slack投稿 の全体パイプライン。"""
    audio_path = None
    transcript_path = None
    vtt_path = None
    try:
        audio_path = download_audio(client, channel_id, filename)
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)

        vtt_path = download_vtt(client, channel_id, filename)
        vtt_msg = ""
        if vtt_path:
            vtt_msg = f"\nVTT ファイル検出: `{vtt_path.name}`（話者情報を議事録に活用します）"

        _post(client, channel_id, thread_ts,
              f"ダウンロード完了: `{filename}` ({file_size_mb:.1f} MB){vtt_msg}\n"
              f"文字起こしを開始します...")

        transcript_path = run_whisper(audio_path)
        _post(client, channel_id, thread_ts,
              f"文字起こし完了: `{transcript_path.name}`\n"
              f"要約を開始します（数十分かかる場合があります）...")

        minutes_path = run_minutes(transcript_path, client, channel_id, thread_ts,
                                   vtt_path=vtt_path)

        client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            file=str(minutes_path),
            filename=minutes_path.name,
            title=minutes_path.stem,
            initial_comment="要約完了しました。",
        )

    except Exception as e:
        logger.exception("Pipeline failed")
        _post(client, channel_id, thread_ts, f"エラーが発生しました:\n{e}")
        raise
    else:
        # 正常完了時のみ削除
        if audio_path:
            audio_path.unlink(missing_ok=True)
        if transcript_path:
            transcript_path.unlink(missing_ok=True)
        if vtt_path:
            vtt_path.unlink(missing_ok=True)
