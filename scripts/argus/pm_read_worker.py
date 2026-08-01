#!/usr/bin/env python3
"""pm_read_worker.py — Read Plane の調査プロセス（docs/security-architecture.md §3.2・Phase 5）

`/argus-investigate` の調査部分を**外部サービスのトークンを持たない別プロセス**で走らせる。
結果は stdout に JSON で返し、Slack / Box / Canvas への投稿は親（Write Plane）が行う。

**なぜプロセスを分けるのか（P1）**

同一プロセスでは「読取能力」と「送信能力」が同居する。コード上の約束（allow-list）は
同じプロセス内のコードから外せるので境界にならない。**分離はプロセスと秘密情報の分割で
強制する** — Read Plane は Slack / Box のトークンを environ に持たず、`net_guard` の
平面制限で write_plane の宛先が許可集合に入らない。

**この対策が証明すること（P10）**

  - 調査プロセスの environ に外部サービスのトークンが**存在しない**こと（起動時に自己検査）
  - 調査プロセスから slack.com / box.com への**名前解決が enforce で遮断される**こと

**証明しないこと**

  - OS レベルの到達不能性。`net_guard` は同一プロセスの socket フックであり、
    subprocess（`box` CLI 等）や、フックを外すコードには効かない。**iptables /
    network namespace による強制が本来の姿**で、ここは運用制約との調整が要る（§3.2）。
  - DB 鍵は持つ（Read Plane の仕事は pm.db を読むこと）。**盗まれて困る度合いは
    トークンと変わらない** — 分離の対象は「外へ出す能力」であって「読む能力」ではない。

使い方（通常は親プロセスから spawn される）:

    PYTHONPATH=scripts python3 scripts/argus/pm_read_worker.py \
        --investigate "M3の進捗状況" --days 14

環境変数:

    ARGUS_READ_WORKER_STRICT=0  自己検査で落とさない（移行期の退避路。既定は 1）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))

# Read Plane から必ず除く環境変数。前方一致で判定する。
# **DB 鍵（PM_DB_KEY）と LLM のトークンは残す** — 前者は仕事に要り、後者は
# read_plane の宛先（理究 / RiVault / embedding）にしか使えないため。
FORBIDDEN_ENV_PREFIXES = (
    "SLACK_",
    "BOX_",
    "PM_BOX_",
    "PM_REPORT_CANVAS",
    "FISH_TTS_",
    "GITHUB_",
)


def scrub_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Read Plane に渡してよい環境変数だけを残した dict を返す。

    親プロセス側で spawn 前に使う。**除くのは「外へ出す能力」に繋がるものだけ**で、
    DB 鍵と LLM トークンは残す（§3.2 のトークン3分割で Read Plane は
    「外部サービスのトークンを持たない」と定義されている）。
    """
    src = dict(os.environ if env is None else env)
    return {k: v for k, v in src.items()
            if not any(k.startswith(p) for p in FORBIDDEN_ENV_PREFIXES)}


def forbidden_env_present(env: dict[str, str] | None = None) -> list[str]:
    """Read Plane に居てはいけない環境変数の名前を返す（空なら健全）。"""
    src = dict(os.environ if env is None else env)
    return sorted(k for k in src if any(k.startswith(p) for p in FORBIDDEN_ENV_PREFIXES))


def self_check(strict: bool = True) -> list[str]:
    """起動時の自己検査。トークンが残っていれば strict で落とす。

    **親が scrub を忘れても、子が気づいて止まる。** 分離が「呼び出し側の作法」に
    依存すると、1箇所の書き忘れで静かに崩れるため（P8 と同じ理由）。
    """
    leaked = forbidden_env_present()
    if leaked and strict:
        raise SystemExit(
            "[READ-PLANE] 外部サービスのトークンが environ に残っています: "
            f"{leaked}\n分離が成立していないため起動を拒否します"
            "（退避するなら ARGUS_READ_WORKER_STRICT=0）"
        )
    return leaked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read Plane の調査ワーカー（トークンを持たないプロセス）"
    )
    parser.add_argument("--investigate", required=True, help="調査内容")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--index-name", default="pm")
    parser.add_argument("--no-encrypt", action="store_true")
    parser.add_argument("--self-check-only", action="store_true",
                        help="自己検査だけ行って終了する（分離の確認用）")
    args = parser.parse_args(argv)

    strict = os.environ.get("ARGUS_READ_WORKER_STRICT", "1").strip() not in ("0", "false", "no")
    leaked = self_check(strict=strict)

    if args.self_check_only:
        print(json.dumps({
            "ok": not leaked,
            "leaked_env": leaked,
            "netguard_mode": os.environ.get("ARGUS_NETGUARD", "warn"),
            "netguard_planes": os.environ.get("ARGUS_NETGUARD_PLANES", ""),
        }, ensure_ascii=False))
        return 0 if not leaked else 1

    # 調査本体は既存の実装をそのまま使う（分離はプロセスと environ の側で行う）
    from argus.pm_argus_agent import run_investigate_for_worker

    try:
        answer = run_investigate_for_worker(
            question=args.investigate, days=args.days, max_steps=args.max_steps,
            timeout=args.timeout, index_name=args.index_name, no_encrypt=args.no_encrypt,
        )
    except Exception as e:  # 親に伝えるため JSON で返す（stderr にも出す）
        print(f"[READ-PLANE] 調査に失敗しました: {e}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "answer": answer}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# 親（Write Plane）側から使うヘルパ
# --------------------------------------------------------------------------- #

def run_in_read_plane(
    question: str, *, days: int = 30, max_steps: int = 20, timeout: int = 480,
    index_name: str = "pm", no_encrypt: bool = False, spawn_timeout: int = 900,
) -> str:
    """調査を Read Plane のサブプロセスで実行し、回答テキストを返す。

    親は Slack / Box のトークンを持ったままでよい（投稿するのは親の仕事）。
    **子には渡さない** — `scrub_env()` で除き、`ARGUS_NETGUARD_PLANES=read_plane` で
    write_plane の宛先を許可集合から外す。子は起動時に自己検査で二重に確認する。

    失敗時は RuntimeError。**親は失敗を握りつぶさないこと** — 分離が壊れたまま
    in-process にフォールバックすると、分離が「あるように見えて無い」状態になる。
    """
    import subprocess

    env = scrub_env()
    env["PYTHONPATH"] = str(_SCRIPT_DIR)
    env.setdefault("ARGUS_NETGUARD", "enforce")
    env["ARGUS_NETGUARD_PLANES"] = "read_plane"

    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--investigate", question, "--days", str(days),
        "--max-steps", str(max_steps), "--timeout", str(timeout),
        "--index-name", index_name,
    ]
    if no_encrypt:
        cmd.append("--no-encrypt")

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=spawn_timeout)
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        raise RuntimeError(f"Read Plane ワーカーが応答を返しませんでした: {proc.stderr[-300:]}")
    try:
        payload = json.loads(line[-1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Read Plane ワーカーの応答が JSON ではありません: {e}") from e
    if not payload.get("ok"):
        raise RuntimeError(f"Read Plane ワーカーが失敗しました: {payload.get('error')}")
    return payload["answer"]


if __name__ == "__main__":
    raise SystemExit(main())
