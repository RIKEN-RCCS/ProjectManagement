#!/usr/bin/env python3
"""output_broker.py — 出力ブローカー（docs/security-architecture.md §4.2、層3）

**輸送層の1段手前**に置く検査点。宛先は識別子で選ぶだけで、モデルは構築できない。

**なぜツール層ではなく輸送層なのか（P8）**

実際に外へ出ている量の大半は、モデルのツール呼び出しを経由しない**自動投稿パイプライン**
（毎朝のブリーフィング cron、Patrol Agent）である。ツール層に検査を置くと、そちらが
構造的に漏れる。この誤りを設計で3回繰り返したので原則にした。

**この対策が証明すること（P10）**

  - ブローカーを通った送信について、宛先が allow-list 内であること
  - ブローカーを通った送信に、active な canary 文字列が含まれていないこと
  - 送信の記録が `tool_calls`（追記専用・ハッシュ連鎖）に残ること

**証明しないこと**

  - **ブローカーを通らない送信が無いこと。** Slack は SDK 直叩きが 25 箇所あり、
    まだ移送していない。**現時点の被覆率は Canvas と Box のみ**であり、
    「ブローカーがあるから守られている」と読んではいけない
  - 内容が安全であること。canary と機械的な特徴（ゼロ幅文字等）しか見ない。
    自然な散文に符号化されたものは通る（TrojanStego 型）

**テキスト以外の出口の扱い**

`/argus-narrate` のように成果物が音声・動画になる経路では、canary もエントロピーも
効かない。**合成前のテキストをブローカーに通し、通過したテキストだけを合成に渡す**。
原則として「テキスト以外の成果物は、生成元テキストの検査をもって代える」。
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))

logger = logging.getLogger("output_broker")

_TARGETS_PATH = _REPO_ROOT / "config" / "egress_targets.yaml"
_ARGUS_CONFIG = _REPO_ROOT / "data" / "argus_config.yaml"

# ゼロ幅文字（TrojanStego 型の埋め込みに使われる）。検出したら送信を止める。
_ZERO_WIDTH_RE = re.compile(r"[​‌‍⁠﻿]")

_targets_cache: dict | None = None


class EgressBlocked(RuntimeError):
    """ブローカーが送信を拒否した（宛先違反・canary 検出・方針違反）。"""


class EgressPendingApproval(RuntimeError):
    """人間の承認が要る宛先（`requires_human_approval: true`）。"""


def load_targets(path: Path | None = None) -> dict:
    """egress_targets.yaml を読む（宛先の方針。実値は含まない）。"""
    global _targets_cache
    p = path or _TARGETS_PATH
    if _targets_cache is not None and path is None:
        return _targets_cache
    data: dict = {}
    if p.is_file():
        try:
            data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("targets") or {}
        except Exception:
            logger.exception("[BROKER] %s の読み込みに失敗しました", p)
    else:
        logger.warning("[BROKER] 宛先定義がありません: %s", p)
    if path is None:
        _targets_cache = data
    return data


def _resolve_config_ref(ref: str) -> str:
    """`data/argus_config.yaml` のドット区切りキーを実値に解決する。

    **実値はリポジトリに置かない**（public なので）。ここでだけ読む。
    """
    if not _ARGUS_CONFIG.is_file():
        raise EgressBlocked(f"argus_config.yaml が見つかりません: {_ARGUS_CONFIG}")
    cfg = yaml.safe_load(_ARGUS_CONFIG.read_text(encoding="utf-8")) or {}
    cur: Any = cfg
    for part in ref.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise EgressBlocked(f"argus_config.yaml に {ref!r} がありません")
        cur = cur[part]
    if not isinstance(cur, str) or not cur:
        raise EgressBlocked(f"{ref!r} の値が文字列ではありません")
    return cur


def scan_payload(content: str, canary_tokens: list[str] | None = None) -> list[str]:
    """送信前の機械的な検査。問題があれば理由の一覧を返す（空なら通過）。

    **canary の検出はここが検知点①**（§4.3）。canary は本来どこにも現れない文字列なので、
    出ようとしていること自体が異常であり、**記録ではなく遮断**する。
    """
    reasons = []
    for tok in canary_tokens or []:
        if tok and tok in content:
            reasons.append(f"canary トークンが含まれています: {tok}")
    if _ZERO_WIDTH_RE.search(content):
        reasons.append("ゼロ幅文字が含まれています（不可視の埋め込みの可能性）")
    return reasons


def _active_canaries(conn) -> list[str]:
    if conn is None:
        return []
    try:
        from db_utils import active_canary_tokens
        return active_canary_tokens(conn)
    except Exception:
        logger.exception("[BROKER] canary 台帳の読み取りに失敗しました（検査は続行）")
        return []


def post(
    target: str, content: str, *, conn=None, source: str = "",
    title: str = "", dry_run: bool = False,
) -> dict:
    """allow-list の宛先へ送る。**唯一の出口にすることが目的**。

    戻り値は送信の記録（dict）。拒否は例外で返す — **戻り値で成否を返すと
    呼び出し側が無視できてしまう**ため。
    """
    targets = load_targets()
    spec = targets.get(target)
    if not spec:
        raise EgressBlocked(
            f"宛先 {target!r} は egress_targets.yaml にありません"
            f"（選べるのは: {', '.join(sorted(targets)) or 'なし'}）"
        )

    reasons = scan_payload(content, _active_canaries(conn))
    if not spec.get("free_text_allowed", True) and content.strip():
        reasons.append("この宛先は自由文を許可していません（free_text_allowed: false）")

    outcome = "ok"
    block_reason = None
    if reasons:
        outcome, block_reason = "blocked", "; ".join(reasons)
    elif spec.get("requires_human_approval"):
        outcome, block_reason = "blocked", "人間の承認が必要な宛先です"

    # 記録は成否にかかわらず残す（拒否した事実こそ残す価値がある）
    _record(conn, target, spec, content, outcome, block_reason, source)

    if outcome == "blocked":
        if spec.get("requires_human_approval") and not reasons:
            raise EgressPendingApproval(f"[BROKER] {target}: {block_reason}")
        raise EgressBlocked(f"[BROKER] {target}: {block_reason}")

    if dry_run:
        return {"target": target, "outcome": "dry_run", "bytes": len(content.encode())}

    dest = _resolve_config_ref(spec["config_ref"])
    _dispatch(spec["type"], dest, content, title=title)
    return {"target": target, "outcome": "ok", "bytes": len(content.encode())}


def _record(conn, target: str, spec: dict, content: str, outcome: str,
            block_reason: str | None, source: str) -> None:
    """送信（および拒否）を tool_calls に記録する（§4.4 と同じ台帳を使う）。

    新しいテーブルを作らないのは、**ハッシュ連鎖を1本に保つ**ため。連鎖が分かれると
    「どちらが先か」が言えなくなる。
    """
    if conn is None:
        return
    try:
        from db_utils import record_tool_call
        record_tool_call(
            conn, session_id=source or "broker", seq=0, plane="egress",
            tool_name=f"broker:{target}",
            args={"type": spec.get("type", ""), "visibility": spec.get("visibility", ""),
                  "chars": len(content)},
            outcome=outcome, result=None if outcome != "ok" else content[:0],
            block_reason=block_reason,
        )
    except Exception:
        logger.exception("[BROKER] tool_calls への記録に失敗しました（送信判断は継続）")


def _dispatch(kind: str, dest: str, content: str, *, title: str = "") -> None:
    """実際の輸送。**ここから下は既存のファネルをそのまま使う。**"""
    if kind == "canvas":
        from utils.canvas_utils import post_to_canvas
        post_to_canvas(dest, content)
    elif kind == "box":
        import tempfile

        from utils.box_cli import box_upload_or_version
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / (title or "argus_output.md")
            f.write_text(content, encoding="utf-8")
            box_upload_or_version(str(f), dest)
    elif kind == "slack":
        # Slack はファネルが存在せず SDK 直叩きが 25 箇所ある（§4.2）。移送は未完了。
        raise EgressBlocked(
            "slack 宛先はまだブローカー経由に移送していません"
            "（SDK 直叩き 25 箇所の移送が先。docs/security-architecture.md §4.2）"
        )
    else:
        raise EgressBlocked(f"未知の輸送種別: {kind!r}")
