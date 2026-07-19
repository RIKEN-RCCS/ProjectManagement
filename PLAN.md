# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

### 全文読込QA（--file）の汎用性補強（保留・様子見）

**ステータス**: 保留（2026-07-18、ユーザー判断で実運用の様子見）。

機構は汎用（アプリ名等のハードコード無し）だが、既知の限定が2つ:
(1) 偽「関連情報なし」ガードのエンティティ抽出が ASCII 限定（`[A-Za-z0-9/-]{3,}`）で、
日本語のみの固有名詞質問では強いガード（制限事項記録・記載なし断言禁止）が効かず
フォールバック（5,000字窓の却下リトライ）頼み。(2) e2e 実証が「アプリ評価報告書×性能系
クエリ」に偏在。再開時は日本語エンティティ抽出の拡張（カタカナ連続・「」引用語）と、
別文書×別質問型の検証スイープを実施。観測ログ（窓別応答字数・所要・エンティティ）は
整備済みで、実運用で問題が出れば診断は即可能。

### embedding エンドポイントの同一ベクトル返却バグ（インフラ調査・保留）

**ステータス**: クライアント側防御まで実施（2026-07-17）、エンドポイント調査は保留。

**事象**: LiteLLM→Ollama bge-m3 経路が、異なる入力テキストに対しバイト同一の誤ベクトルを
決定論的に返すことがある（単独リクエスト再取得で再現済み＝バッチ順序ズレではない）。
qa_index.db に12グループ96チャンクの被害（例: FrontFlow 実行時間の図チャンク 58730 が
無関係な E-WAVE 章とベクトル共有し、ベクトル検索で最下位に沈む）。content 同一の
正当な重複59グループとは区別済み。

**実施済み**: 検出・修復オプション `pm_embed.py --repair-duplicate-embeddings [--execute-repair]`、
書き込み時ガード（異なるテキストに同一ベクトル→保存せず警告、実装中）。

**保留（再開時にやること）**: エンドポイント側の切り分け — LiteLLM プロキシのキャッシュ設定
（キー衝突疑い）と Ollama 直接呼び出しの比較で層を特定 → 解消後に `--execute-repair` → 通常
実行で96件再計算。修復単独ではエンドポイントが同じ誤ベクトルを再生成するため無効。
**追加の傍証（2026-07-18）**: chat completions 側でも同種の疑い — 全文読込QAの map で
異なる窓4つが同一6字応答をサブ秒で返す事象を観測（rikyu glm-5.2 経由）。クライアント側は
nonce によるプロンプト文面変化＋リトライで回避済みだが、エンドポイント調査時は
embedding と chat の両方でキャッシュ挙動を確認すること。
**ライブ観測（2026-07-18 全coreロールアウト時）**: 新規7,378チャンクの backfill で書き込み
ガードが**28件**の異常同一ベクトルを検出・スキップ（多くは文書冒頭の`【…】`見出しプレフィックス
を含むチャンク＝類似冒頭文でキャッシュ衝突する仮説と整合）。該当28件は未embeddingのまま残置
され、エンドポイント解消後の通常実行で自然回復する。修復対象は既存96件＋この28件。
**再現スクリプト（2026-07-19）**: `scripts/eval/embedding_duplicate_repro.py`（運用者向け・
リポジトリ非依存・env/引数でエンドポイント指定）。合成データでは再現せず（プレフィックス長
64〜1024字の衝突プローブ全PASS、匿名化で衝突が崩れる、部分入替で非単調）＝単純なプレフィックス
閾値ではなく**内容依存の弱いハッシュ/縮退キャッシュキー空間での偶発衝突**を示唆。実データペアを
`--pairs-file` で渡すと単独リクエストで即再現。副次発見: 実効入力長 ~8,000字で切り詰め
（別現象・要報告）。運用者への依頼文サマリは本スクリプトの docstring とログ参照。

### Argus 垂直軸: 前提・意思決定台帳（有向グラフ）+ 流入拡張

**ステータス**: 着手（2026-07-01〜）。設計書 `data/FugakuNEXT_Argus_designsheet.docx`（v0.1・要批准）を
読解し実装計画を策定。

**背景**: 現状 Argus は実行層（正しく作れているか）のみで、整合層・方向層が空白。
マイルストーンを全達成しても要件不適合のマシンを完成させうるという失敗を防ぐため、
「意図された方向」と「実態の方向」の乖離 Δ を可視化する垂直軸（台帳・流入・機能1・機能2）を追加する。
機械は可視化まで、是正判断は人（PM）が行う。

**Phase 1（台帳の骨格 + シード + 流入）— 完了（2026-07-01〜07-03、本番投入・検証済み）**。
詳細は LOG.md「Argus 垂直軸 Phase 1 完了」参照。

**Phase 2（機能1: 上位意思・外界取り込み）— 完了（2026-07-02〜07-03）**。
着地処理の3作用（確信度更新・既存決定への警告・監視継続）全て実装・実LLM検証済み。
詳細は LOG.md「Argus 垂直軸 Phase 2 完了」参照。

**Phase 3（機能2: 決定クラスタ集約・方向Δ）— 完了（2026-07-03〜07-04）**、その後
**2026-07-05 に抜本見直し（R1+R2）を実施** — 「クラスタ表示から所見検出へ」。
選別ゲート導入・辺の全件再判定・所見5種（停滞/違反疑い/論点ブロック/衝突/前提健全性）へ
検出器を再定義。詳細は LOG.md「Argus 垂直軸の抜本見直し」参照。

**残作業**:
- `/argus-direction`（所見型）の実Slack投稿確認
- Argus Console（Web UI）への対話型グラフ追加（cytoscape.js等）は見送り中。
  将来必要になれば別途検討（PNG静止画像で当面のニーズは満たしている）

**R3 構想（流入モードA: argus-transcribe の決定捕捉拡張）— 未着手・保留**:
設計書§4が「最大レバレッジの一手」とする、決定確定の場での捕捉。argus-transcribe を
議事録生成器から決定捕捉器へ拡張し、決定の責任者にその場で2〜3行の確認
（理由・捨てた案・覆す条件）を求める。遡及エンリッチ（モードB）より確度の高い
台帳エントリが得られ、reversal_condition（覆す条件→レビュー発火）の実運用も
これで初めて成立する。**会議運用の変更を伴うため、R1+R2の効果を見てから
PMが着手判断する**（2026-07-05 の抜本見直し時に明示的に見送り）。

### WhisperX 本番採用（保留 — ctranslate2 の Blackwell 対応待ち）

**ステータス**: テスト完了（2026-07-06、経緯は LOG.md「WhisperX/GB10テスト完了」）。
`whisper_vad.py --engine whisperx` として実装済み・レビュー済みで、品質重視の会議には
手動指定で今すぐ使える。話者分離品質は明確に優位だが、ctranslate2 が GB10(Blackwell)
のカーネル未対応のため転写が旧エンジン比8倍遅く、既定切替は見送り。

**再開条件**: ctranslate2 の新リリースが Blackwell (sm_120/121) ネイティブ対応したら、
`whisperx_pyfix/` のソースビルドを更新して5分WAVベンチを再実施（手順・環境変数は
LOG.md 該当エントリ参照）。転写が旧エンジン同等以下になれば、wrapper
（pm_from_recording.sh / transcribe_pipeline.py）に `WHISPER_ENGINE` スイッチを追加して
本番切替を提案する。

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

### initial-search 既定ON の事後観察（期限 2026-07-27）

**ステータス**: 観察中（2026-07-13〜）。コミット `6855533` で investigate/メンション応答の
全経路に初期 retrieval シードを既定ON化したが、検証は SCALE-LETKF 1クエリの before/after のみ。
n=1 デプロイのため、実トラフィックで2週間観察してから定着/巻き戻しを判断する。

**確認コマンド**（`logs/pm_qa_server.log` に対して。qa デーモン稼働ホストで）:
- 発火実績: `grep -c "\[initial-search\]" logs/pm_qa_server.log`
- 所要分布: `grep "\[initial-search\] 完了" logs/pm_qa_server.log`（各行に `(X.Xs, N件)`）
- 失敗率: `grep -c "\[initial-search\] 事前検索スキップ" logs/pm_qa_server.log`

**判定基準**: メンション応答の体感遅延に不満が出る / シード所要が恒常的に 30s 超
→ qa デーモン起動環境に `ARGUS_DISABLE_INITIAL_SEARCH=1` を設定して opt-out（要デーモン再起動）。
問題なければ本エントリを削除し LOG.md に1行「観察完了・定着」を記録。

### アクションアイテム自動消化検出・自動クローズ（コード完了・ロールアウト保留）

**ステータス**: 実装・レビュー・修正・単体検証まで完了（2026-07-14）。既定 config が全て off のため
本番影響ゼロでマージ可能。設計判断の経緯は LOG.md「アクションアイテム自動消化検出」参照。

**何をしたか**: 既存 Patrol の `detect_completion_signals`（`scripts/argus/patrol/detect.py`）を拡張。
対象を `source IN ('slack','meeting')` に広げ、`_get_activity_evidence` が qa_index.db の
ハイブリッド検索で活動報告（議事録・別チャンネルSlack・レポート）を証拠として引き、LLM が
確信度付き（`YES|HIGH` / `YES|LOW` / `NO`）で完了判定。HIGH かつ `auto_close_enabled` のときのみ
完全自動 close（＋note追記＋audit_log source=`argus_auto`＋リーダーチャンネル事後通知）、
それ以外（auto_close無効 or LOW）は旧来の承認ボタン DM へフォールバック（退行なし）。

**残作業（ロールアウト、PMが段階判断）**:
1. **config 追加**: `data/patrol_config.yaml` の `patrol.completion_detection.*` に新キーを追記
   （`evidence_from_index` / `evidence_index_name` / `evidence_k` / `evidence_since_extracted` /
   `auto_close_enabled` / `auto_close_min_confidence` / `post_close_notify`。既定値はコード側にあり）。
2. **証拠検索の有効化と観察**: まず `evidence_from_index: true`・`auto_close_enabled: false` で、
   承認ボタン経路のまま活動報告ベース検出の精度を dry-run/実運用ログで観察。
3. **LLM 過剰 HIGH 付与のチューニング**: 検証時 glm-5.2 が「関連文書がヒットしただけ」で完了寄りに
   判定する傾向を確認済み。プロンプトに抑制文を追加済みだが、実データで HIGH 精度を要観察。
   誤検出が多ければ閾値・プロンプトを調整してから `auto_close_enabled: true` に上げる。
4. **反映**: patrol は cron/都度プロセスなので次回起動で反映（qa デーモン再起動不要 —
   `pm_qa_server.py` は `confirm.py` のみ import、`detect.py`/`actions.py` は読まない）。

### 実績DB（achievements ledger）の運用定着（コード完了・運用整備が残る）

**ステータス**: 本体実装・本番投入完了（2026-07-16、経緯は LOG.md「実績DB（achievements ledger）を
新設」参照）。全6アプリ39件投入済みで `/argus` の `get_app_achievements` も稼働中。以下は残る運用整備。

**残作業**:
1. **定期 populate の cron 化** — 現状は手動 `pm_ingest.py achievements` 実行のみ。新規完了実績を
   拾う頻度を決めて `pm_box_update.sh` 等の夜間バッチに乗せるか判断する。
2. **rejected の再提案抑止** — `scripts/ingest/achievements.py` の `known_titles` 抽出は
   `status IN ('confirmed','proposed')` のみで `rejected` を含めていないため、人間が却下した実績が
   次回 run で再提案されうる。`known_titles` に rejected も加えて抑止する。
3. **per-app commit** — 現状 `ctx.pm_conn.commit()` は全アプリ処理後の一括 1 回のみ。途中失敗時に
   全アプリ分の upsert が失われるため、アプリ単位でコミットするよう変更を検討。
4. **抽出品質の polish** — `evidence_ref` が日付のみで出典（議事録/Slack/レポート等）ラベルが無く
   検証しづらい。また `title` にアプリ名が重複して入るケースがあり表記の正規化余地がある。

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

### 5. investigate の retrieval recall 限界（主題外の固有名詞に埋もれた事実）

**ステータス**: 保留中（2026-07-13）。診断済み・軽量対策は失敗確認済み。

**問題**: investigate が「主題（例: GPU化・性能評価）から意味的に離れた語彙で書かれた事実」を
取りこぼす。実例: SCALE-LETKF レポートで benchpark/Benchkit の完了状況（qa_index.db
id=22835[2026-05-15 Yamaura], 22877[2026-06-16], 17624[2026-06-16 Status表「Benchkit/fj/6」]、
いずれも `--since 2026-03-01` の在窓）を拾えず「確認できなかった」と過小報告。原因は
(a) 該当チャンクが簡潔な進捗ボックスノート／ステータス表で `GPU` 語を含まない、
(b) 文書側が英語 `benchpark`/`Benchkit`＋レベル番号、クエリ側が片仮名「ベンチマーク」＋GPU寄り、
という主題・日英表記のミスマッチ。

**試して失敗した案（2026-07-13）**: 案A＝rewrite プロンプトにドメイン同義語（英表記・略語・環境名）
併記の**汎用**ガイダンスを追加。→ 英語表現(status/porting)や富岳は入ったが、質問に無い固有名詞
（Benchkit/Benchpark/FX700/GH200/Genoa）までは LLM が生成せず、該当チャンクは依然未取得。
かつ latency が 2分→9分に増。**汎用の語彙拡張ではこの種の miss は解けない**と結論し、変更は破棄
（コミットせず）。

**却下した案（個別最適化のため不採用）**: 特定フォルダ（`22_進捗報告`）やステータス表を検索で
優先する案は、SCALE-LETKF（特定の文書配置）に着目した**個別最適化**であり、他エンティティ・他の
フォルダ構成では効かず他クエリの精度を歪めるだけなので採らない。

**汎用の方向性（特定アプリ/フォルダ非依存）**:
- **索引由来の共起語拡張** — ❌ **試して失敗（2026-07-13, Stage1）**。qa_index.db に
  entity_cooccurrence を構築し retrieve_chunks に opt-in 配線して baseline-v1 と Δ 測定した結果、
  topic hit@k は改善せず（Δ≤0、悪化複数）。原因: エンティティの大域共起は「アプリ一覧に併記される
  他アプリ名・領域専門語」に支配され、狙った具体的関連語（Benchkit/Yamaura 等）は 70〜900 位に埋もれる。
  ＋FTS 暗黙 AND で変種がノイズ化。コード・テーブルは破棄済み（コミットせず）。
- **エンティティ起点の網羅パス** — ⏸ **検証したが保留（2026-07-13, Stage1）**。entity 検出＋recency
  取得は正しく動き、reserve slots(M=8) で **fts 層は topic hit@60 +0.071 と新規 recall を開けた**
  （メカニズムは実証）。しかし2つの壁で**実運用 hybrid では効果が正味ゼロ**: (1) hybrid 外側の
  fts+vector 融合(_rrf_merge)で anchor 候補が再クラウドアウト、(2) 動機ケース(SCALE-LETKF/22877)は
  anchor recency 20位で M=8 に届かず、届かせる M 拡大は base 排除(precision 退行)と表裏。
  再開時の残作業: reserve を hybrid/hyde 最外層へ移す＋M を数水準で振り precision コストを実測して
  recall↔precision を判断。コードは破棄済み（コミットせず、ハーネス・baseline は温存）。
- **source_type 多様化（未着手）**: top-k を source_type（議事録/Slack/box）で分散させ、構造化文書が
  narrative に押し出されないようにする（特定フォルダ優先ではなく「型の多様性」を担保）。

いずれもランキング／取得ロジックに触るため、着手時は before/after の回帰測定が必須。当面は
安全側のヘッジ（「確認できなかった」）で運用継続。
（回帰測定の土台＝recall 評価ハーネス `scripts/eval/recall_eval.py`・baseline-v1（run_id 3）を
2026-07-13 に整備。以後の recall/precision 改善は本ハーネスの Δ で合否判定する。）

