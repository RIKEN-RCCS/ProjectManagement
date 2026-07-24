"""build_cluster_name_map / resolve_speaker_name の純関数テスト（LLM/DB/GPU 不使用）。"""
from utils.transcript import build_cluster_name_map, resolve_speaker_name


def _hms(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _w(speaker: str, start: int, end: int, text: str = "発言") -> dict:
    return {"speaker": speaker, "start": start, "end": end, "text": text}


def _v(speaker: str, start: int, end: int, text: str = "発言") -> dict:
    return {"speaker": speaker, "start": _hms(start), "end": _hms(end), "text": text}


# --------------------------------------------------------------------------- #
# 明確な多数決 → 確定
# --------------------------------------------------------------------------- #

def test_clear_majority_confirms():
    whisper_segs = [_w("SPEAKER_00", 0, 100)]
    vtt_segs = [
        _v("田中太郎", 0, 90),
        _v("Alice Smith", 90, 100),
    ]
    result = build_cluster_name_map(whisper_segs, vtt_segs)
    assert "SPEAKER_00" in result
    info = result["SPEAKER_00"]
    assert info["name"] == "田中太郎"
    assert info["share"] == 0.9
    assert info["overlap_sec"] == 90.0


# --------------------------------------------------------------------------- #
# 2 名が拮抗（ratio < 1.5）→ 未確定
# --------------------------------------------------------------------------- #

def test_contested_speakers_unresolved():
    whisper_segs = [_w("SPEAKER_01", 0, 100)]
    vtt_segs = [
        _v("田中太郎", 0, 55),
        _v("Alice Smith", 55, 100),
    ]
    result = build_cluster_name_map(whisper_segs, vtt_segs)
    assert "SPEAKER_01" not in result


# --------------------------------------------------------------------------- #
# 総重なりが min_overlap_sec 未満 → 未確定
# --------------------------------------------------------------------------- #

def test_below_min_overlap_unresolved():
    whisper_segs = [_w("SPEAKER_02", 0, 4)]
    vtt_segs = [_v("田中太郎", 0, 4)]
    result = build_cluster_name_map(whisper_segs, vtt_segs, min_overlap_sec=5.0)
    assert "SPEAKER_02" not in result


# --------------------------------------------------------------------------- #
# 複数クラスタ→同一名 → 両方確定
# --------------------------------------------------------------------------- #

def test_multiple_clusters_same_name_both_confirm():
    whisper_segs = [
        _w("SPEAKER_00", 0, 50),
        _w("SPEAKER_03", 200, 250),
    ]
    vtt_segs = [
        _v("田中太郎", 0, 50),
        _v("田中太郎", 200, 250),
    ]
    result = build_cluster_name_map(whisper_segs, vtt_segs)
    assert result["SPEAKER_00"]["name"] == "田中太郎"
    assert result["SPEAKER_03"]["name"] == "田中太郎"


# --------------------------------------------------------------------------- #
# whisper "UNKNOWN" と VTT "Unknown" が集計から除外される
# --------------------------------------------------------------------------- #

def test_unknown_speakers_excluded():
    whisper_segs = [
        _w("UNKNOWN", 0, 100),
        _w("SPEAKER_04", 100, 200),
    ]
    vtt_segs = [
        _v("田中太郎", 0, 100),
        _v("Unknown", 100, 200),
    ]
    result = build_cluster_name_map(whisper_segs, vtt_segs)
    # UNKNOWN はクラスタとして扱われないので、まず result 自体に含まれない
    assert "UNKNOWN" not in result
    # SPEAKER_04 は Unknown ラベルの VTT セグメントとしか重ならないため除外され未確定
    assert "SPEAKER_04" not in result


# --------------------------------------------------------------------------- #
# vtt_offset_sec を与えるとオフセット付きで正しく重なる
# --------------------------------------------------------------------------- #

def test_vtt_offset_enables_overlap():
    whisper_segs = [_w("SPEAKER_05", 0, 50)]
    vtt_segs = [_v("鈴木花子", 100, 150)]

    # オフセットなしでは重ならない
    result_no_offset = build_cluster_name_map(whisper_segs, vtt_segs)
    assert "SPEAKER_05" not in result_no_offset

    # オフセットありでは完全に重なる
    result_offset = build_cluster_name_map(whisper_segs, vtt_segs, vtt_offset_sec=100)
    assert "SPEAKER_05" in result_offset
    assert result_offset["SPEAKER_05"]["name"] == "鈴木花子"
    assert result_offset["SPEAKER_05"]["overlap_sec"] == 50.0


# --------------------------------------------------------------------------- #
# 2位が存在しない単独話者クラスタ → 確定
# --------------------------------------------------------------------------- #

def test_single_candidate_confirms_unconditionally():
    whisper_segs = [_w("SPEAKER_06", 0, 20)]
    vtt_segs = [_v("Bob Jones", 0, 20)]
    result = build_cluster_name_map(whisper_segs, vtt_segs)
    assert "SPEAKER_06" in result
    info = result["SPEAKER_06"]
    assert info["name"] == "Bob Jones"
    assert info["share"] == 1.0
    assert info["overlap_sec"] == 20.0


# --------------------------------------------------------------------------- #
# resolve_speaker_name — 正規化で姓が落ちるケースの補正
# --------------------------------------------------------------------------- #

def test_resolve_speaker_name_restores_dropped_surname():
    # ASCII・部分文字列・短い → VTT 名（括弧サフィックスなし）を採用
    assert resolve_speaker_name("William Dawson", "William") == "William Dawson"


def test_resolve_speaker_name_strips_paren_suffix_when_restoring():
    assert resolve_speaker_name("Bob Jones (RIKEN)", "Bob") == "Bob Jones"


def test_resolve_speaker_name_keeps_non_ascii_mapped_name():
    # 非ASCIIの場合は対象外（マッピング結果をそのまま採用）
    assert resolve_speaker_name("Yasumichi Aoki (RIKEN)", "青木 保道") == "青木 保道"


def test_resolve_speaker_name_keeps_unrelated_mapped_name():
    # mapped_name が VTT 名の部分文字列でない場合は変更しない
    assert resolve_speaker_name("William Dawson", "Charlie") == "Charlie"


def test_resolve_speaker_name_keeps_equal_length_match():
    # mapped_name が VTT 名（サフィックス除去後）と同じ長さ・内容なら変更不要
    assert resolve_speaker_name("Bob Jones", "Bob Jones") == "Bob Jones"
