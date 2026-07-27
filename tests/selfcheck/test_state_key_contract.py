"""scripts/argus/patrol/ 配下の already_notified / record_notification キー契約を
網羅的に静的検査する。

バグクラス (c): 状態キーの記録側と判定側の名前不一致（cooldown が永遠に効かない）
の一般化版。tests/argus/test_patrol_reminder_cooldown.py はリマインダー3種のみを
対象とした個別テストだが、本テストは patrol/ 配下の全ソースをソース走査し、
already_notified() で判定される全キーが record_notification() で書かれ得る
キー集合に含まれることを検証する。

変数渡しの record_notification(event_type, ...) 呼び出し（actions.send_reminder）
は、actions._REMINDER_EVENT_TYPES の値集合で解決する。
"""
import re
from pathlib import Path

from argus.patrol import actions

PATROL_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "argus" / "patrol"

_ALREADY_NOTIFIED_RE = re.compile(r'already_notified\(\s*"([^"]+)"')
_RECORD_LITERAL_RE = re.compile(r'record_notification\(\s*"([^"]+)"')
# 文字列リテラルでない（＝変数渡し）呼び出しを検出する。次のトークンがクォートで
# 始まらない識別子であれば変数渡しとみなす。
_RECORD_VARIABLE_RE = re.compile(r'record_notification\(\s*\n?\s*([A-Za-z_][A-Za-z0-9_]*)\s*,')


def _patrol_sources() -> dict[str, str]:
    sources = {}
    for p in sorted(PATROL_DIR.glob("*.py")):
        if p.name == "__init__.py":
            continue
        sources[p.name] = p.read_text(encoding="utf-8")
    return sources


def test_already_notified_keys_are_all_recordable():
    sources = _patrol_sources()
    assert sources, "scripts/argus/patrol/ にソースが見つかりません"

    already_keys: set[str] = set()
    record_keys: set[str] = set()
    has_variable_record_call = False

    for _name, src in sources.items():
        already_keys.update(_ALREADY_NOTIFIED_RE.findall(src))
        record_keys.update(_RECORD_LITERAL_RE.findall(src))
        if _RECORD_VARIABLE_RE.search(src):
            has_variable_record_call = True

    # 変数渡し（actions.send_reminder の event_type）は _REMINDER_EVENT_TYPES の
    # 値集合で解決する（既存 test_patrol_reminder_cooldown.py と同じ解決方法）。
    if has_variable_record_call:
        record_keys.update(actions._REMINDER_EVENT_TYPES.values())

    assert already_keys, "already_notified() 呼び出しが1件も検出されませんでした"
    assert record_keys, "record_notification() 呼び出しが1件も検出されませんでした"

    missing = already_keys - record_keys
    assert not missing, (
        "already_notified() で判定されるが record_notification() で書かれ得ない"
        f" キーがあります（cooldown が永遠に効かない不一致）: {sorted(missing)}\n"
        f"already_notified keys: {sorted(already_keys)}\n"
        f"record_notification keys: {sorted(record_keys)}"
    )
