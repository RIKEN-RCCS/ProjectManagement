# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

### Argus 垂直軸: 前提・意思決定台帳（有向グラフ）+ 流入拡張（Phase 1）

**ステータス**: 着手（2026-07-01〜）。設計書 `data/FugakuNEXT_Argus_designsheet.docx`（v0.1・要批准）を
読解し実装計画を策定。詳細な計画本体は Claude Code のプランファイルとして保存済み
（`~/.claude/plans/concurrent-pondering-peacock.md`、本エントリはそのポインタ + 進捗管理用）。

**背景**: 現状 Argus は実行層（正しく作れているか）のみで、整合層・方向層が空白。
マイルストーンを全達成しても要件不適合のマシンを完成させうるという失敗を防ぐため、
「意図された方向」と「実態の方向」の乖離 Δ を可視化する垂直軸（台帳・流入・機能1・機能2）を追加する。
機械は可視化まで、是正判断は人（PM）が行う。

**Phase 1 スコープ**（台帳の骨格 + シード + 流入。機能1/機能2 は後続 Phase）— **実装・検証完了（2026-07-01）**:
- [x] `scripts/utils/db_utils.py` の `_PM_SCHEMA` + `init_pm_db`/`open_pm_db` migrations に
      `ledger_goals` / `ledger_assumptions` / `ledger_issues` / `ledger_edges` を追加、
      `decisions` に `trade_off` / `reversal_condition` 列を追加
- [x] `data/ledger_seed.json`（設計書 §8 の 11 エントリ・6 エッジを再構成。実際に導出できたのは
      5 エッジのみ。原文の6本目は具体的指定が無く、出所主義に基づき未追加）+
      `scripts/ingest/ledger.py`（ingest プラグイン、`pm_ingest.py` の `PLUGINS` に登録）
- [x] 流入拡張: `scripts/ingest/slack.py` の `EXTRACT_PROMPT`/`save_slack_items()` に
      rationale/trade_off/reversal_condition + 分類ゲート3問を追加
- [x] 流入拡張（会議経路）: `generate_minutes_local.py` の `DECISIONS_TEMPLATE`/`CONSENSUS_DECISIONS_TEMPLATE`
      にブラケットタグ形式（`[根拠:]` `[捨てた案:]` `[覆す条件:]`）を追加、
      `pm_minutes_import.py` の `_parse_decisions()`/スキーマ/`--export`往復、
      `scripts/ingest/minutes.py` 転記 INSERT を追従
- [x] `scripts/enrich/enrich_items.py` の `enrich_decision()` を拡張し、決定→目標(contributes) の
      `ledger_edges` を生成（前提(depends_on)は `ledger_assumptions` が空のため Phase 1 では未生成）
- [x] 副次修正: `_save_decision_enrichment()` が enrich 結果 rationale=None で流入時の rationale を
      上書き消去するバグを発見・修正（COALESCE で既存値を保護。モードA優先の原則に合わせた）
- [x] 検証: スキーマ冪等性（2回実行）、シード投入・冪等性、Slack save_slack_items、
      enrich 辺生成・COALESCE保護をスクラッチコピーのpm.dbで確認済み（本番 `data/pm.db` は未変更）

**未着手（Phase 1 の残り）**:
- 本番 `data/pm.db` へのシード投入は未実施（重み・出所が「要批准」のため、批准後に
  `python3 scripts/ingest/pm_ingest.py ledger` を実行する）
- LLM 呼び出しを伴う end-to-end 動作確認（実際の議事録/Slackスレッドでの抽出）は未実施
  （ネストセッション制限のためセッション外ターミナルで確認する）

**未確定・要批准（実装をブロックしない）**: 識別要件5件の重み確定・出所確定（G-REPRO/G-NS）・
Q-FP64 の責任者/期限割当は `weight_status: provisional` / `source_status: needs_source` のまま先行投入。

**Phase 2（機能1: 上位意思・外界取り込み）— 第一実装完了（2026-07-02、議事録一括再生成
バッチと資源競合しない範囲で先行着手。詳細は `~/.claude/plans/concurrent-pondering-peacock.md`）**:
- [x] `scripts/ingest/ledger.py` に `upsert_assumptions()` を追加（`ledger_assumptions` の
      投入経路。自然キーが無いため `content` 完全一致で重複判定）、`list_ledger()`/`run()` に配線
- [x] `scripts/argus/patrol/detect.py` に `detect_external_signals()` を追加（検出器8）。
      `ledger_assumptions.monitor_target` と `data/web_articles.db` の記事を
      **キーワードマッチのみ（LLM不使用）**で突き合わせ、リーダー会議チャンネルへ
      「確認してください」通知。confirm/contradictの自動判定は将来拡張として据え置き
- [x] `pm_argus_patrol.py` の `DETECTORS` に登録（`--only external_signal` で単体実行可）
- [x] `data/patrol_config.yaml` に `external_signal:` ブロック追加。あわせて検出器実装が
      既に存在しない死んだ `knowledge_conflict:` ブロックを削除
- [x] `docs/argus_system.md`/`docs/schema.md` 更新、副次修正として `--only` 表の
      `decision`→`decisions` 表記ゆれも修正
- [x] スクラッチ環境（本番 `data/pm.db`・`data/web_articles.db` 非接触）で
      upsert冪等性・キーワードマッチ・cooldown・dry-run（LLM/embedding呼び出しなし）を検証
- [x] バッチ完了後（2026-07-03）、`detect_external_signals()` に LLM 判定
      （confirms/contradicts/neutral）を追加。`neutral` は通知抑制、`confirms`/`contradicts`
      で `ledger_assumptions.confidence`/`state` を更新（設計書§5 着地処理の3作用のうち
      確信度更新・監視継続を実装。既存決定への警告は `depends_on` 辺の生成経路が無いため
      コードは用意したが実際には未発火）。`patrol_config.yaml` に `use_llm` 追加、
      実LLM呼び出しでスクラッチ検証済み（confirm/contradict/neutralの3記事で判定精度確認）
- [x] `monitor_target` 実データ: 設計書 §8 に前提の具体例が無いことを確認（goals/issues/edges
      のみ記載）。「LLMが提案し人が承認する」原則に従い、`scripts/ingest/ledger.py` に
      `suggest_assumptions()` + `--ledger-suggest-assumptions` を追加。本番 `data/pm.db` の
      `decisions`（rationale付き30件）を入力に5件の前提候補を生成、ユーザー承認の上
      `--ledger-seed` で本番投入完了（`ledger_assumptions` #1〜#5、全て `confidence`/
      `monitor_target` 付き）
- [x] `docs/commands.md` に未記載だった ledger ソースのオプション一覧を新規追加

**Phase 2 の残課題**（機能1着地処理の完全化。着手判断は都度）:
- `decisions →依拠→ 前提`（`depends_on` 辺）の生成経路が無い。`enrich_items.py` の
  `enrich_decision()` 拡張が必要（`contributes`→goal と同様のパターンで実装可能）。
  生成されれば「既存決定への警告」（設計書§5 作用3）が実際に発火するようになる

**Phase 3（機能2: 決定クラスタ集約・方向Δ、骨子のみ・未着手）**:
- `/argus-direction` 新設、brief/risk の Orchestrator-Worker と
  `embed_utils.cosine_similarity_matrix` を活用。LLM 裁量は「命名」のみに限定
- `embed_utils.embed_batch()` が RiVault bge-m3 への実ネットワーク呼び出しを伴い、
  録音バッチと同じGPUホストに資源競合するため、バッチ完了後に着手する

### V4-Flash 切替の本番適用と follow-up

**ステータス**: コード修正完了・デーモン稼働確認済み (2026-06-05)。録音パイプライン (`pm_from_recording.sh` / `/argus-transcribe`) も RiVault 経由で動作確認済み。

**完了項目**:
- `call_claude()` / `call_local_llm()` / `detect_vllm_model()` / `slide_ocr` / `transcribe_pipeline` すべて
  `ARGUS_PREFER_RIVAULT=1` で RiVault に切替。CLI フラグ追加なし。
- `pm_daemon.sh` が `rivault_tokens.sh` 読み込み後に `ARGUS_PREFER_RIVAULT=1` を自動 export。
- V4-Flash のアクションアイテム過剰抽出対策（個数上限 5 件を明示）。

**残課題**:
- **Pass1 抽出 (Slack/議事録)**: `scripts/ingest/slack.py:368` が `call_local_llm()` を直接叩く。
  V4-Flash に乗せるなら `call_argus_llm` 経由に書き換える必要あり (gemma4 のままで良いか要判断)。
- **think モード再検証**: investigate の深い推論ケースだけ think ON のほうが良い可能性。
  Stage 6 では brief/risk が支配的だったため Non-think 優位だが、investigate のサブセットで
  再評価する余地あり (`scripts/eval/argus_ab.py run --target rivault --think-on-v4`)。
- **GB10 の余剰メモリ活用**: gemma4 が外れることで gpu_memory_utilization を 0.5 → 0.8 程度に
  上げられる。Whisper 同居の OOM 余裕度が増す。

---

## 保留中の構想

着手判断待ちの計画。動かすときは「現在進行中の計画」セクションに移動して詳細化する。

### 1. 日程調整 Agent (`/argus-schedule`)

**ステータス**: 保留中（2026-05-26〜）。Modal (views.open) 案が最有力。

**保留理由**: Slack 単独 UI では TONTON 並みのグリッド体験が得られない。
DM + checkboxes は要素上限制約、Box xlsx 共同編集は排他制御リスクで NG（ユーザー判断）。

**再開時の出発点**:
- UI は Modal (views.open) を第一候補に詳細化
- `pm_qa_server.py` に Socket Mode で trigger を受信するハンドラを追加
- 候補日生成は Argus が直近会話から推定 + 引数明示の両対応
- 確定後は `.ics` 添付で OAuth 不要のカレンダー連携
- DB は `data/schedule.db` を新設、確定したものだけ `pm.db.meetings` に転記
- 締切処理は `pm_argus_patrol.py` の cron サイクルに乗せる

UI モックの履歴は専用 Canvas 末尾と検討用 DM に蓄積（具体的な ID は memory `project_schedule_agent` を参照）。

### 2. Argus Phase 2: `/argus-do` 自動実行

**ステータス**: 保留中。LLM の JSON 構造化出力品質が安定したら着手。

**設計方針**:
- `/argus-brief` のアクション提案に `action_id` を付与
- 提案内容を `secretary_proposals` テーブルに保存
- `/argus-do a1` で対応する提案を pm.db に反映（assign_item, close_item 等）
- 実行前に対象アイテム ID をユーザーに確認表示する安全策を必須化

**未着手の理由**: 自動実行は誤りの影響が大きいため、まず Phase 1（提案・草案）の品質と
ユーザー受容を確認してから着手。

### 3. Web UI 認証追加・ログインノード移設

**ステータス**: 情報セキュリティ部門の確認待ち（実装保留中）。
詳細はメモリ `project_web_auth_todo` 参照。

### 4. argus-investigate と同種バグの追加調査（残課題）

**ステータス**: 保留中（2026-05-28〜）。今回 #1, #2（generate_minutes_local.py の Stage 3）は対応済み。

**残っている疑い箇所**（2026-07-02、Argus垂直軸作業でのファイル変更に伴い行番号を再確認）:
- `scripts/ingest/slack.py:675` (`consensus_n <= 1` 分岐) — `consensus_n=1` 時は集約せず単発抽出、
  `extract_json`（同ファイル415-422）の `ValueError` が上位に伝播しうる（consensus_n>=2 経路では
  空配列で吸収）。1 スレッドが静かに失われるリスクは残存
- `scripts/ingest/slack.py:659` — `retrieve_knowledge_for_extraction` にスレッド全文を投入しており、
  4ba721c の query rewrite 相当（固有名詞展開・略語正規化）が無い。HyDE 過剰展開のリスク
- `scripts/enrich/enrich_items.py:333, 382` — LLM 失敗時に `{"error":...}` で個別アイテムを
  未エンリッチのまま記録。リトライなし

**判断軸**: enrich の「歩留まり」が運用上問題になるかを、pm.db の
`decisions WHERE decided_by IS NULL AND rationale IS NULL`（未エンリッチ相当）件数で
観察してから着手判断する（専用の進捗管理列は無い）。

