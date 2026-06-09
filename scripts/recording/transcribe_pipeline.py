"""
transcribe_pipeline.py - Whisper文字起こし → LLM議事録生成パイプライン

Minutes/slack_bot/pipeline.py を ProjectManagement に吸収したもの。
config.py への依存を除去し、PM側の環境変数体系（LOCAL_LLM_URL 等）を使用する。

環境変数:
    AUDIO_SAVE_DIR      ダウンロード・中間ファイルの保存先（デフォルト: /tmp/whisper_audio）
    LOCAL_LLM_URL     vLLM エンドポイント（デフォルト: http://localhost:8000/v1）
    （モデル名は vLLM /v1/models から自動取得）
    SLACK_BOT_TOKEN     ファイルダウンロード用 Bot Token
    HUGGING_FACE_TOKEN  PyAnnote モデルダウンロード用（任意）
"""

import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
_RECORDING_DIR = Path(__file__).resolve().parent

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
    if os.environ.get("ARGUS_PREFER_RIVAULT") == "1":
        url = os.environ.get("RIVAULT_URL", "").rstrip("/")
        token = os.environ.get("RIVAULT_TOKEN", "")
        if url and token:
            return url
    return os.environ.get("LOCAL_LLM_URL", "http://localhost:8000/v1")


def _get_vllm_model() -> str:
    if os.environ.get("ARGUS_PREFER_RIVAULT") == "1":
        model = os.environ.get("RIVAULT_MODEL", "").strip()
        if model:
            return model
    from cli_utils import detect_vllm_model
    return detect_vllm_model(_get_vllm_api_base())


def _get_hugging_face_token() -> str:
    return os.environ.get("HUGGING_FACE_TOKEN", "")


def _post(client, channel_id, thread_ts, text):
    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)


def _post_minutes_voice(client, channel_id: str, thread_ts: str, minutes_path: Path) -> None:
    """議事録 Markdown を音声化してスレッドにアップロードする。

    失敗してもパイプライン全体を落とさないよう、内側で例外を握りつぶす。
    無効化したい場合は MINUTES_VOICE=0 を設定。
    """
    try:
        import sys as _sys
        if str(_SCRIPT_DIR) not in _sys.path:
            _sys.path.insert(0, str(_SCRIPT_DIR))
        import pm_tts
    except ImportError as exc:
        logger.warning(f"voice: pm_tts import 失敗 ({exc})、スキップ")
        return

    mp3_path = minutes_path.with_suffix(".mp3")
    speaker_id = pm_tts.DEFAULT_SPEAKER
    try:
        _post(client, channel_id, thread_ts, "音声版を生成しています...")
        markdown = minutes_path.read_text(encoding="utf-8")
        pm_tts.synthesize_markdown(
            markdown,
            mp3_path,
            speaker=speaker_id,
            summarize=True,
            summarize_mode="minutes",
            quiet=True,
        )
        credit = pm_tts.credit_line(speaker_id)
        client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            file=str(mp3_path),
            filename=mp3_path.name,
            title=f"{minutes_path.stem} (音声版)",
            initial_comment=(
                ":sound: 議事録の音声版（要約・短縮）です。\n"
                f"_{credit}_\n"
                "削除する場合はこのメッセージに :wastebasket: リアクションを付けてください。"
            ),
        )
    except Exception as exc:
        logger.exception(f"voice: 失敗 {exc}")
        try:
            _post(client, channel_id, thread_ts, f":warning: 音声版の生成に失敗しました: {exc}")
        except Exception:
            pass
    finally:
        try:
            if mp3_path.exists():
                mp3_path.unlink()
        except Exception:
            pass


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


def download_vtt(client, channel_id, audio_filename, thread_ts=None,
                 max_retries: int = 6, retry_delay: float = 10.0):
    """音声ファイルと同名の .vtt をチャンネルから検索・ダウンロードする。見つからなければ None。

    検索順:
      1) {stem}.transcript.vtt / {stem}.vtt
      2) 解像度サフィックス (_3840x2160 等) を剥がした stem
      3) ブラウザ重複DLサフィックス ((1), (12) 等) を剥がした stem
      4) {base_name}.transcript {paren}.vtt / {base_name}{paren}.transcript.vtt
         （例: "Recording (1).m4a" → "Recording.transcript (1).vtt"）

    VTT ファイルは音声後に別途アップロードされる可能性があるため、リトライに対応。
    未検出時、thread_ts が指定されていればスレッドに警告を投稿する。
    """
    import re

    stem = Path(audio_filename).stem

    # stem の派生バリエーションを生成（重複は除外）
    stem_nores = re.sub(r"_\d+x\d+$", "", stem)
    stem_nodup = re.sub(r" ?\(\d+\)$", "", stem)
    stem_bare = re.sub(r" ?\(\d+\)$", "", stem_nores)
    stem_variants = []
    for s in (stem, stem_nores, stem_nodup, stem_bare):
        if s and s not in stem_variants:
            stem_variants.append(s)

    vtt_candidates = []
    for s in stem_variants:
        vtt_candidates.extend([f"{s}.transcript.vtt", f"{s}.vtt"])

    # 括弧を含むファイル名の場合、Zoom の "{base}.transcript (N).vtt" 形式にも対応
    match = re.match(r"^(.+?)\s*(\(\d+\))$", stem)
    if match:
        base_name, paren = match.groups()
        vtt_candidates.extend([
            f"{base_name}.transcript {paren}.vtt",
            f"{base_name}{paren}.transcript.vtt",
        ])

    try:
        for attempt in range(1, max_retries + 1):
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

            if attempt < max_retries:
                logger.info(f"VTT 未検出 (試行 {attempt}/{max_retries}, 検索: {', '.join(vtt_candidates[:2])}) — {retry_delay:.0f}秒後にリトライ")
                time.sleep(retry_delay)

        logger.info(f"VTT ファイルはチャンネルに存在しません（検索: {', '.join(vtt_candidates[:2])}、{max_retries}回試行）")
        if thread_ts is not None:
            try:
                _post(
                    client, channel_id, thread_ts,
                    f":warning: VTT ファイルが見つかりませんでした（`{audio_filename}`）。"
                    f"Whisper のみで担当者を推定します。\n"
                    f"検索した stem: {', '.join(stem_variants)}",
                )
            except Exception as e:
                logger.warning(f"VTT 未検出通知の投稿に失敗: {e}")
        return None
    except Exception as e:
        logger.warning(f"VTT ダウンロードでエラー（スキップ）: {e}")
        return None


def run_slide_ocr(video_path: Path) -> tuple[Path | None, Path | None]:
    """動画（mp4 等）からスライドOCRを実行し (slide_context.md, terminology.txt) を返す。

    失敗・該当なしの場合はそれぞれ None。mp4 以外は即 (None, None)。
    """
    if video_path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        return None, None

    import shutil as _shutil
    work_dir = Path(_get_audio_save_dir()) / f"slides_{video_path.stem}"
    # 前回の残骸があると新規フレームと混在してインデックスがズレるので事前に掃除
    if work_dir.exists():
        _shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    context_path = work_dir / "slide_context.md"
    terminology_path = work_dir / "terminology.txt"

    cmd = [
        sys.executable, "-u",  # -u: stdout/stderr をラインバッファにして即時フラッシュ
        str(_RECORDING_DIR / "slide_ocr.py"),
        str(video_path),
        "--out-dir", str(work_dir),
        "--context-out", str(context_path),
        "--terminology-out", str(terminology_path),
        "--verbose",
    ]
    logger.info(f"slide_ocr 起動: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as e:
        logger.warning(f"slide_ocr 起動失敗: {e}")
        return None, None

    try:
        for line in proc.stdout:
            logger.info("[slide_ocr] %s", line.rstrip())
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.warning("slide_ocr.py タイムアウト — 文脈なしで続行")
        return None, None

    if proc.returncode != 0:
        logger.warning(f"slide_ocr.py 失敗 (exit={proc.returncode})")
        return None, None

    ctx = context_path if context_path.exists() and context_path.stat().st_size > 0 else None
    term = terminology_path if terminology_path.exists() and terminology_path.stat().st_size > 0 else None
    logger.info(f"スライドOCR完了: context={ctx} terminology={term}")
    return ctx, term


def run_whisper(audio_path, terminology_path: Path | None = None):
    """Singularityコンテナ内でffmpeg変換 + whisper_vad.py を実行する。"""
    audio_save_dir = _get_audio_save_dir()
    hugging_face_token = _get_hugging_face_token()
    transcript_path = audio_path.with_suffix(".md")
    extra_opt = f"--initial-prompt-extra '{terminology_path}'" if terminology_path else ""

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

ffmpeg -y -i '{audio_path}' -ac 1 -ar 16000 -vn -af "highpass=f=1000" -sample_fmt s16 '{wav_path}'
python3 '{_RECORDING_DIR}/whisper_vad.py' '{wav_path}' '{transcript_path}' {extra_opt}
""")

    env = os.environ.copy()
    env["SINGULARITY_BIND"] = "/lvs0"

    # GPU OOM は再実行で成功することが多い（vLLM 等の他プロセスのメモリ占有が変動するため）。
    # OOM パターンを検出した場合のみリトライする。
    max_retries = int(os.environ.get("WHISPER_MAX_RETRIES", "3"))
    retry_sleep = int(os.environ.get("WHISPER_RETRY_SLEEP", "30"))
    oom_pattern = re.compile(
        r"out of memory|CUDA out of memory|OutOfMemoryError|CUDA error: out of memory",
        re.IGNORECASE,
    )

    last_exit = 1
    last_tail = ""
    try:
        for attempt in range(1, max_retries + 1):
            logger.info(f"Whisper 試行 {attempt} / {max_retries}")
            output_lines = []
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

            last_exit = proc.returncode
            last_tail = "\n".join(output_lines[-50:])
            if last_exit == 0:
                break

            joined = "\n".join(output_lines[-200:])
            is_oom = bool(oom_pattern.search(joined)) or last_exit == 137
            if is_oom and attempt < max_retries:
                logger.warning(
                    f"OOM 検出 (exit={last_exit})。{retry_sleep}s 待機後にリトライします"
                )
                time.sleep(retry_sleep)
                continue
            if is_oom:
                logger.error(f"OOM のため {max_retries} 回リトライしましたが成功しませんでした")
            else:
                logger.error(f"OOM 以外の失敗 (exit={last_exit})。リトライしません")
            break

        if last_exit != 0:
            raise RuntimeError(
                f"Whisperエラー (exit={last_exit}):\n"
                f"```{last_tail[-2000:]}```"
            )
    finally:
        run_sh.unlink(missing_ok=True)
        wav_path = audio_path.with_suffix(".wav")
        wav_path.unlink(missing_ok=True)

    return transcript_path


def run_minutes(transcript_path, client, channel_id, thread_ts,
                vtt_path=None, slide_context_path=None, consensus_n=3):
    """generate_minutes_local.py をコンテナ外のPythonで実行し、議事録パスを返す。

    consensus_n >= 2 の場合は --consensus N を渡して self-consistency サンプリング
    を有効化する（Stage 2 / Stage 3 を N 回サンプリング → embedding クラスタリング
    + LLM 集約）。
    """
    audio_save_dir = _get_audio_save_dir()
    vllm_api_base = _get_vllm_api_base()
    try:
        vllm_model = _get_vllm_model()
        logger.info(f"vLLM モデル: {vllm_model}")
    except Exception as e:
        logger.error(f"vLLM モデル取得エラー (API: {vllm_api_base}): {e}", exc_info=True)
        raise

    minutes_dir = Path(audio_save_dir) / "minutes"
    minutes_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(_RECORDING_DIR / "generate_minutes_local.py"),
           str(transcript_path),
           "--model", vllm_model,
           "--output", str(minutes_dir),
           "--multi-stage", "--chunk-minutes", "10",
           "--max-tokens", "16384"]
    # ARGUS_PREFER_RIVAULT=1 かつ RIVAULT_URL/TOKEN が揃っている場合は --url を渡さない
    # (load_local_llm_endpoint() が RIVAULT_URL を返すため --url で上書きすると壊れる)
    # それ以外は常に --url を明示する（子プロセスの環境変数に依存しない）
    rivault_active = (
        os.environ.get("ARGUS_PREFER_RIVAULT") == "1"
        and os.environ.get("RIVAULT_URL", "").strip()
        and os.environ.get("RIVAULT_TOKEN", "").strip()
    )
    if not rivault_active:
        cmd.extend(["--url", vllm_api_base])
    if vtt_path:
        cmd.extend(["--vtt", str(vtt_path)])
    if slide_context_path:
        cmd.extend(["--slide-context", str(slide_context_path)])
    if consensus_n and consensus_n >= 2:
        cmd.extend(["--consensus", str(consensus_n)])

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


def run_pipeline(client, channel_id, filename, thread_ts, consensus_n=3):
    """ダウンロード → 文字起こし → 議事録生成 → Slack投稿 の全体パイプライン。

    consensus_n >= 2 の場合は議事録生成を self-consistency モードで実行する。
    """
    audio_path = None
    transcript_path = None
    vtt_path = None
    try:
        audio_path = download_audio(client, channel_id, filename)
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)

        vtt_path = download_vtt(client, channel_id, filename, thread_ts=thread_ts)
        vtt_msg = ""
        if vtt_path:
            vtt_msg = f"\nVTT ファイル検出: `{vtt_path.name}`（話者情報を議事録に活用します）"

        consensus_msg = ""
        if consensus_n and consensus_n >= 2:
            consensus_msg = (
                f"\n:repeat: Self-consistency 有効: N={consensus_n}"
                f"（生成時間が ~5-8x になります）"
            )

        _post(client, channel_id, thread_ts,
              f"ダウンロード完了: `{filename}` ({file_size_mb:.1f} MB){vtt_msg}{consensus_msg}\n"
              f"文字起こしを開始します...")

        slide_context_path, terminology_path = run_slide_ocr(audio_path)
        if slide_context_path:
            _post(client, channel_id, thread_ts,
                  "スライドOCR完了: 固有名詞・用語を Whisper prompt と議事録生成に反映します")

        transcript_path = run_whisper(audio_path, terminology_path=terminology_path)
        _post(client, channel_id, thread_ts,
              f"文字起こし完了: `{transcript_path.name}`\n"
              f"要約を開始します（数十分かかる場合があります）...")

        minutes_path = run_minutes(transcript_path, client, channel_id, thread_ts,
                                   vtt_path=vtt_path,
                                   slide_context_path=slide_context_path,
                                   consensus_n=consensus_n)

        minutes_size = minutes_path.stat().st_size if minutes_path and minutes_path.exists() else 0
        if minutes_size < 2:
            raise RuntimeError(
                f"議事録が空です（{minutes_size} bytes）: {minutes_path}\n"
                "LLM の呼び出しに失敗した可能性があります。ログを確認してください。"
            )

        client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            file=str(minutes_path),
            filename=minutes_path.name,
            title=minutes_path.stem,
            initial_comment="要約完了しました。",
        )

        if os.environ.get("MINUTES_VOICE", "1") != "0":
            _post_minutes_voice(client, channel_id, thread_ts, minutes_path)

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
