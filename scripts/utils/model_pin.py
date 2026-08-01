"""model_pin.py — 供給網の固定（docs/security-architecture.md §4.6）

`config/model_pin.yaml` に宣言したモデルだけを本番経路で使わせる。

**この対策が証明すること・しないこと（P10）**

証明する:
  - 本番経路で使われたモデル id が、宣言された集合の中にあること
  - 宣言されていないモデルに黙って切り替わっていないこと（enforce 時）

証明しない:
  - **チェックポイントが宣言どおりのものであること。** OpenAI 互換の `/v1/models` は
    `id` しか返さないため、revision（sha）を Argus 側から取得する手段がない。
    `declared_*` は**運用主体の申告の記録**であって検証結果ではない（R12）。
  - `trust_remote_code` や engine の実際の設定。これもサービス側にあり取得できない。

したがって pin の実効は「id の一致」と「**申告値の変更が git の diff に現れること**」の
2つに限られる。モデル更新の通知を運用主体から受ける取り決め（Phase 0）が対で必要になる。

モード（環境変数 `ARGUS_MODEL_PIN`）:

    warn (既定) — pin 外のモデルでも通す。WARNING でログに残す
    enforce     — pin 外・`production: false`・`verified_at` が null のモデルを
                  `ModelPinError` で拒否する（fail-closed）
    off         — 照合しない（テスト用）

CLI:

    python3 scripts/utils/model_pin.py --list
        宣言内容と、検証済み／申告のみの別を表示する

    python3 scripts/utils/model_pin.py --check
        endpoint_env から解決した各エンドポイントの `/v1/models` を叩き、
        宣言した `served_model_name` が実在するかを照合する
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.request
from pathlib import Path

import yaml

# net_guard の import（import 時の install() 副作用のため）。
# 直接実行（`python3 scripts/utils/model_pin.py`）では sys.path[0] が scripts/utils に
# なるため `from utils import ...` が解決できない。db_utils.py と同じフォールバックを置く。
try:
    from utils import net_guard  # noqa: F401
except ImportError:
    import sys as _sys
    _scripts_dir = str(Path(__file__).resolve().parent.parent)
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    from utils import net_guard  # noqa: F401

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PIN_PATH = _REPO_ROOT / "config" / "model_pin.yaml"
_MODE_ENV = "ARGUS_MODEL_PIN"
_PIN_PATH_ENV = "ARGUS_MODEL_PIN_PATH"

_pin_cache: tuple[Path, float | None, dict] | None = None
# 同じモデルで毎回ログを出さないための記録（プロセス内）
_warned: set[str] = set()


class ModelPinError(RuntimeError):
    """pin に反するモデルを本番経路で使おうとした（enforce 時）。"""


def _mode() -> str:
    m = os.environ.get(_MODE_ENV, "warn").strip().lower()
    return m if m in ("warn", "enforce", "off") else "warn"


def _pin_path() -> Path:
    override = os.environ.get(_PIN_PATH_ENV)
    return Path(override) if override else _DEFAULT_PIN_PATH


def load_pin(path: Path | None = None) -> dict:
    """model_pin.yaml を mtime キャッシュ付きで読む。読めなければ空 dict。"""
    global _pin_cache
    p = path or _pin_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    if _pin_cache and _pin_cache[0] == p and _pin_cache[1] == mtime:
        return _pin_cache[2]
    data: dict = {}
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("[MODELPIN] %s の読み込みに失敗しました", p)
            data = {}
    else:
        logger.warning("[MODELPIN] pin ファイルがありません: %s", p)
    _pin_cache = (p, mtime, data)
    return data


def models(path: Path | None = None) -> dict:
    return (load_pin(path).get("models") or {})


def _find(model_id: str, path: Path | None = None) -> tuple[str, dict] | None:
    """モデル id（served_model_name もキー名も可）でエントリを引く。"""
    for key, entry in models(path).items():
        if not isinstance(entry, dict):
            continue
        if model_id == key or model_id == entry.get("served_model_name"):
            return key, entry
    return None


def assert_model_allowed(model_id: str, *, production: bool = True,
                         path: Path | None = None) -> None:
    """モデルが本番経路で使ってよいものか検査する。

    enforce では以下を拒否する。warn では WARNING を出して通す。

      - pin に無いモデル
      - `production: false` のモデル（評価専用）を本番経路で使う
      - `verified_at` が null のモデル（id 照合が済んでいない）

    **`declared_*` の値は判定に使わない** — 検証できないものを根拠にすると、
    「pin が通った＝安全」という誤った確信を与えるため（P10）。
    """
    mode = _mode()
    if mode == "off" or not model_id:
        return

    found = _find(model_id, path)
    reason = None
    if found is None:
        reason = f"モデル {model_id!r} は model_pin.yaml に宣言されていません"
    else:
        _key, entry = found
        if production and not entry.get("production"):
            reason = f"モデル {model_id!r} は評価専用（production: false）です"
        elif entry.get("verified_at") in (None, ""):
            reason = (
                f"モデル {model_id!r} は id 照合が未実施です"
                "（model_pin.py --check の後 verified_at を記入してください）"
            )

    if reason is None:
        return
    msg = f"[MODELPIN] {reason}"
    if mode == "enforce":
        raise ModelPinError(msg)
    if model_id not in _warned:
        _warned.add(model_id)
        logger.warning("%s（warn モードのため続行）", msg)


def fetch_served_models(base_url: str, api_key: str | None = None,
                        timeout: int = 10) -> list[str]:
    """`/v1/models` の id 一覧を返す。取得できなければ例外。"""
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        url += "/models"
    if api_key is None:
        # base_url に対応したトークンを選ぶ。RIKYU と RiVault はトークンが別なので、
        # 「RIVAULT_TOKEN を先に試す」固定順だと RIKYU に対して常に 401 になる
        # （llm.py:detect_vllm_model で実際に 58 回記録されていた同型のバグ）。
        # 遅延 import なのは llm.py がこのモジュールを import しているため（循環回避）。
        from utils.llm import _token_for_base
        api_key = _token_for_base(url)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read())
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]


def check_endpoints(path: Path | None = None, timeout: int = 10) -> list[dict]:
    """宣言した served_model_name が実際のエンドポイントに存在するか照合する。

    endpoint_env が未設定のエントリは skip（環境によって使わないモデルがあるため）。
    """
    results = []
    for key, entry in models(path).items():
        if not isinstance(entry, dict):
            continue
        served = entry.get("served_model_name") or key
        envs = entry.get("endpoint_env") or []
        base = next((os.environ[e] for e in envs if os.environ.get(e)), None)
        if not base:
            results.append({"model": key, "status": "skip",
                            "detail": f"endpoint_env が未設定: {envs}"})
            continue
        # トークンはエンドポイントごとに違う（RIKYU と RiVault で別）。
        # token_env の指定が無ければ従来の推定にフォールバックする。
        token_envs = entry.get("token_env") or []
        if isinstance(token_envs, str):
            token_envs = [token_envs]
        api_key = next((os.environ[e] for e in token_envs if os.environ.get(e)), None)
        try:
            ids = fetch_served_models(base, api_key)
        except Exception as e:
            results.append({"model": key, "status": "error", "detail": str(e)[:120]})
            continue
        results.append({
            "model": key,
            "status": "ok" if served in ids else "mismatch",
            "detail": f"served={served} 実在={'あり' if served in ids else 'なし'}"
                      f"（{len(ids)}件）",
        })
    return results


def _format_list(path: Path | None = None) -> str:
    out = []
    for key, e in models(path).items():
        if not isinstance(e, dict):
            continue
        v = "検証済み" if e.get("verified_at") else "**未検証**"
        prod = "本番" if e.get("production") else "評価専用"
        out.append(f"{key:<20} {prod:<8} id照合={v}  役割: {e.get('role','-')}")
        decl = {k: e.get(k) for k in ("declared_revision", "declared_trust_remote_code",
                                      "declared_engine")}
        out.append(f"{'':<20} 申告（検証不能・R12）: {decl}")
    return "\n".join(out) or "（宣言されたモデルがありません）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="供給網の固定（§4.6）— モデル宣言の一覧と id 照合"
    )
    parser.add_argument("--list", action="store_true", help="宣言内容を表示する")
    parser.add_argument("--check", action="store_true",
                        help="/v1/models と served_model_name を照合する")
    args = parser.parse_args(argv)

    if args.list:
        print(_format_list())
        return 0
    if args.check:
        rows = check_endpoints()
        bad = 0
        for r in rows:
            mark = {"ok": "OK  ", "mismatch": "★NG ", "error": "ERR ", "skip": "--  "}[r["status"]]
            print(f"{mark}{r['model']:<20} {r['detail']}")
            if r["status"] == "mismatch":
                bad += 1
        print()
        print("※ id の一致しか確認していない。revision / trust_remote_code / engine は"
              "取得手段が無く、model_pin.yaml の declared_* は申告の記録である（R12）。")
        return 1 if bad else 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
