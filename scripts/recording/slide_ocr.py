#!/usr/bin/env python3
"""
slide_ocr.py — mp4 動画からスライドフレームを抽出しマルチモーダルLLMで OCR する。

用途:
  1. Whisper の initial_prompt 用に固有名詞リスト（terminology.txt）を生成
  2. generate_minutes_local.py の Stage 1/2/3 プロンプトに同梱する
     スライド文脈（slide_context.md）を生成

使い方 (CLI):
  python3 slide_ocr.py VIDEO.mp4 --out-dir /tmp/slides \
      --terminology-out /tmp/terminology.txt --context-out /tmp/slide_context.md

環境変数:
  LOCAL_LLM_URL  マルチモーダル対応 vLLM エンドポイント（必須）
  LOCAL_LLM_TOKEN   API キー（省略時 "dummy"）
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logger = logging.getLogger(__name__)


# 会議録画の映像は多様: プレゼンスライド・スプレッドシート・ブラウザ・コード
# エディタ・チャット画面など。いずれのケースでも「その時点で画面に映っている
# テキスト情報をすべて抜き出す」ことを最優先にする。
MEETING_FRAME_OCR_PROMPT = """\
この画像は会議中に画面共有された1フレームです。内容は以下のいずれかまたは混在している可能性があります:
- プレゼンテーションのスライド
- Google スプレッドシート / Excel / 表形式のデータ
- Web ブラウザの画面 / Web ページ / ダッシュボード
- ソースコード / ターミナル / IDE
- PDF / ドキュメント / 議事録
- Slack / チャット / メール
- 図表 / グラフ / ホワイトボード

# 重要: アプリケーション UI 要素の除外
画面上に「LibreOffice / PowerPoint / Excel 等のアプリ自身のメニューバー・
ステータスバー・タスクバー・サイドバー (例: 'ファイル(F) 編集(E) 表示(V)' / 'スライド N / N' /
'文字スタイル' / フォント名 / ズーム倍率 / 時計表示 / 通知ポップアップ /
ニュース速報 / バッテリー警告 / OS タスクバーのアプリ一覧)」が見えていても、
それらは抽出しないでください。

抽出するのはあくまで **コンテンツとして表示されている本文** (スライド本体・表の中身・
ドキュメント本文・コード・チャット本文) のテキストのみです。

種別に関わらず、コンテンツ本文上に読み取れるテキストをできるだけ漏れなく抽出して
Markdown で出力してください。

出力ルール:
1. タイトル・見出しらしき部分は `##` / `###` で表現
2. 箇条書きはそのまま `-` で列挙
3. 表は Markdown テーブル `| A | B |` で再現（列がはっきりしない場合は箇条書きでも可）
4. 図・グラフ・スクリーンショットなど抽出困難な要素は `[図: 概要]` の形式で1行メモ
5. 日本語・英語はそのまま保持。読めない文字は `[不読]` と記述
6. URL やファイルパスも抽出対象
7. 画面構成の説明や「これはスプレッドシートです」等のメタ説明は不要。テキスト内容そのものを出力してください
8. 画面に意味のあるテキストが全く無い場合のみ `[テキストなし]` の一行だけを返してください

# 重要: 会議と無関係な映像の除外
会議の議事録補助が目的のため、以下のいずれかに該当する場合は抽出せず `[会議無関係]` の一行だけを返してください:
- ショッピングサイト・オークション・フリマ (Yahoo!オークション、Amazon、楽天、メルカリ等)
- SNS・動画サイト・ニュースサイトの個人的閲覧画面 (Twitter/X、YouTube、Netflix 等)
- Chrome/Edge 等のデフォルトタブ・新規タブ画面・ブックマーク一覧
- OS のデスクトップ・ファイルエクスプローラ・ゴミ箱
- 個人メール (Gmail 受信トレイ等) や個人チャット
- 明らかに会議の議題 (HPC・富岳NEXT・GPU・ベンチマーク・Benchpark・アプリケーション開発・コデザイン等) と無関係な内容

判断に迷う場合は抽出する。

Markdown だけを出力してください（前置き・後書き不要）。"""


# --------------------------------------------------------------------------- #
# フレーム抽出
# --------------------------------------------------------------------------- #
def extract_slide_frames(
    video_path: Path,
    out_dir: Path,
    scene_threshold: float = 0.25,
    max_frames: int = 200,
) -> list[Path]:
    """ffmpeg の scene detect で連続フレーム差分が閾値を超えた時点のフレームを抽出する。

    雑談会議（スライドなし）では 0〜数枚に収まる。max_frames を超える場合は全フレーム
    抽出した上で時系列に均等間引きして max_frames 枚に圧縮する（先頭だけ拾って後半を
    捨てることは避ける）。

    テキストスクロール型の画面共有では scene detect が効かず少数枚になるが、
    固定間隔フォールバックは無関係なフレーム（顔・ギャラリー等）を拾うため行わない。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg が見つかりません。スライド抽出をスキップします")
        return []

    pattern = out_dir / "slide_%04d.png"
    # 高速化: scene detect を downscale 後のフレームで行う（4K → 1280 幅で ~9x 高速）
    # 会議のスライド説明は 1 枚あたり数十秒かかるため 10 秒に 1 フレームで十分
    # （1/10 fps = 0.1 fps）。scene detect の処理量もさらに減る
    # -threads 0 で ffmpeg に全 CPU を使わせる
    vf = (
        f"scale=1280:-2,"
        f"fps=1/10,"
        f"select='gt(scene,{scene_threshold})'"
    )
    cmd = [
        "ffmpeg", "-y",
        "-threads", "0",
        "-i", str(video_path),
        "-vf", vf,
        "-vsync", "vfr",
        "-progress", "pipe:1", "-nostats",
        str(pattern),
    ]
    logger.info(f"ffmpeg scene detect 開始: {video_path} (threshold={scene_threshold})")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        last_time = ""
        last_written = 0
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("out_time="):
                last_time = line.split("=", 1)[1]
            elif line.startswith("progress="):
                written = len(list(out_dir.glob("slide_*.png")))
                if written > last_written:
                    logger.info(f"ffmpeg 進捗: written={written} t={last_time}")
                    last_written = written
                if line == "progress=end":
                    logger.info(f"ffmpeg: 完了 written={written} t={last_time}")
        proc.wait(timeout=3600)
        if proc.returncode != 0:
            logger.warning(f"ffmpeg exit={proc.returncode}")
            return []
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"ffmpeg scene detect 失敗: {e}")
        return []

    frames = sorted(out_dir.glob("slide_*.png"))
    total = len(frames)
    logger.info(f"スライドフレーム抽出: {total} 枚 (threshold={scene_threshold})")

    if total > max_frames:
        # 均等間引き: インデックス [0, total) から max_frames 個を等間隔で選ぶ
        step = total / max_frames
        picked_idx = {int(i * step) for i in range(max_frames)}
        kept: list[Path] = []
        for i, f in enumerate(frames):
            if i in picked_idx:
                kept.append(f)
            else:
                try:
                    f.unlink()
                except OSError:
                    pass
        logger.info(f"抽出フレームを均等間引き: {total} → {len(kept)} 枚 (max_frames={max_frames})")
        frames = kept

    return frames


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
def ocr_slides(
    frames: list[Path],
    base_url: str | None = None,
    max_workers: int = 8,
) -> list[str]:
    """各フレームをマルチモーダルLLMでOCRし Markdown のリストを返す（入力順を維持）。

    max_workers 個のスレッドで並列に OCR する。vLLM 側が連続バッチをサポートするため
    I/O 待ち中に次のリクエストを投げることでスループットが向上する。失敗は空文字で埋める。
    """
    if not frames:
        return []
    if base_url is None:
        # OCR は vision 対応モデルが必要。RIVAULT_OCR_MODEL が明示指定されている場合のみ
        # RiVault を使う。それ以外は ARGUS_PREFER_RIVAULT=1 でもローカル vLLM を使う
        # (DeepSeek-V4-Flash 等テキスト専用モデルへの 400 エラーを防ぐため)。
        if os.environ.get("RIVAULT_OCR_MODEL", "").strip() and os.environ.get("RIVAULT_URL"):
            base_url = os.environ["RIVAULT_URL"].rstrip("/")
            token = os.environ.get("RIVAULT_TOKEN", "")
            if token:
                os.environ["LOCAL_LLM_TOKEN"] = token
        else:
            base_url = os.environ.get("LOCAL_LLM_URL")
    if not base_url:
        logger.warning("LOCAL_LLM_URL 未設定のため OCR をスキップします")
        return [""] * len(frames)

    from pm_box_crawl import ocr_slide_image

    import time as _time
    completed_count = 0
    start_ts = _time.time()

    def _one(idx_frame: tuple[int, Path]) -> tuple[int, str]:
        idx, frame = idx_frame
        t0 = _time.time()
        try:
            md = ocr_slide_image(frame, base_url, prompt=MEETING_FRAME_OCR_PROMPT)
        except Exception as e:
            logger.warning(f"  OCR 失敗 ({frame.name}): {e}")
            md = None
        elapsed = _time.time() - t0
        logger.info(
            f"OCR done {idx + 1}/{len(frames)} ({frame.name}, {elapsed:.1f}s, "
            f"{len(md or '')} chars)"
        )
        return idx, md or ""

    results: list[str] = [""] * len(frames)
    workers = max(1, min(max_workers, len(frames)))
    logger.info(f"OCR 開始: {len(frames)} 枚 × 並列 {workers}, endpoint={base_url}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, md in pool.map(_one, enumerate(frames)):
            results[idx] = md
            completed_count += 1
            if completed_count % max(1, workers) == 0 or completed_count == len(frames):
                rate = completed_count / max(1e-3, _time.time() - start_ts)
                eta = (len(frames) - completed_count) / max(1e-3, rate)
                logger.info(
                    f"OCR 進捗: {completed_count}/{len(frames)} "
                    f"({rate:.2f} img/s, ETA {eta:.0f}s)"
                )
    total_elapsed = _time.time() - start_ts
    logger.info(f"OCR 完了: {len(frames)} 枚 / {total_elapsed:.1f}s")
    return results


# --------------------------------------------------------------------------- #
# 文脈テキスト生成
# --------------------------------------------------------------------------- #
_SKIP_MARKERS = ("[会議無関係]", "[テキストなし]")


def _is_skippable(md: str) -> bool:
    stripped = md.strip()
    if not stripped:
        return True
    # 1行目が除外マーカーで始まる場合はスキップ（LLM が前置きを付けた場合にも対応）
    first_line = stripped.splitlines()[0].strip()
    for marker in _SKIP_MARKERS:
        if marker in first_line:
            return True
    return False


def build_slide_context(slide_mds: list[str], max_chars: int = 15000) -> str:
    """Stage 1/2/3 プロンプトに同梱する整形済みテキストを作る。

    全体が max_chars を超える場合は各スライドを先頭部のみに切り詰める。
    `[会議無関係]` / `[テキストなし]` とマークされたフレームは除外する。
    """
    non_empty = [
        (i, md.strip()) for i, md in enumerate(slide_mds, 1)
        if not _is_skippable(md)
    ]
    dropped = len(slide_mds) - len(non_empty)
    if dropped:
        logger.info(f"スライド文脈から除外: {dropped} 枚（会議無関係・テキストなし）")
    if not non_empty:
        return ""

    total_raw = sum(len(md) for _, md in non_empty)
    if total_raw <= max_chars:
        blocks = [f"### スライド {i}\n{md}" for i, md in non_empty]
        return "\n\n".join(blocks)

    # 切り詰め: 各スライドに割ける文字数を均等配分
    per_slide = max(200, max_chars // len(non_empty))
    blocks = []
    for i, md in non_empty:
        if len(md) > per_slide:
            md = md[:per_slide].rstrip() + "…"
        blocks.append(f"### スライド {i}\n{md}")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# 用語抽出
# --------------------------------------------------------------------------- #
# 英大文字略語（GPU, MONAKA-X, NVLink-C2C 等）
_RE_ACRONYM = re.compile(r'\b[A-Z][A-Z0-9]{1,}(?:[-/][A-Z0-9]+)*\b')
# キャメルケース・固有英名（Benchpark, FrontFlow 等）
_RE_CAMEL = re.compile(r'\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b')
# カタカナ語（4文字以上）
_RE_KATAKANA = re.compile(r'[ァ-ヺー]{4,}')

_STOPWORDS = {
    "PDF", "PPT", "PPTX", "OCR", "JPG", "PNG", "URL", "HTTP", "HTTPS",
    "YES", "NO", "TRUE", "FALSE", "TODO",
}


def extract_terminology(slide_mds: Iterable[str], max_terms: int = 80) -> list[str]:
    """スライド Markdown から Whisper initial_prompt 用の固有名詞候補を抽出する。"""
    seen: dict[str, None] = {}  # 出現順保持のため dict を set 代わりに使う
    for md in slide_mds:
        if _is_skippable(md):
            continue
        for pat in (_RE_ACRONYM, _RE_CAMEL, _RE_KATAKANA):
            for m in pat.findall(md):
                if m in _STOPWORDS:
                    continue
                if len(m) < 2:
                    continue
                seen.setdefault(m, None)
                if len(seen) >= max_terms:
                    return list(seen.keys())
    return list(seen.keys())


# --------------------------------------------------------------------------- #
# 高レベルラッパ
# --------------------------------------------------------------------------- #
def process_video(
    video_path: Path,
    work_dir: Path,
    scene_threshold: float = 0.25,
    max_frames: int = 200,
    base_url: str | None = None,
    max_workers: int = 8,
) -> tuple[str, list[str]]:
    """動画 → フレーム抽出 → OCR → (slide_context, terminology) を返す。

    work_dir 配下に frames/ を作成しフレーム PNG を残す（デバッグ用）。
    """
    frames_dir = work_dir / "frames"
    frames = extract_slide_frames(video_path, frames_dir, scene_threshold, max_frames)
    if not frames:
        return "", []

    slide_mds = ocr_slides(frames, base_url, max_workers=max_workers)
    slide_context = build_slide_context(slide_mds)
    terminology = extract_terminology(slide_mds)
    return slide_context, terminology


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="動画からスライドOCR情報を抽出する")
    parser.add_argument("video", help="入力動画ファイル (.mp4 等)")
    parser.add_argument("--out-dir", default=None,
                        help="作業ディレクトリ（省略時は一時ディレクトリ）")
    parser.add_argument("--scene-threshold", type=float, default=0.25,
                        help="ffmpeg scene detect の閾値（デフォルト: 0.25）")
    parser.add_argument("--max-frames", type=int, default=200,
                        help="OCR に渡すフレーム数の上限。超過時は時系列に均等間引き（デフォルト: 200）")
    parser.add_argument("--max-workers", type=int, default=8,
                        help="OCR 並列ワーカー数（デフォルト: 8）")
    parser.add_argument("--context-out", default=None,
                        help="slide_context.md の出力パス")
    parser.add_argument("--terminology-out", default=None,
                        help="terminology.txt の出力パス（1行1語）")
    parser.add_argument("--ocr-only", action="store_true",
                        help="VIDEO を既存のフレーム PNG ディレクトリとして扱い OCR のみ実行")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"[ERROR] 入力が見つかりません: {video_path}", file=sys.stderr)
        return 1

    if args.ocr_only:
        if not video_path.is_dir():
            print(f"[ERROR] --ocr-only はディレクトリを指定してください: {video_path}",
                  file=sys.stderr)
            return 1
        frames = sorted(video_path.glob("*.png"))
        slide_mds = ocr_slides(frames, max_workers=args.max_workers)
        slide_context = build_slide_context(slide_mds)
        terminology = extract_terminology(slide_mds)
    else:
        work_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="slide_ocr_"))
        work_dir.mkdir(parents=True, exist_ok=True)
        slide_context, terminology = process_video(
            video_path, work_dir,
            scene_threshold=args.scene_threshold,
            max_frames=args.max_frames,
            max_workers=args.max_workers,
        )

    if args.context_out:
        Path(args.context_out).write_text(slide_context, encoding="utf-8")
        print(f"[INFO] slide_context: {args.context_out} ({len(slide_context)} 字)",
              file=sys.stderr)
    else:
        print(slide_context)

    if args.terminology_out:
        Path(args.terminology_out).write_text(
            "\n".join(terminology) + ("\n" if terminology else ""),
            encoding="utf-8",
        )
        print(f"[INFO] terminology: {args.terminology_out} ({len(terminology)} 語)",
              file=sys.stderr)
    else:
        print("\n--- terminology ---", file=sys.stderr)
        for t in terminology:
            print(t, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
