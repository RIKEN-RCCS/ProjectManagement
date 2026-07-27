"""scripts/eval/knowledge_ab.py の --compare triage 追加分のテスト。

LLM 実接続なし。scripts/eval は pytest の pythonpath 対象外のため、
import 前に sys.path へ追加する（knowledge_ab.py 自身が行っている
ブートストラップと同じパス）。
"""
import inspect
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "eval"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import knowledge_ab  # noqa: E402 — 上記パス追加後にインポート
from ingest.slack import extract_from_thread  # noqa: E402


def test_triage_variant_kwargs_match_extract_from_thread_signature():
    sig = inspect.signature(extract_from_thread)
    for _name, kwargs in knowledge_ab._COMPARE_VARIANTS["triage"]:
        # row/context/milestones/repo_root は呼び出し側で別途渡す固定引数のため
        # ダミー値を補って bind する。
        sig.bind_partial(row={}, context="", milestones=[], repo_root=None, **kwargs)


def test_detect_compare_mode_triage():
    rec = {"items_two_stage": "d=0,a=0", "items_integrated": "d=1,a=0"}
    assert knowledge_ab._detect_compare_mode(rec) == "triage"
