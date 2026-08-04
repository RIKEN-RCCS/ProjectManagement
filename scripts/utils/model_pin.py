"""model_pin.py — 供給網の固定（docs/security-architecture.md §4.6）

`config/model_pin.yaml` に宣言したモデルだけを本番経路で使わせる。

**この対策が証明すること・しないこと（P10）**

証明する:
  - 本番経路で使われたモデル id が、宣言された集合の中にあること
  - 宣言されていないモデルに黙って切り替わっていないこと（enforce 時）

証明しない:
  - **エンドポイントが実際に配信しているモデルの全体像。** `--check` は
    「宣言 → 実在」の向きだけを見る。宣言していないモデルが増えても違反にならない。
    逆向き（実在 → 宣言）は `unknown_served()` / `--list-served` と
    `pm_selfcheck.py` の `model_pin_unknown_served` が担う（2026-08-04 追加）。
  - **トークンで見える範囲を超えたこと。** `/v1/models` の応答は**資格情報の
    スコープに依存する**。2026-08-04 実測: 同じ RiVault に対し、旧トークンでは 15 件、
    更新後のトークンでは 24 件が返った。**旧トークンでの「OK・15件」は、配信されている
    24 件のうち 9 件が見えていない状態だった**（見えないものは照合もされない）。
    したがってこの検査が言えるのは「**このトークンで見える範囲では**宣言と一致する」
    までであり、「配信されているのはこれだけ」ではない（P10）。
  - **チェックポイントが宣言どおりのものであること。** OpenAI 互換の `/v1/models` は
    `id` の他に一部エンドポイントで `max_input_tokens` / `max_output_tokens` を返すが、
    revision（sha）を Argus 側から取得する手段は無い。
    `declared_*` は**運用主体の申告の記録**であって検証結果ではない（R12）。
  - `trust_remote_code` や engine の実際の設定。これもサービス側にあり取得できない。

したがって pin の実効は「id の一致」「取得できる範囲での max_input_tokens /
max_output_tokens の一致（`verified_max_input_tokens` / `verified_max_output_tokens`）」
「**申告値の変更が git の diff に現れること**」の3つに限られる。
**max_tokens の一致は id 単独より強い指紋にすぎず、同一性の証明ではない** —
文脈長が同じ別モデルへ黙って差し替えられた場合はこれも通ってしまう。
モデル更新の通知を運用主体から受ける取り決め（Phase 0）が対で必要になる。

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

    **この関数はネットワークに一切出ない**（yaml を読むだけ）。この関数は
    LLM 呼び出しのたびに評価されるため、ここでエンドポイント疎通を行うと
    エンドポイント障害時に本番呼び出し自体が引きずられて落ちる。id / max_tokens の
    実照合（ネットワークが要る）は `check_endpoints()` に分離し、日次の
    pm_selfcheck.py（`model_pin_drift`）からだけ呼ぶ。
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
                        timeout: int = 10) -> list[dict]:
    """`/v1/models` のエントリ一覧を返す。取得できなければ例外。

    各要素は {"id", "max_input_tokens", "max_output_tokens"}。後者2つを
    返さない API（RiVault の一部・embedding 等）では None になる
    （呼び出し側はこれを「取得不能＝照合スキップ」として扱う）。
    """
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
    return [
        {
            "id": m["id"],
            "max_input_tokens": m.get("max_input_tokens"),
            "max_output_tokens": m.get("max_output_tokens"),
        }
        for m in data.get("data", []) if isinstance(m, dict) and "id" in m
    ]


def check_endpoints(path: Path | None = None, timeout: int = 10) -> list[dict]:
    """宣言した served_model_name が実際のエンドポイントに存在するか照合する。

    id の一致に加え、`verified_max_input_tokens` / `verified_max_output_tokens` が
    yaml で null でないエントリについては `/v1/models` の同名フィールドとも照合する。
    API がそもそも返さないエントリ（yaml 側も null）は照合をスキップし、その旨を
    detail に含める。

    **限界**: max_tokens が同じ別モデルへの差し替えは通る。id 単独より強い指紋に
    すぎず、同一性の証明ではない（R12 と同じ限界。トップの docstring参照）。

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
            served_models = fetch_served_models(base, api_key, timeout=timeout)
        except Exception as e:
            results.append({"model": key, "status": "error", "detail": str(e)[:120]})
            continue
        ids = {m["id"] for m in served_models}
        if served not in ids:
            results.append({
                "model": key, "status": "mismatch",
                "detail": f"served={served} 実在=なし（{len(ids)}件）",
            })
            continue

        matched = next(m for m in served_models if m["id"] == served)
        mismatches = []
        skipped_fields = []
        for field, expected_key in (
            ("max_input_tokens", "verified_max_input_tokens"),
            ("max_output_tokens", "verified_max_output_tokens"),
        ):
            expected = entry.get(expected_key)
            if expected is None:
                skipped_fields.append(field)
                continue
            actual = matched.get(field)
            if actual != expected:
                mismatches.append(f"{field}: 期待={expected} 実際={actual}")

        detail = f"served={served} 実在=あり（{len(ids)}件）"
        if skipped_fields:
            detail += f" ／ max_tokens照合スキップ（API未提供 or 未記録）: {skipped_fields}"
        if mismatches:
            detail += f" ／ max_tokens不一致: {'; '.join(mismatches)}"
            results.append({"model": key, "status": "mismatch", "detail": detail})
        else:
            results.append({"model": key, "status": "ok", "detail": detail})
    return results


def observed_not_used(path: Path | None = None) -> dict:
    """`observed_not_used`（配信されているが Argus では使わないモデル）を返す。

    ここに載っているのは「見たことを人が確認済み」の意味しかない。**使ってよいという
    意味ではない**（使用の許可は `models:` 側の `production: true` が決める）。
    """
    return (load_pin(path).get("observed_not_used") or {})


def unknown_served(path: Path | None = None, timeout: int = 10) -> list[dict]:
    """各エンドポイントの `/v1/models` を列挙し、**pin に載っていない id** を報告する。

    `check_endpoints()` は「宣言 → 実在」の向きしか見ないため、**知らないモデルが
    増えても違反にならない**。2026-08-04 に RiVault へ Kimi-K3 / GLM-5.2 / GLM-OCR /
    RiVault 自製モデル 6 本が加わったが、`--check` は全件 OK のままだった
    （気づいたのは人からの連絡）。net_guard 層3 の `egress_dest_unknown`
    （知らない宛先を報告する）と同じ形をモデル側にも置く。

    「知らない」= `models:` にも `observed_not_used:` にも無い id。新しいモデルは
    **一度は人が yaml に書いて認める**必要があり、その追記が git の diff に残る
    （このファイルの他の申告値と同じ扱い）。

    **限界**: 見えるのはトークンのスコープ内だけ（モジュール docstring 参照）。
    「unknown 0 件」は「配信されているのは宣言したものだけ」を意味しない。

    戻り値: [{"endpoint_env", "base", "status", "unknown": [id...], "n_served": int,
              "detail": str}, ...]
    """
    known = set()
    for key, entry in models(path).items():
        known.add(key)
        if isinstance(entry, dict) and entry.get("served_model_name"):
            known.add(entry["served_model_name"])
    known |= set(observed_not_used(path).keys())

    # endpoint_env をモデル宣言から集める（同じエンドポイントを1回だけ叩く）。
    env_names: list[str] = []
    for entry in models(path).values():
        if not isinstance(entry, dict):
            continue
        for e in entry.get("endpoint_env") or []:
            if e not in env_names:
                env_names.append(e)

    results: list[dict] = []
    seen_bases: dict[str, str] = {}
    for env in env_names:
        base = os.environ.get(env)
        if not base:
            results.append({"endpoint_env": env, "base": None, "status": "skip",
                            "unknown": [], "n_served": 0,
                            "detail": f"{env} が未設定（照合しません）"})
            continue
        if base in seen_bases:
            results.append({"endpoint_env": env, "base": base, "status": "skip",
                            "unknown": [], "n_served": 0,
                            "detail": f"{seen_bases[base]} と同じエンドポイント（重複のためスキップ）"})
            continue
        seen_bases[base] = env

        api_key = None
        for entry in models(path).values():
            if not isinstance(entry, dict) or env not in (entry.get("endpoint_env") or []):
                continue
            token_envs = entry.get("token_env") or []
            if isinstance(token_envs, str):
                token_envs = [token_envs]
            api_key = next((os.environ[t] for t in token_envs if os.environ.get(t)), None)
            if api_key:
                break
        try:
            served = fetch_served_models(base, api_key, timeout=timeout)
        except Exception as e:
            results.append({"endpoint_env": env, "base": base, "status": "error",
                            "unknown": [], "n_served": 0,
                            "detail": f"取得失敗（判定不能）: {str(e)[:120]}"})
            continue
        ids = sorted(m["id"] for m in served)
        unknown = [i for i in ids if i not in known]
        results.append({
            "endpoint_env": env, "base": base,
            "status": "unknown" if unknown else "ok",
            "unknown": unknown, "n_served": len(ids),
            "detail": (f"配信 {len(ids)} 件のうち pin 未記載 {len(unknown)} 件: "
                       + ", ".join(unknown)) if unknown
                      else f"配信 {len(ids)} 件すべて pin に記載あり",
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
                        help="/v1/models と served_model_name を照合する（宣言→実在）")
    parser.add_argument("--list-served", action="store_true",
                        help="各エンドポイントの /v1/models を列挙し、pin に載っていない "
                             "id を報告する（実在→宣言）。見えるのはトークンのスコープ内だけ")
    args = parser.parse_args(argv)

    if args.list:
        print(_format_list())
        return 0
    if args.list_served:
        rows = unknown_served()
        bad = 0
        for r in rows:
            mark = {"ok": "OK", "unknown": "★NEW", "error": "ERR", "skip": "--"}[r["status"]]
            print(f"{mark:<5} {r['endpoint_env']:<24} {r['detail']}")
            if r["status"] == "unknown":
                bad += 1
        print()
        print("※ 見えるのは**トークンのスコープ内だけ**である（2026-08-04 実測: 同じ RiVault で "
              "旧トークン 15 件 / 更新後 24 件）。unknown 0 件は「配信されているのは宣言した "
              "ものだけ」を意味しない。新しい id は config/model_pin.yaml の models: か "
              "observed_not_used: に人が追記して認めること（追記が git の diff に残る）。")
        return 1 if bad else 0
    if args.check:
        rows = check_endpoints()
        bad = 0
        for r in rows:
            mark = {"ok": "OK  ", "mismatch": "★NG ", "error": "ERR ", "skip": "--  "}[r["status"]]
            print(f"{mark}{r['model']:<20} {r['detail']}")
            if r["status"] == "mismatch":
                bad += 1
        print()
        print("※ id と（取得できる範囲の）max_input_tokens/max_output_tokens しか"
              "確認していない。revision / trust_remote_code / engine は取得手段が無く、"
              "model_pin.yaml の declared_* は申告の記録である（R12）。"
              "max_tokens が同じ別モデルへの差し替えは検出できない。")
        return 1 if bad else 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
