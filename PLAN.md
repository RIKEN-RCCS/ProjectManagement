# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

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

**残っている疑い箇所**:
- `scripts/pm_box_distill.py:524-556` `llm_quality_judge` — LLM/JSON エラー時に Stage 1 で抽出した候補を全 drop。設計ポリシー（迷ったら drop）と整合するが、JSON parse 失敗時に raw text から `verdict` を救う余地あり
- `scripts/ingest/slack.py:519-521` — `consensus_n=1` 時に `extract_json` の `ValueError` を上位に投げる（consensus_n>=2 経路では空配列を返す）。1 スレッドが静かに失われる
- `scripts/ingest/slack.py:503-508` — `retrieve_knowledge_for_extraction` にスレッド全文を投入しており、4ba721c の query rewrite 相当（固有名詞展開・略語正規化）が無い。HyDE 過剰展開のリスク
- `scripts/enrich/enrich_items.py:276-280, 323-327` — LLM 失敗時に `{"error":...}` で個別アイテムを未エンリッチのまま記録。リトライなし

**判断軸**: 蒸留・enrich の「歩留まり」が運用上問題になるかを `pm_knowledge_inspect.py` と pm.db の
`enriched_at IS NULL` 件数で観察してから着手判断する。

### 5. ナレッジ蒸留 (knowledge.db) の品質改善

**ステータス**: Stage 2 二段ゲート導入後の運用観察中。重複・ノイズの実測値次第で次の改善を検討。

**観察ポイント**:
- `pm_knowledge_inspect.py` で重複・多重抽出の発生率
- `/argus-investigate` でナレッジ引用の有用性
- `pm_knowledge_edit.py --invalidate` の発動頻度（誤抽出の指標）

### 6. scripts/ ディレクトリのテーマ別再編

**ステータス**: 保留中（2026-05-29〜）。機能追加が落ち着いたタイミングで 1 セッション内で完結させる。

**背景**: `scripts/` 直下に Python 40 + shell 11 がフラットに並び、`docs/architecture.md` の
論理分類と物理配置が乖離している。grep の見通しと新規開発の判断コストが上がっている。

**方針**: 既存 4 サブパッケージ (`argus/ enrich/ ingest/ recording/`) と同じ「空 `__init__.py` +
絶対パス起動」スタイルで `box/ minutes/ knowledge/ report/ quality/ tts/ _archive/` を新設し、
top-level Python を分類する。shell ラッパは据え置き、影響を受けるパス参照のみ更新する。

**主な移動対象**:
- `box/` ← `pm_box_{crawl,distill,relevance}.py` `pm_slack_box_links.py`
- `minutes/` ← `pm_minutes_{import,catalog}.py`
- `knowledge/` ← `pm_knowledge_{edit,inspect,dedupe}.py`
- `report/` ← `pm_report.py` `pm_insight.py` `pm_biweekly_report.py` `pm_xlsx_{report,sync}.py` `build_argus_outcomes_pptx.py` `pptx_theme.py`
- `quality/` ← `pm_screen.py` `pm_relink.py` `pm_sync_canvas.py` `pm_link_milestones.py` `range_filter_pm.py` `close_old_items.py`
- `tts/` ← `pm_tts.py` `voice_uploads.py`
- `_archive/` ← `migrate_qa_to_unified.py` `migrate_slack_to_unified.py`

**影響を受けるシェル**: `pm_box_update.sh` `pm_biweekly_report.sh` `canvas_report.sh` `slack_post_minutes.sh`。
`pm_daemon.sh` の SERVICES (argus/pm_qa_server.py, pm_api.py) は移動対象外。

**詳細計画**: `~/.claude/plans/plan-stateful-curry.md`
