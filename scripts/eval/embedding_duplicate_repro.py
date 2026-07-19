#!/usr/bin/env python3
"""embedding_duplicate_repro.py — embedding エンドポイントの「異なる入力に同一
ベクトルを返す」バグを再現するための自己完結スクリプト（運用者向け）。

# 背景（観測された異常）
bge-m3:567m を提供する embedding エンドポイント (`/v1/embeddings`) が、内容の
異なる複数テキストに対し **バイト同一のベクトル** を決定論的に返すことがある。
単独リクエストを再取得しても再現する（バッチ内の順序ズレではない）ため、
サーバ側のキャッシュ／プレフィックスキャッシュ層に何らかの不具合がある疑いが
強い。

# このスクリプトが行う 4 つのテスト（各テストは PASS/FAIL を出力する）
  1. determinism  : 同一テキストを 2 回埋め込み、一致することを確認する
                     （正常系の確認。不一致なら非決定性という別の異常）。
  2. truncation   : 長文 A と A の先頭 k 文字を埋め込み、embed(A) == embed(A[:k])
                     となる k があるかを調べる。あれば「入力が k 文字相当で
                     切り詰められている」ことの証明（サーバ側の仕様の可能性も
                     あるが報告価値は大きい）。
  3. prefix       : 冒頭が同一・後半が異なる合成テキストのペアを複数の
     collision      プレフィックス長で作り、ベクトルが一致するか調べる
                     （本命仮説：キャッシュキーが冒頭部分のみで決まっている）。
                     一致すれば社内テキスト無しで再現成立。
  4. pairs-file   : `--pairs-file` で外部 JSON からテキストペアを読み込んで
     (オプション)    検証する。社内の実データ（既知の異常ペア）を渡したい場合は
                     このモードを使う。スクリプト本体には社内テキストを
                     一切埋め込まない。

# 使い方（運用者向け）
    export EMBED_API_BASE="http://<endpoint-host>:<port>/v1"   # または RIVAULT_URL
    export EMBED_API_KEY="<token>"                              # 未設定なら "dummy"
    export EMBED_MODEL="bge-m3:567m"                            # 省略可（既定値）

    python3 embedding_duplicate_repro.py --all

    # 実データ検証（既知の異常ペアを JSON で用意した場合）
    python3 embedding_duplicate_repro.py --pairs-file suspects.json

--pairs-file の JSON 形式:
    {
      "pairs": [
        {"label": "case1", "text_a": "...", "text_b": "...", "expect_collision": true},
        {"label": "case2", "text_a": "...", "text_b": "..."}
      ]
    }
    "expect_collision" 省略時は true（＝運用者は「異常ペア」として渡す前提）。
    テキスト本文は端末に出力しない（先頭 --preview-chars 文字＋文字数のみ表示）。

# 期待される正常挙動 / 異常挙動
    - determinism: 常に PASS が正常。FAIL はサーバの非決定性を意味する。
    - truncation : 「一致する k なし」が最もクリーンだが、大きい k
                    （モデルの実効コンテキスト長付近）で一致するのはモデル
                    仕様として起こり得る。ごく小さい k で一致するのは異常。
    - prefix     : どの長さでも不一致（FAIL 無し）が正常。1 件でも一致すれば
                    バグの再現＝FAIL。
    - pairs-file : expect_collision=true のペアが実際に一致すれば「再現」、
                    不一致ならその時点では再現しなかったことを意味する
                    （エンドポイントの状態に依存し得るため複数回の実行を推奨）。

依存: `requests` のみ（本リポジトリのユーティリティ・秘密情報には一切依存しない）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

DEFAULT_MODEL = "bge-m3:567m"
DEFAULT_TIMEOUT = 60
DEFAULT_PREVIEW_CHARS = 30

_PASS = "PASS"
_FAIL = "FAIL"
_INFO = "INFO"


def resolve_endpoint(args: argparse.Namespace) -> tuple[str, str, str]:
    """(base_url, api_key, model) を CLI 引数 > 環境変数の優先順位で解決する。"""
    base = args.base_url or os.environ.get("EMBED_API_BASE") or os.environ.get("RIVAULT_URL")
    if not base:
        raise RuntimeError(
            "エンドポイントが未設定。--base-url か環境変数 EMBED_API_BASE / RIVAULT_URL"
            " のいずれかを指定してください。"
        )
    api_key = (
        args.api_key
        or os.environ.get("EMBED_API_KEY")
        or os.environ.get("RIVAULT_TOKEN")
        or "dummy"
    )
    model = args.model or os.environ.get("EMBED_MODEL", DEFAULT_MODEL)
    return base.rstrip("/"), api_key, model


class CallBudgetExceeded(RuntimeError):
    pass


class EmbedClient:
    """単一テキストを 1 リクエストずつ埋め込む素の HTTP クライアント。

    バッチ順序ズレの可能性を排除するため、常に input=[text] で 1 件ずつ送る
    （報告されている再現条件＝単独リクエストの再取得に合わせている）。
    """

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: int, max_calls: int):
        self.url = f"{base_url}/embeddings"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.model = model
        self.timeout = timeout
        self.max_calls = max_calls
        self.call_count = 0

    def embed(self, text: str) -> list[float]:
        if self.call_count >= self.max_calls:
            raise CallBudgetExceeded(
                f"API 呼び出し上限 ({self.max_calls} 回) に達したため中断しました。"
                " --max-calls で上限を上げられます。"
            )
        self.call_count += 1
        resp = requests.post(
            self.url,
            json={"input": [text], "model": self.model},
            headers=self.headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") or []
        if len(items) != 1:
            raise RuntimeError(f"埋め込み API の応答件数が不正: got={len(items)}")
        vec = items[0].get("embedding")
        if not vec:
            raise RuntimeError("埋め込みが返ってこない")
        return vec


def preview(text: str, n: int) -> str:
    head = text[:n].replace("\n", "\\n")
    return f"'{head}...' (len={len(text)})" if len(text) > n else f"'{head}' (len={len(text)})"


# --------------------------------------------------------------------------- #
# テスト 1: 決定論プローブ
# --------------------------------------------------------------------------- #
def test_determinism(client: EmbedClient) -> bool:
    print("\n=== [1/4] 決定論プローブ (同一テキストを2回embed → 一致するか) ===")
    text = "これは決定論プローブ用のダミーテキストです。内容は本番データとは無関係です。"
    v1 = client.embed(text)
    v2 = client.embed(text)
    ok = v1 == v2
    status = _PASS if ok else _FAIL
    print(f"  [{status}] embed(text) == embed(text): {ok}")
    if not ok:
        print("  → 同一入力に対し非決定的な応答（キャッシュ不在 or 内部状態依存の可能性）")
    return ok


# --------------------------------------------------------------------------- #
# テスト 2: 切り詰めプローブ
# --------------------------------------------------------------------------- #
def _build_long_text(target_len: int) -> str:
    sentences = [
        "セクションAには設計判断の要点が記述されている。",
        "セクションBにはリスク分析の詳細が含まれる。",
        "セクションCには性能評価の結果がまとめられている。",
        "セクションDには今後のスケジュールが記載されている。",
        "セクションEには予算に関する議論がある。",
    ]
    out = ""
    i = 0
    while len(out) < target_len:
        out += sentences[i % len(sentences)] + f"(段落{i}) "
        i += 1
    return out[:target_len]


def test_truncation(client: EmbedClient, ks: list[int]) -> bool:
    print("\n=== [2/4] 切り詰めプローブ (embed(A) == embed(A[:k]) となる k はあるか) ===")
    target_len = max(ks) + 500
    text_a = _build_long_text(target_len)
    print(f"  合成長文 A: len={len(text_a)}")
    v_full = client.embed(text_a)

    matched_ks: list[int] = []
    for k in ks:
        if k >= len(text_a):
            continue
        sub = text_a[:k]
        v_k = client.embed(sub)
        eq = v_full == v_k
        print(f"  k={k:6d}  embed(A)==embed(A[:k]): {eq}")
        if eq:
            matched_ks.append(k)

    if matched_ks:
        min_k = min(matched_ks)
        print(f"  [{_FAIL}] embed(A) と一致する最小の k = {min_k}"
              f"（入力がおよそ {min_k} 文字相当で切り詰められている可能性）")
        return False
    print(f"  [{_PASS}] テストした k の範囲内 ({ks}) では一致なし（切り詰めは未検出）")
    return True


# --------------------------------------------------------------------------- #
# テスト 3: プレフィックス衝突プローブ（本命仮説）
# --------------------------------------------------------------------------- #
def _build_prefix(n: int) -> str:
    header = "【ダミー/フォルダ/パス/dummy_file_sample.pdf】\n"
    return (header * ((n // len(header)) + 1))[:n]


def test_prefix_collision(client: EmbedClient, prefix_lens: list[int]) -> bool:
    print("\n=== [3/4] プレフィックス衝突プローブ (同じ冒頭+異なる後半 → 一致するか) ===")
    total_len = max(prefix_lens) + 500
    suffix_x = (
        "こちらは前半共通でも後半が全く異なるダミーテキストAの内容です。"
        "設計判断についての議論が続く。予算超過のリスクが指摘され、対応方針が検討された。"
    )
    suffix_y = (
        "こちらは同じ冒頭を持つが後半が完全に異なるダミーテキストBです。"
        "人員配置とスケジュール調整に関する合意事項がまとめられている。次回日程も決定した。"
    )
    suffix_x = suffix_x * (total_len // len(suffix_x) + 1)
    suffix_y = suffix_y * (total_len // len(suffix_y) + 1)

    any_collision = False
    for plen in prefix_lens:
        prefix = _build_prefix(plen)
        text_x = (prefix + suffix_x)[:total_len]
        text_y = (prefix + suffix_y)[:total_len]
        assert text_x[:plen] == text_y[:plen], "プレフィックス構築ロジックの不整合"
        assert text_x != text_y, "後半が同一になってしまっている（テスト構築ミス）"
        vx = client.embed(text_x)
        vy = client.embed(text_y)
        eq = vx == vy
        status = _FAIL if eq else _PASS
        print(f"  [{status}] prefix_len={plen:5d}  異なる後半でも一致: {eq}")
        if eq:
            any_collision = True

    if any_collision:
        print(f"  [{_FAIL}] 少なくとも1つのプレフィックス長で衝突を検出"
              "（社内テキスト無しで再現成立）")
        return False
    print(f"  [{_PASS}] 全プレフィックス長で衝突なし（合成データでは未再現）")
    return True


# --------------------------------------------------------------------------- #
# テスト 4: 実データ検証モード（--pairs-file）
# --------------------------------------------------------------------------- #
def test_pairs_file(client: EmbedClient, pairs_file: str, preview_chars: int) -> bool:
    print(f"\n=== [4/4] 実データ検証モード (--pairs-file {pairs_file}) ===")
    with open(pairs_file, encoding="utf-8") as f:
        data = json.load(f)
    pairs: list[dict[str, Any]] = data.get("pairs") or []
    if not pairs:
        print("  pairs が空、またはキー 'pairs' が見つかりません。スキップします。")
        return True

    all_ok = True
    for i, pair in enumerate(pairs):
        label = pair.get("label", f"pair{i}")
        text_a = pair.get("text_a", "")
        text_b = pair.get("text_b", "")
        expect_collision = pair.get("expect_collision", True)
        if not text_a or not text_b:
            print(f"  [{label}] text_a / text_b が不足。スキップ。")
            continue
        va = client.embed(text_a)
        vb = client.embed(text_b)
        eq = va == vb
        ok = eq == expect_collision
        status = _PASS if ok else _FAIL
        print(f"  [{status}] {label}")
        print(f"      text_a: {preview(text_a, preview_chars)}")
        print(f"      text_b: {preview(text_b, preview_chars)}")
        print(f"      expect_collision={expect_collision}  actual_equal={eq}")
        if not ok:
            all_ok = False
    return all_ok


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="embedding エンドポイントの同一ベクトル返却バグ再現スクリプト",
    )
    p.add_argument("--base-url", help="埋め込みAPIのベースURL（未指定なら EMBED_API_BASE / RIVAULT_URL）")
    p.add_argument("--api-key", help="APIキー（未指定なら EMBED_API_KEY / RIVAULT_TOKEN / dummy）")
    p.add_argument("--model", help=f"モデル名（既定値: {DEFAULT_MODEL}）")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTPタイムアウト秒")
    p.add_argument("--max-calls", type=int, default=80, help="API呼び出し回数の上限（安全弁）")
    p.add_argument(
        "--truncation-ks",
        default="256,512,1024,2048,4096,6000,7000,7500,8000,8500,9000",
        help="切り詰めプローブで試す k（カンマ区切り）",
    )
    p.add_argument(
        "--prefix-lens",
        default="64,128,256,512,1024",
        help="プレフィックス衝突プローブで試す共通冒頭長（カンマ区切り）",
    )
    p.add_argument("--pairs-file", help="実データ検証モード用の JSON ファイルパス")
    p.add_argument("--preview-chars", type=int, default=DEFAULT_PREVIEW_CHARS,
                   help="--pairs-file 使用時に表示するテキスト冒頭の文字数")
    p.add_argument(
        "--skip-builtin",
        action="store_true",
        help="組み込みテスト(1-3)をスキップし --pairs-file のみ実行する",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        base_url, api_key, model = resolve_endpoint(args)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    print(f"エンドポイント: {base_url}/embeddings  model={model}")
    client = EmbedClient(base_url, api_key, model, timeout=args.timeout, max_calls=args.max_calls)

    results: list[bool] = []
    try:
        if not args.skip_builtin:
            results.append(test_determinism(client))
            ks = [int(x) for x in args.truncation_ks.split(",") if x.strip()]
            results.append(test_truncation(client, ks))
            prefix_lens = [int(x) for x in args.prefix_lens.split(",") if x.strip()]
            results.append(test_prefix_collision(client, prefix_lens))

        if args.pairs_file:
            results.append(test_pairs_file(client, args.pairs_file, args.preview_chars))
    except CallBudgetExceeded as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        return 3

    print(f"\n=== 完了: API呼び出し回数={client.call_count} ===")
    if not results:
        print("実行したテストがありません（--skip-builtin かつ --pairs-file 未指定）。")
        return 0

    overall_ok = all(results)
    print(f"総合結果: {_PASS if overall_ok else _FAIL}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
