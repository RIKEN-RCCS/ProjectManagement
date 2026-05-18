"""
embed_utils.py — Embedding API 呼び出し + ベクトル類似度ユーティリティ

OpenAI 互換の `/v1/embeddings` を呼ぶ。RiVault が `bge-m3:567m` を
提供しているのでデフォルトはそちらを使う。`EMBED_API_BASE` / `EMBED_MODEL`
で他のプロバイダにも差し替え可能。

Usage:
    from embed_utils import embed_one, embed_batch, cosine_similarity

    vec = embed_one("富岳NEXTのGPU構成")
    mat = embed_batch(["text1", "text2", ...])
    sim = cosine_similarity(vec_a, vec_b)
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

import numpy as np

logger = logging.getLogger(__name__)

# デフォルトは RiVault の bge-m3。EMBED_API_BASE / EMBED_MODEL で上書き可能。
_DEFAULT_BASE = "http://llm.ai.r-ccs.riken.jp:11434/v1"
_DEFAULT_MODEL = "bge-m3:567m"

# bge-m3 のサーバ側上限を超えないようにテキストを切る
_MAX_INPUT_CHARS = 4000


def _resolve_endpoint() -> tuple[str, str, str]:
    """(base_url, api_key, model) を返す。"""
    base = os.environ.get("EMBED_API_BASE") or os.environ.get("RIVAULT_URL") or _DEFAULT_BASE
    api_key = (
        os.environ.get("EMBED_API_KEY")
        or os.environ.get("RIVAULT_TOKEN")
        or "dummy"
    )
    model = os.environ.get("EMBED_MODEL", _DEFAULT_MODEL)
    return base.rstrip("/"), api_key, model


def _truncate(text: str) -> str:
    s = (text or "").strip()
    return s[:_MAX_INPUT_CHARS]


def embed_batch(
    texts: list[str],
    *,
    timeout: int = 60,
    batch_size: int = 32,
) -> np.ndarray:
    """テキストを一括で埋め込む。返り値は (N, dim) の float32 ndarray。

    空テキストは零ベクトルで埋める（次元は最初の有効テキストから決定）。
    API エラーは例外送出。
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    import requests

    base, api_key, model = _resolve_endpoint()
    url = f"{base}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    cleaned = [_truncate(t) for t in texts]
    out_vectors: list[np.ndarray | None] = [None] * len(cleaned)
    valid_indices = [i for i, t in enumerate(cleaned) if t]

    for chunk_start in range(0, len(valid_indices), batch_size):
        chunk_idxs = valid_indices[chunk_start: chunk_start + batch_size]
        chunk_texts = [cleaned[i] for i in chunk_idxs]
        payload = {"input": chunk_texts, "model": model}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"embedding API エラー: {e}") from e
        items = data.get("data") or []
        if len(items) != len(chunk_idxs):
            raise RuntimeError(
                f"埋め込み API の応答件数が不一致: expected={len(chunk_idxs)} got={len(items)}"
            )
        for idx, item in zip(chunk_idxs, items):
            v = item.get("embedding")
            if not v:
                raise RuntimeError(f"埋め込みが返ってこない: index={idx}")
            out_vectors[idx] = np.asarray(v, dtype=np.float32)

    # 空テキスト分は最初の有効ベクトルと同じ次元の零ベクトルを置く
    dim = next((v.shape[0] for v in out_vectors if v is not None), 0)
    if dim == 0:
        return np.zeros((len(cleaned), 0), dtype=np.float32)
    zero = np.zeros(dim, dtype=np.float32)
    arr = np.stack([(v if v is not None else zero) for v in out_vectors])
    return arr


def embed_one(text: str, *, timeout: int = 60) -> np.ndarray:
    return embed_batch([text], timeout=timeout)[0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """2 ベクトル間のコサイン類似度。零ベクトルなら 0."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cosine_similarity_matrix(query: np.ndarray, mat: np.ndarray) -> np.ndarray:
    """1×D のクエリと N×D のマトリクスから (N,) のコサイン類似度ベクトル。"""
    if mat.size == 0:
        return np.zeros(0, dtype=np.float32)
    qn = float(np.linalg.norm(query))
    if qn == 0.0:
        return np.zeros(mat.shape[0], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0.0] = 1.0  # 零ベクトル除算回避
    return (mat @ query) / (norms * qn)


def vector_to_blob(v: np.ndarray) -> bytes:
    """numpy ベクトルを SQLite BLOB 用に float32 raw bytes に変換。"""
    return np.asarray(v, dtype=np.float32).tobytes()


def blob_to_vector(b: bytes, dim: int | None = None) -> np.ndarray:
    """raw bytes を float32 ベクトルに復元する。dim 指定で reshape チェック。"""
    arr = np.frombuffer(b, dtype=np.float32)
    if dim is not None and arr.shape[0] != dim:
        raise ValueError(f"次元不一致: expected={dim} got={arr.shape[0]}")
    return arr


def healthcheck(timeout: int = 5) -> bool:
    """埋め込みエンドポイントの疎通確認。1 件埋め込んで OK なら True。"""
    try:
        v = embed_one("ping", timeout=timeout)
        return v.shape[0] > 0
    except Exception as e:
        logger.warning(f"embed_utils healthcheck 失敗: {e}")
        return False
