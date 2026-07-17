# Argus AI — コマンド正式リファレンス

Slack から呼び出せる Argus AI の **8 コマンド**の正式リファレンス。全て `pm_qa_server.py`
の `build_app()` で `@app.command` 登録され、第一報は ephemeral（本人のみ可視）。
一部コマンドは追加で公開チャンネル・Canvas・Box にも出力する（各節参照）。

| コマンド | いつ使うか | 応答形式 | 典型的な所要時間 |
|---|---|---|---|
| `/argus-brief`      | 今週〜直近の状況を俯瞰して優先順位をつけたい | ephemeral（自分のみ） | 30〜60秒 |
| `/argus-today`      | 本日の動きと自分宛メンションを見落としなく確認したい | ephemeral | 20〜40秒 |
| `/argus-draft`      | アジェンダ・レポートの草案が欲しい | ephemeral | 30〜60秒 |
| `/argus-risk`       | 顕在化しているリスク・予兆を洗い出したい | ephemeral | 30〜60秒 |
| `/argus-direction`  | 前提・意思決定台帳から所見（停滞/矛盾等）を確認したい | ephemeral＋チャンネルに図 | 10〜30秒 |
| `/argus-investigate`| 「なぜ」「どこで決めた」の深掘り調査がしたい | ephemeral（`--to-*` で追加出力） | 30〜180秒 |
| `/argus-transcribe` | 会議の録音・録画から議事録を生成したい（`/transcribe` エイリアス可） | スレッドに投稿 | 10〜20分 |
| `/argus-narrate`    | PPTX/PDF資料をTTSナレーション付き動画にしたい | チャンネルに確認メッセージ→完了時ファイル投稿 | 数分〜 |

すべてのコマンドの第一報は本人だけに見える **ephemeral 返信**。例外:
`/argus-transcribe` の進捗通知（スレッド全員可視、完了/エラーのみ ephemeral）、
`/argus-direction` のグラフ画像（チャンネルへ公開投稿）、
`/argus-investigate` の `--to-slack`/`--to-canvas`/`--to-box`（明示指定時のみ）、
`/argus-narrate` のビルド確認・完了投稿（チャンネル公開、ボタン確認フロー）。

---

## 1. `/argus-brief` — 優先順位づけブリーフィング

今日やるべきことを優先度順に最大5件提示する。pm.db 統計・Slack 生メッセージ・議事録を総合分析。

### 使い方

```
/argus-brief                     # 直近30日を全体俯瞰
/argus-brief 60                  # 期間を60日に拡張
/argus-brief @富岳太郎               # 富岳太郎さん担当事項にフォーカス
/argus-brief Benchpark           # Benchpark 話題にフォーカス
/argus-brief 60 @富岳太郎 GPU性能    # 組み合わせも可
```

### 引数ルール

| トークン | 解釈 | 例 |
|---|---|---|
| 数字のみ   | 直近日数           | `60` → 過去60日分 |
| `@` 始まり | 担当者フォーカス   | `@富岳太郎` |
| その他文字 | 話題フォーカス     | `Benchpark` |

### 想定シーン

- 毎朝の 5 分レビュー
- 週初めに「自分のチームが今抱えている山」を把握
- 特定マイルストーンが遅延気味で、関連AI・決定を一望したい

---

## 2. `/argus-today` — 今日の活動サマリー（個人向け）

本日の Slack・議事録を 4 観点（議論・決定・AI・進捗）でサマライズし、さらに **実行者宛のメンション** を別セクションで生データ表示。

### 使い方

```
/argus-today
```

引数なし。本日分のデータのみが対象。

### 想定シーン

- **17時以降**に「今日1日の動き」と「自分宛メンション」をまとめて確認
- 退勤前のラップアップ（依頼・決定・AIの取りこぼしチェック）
- 全体向けの日次サマリーとは別に、自分宛メンションを含む個人視点のラップアップを取得

### 前提データ

16:50 の cron で当日分の Slack を取り込む処理が走るため、**17:00 以降** に実行するのが前提。それ以前に実行すると当日のメッセージが不完全な可能性がある。

### `/argus-brief` との違い

| 観点 | `/argus-brief` | `/argus-today` |
|---|---|---|
| 対象期間   | 直近30日（`--days` で変更可） | 本日のみ |
| 出力形式   | 優先アクション5件 | 4観点の活動サマリー |
| メンション | なし | あなた宛メンション別セクション |

---

## 3. `/argus-draft` — アジェンダ/レポート草案生成

会議アジェンダやレポートの草案を生成する（ハンドラ `_run_draft`）。応答は ephemeral。

### 使い方

```
/argus-draft 次回リーダー会議のアジェンダ案
/argus-draft M3進捗レポートの草案
```

### 想定シーン

- 会議前にたたき台となるアジェンダを素早く用意したい
- レポートの骨子を先に作ってから肉付けしたい

内部の生成ロジックの詳細（プロンプト構成・参照データ範囲）は本ドキュメントの
調査範囲外のため、ここでは概要のみ記載する。挙動の詳細確認が必要な場合は
`scripts/argus/` 配下の draft 関連実装を直接参照すること。

---

## 4. `/argus-risk` — リスク分析

顕在化しているリスクと放置すると問題になる予兆を優先度付きで列挙。`/argus-brief` と同じ引数ルール。

### 使い方

```
/argus-risk                      # 全プロジェクトのリスク俯瞰
/argus-risk 60                   # 期間拡大
/argus-risk @富岳太郎                # 特定担当者のリスク
/argus-risk Benchpark            # 特定話題のリスク
```

### 想定シーン

- 週次の役員会前に「報告すべき火種」を洗い出す
- 新しい責任範囲を引き継いだ直後に全体のリスク状況を理解
- `/argus-brief` で気になった項目をリスク観点で掘り下げ

---

## 5. `/argus-direction` — 所見レポート（意思決定台帳の点検）

前提・意思決定台帳（`ledger_edges` / `ledger_goals` / `ledger_assumptions` / `decisions`）
**のみ**を見て、機械的に検出できる「所見（finding）」を洗い出すコマンド。Slack 会話・議事録
は一切参照しない。実装は `scripts/argus/direction.py`。

> **旧仕様との違い**: 以前は「投入不足領域／未着手の目標／非収束の警告／決定クラスタ一覧」
> という表示だったが、現行は下記の**所見5種**方式に全面差し替え済み（旧表示は陳腐化・削除済み）。

LLM の裁量はごく限定的で、**(1)(2)(3)(5) は SQL のみで判定**、LLM が関与するのは
「クラスタの命名・要約」と「(4) トレードオフ衝突の判定」だけ（もっともらしい誤りを防ぐ設計）。

### 使い方

```
/argus-direction
```
（引数なし）

### 検出される所見5種

1. **停滞/未着手**（SQLのみ）— 重要な目標なのに紐づく決定が無い、または長期間更新が無い
2. **制約違反疑い** — 前提（assumption）と矛盾する決定が存在する疑い
3. **論点ブロック** — 特定の論点に決定が集中せず滞留している状態
4. **トレードオフ衝突**（LLM1回呼び出し。旧「非収束の警告」に相当する新定義）— 同じ目標に対し
   方向性が対立する決定群が併存している場合
5. **前提の健全性** — 前提（assumption）自体が古い・未検証など健全性に疑義がある場合

いずれの所見も無ければ「無い」と明言する（無理に何かを検出したように装わない）。

### 出力先

ephemeral 本文に加えて、グラフ画像を `files_upload_v2` でチャンネルへ投稿する（全員可視）。

### 想定シーン

- 「マイルストーンは順調なのに、なぜか手応えが無い」ときに構造面から原因を探る
- 前提と決定の矛盾・停滞領域を定期的に機械チェックしたい

### 前提データ・cron

決定事項が `ledger_goals`（目標）・`ledger_assumptions`（前提）に紐づいている必要がある
（`enrich_items.py` によるエンリッチメントで自動生成）。紐づけがまだ無い場合は所見なしと
表示される。**cron化されていない**（Slack コマンド／CLI からの手動実行のみ）。

---

## 6. `/argus-investigate` — マルチステップ調査（Agent）

LLM が自律的にツール（DB検索・FTS全文検索・Slackメッセージ取得・出力系）を選択しながら
最大 **5 ステップ**（timeout 480秒）で調査する。単発のキーワード検索から因果分析まで対応。
第一報は ephemeral。

### 使い方

```
/argus-investigate M3マイルストーンの遅延原因を調査して
/argus-investigate 先週の決定事項が実行されているか確認
/argus-investigate @富岳太郎 の負荷が高い原因を分析して
/argus-investigate 設計方針に関する最近の議論は？
/argus-investigate GPU Outsourcing Agreement の最新版はどこか
```

### 得意なこと

| 質問タイプ | 例 |
|---|---|
| 因果分析            | 「遅延の原因」「負荷集中の理由」 |
| クロスソース相関    | pm.db + 議事録 + Slack を跨いだ整合確認 |
| 過去決定の検索      | 「前に何を決めたか」「どの会議の決定か」 |
| ドキュメント探索    | BOX資料・外部Web記事のタイトル・共有者・URL |
| Explorer分析        | 複数視点（保守的/積極的/客観的/未来志向）での分析統合 |
| 構造化QA            | 「〇〇さんの担当」「M2のAI件数」など SQL 相当 |
| 過去実績の参照      | 「〇〇アプリはこれまで何を達成したか」 |

### `--file` — 特定ファイル(Box資料)へのスコープ検索

```
/argus-investigate この資料の要点は？ --file="GPU Outsourcing Agreement"
/argus-investigate 前提条件は何か --file="設計書" --to-box
```

ファイル名の一部を指定すると、embedding 済みの Box 資料 1 本だけにスコープした QA ができる。
`box_docs.db` でファイル名から `box_file_id` を解決し、該当資料のチャンクのみを検索対象に
**決定論的に**固定する（LLM のツール引数任せではなく、pin した record_ids で強制）。
ピン中は全文/資料検索（`search_text` 系）のみが有効になり、pm.db 構造化データ検索・
Slack 生ログ検索の 9 ツールは封鎖される（封鎖対象は下表参照）。ファイル名が0件解決の
場合は調査を開始せず**エラー停止**する。出力先フラグと併用可。

### 出力先フラグ

コマンド末尾に付けると、ephemeral 応答に加えて調査結果を追加の出力先にも送信する。
組み合わせ可能。**出力先フラグが使えるのは `/argus-investigate` のみ**（他コマンドには無い）。

```
/argus-investigate M3の遅延原因 --to-box          # 調査結果を Box (md) にアップロード
/argus-investigate M3の遅延原因 --to-slack         # 調査結果をこのチャンネルに公開投稿
/argus-investigate M3の遅延原因 --to-canvas        # 調査結果を Canvas に投稿
/argus-investigate M3の遅延原因 --to-box --to-slack  # 組み合わせも可
```

### 内部ツール一覧（現行15個。`agent_tools.py` の `TOOLS` が正）

| ツール | 情報源 | 説明 |
|---|---|---|
| `get_milestone_progress` | pm.db | マイルストーン完了率・期限・残日数 |
| `get_overdue_items` | pm.db | 期限超過アクションアイテム（担当者フィルタ可） |
| `get_assignee_workload` | pm.db | 担当者別オープン件数・超過件数 |
| `search_action_items` | pm.db | アクションアイテム条件検索 |
| `search_decisions` | pm.db | 決定事項キーワード検索 |
| `get_app_achievements` | pm.db（achievements） | アプリ別確定実績（過去の到達点） |
| `search_text` | qa_index.db（FTS5＋re-rank枠・HyDE） | 全文検索。`--file` 指定時はスコープ限定 |
| `search_text_hybrid` | qa_index.db（FTS5＋ベクトルRRF） | ハイブリッド検索。`--file` 指定時はスコープ限定 |
| `search_entity` | qa_index.db（pm_data/minutes/slack/box_docs × conservative/aggressive/objective/future_oriented） | Explorer マルチ視点分析 |
| `synthesize_answers` | LLMのみ | 複数 Explorer 回答の統合 |
| `search_mentions` | slack.db | ユーザーへのメンション・名指し集計 |
| `get_slack_messages` | slack.db | チャンネル生メッセージ取得 |
| `box_upload_file` | 出力（副作用） | Box アップロード（要確認） |
| `slack_post_message` | 出力（副作用） | Slack 投稿（要確認） |
| `canvas_post_content` | 出力（副作用） | Canvas 内容置換（要確認） |

**廃止済み（記載しない）**: `get_weekly_trends` / `get_unacknowledged_decisions` はコード上
存在しない（旧ドキュメントの記載は誤り、既に本ドキュメントから削除済み）。

**`--file` 決定論ピン時の封鎖ツール（9個）**: `search_entity`, `search_action_items`,
`search_decisions`, `get_app_achievements`, `get_milestone_progress`, `get_overdue_items`,
`get_assignee_workload`, `search_mentions`, `get_slack_messages`。ピン時に有効なのは
`search_text` / `search_text_hybrid` / `synthesize_answers` / 出力系3種のみ。

### 出力中のID参照

回答中に `a:670` / `d:42` のようなID参照が現れた場合、自動的に対象アイテムの冒頭60文字を併記するので、参照先をいちいち開かなくても意味が分かる。

### HyDE クエリ拡張による取りこぼし防止

`search_text` は **HyDE（Hypothetical Document Embeddings）風クエリ拡張** を自動で行う。ユーザーの質問語彙とドキュメント本文の語彙が乖離していても本文チャンクを拾えるようにする仕組み。

- 質問例: 「Claude Code のAPIの配布を希望する方々のリストは？」
- ドキュメント本文: 「富岳太郎 / 富岳花子 / 富岳次郎 …」（名前の羅列のみで「配布」「希望」が無い）

このギャップを埋めるため、LLM が元クエリから「本文に出てきそうな別表現」を 2 パターン生成し、**計 3 クエリで並列検索→重複排除→マージ** してから `_combined_score`（BM25 + 鮮度）降順で上位を選ぶ（LLMによる re-rank は現在無効化中。詳細は `docs/argus_system.md`「LLM re-ranking（現在無効）」を参照）。従来はファイル名や周辺メタを指定しないとヒットしなかった情報も、直接質問するだけで拾えるようになった。

同じ仕組みは `/argus-brief` / `/argus-risk` / エンリッチメント（判断者・根拠の補完）にも適用されており、質問語彙と記載語彙が違うケースで検索精度が改善される。

---

## 7. `/argus-transcribe`（`/transcribe` エイリアス） — 会議録音の文字起こし・議事録生成

Slack チャンネルにアップロードされた音声・動画ファイルをダウンロードし、Whisper → LLM 議事録生成までを自動実行してスレッドに投稿する。

### 使い方

```
/argus-transcribe GMT20260302-032528_Recording.mp4
/argus-transcribe 2026-04-20_Leader_Meeting.m4a
/argus-transcribe Recording.mp4 consensus=5    # Self-Consistency サンプル数を増やす
/argus-transcribe Recording.mp4 consensus=1    # 単発生成（Self-Consistency 無効）
```

ファイル名は太字 (`*foo.mp4*`) やコード記法 (`` `foo.mp4` ``) で囲まれていても自動で剥がす。
`consensus=N` は位置不問（ファイル名の前後どちらでも可）、既定値は **N=3**。

### Self-Consistency（議事録生成の複数サンプリング）

`consensus_n >= 2` で有効。進捗通知に「Self-consistency 有効: N=…」と表示される。
`N=1` を指定すると単発生成（Self-Consistency 無効）になる。

### 処理フロー

1. Slack からファイル検索・ダウンロード（同名 VTT も自動検出、Zoom形式対応）
2. 動画（mp4 等）の場合は **スライドOCR** 実行（ffmpeg scene detect + マルチモーダルLLM、
   抽出した固有名詞を initial_prompt / slide-context として注入）
3. Whisper large-v3 で文字起こし（Singularity SIF ＋ VAD）
4. ローカルLLM（gemma4）で議事録生成（マルチステージ: 抽出 → 統合 → 決定事項・AI 抽出、
   Self-Consistency 有効時は複数サンプルを統合）
5. スレッドに議事録ファイル（Markdown）をアップロード

### 品質向上の3系統（独立ON/OFF・共存可）

| 系統 | 効果 |
|---|---|
| **VTT話者情報** | Zoom自動文字起こしの話者名を統合 → 担当者推定精度向上 |
| **スライドOCR** | 固有名詞・技術用語・数値の誤変換を抑制 |
| **Whisper + LLM** | 高品質日本語文字起こし + 構造化抽出（常時有効） |

### 進捗通知

- 処理開始・DL完了・ASR完了・Stage 1/2/3 進捗 → **スレッドに投稿**（全員に可視）
- 最終完了・エラー → **ephemeral**（実行者のみ）

### 排他制御

同時実行は 1 ジョブのみ。処理中の再実行は現在のジョブ情報を表示してエラーを返す。

### 想定シーン

- Zoom 会議終了直後、録画ファイルをチャンネルに投げて即議事録化
- 「口頭で決まったこと」を pm.db に載せるため、聞き直し不要で AI・決定が抽出される

---

## 8. `/argus-narrate` — TTSナレーション付き動画生成

PPTX/PDF 等の資料から、TTS ナレーション付き mp4 を生成する（ハンドラ `_run_narrate`、
実装 `narrate.py`）。チャンネルに親メッセージ＋Block Kit ボタン（build/cancel）を投稿し、
ユーザー確認を経てからビルドを開始する確認フロー。

### 使い方

```
/argus-narrate 週次進捗報告.pptx
/argus-narrate 設計資料.pdf --lang en
```

### 引数

| 引数 | 説明 |
|---|---|
| ファイル名 | Slack上のPPTX/PDF等資料 |
| `--lang en` | ナレーションを英語で生成 |

### 想定シーン

- 会議に出られない関係者向けに、資料をナレーション動画化して共有したい

内部の生成ロジックの詳細（スライド分割・音声合成パイプラインの内部段階）は本ドキュメントの
調査範囲外のため、ここでは概要のみ記載する。挙動の詳細確認が必要な場合は
`scripts/argus/narrate.py` を直接参照すること。

---

## CLI 実行

Slack コマンドの一部は CLI からも同じ処理を実行できる。実行前に必ず以下を行う:

```sh
source ~/.secrets/slack_tokens.sh
source ~/.secrets/rivault_tokens.sh
# PYTHONPATH=scripts、venv は uname -m で aarch64/x86_64 を確認して明示指定
```

### `/argus-investigate` 相当（`scripts/argus/pm_argus_agent.py`）

```sh
PYTHONPATH=scripts ~/.venv_x86_64/bin/python3 scripts/argus/pm_argus_agent.py \
  --investigate "M3の進捗状況とリスク" \
  --to-slack C12345XXX --to-box --to-canvas \
  --max-steps 3 --days 14
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--investigate TEXT` | 必須 | 調査内容 |
| `--max-steps N` | `5` | 最大ステップ数 |
| `--timeout SEC` | `480` | タイムアウト（秒） |
| `--days N` | `30` | 直近何日分を対象にするか |
| `--since` | - | 開始日を明示指定 |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--no-encrypt` | - | 平文モード |
| `--dry-run` | - | LLM 呼び出しなし（シードデータ確認用） |
| `--to-box` | - | 調査結果を Box にアップロード |
| `--to-slack CHANNEL_ID` | - | 調査結果を Slack チャンネルに投稿 |
| `--to-canvas` | - | 調査結果を Canvas に投稿 |
| `--no-intent-header` | - | 意図ヘッダーを省略 |
| `--context-file` | - | 追加コンテキストファイルを注入 |
| `--file TEXT` | - | ファイル名の一部を指定し、その Box 資料 1 本にスコープして QA する |

### `/argus-brief` / `/argus-risk` / `/argus-direction` / `/argus-today` 相当（`scripts/argus/pm_argus.py`）

| オプション | 説明 |
|---|---|
| `--brief-to-canvas` | ブリーフィングを Canvas に投稿 |
| `--risk` | リスクモードで実行 |
| `--direction` | 所見レポートモードで実行 |
| `--canvas-id` | 投稿先 Canvas ID を明示指定 |
| `--today-only` | today 相当（本日分のみ） |
| `--assignee` | 担当者フォーカス |
| `--topic` | 話題フォーカス |
| `--since` / `--days` | 対象期間 |
| `--index-name` | 検索対象 index の限定 |
| `--db` | pm.db のパス |
| `--dry-run` / `--no-encrypt` | 動作確認・平文モード |

---

## cron 定期実行

| 時刻 | スクリプト | 内容 |
|---|---|---|
| 06:57 月〜金 | `pm_argus_daily.sh` | `/argus-brief` 相当 → Canvas |
| 16:50 月〜金 | `pm_from_slack_daily.sh` | Slack 当日分取込 → `/argus-today` 前提データ整備 |
| 17:00 月〜金 | `pm_argus_daily_summary.sh` | `/argus-brief --today-only` 相当 |

`/argus-direction` は cron 化されていない（Slack コマンド／CLI からの手動実行のみ）。

