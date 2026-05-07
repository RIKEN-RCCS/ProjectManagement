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

## 成果6: 過去の決定・議論を背景付きで素早く検索

**ユーザーの得られるもの**: 過去の決定事項・議論・ドキュメントを素早く引用可能な形式で検索。「以前決めたことはなんだったか」をリアルタイムで回答。

| 場面 | ユーザーのアクション | 得られるもの | 応答時間 |
|---|---|---|---|
| Slack上での質問 | `/argus-investigate GPU性能に関する決定事項は？` | 関連情報を上位5件、出典付きで返答 | 10～30秒 |
| 会議中の確認 | `/argus-investigate 先週決めた設計方針について再度確認したい` | 過去の設計会議の決定内容・背景を提示 | 10～30秒 |
| ドキュメント確認 | `/argus-investigate GPU Outsourcing Agreement の最新版はどこか` | BOXドキュメントの位置・共有者・更新日を提示 | 5～10秒 |
| 業界情報確認 | `/argus-investigate Zettaスケール達成の業界動向は` | RIKEN公式・HPC ニュース・NVIDIA ブログの関連記事を提示 | 10～20秒 |

**ユーザーが得る効果**:
- 会議中に「昔何決めたっけ？」と迷わない
- 同じ議論を繰り返さない
- ドキュメントの在処を素早く確認
- 業界トレンドと内部決定の整合性を確認可能

**技術基盤**: 議事録・Slack生メッセージ・BOXドキュメント・外部Web記事を FTS5 全文検索インデックスに統合。SudachiPy 形態素解析 + LLM re-ranking で精度向上。

---

## 成果7: 毎週の進捗レポート自動投稿 + Canvas での対応状況管理

**ユーザーの得られるもの**: 毎週月曜朝に Canvas にレポート自動投稿。会議中に対応状況を直接編集 → pm.db に即座に反映。変更履歴が監査可能。

| タイミング | ユーザーのアクション | 効果 |
|---|---|---|
| 月曜朝 | Canvas を開く | マイルストーン進捗・期限超過・要注意事項を把握 |
| 会議中 | Canvas 上でアイテムを編集（担当者・期限・マイルストーン・状況・対応状況） | リアルタイムに pm.db に反映・audit_log に記録 |
| 会議中 | 決定事項のチェックボックスにチェック | 確認済み状態を記録 |
| Canvas から source 列クリック | Slack スレッド or 議事録本文へジャンプ | アイテムの背景・根拠を即座に確認 |

**レポート構成**: プロジェクト現在地（マイルストーン完了率）→ 直近の決定事項 → 要注意事項（期限超過・停滞・未確認決定）→ 未完了アクションアイテム表（全フィールド編集可能）

**ユーザーが得る効果**:
- 全員が同じ進捗情報で同期
- Canvas ↔ pm.db が常に同期
- 「いつ誰が何を変更したか」を監査可能
- 「誰が言った」「いつ決めた」の根拠をリンク経由で即座に遡行可能

**自動実行**: 毎週月曜朝 9:00（cron）に pm_report.py が Canvas へ投稿

---

## 成果まとめ表

| 成果 | 使用場面 | ユーザーアクション | 実施主体 | 更新頻度 |
|---|---|---|---|---|
| 朝の優先順位づけ | 毎朝 5分で確認 | `/argus-brief [引数]` | ユーザー or cron自動投稿 | 毎日 |
| リスク自動検知 | 顕在化・予兆リスクの通知 | 自動 DM・チャンネル通知 | Patrol Agent（cron 30分ごと） | 常時 |
| 根本原因分析 | 「なぜ遅れているのか」を深掘り | `/argus-investigate <質問>` | ユーザー発動 | オンデマンド |
| 草案生成 | 会議資料・メッセージ作成 | `/argus-draft <用途> <件名>` | ユーザー発動 | オンデマンド |
| 会議録音 → 議事録 | 会議終了直後 | `/argus-transcribe <ファイル名>` | ユーザー発動 | オンデマンド |
| 過去の検索・引用 | 「昔何決めたっけ？」を確認 | `/argus-investigate <質問>` | ユーザー発動 | オンデマンド |
| 進捗・対応状況管理 | 月曜朝に全員で確認・編集 | Canvas 編集 | cron自動投稿 + ユーザー編集 | 毎週 |

---

## アーキテクチャ図（成果 → 機能マッピング）

```
ユーザーのアクション（7つの成果）
  ├─ /argus-brief [引数]              ← 成果1: 朝の優先順位づけ
  ├─ 自動 DM・チャンネル通知           ← 成果2: リスク自動検知
  ├─ /argus-investigate <質問>        ← 成果3,6: 根本原因分析・過去検索
  ├─ /argus-draft <用途> <件名>       ← 成果4: 草案生成
  ├─ /argus-transcribe <ファイル>    ← 成果5: 議事録自動生成
  └─ Canvas 上で編集＆確認            ← 成果7: 進捗・対応状況管理
       ↓
システム処理層
  pm_qa_server.py         ← Socket Mode デーモン（全コマンド統一処理）
  pm_argus.py             ← データ収集・LLM プロンプト構築
  pm_argus_agent.py       ← Investigation Agent（マルチステップ調査）
  pm_argus_patrol.py      ← Patrol Agent（自動リスク検知）
  pm_report.py            ← 週次レポート生成・投稿
  pm_sync_canvas.py       ← Canvas ↔ pm.db 同期
       ↓
データ基盤層
  pm.db                    ← PM統合データ（決定・AI・MS・audit_log）
  data/minutes/{kind}.db   ← 議事録DB（会議ごと）
  {channel_id}.db          ← Slack 生メッセージDB
  data/qa_pm*.db           ← FTS5 インデックス（議事録・Slack・ドキュメント・Web）
  patrol_state.db          ← Patrol 冪等性・cooldown 管理
       ↓
LLM 層
  gemma4（ローカル vLLM）  ← 優先使用（128K context）
  RiVault（理研製）        ← gemma4 未起動時フォールバック（200K context）
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

## ロードマップ

| 優先度 | 施策 | 説明 |
|---|---|---|
| P1 | リスク検知の精緻化 | 完了シグナル検出の LLM 精度向上・検出ルール増強 |
| P2 | セマンティック検索導入 | FTS5 から Embedding ベース検索へ（意味的な近さを考慮） |
| P3 | Slack アプリ化 | Canvas 代わりに Slack アプリで pm.db 編集できるようにする |
| P4 | マルチモーダル議事録 | 動画フレームから話者同定・スライド OCR による精度向上 |
