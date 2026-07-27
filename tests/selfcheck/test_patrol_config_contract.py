"""scripts/argus/patrol/ の cfg.get("key") 参照が data/patrol_config.yaml に
存在するかを検査する（タイポで既定値へ静かにフォールバックするクラスの検出）。

値は一切 print しない（channel/user ID を含み得るため）。assert メッセージにも
キー名のみを含める。

data/patrol_config.yaml が存在しない環境（クリーン clone）では skip する。
"""
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DETECT_PY = REPO_ROOT / "scripts" / "argus" / "patrol" / "detect.py"
PATROL_CONFIG_YAML = REPO_ROOT / "data" / "patrol_config.yaml"

_SECTION_CFG_RE = re.compile(
    r'cfg\s*=\s*ctx\.config\.get\("patrol",\s*\{\}\)\.get\("([^"]+)"'
)
_CFG_GET_RE = re.compile(r'cfg\.get\("([^"]+)"')

# コード側にあって yaml に無いキー（既定値運用と判断済み）。
#   completion_detection: auto_close の高度な挙動制御で、既定値のまま運用中
#     （HIGH 確信度のみ自動クローズ・自動クローズ後にチャンネル通知・
#      証拠検索は extracted_at 以降のみ・box は index_name 未指定で全域検索）
#   obsolete_detection: セクション自体が yaml に未定義（2026-07-22 導入の
#     方針転換検出器はまだ明示設定を持たず、コード既定値のみで稼働中）
KNOWN_DEFAULTS: dict[str, set[str]] = {
    "completion_detection": {
        "auto_close_min_confidence",
        "post_close_notify",
        "evidence_since_extracted",
        "evidence_index_name",
    },
    "obsolete_detection": {
        "enabled",
        "max_llm_per_run",
        "max_per_run",
        "recheck_days",
    },
}


def _collect_code_sections() -> dict[str, set[str]]:
    """detect.py を `def ` 単位で分割し、各関数内の cfg.get(...) キーをセクション別に集める。"""
    src = DETECT_PY.read_text(encoding="utf-8")
    funcs = re.split(r"(?=^def )", src, flags=re.M)
    sections: dict[str, set[str]] = {}
    for func_src in funcs:
        m = _SECTION_CFG_RE.search(func_src)
        if not m:
            continue
        section = m.group(1)
        keys = set(_CFG_GET_RE.findall(func_src))
        sections.setdefault(section, set()).update(keys)
    return sections


@pytest.fixture(scope="module")
def patrol_config_yaml():
    if not PATROL_CONFIG_YAML.exists():
        pytest.skip("data/patrol_config.yaml が存在しない環境のためスキップ")
    with open(PATROL_CONFIG_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("patrol", {}) or {}


def test_code_sections_detected():
    sections = _collect_code_sections()
    assert sections, "detect.py から cfg = ctx.config.get(\"patrol\", {}).get(...) パターンを検出できませんでした"


def test_cfg_get_keys_exist_in_yaml_or_known_defaults(patrol_config_yaml):
    sections = _collect_code_sections()
    unexpected: list[str] = []

    for section, keys in sections.items():
        yaml_section = patrol_config_yaml.get(section)
        yaml_keys = set(yaml_section.keys()) if isinstance(yaml_section, dict) else set()
        known = KNOWN_DEFAULTS.get(section, set())
        for key in keys:
            if key in yaml_keys:
                continue
            if key in known:
                continue
            unexpected.append(f"{section}.{key}")

    assert not unexpected, (
        "data/patrol_config.yaml に存在せず KNOWN_DEFAULTS にも無い cfg.get() "
        f"キーが増えています（タイポの疑い）: {sorted(unexpected)}"
    )
