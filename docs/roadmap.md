## ロードマップ

### フェーズ1: Slack DB化と差分処理（実装済み）

- SlackメッセージをチャンネルごとのSQLiteに永続化
- 新規・更新スレッドのみ取得・要約（変化なしはAPI/LLM呼び出しゼロ）
- 全スレッド要約からCanvasに全体要約を投稿

### フェーズ2: 会議議事録との統合（実装済み）

- `meetings/*.md` をLLMで解析し `data/minutes/{kind}.db` に詳細保存（`pm_minutes_import.py`）、その後 `pm_ingest.py minutes` で `pm.db` に転記
- Slack要約から決定事項・アクションアイテムを抽出し `pm.db` に保存（`pm_ingest.py slack`）
- `source_ref` により背景（会議議事録 or Slackスレッド）に常に遡れる設計
- 差分処理: `slack_extractions` テーブルで抽出済みスレッドを管理、変化なしはLLM呼び出しゼロ

### フェーズ3: PMレポートと次回会議アジェンダ自動生成（実装済み）

- 未完了アクションアイテム一覧（担当者・期限付き）の自動生成
- 次回会議アジェンダ草案（未解決課題 + Slackで浮上した検討事項）
- 週次/月次進捗レポート
- リスク検知（「問題」「障害」「遅延」等を含むアイテムへの自動フラグ）

### フェーズ4: インポート済み議事録の記録（実装済み）

- `pm_minutes_import.py --list` で議事録DBにインポート済みの一覧を表示できる
- `pm_ingest.py minutes --minutes-list` で pm.db に転記済みの会議一覧を表示できる
  - 開催日・決定数・AI数・登録日時・meeting_id を一覧表示
  - `--since YYYY-MM-DD` と組み合わせて期間絞り込みも可能
  - 再インポート・抜け漏れ確認・監査証跡として活用できる

```sh
# 議事録DBの一覧表示
python3 scripts/pm_minutes_import.py --list

# pm.db 転記済み一覧
python3 scripts/ingest/pm_ingest.py minutes --minutes-list --since 2026-02-01
```

### フェーズ5: ゴール・マイルストーン管理と達成状況トラッキング（実装済み）

ボトムアップのタスク管理（フェーズ1〜3）に加え、プロジェクトのゴールとマイルストーンを
トップダウンで定義し、現在地を把握できるようにする。

#### 5.1 ゴール・マイルストーンの外部定義（goals.yaml）

- プロジェクトのゴールと主要マイルストーンを `goals.yaml`（リポジトリ直下）に人手で定義する
  - LLMによる自動抽出ではなく、意思決定者（近藤部門長・青木先生等）が承認した内容を記述
  - git で変更履歴を管理することで、マイルストーン変更の経緯を追跡できる
- `goals.yaml` のスキーマ:
  ```yaml
  project: 富岳NEXT
  goals:
    - id: G1
      name: AI-HPCプラットフォームとして世界最高水準の性能達成
  milestones:
    - id: M1
      goal_id: G1
      name: 基本設計完了
      due_date: 2026-03-18
      success_criteria:
        - 基本設計技術報告書（最終版）の提出
        - NVIDIAとの性能倍率合意
      area: 全エリア
  ```

#### 5.2 pm.db へのロード・完全同期（pm_ingest.py goals）

- `goals.yaml` を読み込み `goals` / `milestones` テーブルに完全同期する
  - yaml に存在するID → upsert（追加・更新）
  - yaml から削除されたID → DBからも削除。紐づく `action_items.milestone_id` は NULL にリセット
- `--dry-run` で追加・更新・削除の予定を確認してからDB操作なしで終了できる
- `--list` で登録済みゴール・マイルストーンと達成状況（完了率）を一覧表示

#### 5.3 アクションアイテムとマイルストーンの紐づけ

- `action_items` テーブルに `milestone_id` 列を追加済み
- `pm_ingest.py slack` / `pm_minutes_import.py` の抽出時に、マイルストーン一覧をLLMの文脈として
  渡し、各アクションアイテムをどのマイルストーンに紐づけるかを推定させる
- インポート済みアイテムの紐づけ修正は `pm_relink.py` でCSV経由で一括編集できる

#### 5.4 達成状況レポート（pm_report.py 拡張）

- レポートの冒頭に「プロジェクトの現在地」セクションを追加済み
  - 各マイルストーンについて、期限・達成条件・関連アクションアイテムの完了率をDBから直接計算
  - 現在日付と期限を照合し、達成済み・進行中・未着手・遅延を自動判定（LLM不使用）

#### 5.5 ドキュメントレジストリ（pm_slack_box_links.py、実装済み）

- Slack投稿中のBOXリンクを自動収集し、ローカルLLMでメタデータ（タイトル・種別・説明・共有者・トピック）を構造化して `docs_{index_name}.db` に保存する
- `pm_embed.py` でFTS5インデックスに組み込み、`/argus-investigate` から検索可能にする
- Canvas投稿機能でドキュメント一覧を共有できる

#### 5.6 ハイブリッド検索（pm_qa_server.py、実装済み）

- `/argus-investigate` でテキスト検索（FTS5）に加え、pm.dbの構造化データ（担当者・期限・マイルストーン・統計）をSQLで直接クエリできるようになった
- LLMによるIntent分類で質問を `structured` / `text` / `hybrid` に自動分類
- 「富岳太郎さんの担当タスクは？」「期限超過アイテムは？」のような構造化質問に直接回答可能

#### 5.7 外部Web情報の取り込み（pm_web_fetch.py、実装済み）

- RIKEN公式サイト・HPC系ニュース（Top500/HPCwire/insideHPC）・NVIDIAブログなどの公開情報を定期取得し `/argus-investigate` で検索可能にする
- `data/web_sources.yaml` でソース・キーワードフィルタ・対象インデックスを定義し、`pm_web_fetch.py` が `data/web_articles.db` に保存する
- `pm_embed.py --web-only` で高速に FTS5 インデックスに組み込み（議事録・Slack処理をスキップ）
- `pm_web_fetch.py` を cron（毎朝03:30 JST）で定期実行。FTS5 組み込みは `pm_box_update.sh`（`pm_embed.py`）が自動で行う
- 出典は `top500.org / Web記事 (2025-11-15)` 形式で表示される

#### 5.8 ナレッジ蒸留レイヤ（pm_box_distill.py、実装済み 2026-05-18）

- BOX 本文 / 議事録 / `pm.db.decisions` を入力として、LLM (gemma4) で「意思決定 / 制約 / 立場 / 用語」の単位に蒸留して `data/knowledge.db` に格納する
- 二段ゲート: Stage 1 (gemma4 抽出) → Stage 2 (bge-m3 で類似度判定 + Kimi で keep/drop/merge_with)
- bge-m3 は RiVault が提供 (`bge-m3:567m`)。`/v1/embeddings` 経由でローカル GPU 不要
- brief / risk / today のプロンプトに常時同梱（プロジェクト全体共通、`index_name` 分割なし）
- investigate に `search_knowledge` / `get_knowledge` ツールを追加。回答末尾に「## 引用したナレッジ」+ 修正導線
- 人手介入: `pm_knowledge_edit.py` (CLI) / `/argus-knowledge` (Slack)。物理削除なし、`deleted=1` の論理削除のみ
- 矛盾検知: Patrol Agent `detect_knowledge_conflicts` がリーダー会議チャンネルへ通知
- 詳細: `docs/distill_policy.md` / `docs/schema.md`「data/knowledge.db」/ `docs/architecture.md`「Pass 3」

---

## 今後の課題（PMフレームワーク観点での欠落領域）

PMBOKの観点から現システムを評価した結果、以下の領域が未実装。優先度順に記載。

### P1: 担当者別負荷の可視化（未実装）

- 担当者ごとのオープンアクションアイテム件数・期限超過件数を集計する
- `pm_report.py` のレポートに「担当者別負荷」セクションを追加する
- 特定の人への集中・無担当アイテムの検出に活用する

### P2: 達成証跡の登録（pm_document_import.py、未実装）

- success_criteria（例:「基本設計技術報告書の提出」）に対して実際の資料を紐づける
- `documents` テーブルと `pm_document_import.py` を実装する（5.5 参照）
- マイルストーンの「達成済」判定を自動化する足がかりとなる

### P3: 変更管理コメント（運用ルール）

- goals.yaml の変更時に `changed_by`・`change_reason` フィールドを記述するルールを設ける
- システム実装ではなく **運用ルールとして CLAUDE.md / goals.yaml のコメントに明記** する
- 例: マイルストーン期限変更時に `change_reason: "NVIDIAとの合意に基づき1週間延期"` を追記する

### P4: 自動化（cron/定期実行、未実装）

- Slack取得・抽出・レポート投稿がすべて手動トリガーであり運用負荷がかかる
- Slurm の定期ジョブ、または crontab で以下を自動化する
  - 平日朝: `slack_pipeline.py` → `pm_ingest.py slack`
  - 月曜朝: `pm_report.py`（週次レポートのCanvas投稿）

### P5: 意思決定の文脈記録（運用ルール）

- 決定事項の「なぜその決定をしたか」「却下された代替案」が記録されない
- システム実装ではなく **議事録の記述粒度に関する運用ガイドライン** として対処する
- 特に重要な意思決定については議事録中に背景・代替案・却下理由を明示的に発言・記録する

### P6: Slackアプリによるpm.db編集UI（未実装）

- Web UIはセキュリティリスクが高いため、Slackアプリ（Socket Mode）でDB操作UIを提供する
- 想定機能:
  - アクションアイテムの担当者・期限・マイルストーン・状況を対話的に編集（`pm_relink.py` 相当）
  - 決定事項の内容・日付を編集（`pm_relink.py` 相当）
  - `pm_report.py` の実行・Canvas投稿をSlackコマンドから起動
  - `pm_sync_canvas.py` の実行（Canvas上の編集をpm.dbに反映）をSlackコマンドから起動
- 実装方針: `slack_sdk` の Socket Mode（`SocketModeHandler`）を使用、`SLACK_APP_TOKEN` が追加で必要

### P7: 外部ステークホルダー状態の追跡（未実装）

- NVIDIA・富士通へのアクションアイテムが何件未回答か一覧できない
- `action_items` の `assignee` フィールドに組織名（NVIDIA / 富士通）を含めるルールを設け、
  `pm_report.py` の要注意セクションで外部待ちアイテムを自動抽出・強調する


### P8: マルチモーダル動画解析による議事録品質向上

Zoom会議の録画動画から、音声・テキスト・映像の複数チャネルを使って議事録の精度を向上させる。

#### 8.1 話者同定（VTT 由来で実装済み、2026-04）

- Zoom クラウド録画には自動文字起こし VTT (`*.transcript.vtt`) が同梱され、これに **正確な参加者名** が記載されている
- `transcribe_pipeline.py` が録画と同名の VTT を自動ダウンロード、`generate_minutes_local.py` の Stage 3 が Whisper の高品質本文と VTT の話者名を統合してアクションアイテムの担当者を推定する
- 当初検討していた「ギャラリービューの画面 OCR で話者を読む」案は不採用 — 現運用の Zoom 録画はクラウド録画のスピーカービュー / 画面共有が主で、ギャラリー枠が映らないため画像から話者を特定できない。VTT 同梱で目的が達成されるため画像経路は不要
- VTT が存在しない録画（手元 mp4 等）では Whisper + pyannote の話者分離で `Speaker 0/1/2...` ラベルになり、人手で実名化する運用は残る

#### 8.2 スライド・資料OCRによる専門用語補正（実装済み、2026-05）

- `scripts/recording/slide_ocr.py` が ffmpeg scene detect でスライド切り替わりのフレームを抽出し、マルチモーダルLLM（`OPENAI_API_BASE`）で Markdown 化する
- 得られた結果は 2 系統で議事録品質に反映される:
  - 固有名詞リスト（terminology.txt）を `whisper_vad.py` の `--initial-prompt-extra` に渡し ASR 段階で誤変換を抑制
  - スライド文脈（slide_context.md）を `generate_minutes_local.py` の Stage 1/2/3 プロンプトに同梱し、LLM に固有名詞の ground truth を与える
- `pm_from_recording.sh`（ローカル版）と `transcribe_pipeline.py`（Slack経由）の両方で有効化済み
- スライドなしの会議（frames=0）や mp4 以外、`OPENAI_API_BASE` 未設定時は自動スキップで既存動作にフォールバック
- `--no-slide-ocr` で無効化可能（`pm_from_recording.sh`）

#### 実装上の考慮事項

- マルチモーダル推論はvLLMの別ポートまたは同一サーバーでの逐次処理で実行
- VTT が利用できない録画（外部ツール録画など）では話者ラベルが `Speaker N` になるため、議事録からの担当者抽出精度が落ちる。これは VTT 取得経路の整備が解決策で、画像 OCR は迂回手段にならない

### 参考: 現時点で対応済みの弱点

| 課題 | 対応状況 |
|---|---|
| Canvas編集の上書きで変更前が消える | ✅ audit_log で解決（2026-03） |
| 会議録が平文でディスクに残る | ✅ --meeting-name オプションで解決（2026-03） |
| LLM抽出品質の保証 | ⚠️ バリデーションサブエージェント未実装（P4以降で検討） |
