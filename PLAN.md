# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

### Argus 垂直軸: 前提・意思決定台帳（有向グラフ）+ 流入拡張

**ステータス**: 着手（2026-07-01〜）。設計書 `data/FugakuNEXT_Argus_designsheet.docx`（v0.1・要批准）を
読解し実装計画を策定。詳細な計画本体は Claude Code のプランファイルとして保存済み
（`~/.claude/plans/concurrent-pondering-peacock.md`、本エントリはそのポインタ + 進捗管理用）。

**背景**: 現状 Argus は実行層（正しく作れているか）のみで、整合層・方向層が空白。
マイルストーンを全達成しても要件不適合のマシンを完成させうるという失敗を防ぐため、
「意図された方向」と「実態の方向」の乖離 Δ を可視化する垂直軸（台帳・流入・機能1・機能2）を追加する。
機械は可視化まで、是正判断は人（PM）が行う。

**Phase 1（台帳の骨格 + シード + 流入）— 完了（2026-07-01〜07-03、本番投入・検証済み）**。
詳細は LOG.md「Argus 垂直軸 Phase 1 完了」参照。

**Phase 2（機能1: 上位意思・外界取り込み）— 完了（2026-07-02〜07-03）**。
着地処理の3作用（確信度更新・既存決定への警告・監視継続）全て実装・実LLM検証済み。
詳細は LOG.md「Argus 垂直軸 Phase 2 完了」参照。

**Phase 3（機能2: 決定クラスタ集約・方向Δ）— 着手（2026-07-03、議事録バッチと資源競合しない
範囲で先行実装）**:

設計書§6を再読し、**集約はembedding不使用・純粋にグラフ構造（`ledger_edges`）ベース**と判明
（旧記載の `embed_utils.cosine_similarity_matrix` 活用は誤り、訂正）。決定クラスタ ＝
「共通の前提（depends_on同一assumption）に立ち、同一目標（contributes同一goal）に貢献する
決定の集合」。処理手順: 集合化（グラフ、LLM不使用）→ 命名（LLM+承認）→投入量集計（SQL）→
照合Δ（計算のみ）。LLM呼び出しはクラスタ命名のみで軽量、embedding呼び出しは無い。

**判明した前提条件**: 本番pm.dbの `ledger_edges` には decision起点の `contributes`/`depends_on`
辺が**1件も無い**（Phase1のseed edgeは goal→goal のみ）。`enrich_items.py` によるdecision
enrichmentが台帳投入後に一度も本番実行されていないため。Phase 3のクラスタ集約が意味を持つには
先に決定へのenrichment実行（LLM呼び出し、件数次第で相応の負荷）が必要 — これは録音バッチ完了後に
実施する。

**実装完了**（2026-07-03、LLM/embedding不使用のコア部分は資源競合なし、
命名LLM呼び出しは1件のみの軽量検証に留めた）:
- [x] `scripts/argus/direction.py` 新規作成:
      `compute_decision_clusters()`（グラフベースの集合化、LLM不使用）、
      `aggregate_cluster_contribution()`（投入量集計、SQL）、
      `compute_direction_delta()`（方向Δ算出、単一スコアに集約しない）、
      `identify_unaddressed_goals()`（入次数ゼロの目標検出）、
      `detect_nonconvergence()`（同一目標への複数クラスタ併存＝非収束検出）、
      `name_cluster_with_llm()`（LLMの裁量を命名のみに限定）、
      `build_direction_report()`（Markdownレポート組み立て）
- [x] スクラッチ環境の合成データ（目標3件・前提1件・決定4件）で全関数を検証。
      クラスタ集約・Δ・未着手検出・非収束検出はLLM不使用で期待通り動作、
      命名は実LLM呼び出し1件のみで動作確認（バッチと資源競合を避けるため最小限に）
- [x] `/argus-direction` を実装: `pm_argus.py::_run_direction()`（Slack、
      brief/riskと異なりSlack/議事録データ収集は不要なため`_collect_all_data()`を
      呼ばない軽量な実装）、`--direction`/`--dry-run` CLIフラグ（`_collect_all_data()`
      より前で早期分岐、資源浪費を回避）、`pm_qa_server.py`のコマンド登録
- [x] CLI `--direction --dry-run` を本番pm.db（読み取りのみ）で動作確認。
      台帳に decision起点の辺が無いため「集約対象がありません」を正しく表示
- [x] `docs/argus_system.md`（コマンド詳細・Slackアプリ登録一覧）、
      `docs/argus_outcomes.md`（6コマンド化、新セクション4追加・以降を繰り下げ）を更新

**バッチ完了後に着手**:
- 本番decisionsへの `enrich_items.py` 一括実行（`contributes_to_goals`/`depends_on_assumptions`
  の本番投入。件数次第で相応のLLM負荷）— これが無いと `/argus-direction` は
  「集約対象なし」を返すのみ
- `/argus-direction` のend-to-end動作確認（実Slack投稿、実データでのクラスタ命名）

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

