#!/usr/bin/env bash
# fish_seed_sweep.sh - fish-speech の seed ごとにサンプル音声を生成して聴き比べる
#
# Usage:
#   bash scripts/fish_seed_sweep.sh
#   bash scripts/fish_seed_sweep.sh --reference-id hikaru
#   bash scripts/fish_seed_sweep.sh --reference-id jp_f --lang ja
#   bash scripts/fish_seed_sweep.sh --text "Hello, this is a test." --seeds "1 7 42 100"
#   bash scripts/fish_seed_sweep.sh --play   # 生成後に順番に再生
#
# 出力: /tmp/fish_seed_sweep/seed_<N>_<lang>.wav

set -euo pipefail

FISH_HOST="${FISH_TTS_HOST:-http://localhost:8080}"
TEXT_JA="これはテスト音声です。Argus AI のナレーションに使用します。"
TEXT_EN="This is a test. Argus AI will use this voice for narration."
SEEDS="1 7 42 100 123 256 512 999 1234 9999"
OUT_DIR="/tmp/fish_seed_sweep"
PLAY=0
EMOTION="${FISH_EMOTION:-}"
REFERENCE_ID=""
LANG="both"   # ja / en / both

# オプション解析
while [[ $# -gt 0 ]]; do
    case "$1" in
        --text)         TEXT_JA="$2"; TEXT_EN="$2"; shift 2 ;;
        --seeds)        SEEDS="$2"; shift 2 ;;
        --out)          OUT_DIR="$2"; shift 2 ;;
        --play)         PLAY=1; shift ;;
        --emotion)      EMOTION="$2"; shift 2 ;;
        --reference-id) REFERENCE_ID="$2"; shift 2 ;;
        --lang)         LANG="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,11p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$OUT_DIR"

# 対象言語を決定
if [[ "$LANG" == "both" ]]; then
    LANGS="ja en"
elif [[ "$LANG" == "ja" || "$LANG" == "en" ]]; then
    LANGS="$LANG"
else
    echo "不明な --lang 値: $LANG（ja / en / both）" >&2
    exit 1
fi

for lang in $LANGS; do
    if [[ "$lang" == "ja" ]]; then
        text="$TEXT_JA"
    else
        text="$TEXT_EN"
    fi
    if [[ -n "$EMOTION" ]]; then
        text="[$EMOTION] $text"
    fi

    echo "=== lang=$lang reference=${REFERENCE_ID:-none} emotion=${EMOTION:-none} ==="
    for seed in $SEEDS; do
        out="$OUT_DIR/seed_${seed}_${lang}.wav"

        # JSON ペイロード構築（reference_id は指定時のみ含める）
        payload=$(python3 -c "
import json, sys
d = {
    'text': sys.argv[1],
    'format': 'wav',
    'normalize': True,
    'streaming': False,
    'seed': int(sys.argv[2]),
}
if sys.argv[3]:
    d['reference_id'] = sys.argv[3]
print(json.dumps(d))
" "$text" "$seed" "$REFERENCE_ID")

        http_code=$(curl -s -X POST "$FISH_HOST/v1/tts" \
            -H "Content-Type: application/json" \
            -d "$payload" \
            -o "$out" -w "%{http_code}")
        if [[ "$http_code" == "200" ]]; then
            size=$(du -h "$out" | cut -f1)
            echo "  seed=$seed → $out ($size)"
        else
            echo "  seed=$seed → 失敗 (HTTP $http_code)" >&2
        fi
    done
done

echo ""
echo "完了: $OUT_DIR"
echo ""
echo "再生コマンド例:"
for lang in $LANGS; do
    echo "  for f in $OUT_DIR/seed_*_${lang}.wav; do echo \"\$f\"; ffplay -nodisp -autoexit \"\$f\" 2>/dev/null; done"
done

if [[ "$PLAY" == "1" ]]; then
    for lang in $LANGS; do
        echo ""
        echo "=== 再生開始 ($lang) ==="
        for f in "$OUT_DIR"/seed_*_${lang}.wav; do
            echo "再生中: $f"
            ffplay -nodisp -autoexit "$f" 2>/dev/null
        done
    done
fi
