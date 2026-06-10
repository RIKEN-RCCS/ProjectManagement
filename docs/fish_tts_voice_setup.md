# fish-speech 自分の声をナレーションに使う設定

## 概要

`/argus-narrate` のナレーション音声を自分の声（リファレンス音声）に切り替える手順。
日本語・英語で異なるリファレンスを使い分けることもできる。

---

## アーキテクチャ

```
/argus-narrate slides.pptx
  ↓ Slack Socket Mode
pm_qa_server.py
  ↓ build_slide_video(lang=ja/en)
    ↓ _synth_narration_to_wav(reference_id=...)
      ↓ pm_tts.synth_chunk(reference_id=...)
        ↓ pm_tts._fish_synth_chunk(reference_id=...)
          ↓ POST http://localhost:8080/v1/tts {"reference_id": "hikaru"}
fish-speech サーバー（別プロセス、port 8080）
  ↓ references/hikaru/sample.wav を使って音声合成
```

fish-speech サーバーと pm_qa_server は**別プロセス**。  
`pm_daemon.sh start qa` は Slack デーモン（pm_qa_server）のみ起動する。

---

## リファレンス音声の登録

```bash
curl -X POST http://localhost:8080/v1/references/add \
    -F "id=hikaru" \
    -F "audio=@my_voice.wav" \
    -F "text=録音時に読み上げたテキスト"
```

登録確認:

```bash
curl -H "Accept: application/json" \
    "http://localhost:8080/v1/references/get?reference_id=hikaru"
```

一覧確認:

```bash
curl -H "Accept: application/json" \
    "http://localhost:8080/v1/references/list"
```

> **注意**: fish-speech API はデフォルトで `application/msgpack`（バイナリ）を返す。  
> JSON で受け取るには `-H "Accept: application/json"` が必要。

---

## 環境変数の設定

`~/.secrets/fish_tts.sh` に以下を記載する:

```bash
export FISH_TTS_HOST=http://localhost:8080
export FISH_REFERENCE_ID=hikaru          # 日本語ナレーション用リファレンスID
export FISH_REFERENCE_ID_EN=hikaru_en    # 英語ナレーション用リファレンスID（任意）
export FISH_SEED=42                      # 同じ seed で毎回同じ声質になる（0=ランダム）
export FISH_EMOTION=excited              # 感情トーン（後述）
```

`pm_daemon.sh start qa` は `SVC_FISH=1` が立っている場合にこのファイルを自動 source する。  
`qa` サービスはデフォルトで `SVC_FISH=1`（`pm_daemon.sh` の `SERVICES` 定義参照）。

---

## 日本語・英語でリファレンスを切り替える仕組み

`/argus-narrate slides.pptx --lang en` のように `--lang en` を付けると:

- `build_slide_video.py` が `FISH_REFERENCE_ID_EN` を読み込み、`reference_id` として渡す
- `FISH_REFERENCE_ID_EN` が未設定の場合は `FISH_REFERENCE_ID` にフォールバック

`--lang ja`（デフォルト）では常に `FISH_REFERENCE_ID` が使われる。

---

## FISH_EMOTION について

テキストの先頭に `[emotion]` プレフィックスを付けて fish-speech に渡す仕組み。  
モデルが学習で見た表現であれば自由テキストとして効く。代表的な値:

| 値 | 説明 |
|---|---|
| `excited` | 興奮・明るい（現在の設定） |
| `happy` | 明るい |
| `calm` | 落ち着いた |
| `sad` | 悲しげ |
| `angry` | 怒り |
| `fearful` | 恐れ |
| `disgusted` | 嫌悪 |
| `surprised` | 驚き |
| `whispering` | ささやき声 |

空文字の場合はプレフィックスなし（無指定）で合成される。

---

## デーモン再起動手順

環境変数を変更した後は pm_qa_server を再起動する。  
**`start` だけでは起動中なら何もしない**ため、`stop` → `start` の順で実行すること。

```bash
bash scripts/pm_daemon.sh stop qa
bash scripts/pm_daemon.sh start qa
```

反映確認:

```bash
pid=$(cat logs/pm_qa_server.pid)
tr '\0' '\n' < /proc/$pid/environ | grep FISH_REFERENCE_ID
```

fish-speech サーバー（port 8080）の再起動は**不要**（クライアント側の変更のみのため）。

---

## リファレンス関連 API エンドポイント一覧

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/v1/references/add` | 音声ファイルとテキストを登録 |
| GET | `/v1/references/list` | 登録済み ID 一覧 |
| GET | `/v1/references/get?reference_id=<id>` | 個別詳細（テキスト・ファイル名・サイズ） |
| DELETE | `/v1/references/delete` | 削除 |
| POST | `/v1/references/update` | ID のリネーム |
