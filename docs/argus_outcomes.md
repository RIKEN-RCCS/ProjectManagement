# Argus AI — PM成果ガイド

プロジェクトマネージャーが**Argusで実現できる成果**と、その背景にある機能・システム設計の対応図。

---

## 成果1: 朝の優先順位づけ（5分で完了）

**ユーザーの得られるもの**: 今日やるべきことを優先度順に最大5件。前日の決定事項・期限超過・Slack動向を総合分析。

| 流れ | 担当 | 実装 |
|---|---|---|
| Slack へ `/argus-brief` を実行 | ユーザー | — |
| pm_qa_server.py がリアルタイム分析 | Argus | `pm_argus.py` |
| • 過去60日の Slack メッセージを収集 | — | argus_config.yaml チャンネル定義 |
| • 該当期間の議事録本文を取得 | — | data/minutes/{kind}.db（会議DB） |
| • pm.db の統計（期限超過・負荷） | — | db_utils.fetch_* 関数 |
| • LLM が優先度づけ（gemma4/RiVault） | — | call_argus_llm() |
| • 本人だけに見える返信（ephemeral） | — | Slack Socket Mode |
| ユーザーが確認・実行 | ユーザー | — |

**オプション引数で焦点を変更**:
- `/argus-brief` ← 全体俯瞰
- `/argus-brief 60` ← 過去60日分に拡大
- `/argus-brief @西澤` ← 西澤さん担当事項に焦点
- `/argus-brief Benchpark` ← Benchpark 話題に焦点

**自動化**: 平日朝 8:57 に Canvas へ自動投稿（cron）→ 全員が朝礼前に確認可能

---

## 成果2: リスク早期検知（24/7 自動巡回）

**ユーザーの得られるもの**: 顕在化しているリスクと放置すると問題になりうる予兆を優先度付きで自動発見。

| リスク種別 | 検知機構 | 通知先 | 判定ロジック |
|---|---|---|---|
| **完了シグナル検知** | Patrol Agent | 担当者（DM + Block Kit） | キーワード + LLM 自然言語判定の二段構え |
| **期限超過リマインダー** | Patrol Agent | 担当者（DM） | `due_date < today`（7日ごと） |
| **期限前警告** | Patrol Agent | 担当者（DM） | `today + 3日 >= due_date`（期限前1回） |
| **未確認決定事項** | Patrol Agent | リーダー会議チャンネル | `decided_at <= today - 7日` かつ `acknowledged_at IS NULL`（7日ごと） |
| **長期停滞** | Patrol Agent | 担当者 + リーダー会議チャンネル | 14日間更新なし（14日ごと） |
| **マイルストーン健全性** | Patrol Agent | リーダー会議チャンネル | `完了率 < 経過割合 × 0.7`（7日ごと） |
| **週次トレンド悪化** | Patrol Agent | リーダー会議チャンネル | `直近2週完了 < 前2週完了 × 0.5`（7日ごと） |

**仕組み**:
- `patrol_detect.py` が決定論的ルール（LLM不使用）で検出
- `patrol_actions.py` が Slack 通知・Block Kit ボタン送信
- `patrol_state.db` で冪等性・cooldown管理（同じリスクの重複通知を防止）
- **完了確認フロー**: 担当者が Block Kit の「完了にする」ボタンを押す → `patrol_confirm.py` が pm.db に反映（audit_log 付き）

**自動実行**: 平日30分ごと（cron）→ 常時ウォッチドッグ

---

## 成果3: リスク・課題の根本原因分析（10分で深掘り）

**ユーザーの得られるもの**: 単発ブリーフィング（成果1）では不可能な因果分析。LLMが自律的にツール選択して段階的に調査。

| 質問例 | LLMが選ぶツール | 情報源 | 回答内容 |
|---|---|---|---|
| `M3マイルストーンの遅延原因を調査して` | get_milestone_progress → get_overdue_items → search_decisions | pm.db + 議事録 | M3の完了率が期待値を下回る理由・関連決定事項・期限超過AI |
| `先週の決定事項が実行されているか確認` | get_unacknowledged_decisions → search_action_items → search_text | pm.db + Slack | 未確認決定のリスト・関連AI状況・実行進捗 |
| `@西澤 の負荷が高い原因を分析して` | get_assignee_workload → search_action_items → search_text | pm.db + Slack | 西澤さんの open AI 件数・分野・期限・外部待ちの有無 |
| `GPU性能に関する最近の議論は？` | search_text | FTS5 インデックス | 議事録・Slack の生メッセージから関連チャンク抽出・LLM re-ranking で上位5件 |

**技術基盤** (`pm_argus_agent.py`):
- LLM が `<tool_call>` タグで必要なツールを指定
- ツール実行結果を LLM にフィードバック
- 最大5ステップまで反復（180秒タイムアウト）
- すべて本人だけに見える ephemeral 返信

| ツール | 実装 | 用途 |
|---|---|---|
| `get_milestone_progress` | db_utils.py | マイルストーン完了率・残日数 |
| `get_overdue_items` | db_utils.py | 期限超過AI（フィルタ可） |
| `get_assignee_workload` | db_utils.py | 担当者別負荷 |
| `get_weekly_trends` | db_utils.py | 週次作成/完了トレンド |
| `get_unacknowledged_decisions` | db_utils.py | 未確認決定事項 |
| `search_action_items` | pm_argus_agent.py | AI の構造化検索（SQL） |
| `search_decisions` | pm_argus_agent.py | 決定事項の構造化検索 |
| `search_text` | pm_qa_server.py | 議事録・Slack 全文検索（FTS5 + LLM re-ranking） |
| `get_slack_messages` | pm_argus_agent.py | 特定チャンネルの生メッセージ |

---

## 成果4: 会議資料・メッセージ自動草案（3分で下書き）

**ユーザーの得られるもの**: 会議アジェンダ・進捗報告・確認依頼メッセージの草案を即座に生成。

| 用途 | 指定方法 | 主な情報源 | 草案の内容例 |
|---|---|---|---|
| アジェンダ | `/argus-draft agenda 次回リーダー会議` | 未確認決定 + 期限超過AI + 直近Slack | • 確認待ち決定事項3件 • 期限超過AI（GPU性能評価など） • Slack で浮上した検討事項 |
| 進捗報告 | `/argus-draft report 4月進捗報告` | マイルストーン進捗 + 週次完了 + 担当者負荷 | • M1 75% 完了（残り3件） • 完了数が前週比20%減 • 期限超過が5件 |
| 確認依頼 | `/argus-draft request NVIDIAへの性能確認` | 期限超過 + 担当者別負荷 + 直近Slack | • 外部待ちAI件数 • 関連決定事項 • 期待納期 |

**実装** (`pm_argus.py` の draft ハンドラ):
- pm.db + Slack 生メッセージ + 議事録から関連情報を LLM コンテキストとして構築
- ユーザーが「どの情報を含めるか」判断する（人間の最終確認）

---

## 成果5: 会議録音 → 議事録（Slack アップロード）

**ユーザーの得られるもの**: 音声ファイルをスレッドにアップロード → 数分待つと議事録（決定・AI付き）が自動生成。

| ステップ | 実装 | 時間 |
|---|---|---|
| ユーザーが Slack にファイルアップロード → `/argus-transcribe GMT20260302-032528_Recording.mp4` を実行 | pm_qa_server.py ソケットハンドラ | 3秒 |
| ファイル + 同名 VTT（Zoom自動文字起こし）を取得 | transcribe_pipeline.py | — |
| ffmpeg で WAV 変換（16kHz mono） | — | — |
| DeepFilterNet ノイズ除去 | whisper_vad.py | — |
| Whisper large-v3 で高品質文字起こし | — | 5～10分 |
| ローカル LLM（gemma4）で議事録生成（マルチステージ） | generate_minutes_local.py | 3～5分 |
|  • Stage 1: チャンク抽出（話題ごと） | — | — |
|  • Stage 2: チャンク統合（段落化） | — | — |
|  • Stage 3: 決定事項・AI 抽出（VTT の話者情報活用で精度向上） | — | — |
| 完成した議事録（Markdown + 構造化 JSON） → スレッドに投稿 | pm_minutes_import.py | 1秒 |

**VTT 話者情報の活用**:
- Zoom の自動文字起こし VTT（例: `2026-04-28_Leader_Meeting.vtt`）があれば自動検出・ダウンロード
- VTT の正確な話者名（「西澤」「小林」など）を Whisper の高品質日本語文字起こしと統合
- アクションアイテムの担当者推定精度が大幅向上（LLM が正確な話者情報を参照）

**進捗通知**: Slack スレッド上でリアルタイム更新（「ダウンロード完了」→「文字起こし完了」→「Stage 1/2/3 処理中」→「完成」）

---

## 成果6: 知識検索・引用可能な情報源（「どこにあるか」を自動判定）

**ユーザーの得られるもの**: 「GPU性能に関する決定事項は？」と聞く → FTS5 インデックス経由で議事録・Slack・ドキュメント・Web記事を横断検索し、引用可能なチャンク + 出典（日時・会議名・投稿者）を返答。

| 情報源 | スコープ | 検索対象 | 出典形式 |
|---|---|---|---|
| **議事録** | 会議名ごと | 本文（チャンク分割） | 「2026-01-14 リーダー会議 / GPU性能評価」 |
| **Slack 生メッセージ** | チャンネルごと | スレッド単位でまとめたチャンク | 「C08SXA4M7JT（20_1_リーダ会議メンバ）/ @西澤」 |
| **ドキュメント** | BOXリンク | タイトル・説明・種別・共有者 | 「BOX共有ドキュメント（2026-04-15 共有）」 |
| **Web 記事** | 外部公開情報 | RIKEN 公式・HPC ニュース・NVIDIA ブログ | 「top500.org（2026-04-20）」 |

**検索アルゴリズム** (`pm_embed.py` + FTS5):
- SudachiPy 形態素解析で質問を解析
- `search_text` ツール（pm_qa_server.py）が FTS5 で最大30件取得
- LLM が質問との関連度を判定して上位5件に絞り込み（re-ranking）
- インライン引用形式で回答に組み込む

**自動更新**: `pm_from_slack.sh` / `pm_from_recording.sh` / `pm_web_fetch.py` 実行後に `pm_embed.py` が差分インデックスを更新（追加分のみ）

---

## 成果7: 情報の散逸防止（「誰が言った」「いつ決めた」の遡路が常に確保）

**ユーザーの得られるもの**: 決定事項・アクションアイテムの背景が常に遡れる。「誰が何を言ったのか」「どの会議で決めたのか」の根拠を失わない。

| 情報 | 記録先 | アクセス方法 | 用途 |
|---|---|---|---|
| **決定の根拠** | `decisions.source_context`（LLM 抽出） | `/argus-investigate` で `search_decisions` | 「なぜその決定をしたか」を再確認・議論 |
| **AI の発生源** | `action_items.source_ref`（パーマリンク or 議事録ID） | Canvas 上で「source」列クリック | Slack スレッド or 議事録本文へ一発ジャンプ |
| **変更履歴** | `audit_log` テーブル | `db_utils.py --audit-log` | 「いつ誰が何を変更したか」を監査 |
| **対応状況の経過** | Canvas `対応状況` 列（pm_sync_canvas.py） | Canvas 上で参照 | 「今どこまで進んでいるか」の進捗コメント履歴 |

**実装**:
- `pm_minutes_to_pm.py` が議事録から決定・AI を抽出時に `source_context` も記録
- `pm_extractor.py` が Slack から抽出時に permalink を記録
- `pm_sync_canvas.py` が Canvas 編集時に `audit_log` に記録

---

## 成果8: 進捗・負荷の可視化（週次レポート自動生成）

**ユーザーの得られるもの**: 毎週定期的に Canvas に進捗レポートが投稿される。マイルストーン進捗・期限超過・担当者別負荷・決定事項が表形式で一覧化。

| セクション | 内容 | 更新頻度 | 編集可否 |
|---|---|---|---|
| **プロジェクト現在地** | マイルストーン進捗（M1～M5の完了率） | 毎回更新（LLM 不使用） | 読み取り専用 |
| **直近の決定事項** | 過去1週の決定 + チェックボックス（確認済みか） | 毎回更新 | チェックボックス入力で `acknowledged_at` を記録 |
| **要注意事項** | 期限超過AI・長期停滞・未確認決定（ハイライト） | 毎回更新 | 読み取り専用 |
| **未完了アクションアイテム表** | ID・担当者・期限・MS・状況・内容・出典・対応状況 | 毎回更新 | 会議中に直接編集（pm_sync_canvas.py で pm.db に反映） |

**実装** (`pm_report.py`):
- `pm.db` から統計クエリで直接計算（LLM 不使用）
- Canvas に投稿（ユーザートークンで）
- 表形式は `format_utils.py` で自動整形

**自動実行**: 毎週月曜朝 9:00（cron）

---

## 成果まとめ表

| 成果 | 使用場面 | 実施主体 | 更新頻度 | リスク対応 |
|---|---|---|---|---|
| 朝の優先順位づけ | 毎朝 5分で確認 | ユーザー or Argus（cron） | 毎日 | 期限超過・決定事項の確認 |
| リスク自動検知 | 24/7 背景で常駐 | Argus（cron 30分ごと） | 常時 | 7種類のリスク検出・ボタン確認フロー |
| 根本原因分析 | 「なぜ遅れているのか」を深掘り | ユーザー発動 | オンデマンド | LLM マルチステップ調査 |
| 草案生成 | 会議資料・メッセージ作成 | ユーザー発動 | オンデマンド | LLM が関連情報を自動集約 |
| 会議録音 → 議事録 | 会議終了直後 | ユーザー発動 | オンデマンド | 自動文字起こし・決定抽出・担当者推定 |
| 知識検索 | 「過去に決めたことは？」を確認 | ユーザー発動 | オンデマンド | FTS5 + LLM re-ranking で精度向上 |
| 情報の散逆可能性 | 監査・議論・背景確認 | ユーザー確認 | 都度記録 | audit_log・source_ref・source_context |
| 週次進捗レポート | 月曜朝に全員で確認 | Argus（cron） | 毎週 | マイルストーン進捗・期限超過・負荷 |

---

## アーキテクチャ図（成果 → 機能マッピング）

```
成果層（ユーザーが見る）
  ↓
Slack スラッシュコマンド層
  /argus-brief        ← 成果1・2・4
  /argus-investigate  ← 成果3
  /argus-draft        ← 成果4
  /argus-risk         ← 成果2
  /argus-transcribe   ← 成果5
  ↓
データ分析・LLM 層
  pm_argus.py         ← データ収集・プロンプト構築
  pm_argus_agent.py   ← マルチステップ調査
  pm_argus_patrol.py  ← リスク検知・通知
  ↓
データ基盤層
  pm.db                    ← 構造化 PM データ（決定・AI・MS）
  data/minutes/{kind}.db   ← 議事録詳細・内容
  {channel_id}.db          ← Slack 生メッセージ
  data/qa_pm.db            ← FTS5 インデックス
  patrol_state.db          ← 冪等性・cooldown 管理
  ↓
LLM 層
  gemma4（ローカル）       ← 優先使用（128K context）
  RiVault               ← フォールバック（200K context）
```

---

## セットアップ（最小要件）

1. **pm_qa_start.sh 実行**
   - Slack トークン読み込み（`~/.secrets/slack_tokens.sh`）
   - Socket Mode デーモン起動
   - 全スラッシュコマンド有効化

2. **FTS5 インデックス構築**
   ```bash
   python3 scripts/pm_embed.py --full-rebuild
   ```

3. **Patrol cron 設定**
   ```
   */30 * * * 1-5  python3 scripts/pm_argus_patrol.py
   ```

4. **朝ブリーフィング cron 設定**
   ```
   57 8 * * 1-5  python3 scripts/pm_argus.py --brief-to-canvas
   ```

---

## よくある質問

**Q: 何を用意すればすぐに使える？**
A: `pm_qa_start.sh` を実行するだけ。Slack トークンと gemma4 が起動していれば全機能が動作。

**Q: LLM の品質が問題なら？**
A: gemma4 が落ちるなら RiVault にフォールバック。または `call_argus_llm()` を Claude API に切り替え可能（性能・コスト トレードオフあり）。

**Q: 検索結果が的外れなら？**
A: `pm_embed.py --full-rebuild` で FTS5 インデックスを再構築。SudachiPy 辞書の形態素解析精度に依存。

**Q: Canvas 編集が pm.db に反映されない？**
A: `pm_sync_canvas.py --dry-run` でデバッグ。Slack API の `url_private` が更新される遅延あり（数分待つ）。

**Q: Patrol のリマインダーが多すぎる？**
A: `patrol_config.yaml` で cooldown や threshold を調整（例: `cooldown_days: 14` → 2週ごとに変更）。

---

## 次のステップ

- **P1: セマンティック検索導入** — FTS5 から Embedding ベース検索へ（意味的な近さを考慮）
- **P2: cron 自動化完成** — Slack 取得・抽出・レポート投稿をすべて自動化
- **P3: UI 改善** — Slack アプリで pm.db 編集できるようにする（現在は Canvas 限定）
- **P4: 多言語対応** — SudachiPy を英語等にも対応させ検索品質向上
