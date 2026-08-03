# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

### セキュリティ対策（docs/security-architecture.md）— 流出面を優先

**方針**: 流出面（層1〜3・監査・供給網固定）は実装済み。改竄側（R5・R8 の拡張・R13 —
出自の独立した第2系統・引用スパン照合・二段抽出・挙動指紋）は、下記の R8 第2系統
トリアージ（実装済み分）を除き **PM 判断で当面着手しない**。

**完了**（実装の経緯・判断理由は LOG.md 参照）:
- ツール allow-list（`COMMAND_TOOLS`）/ MCP Server 廃止（チョークポイント一本化）
- 外向き通信 allow-list（`net_guard`）— デーモン・cron 5 本とも enforce 済み
- Box 共有リンクの `collaborators` 化 / 認証境界の棚卸し
- `tool_calls` + `reasoning_traces` 台帳（追記専用・ハッシュ連鎖、拒否呼び出しも記録）
- 供給網固定（`model_pin.yaml`、本番構成に合わせ enforce 済み）
- R8 第2系統トリアージ（`Llama-4-Scout-17B`、フラグ語のみ・欠落検出型に作り直し済み）
- 出力ブローカー（Canvas / Box / Slack 全ファネル化・canary 検査・承認フロー・
  外部アンカー `anchors` ブランチへの push）
- 能力分離 5b（Read Plane、メンション + スラッシュコマンド、既定 ON）
- canary の発行・植え付け・生存確認、`qa_index.db` の暗号化
- pre-commit 5 lint（`no-box-open-company-access` / `net-guard-import-required` /
  `no-slack-id-literals` / `no-mcp-server-registration` / `slack-egress-funnel`）

> [!note] Kimi-K3 再開との関係
> K3 優先度1（API クライアント層再設計）は `tool_calls` / `reasoning_traces` と同一コードパス。
> 優先度2（視覚入力）・優先度3（長時間自律）のゲートだった Phase 5（5b）は
> **2026-08-03 に既定 ON へ移行済み**。R8 も実装済みだが**本番観測はこれから**。
> K3 再開の可否自体は別途 PM 判断（LOG.md 2026-07-31）。

**残っている作業**（優先順）:
1. **`network_allowlist.yaml` の実値化と enforce の拡大** — read_plane / write_plane の
   実値が未確定。デーモン・cron は enforce 済みだが、手動実行の録音・TTS 系ラッパー、
   Web UI 経由ジョブ、Canvas `--recreate`、Box 共有リンク作成など cron に載らない経路は
   個別に叩いて `stage=resolve`/`connect` の deny 0 を確認してから対象に加える
2. **canary 運用の残り** — 本番 pm.db への `canary_tokens` 作成、人間向けレポート経路
   （action_items/decisions）への植え付け（`is_canary` 列と全レポート除外が先に必要）、
   アラート届け先（現状 exit 1 + ログのみ）、生存確認の対象拡大（現状 box_docs 限定）
3. **Box 既存共有リンクの正規化** — 事前に「読ませたい人が全員 collaborator か」を Box 側で
   揃える（順序を逆にすると一時的にリンク切れ）
4. **供給網固定の残り** — (a) 運用主体からのモデル更新通知の取り決め（Phase 0、合意事項）、
   (b) K3 の `declared_engine` / `declared_trust_remote_code` の確認（取れるまで K3 は
   `production: false`）
5. **能力分離 5b の残り** — `--file` 経路（`run_document_qa`）は Read Plane 未分離
   （`respond`/`record_ids` と強く結合しているため対象外）。OS レベルの強制
   （iptables / network namespace）は **sudo が使えず着手不能** — `net_guard` は
   同一プロセスの socket フックのみで box CLI 等の subprocess は覆えない
6. **輸送層ブローカーの残り** — 層3（宛先粒度）の enforce 判断（観測待ち。`/argus-*`
   ephemeral の正当な宛先集合がまだ確定できていない。`pm_selfcheck` の
   `egress_dest_unknown` で観測中）
7. **改竄側（R5・R8 の拡張・R13）— PM 判断で当面着手しない**。R8 の第2系統トリアージ
   自体は実装済みで本番観測待ちだが、それ以上の拡張・独立した第2系統・引用スパン照合・
   二段抽出・挙動指紋は保留する

**public リポジトリの機微情報**（origin: RIKEN-RCCS/ProjectManagement）:

HEAD からは除去済み（アプリ名 0 / Slack ID 0 / 機微ファイル 0）。再発は pre-commit の
5 lint（上記）が防ぐ。

- **人名 — 当面そのまま（2026-07-31 PM 判断）。** 敬称つきで 18 件 / 7 ファイル。
  敬称なしの姓はパターンで検出できない（例: `patrol/users.py` の docstring）。
  棚卸しには `docs/project.md` の名簿との突き合わせが必要（Claude は読めないため PM 実行）。
  lint も無いので今後も混入しうる
- **会議名 — 当面そのまま（2026-07-31 PM 判断）。** `docs/commands.md` 21 /
  `docs/argus_system.md` 15 / `pm_minutes_import.py` 27 ほか。既定の会議種別として
  コード全体に埋まっており設定化は広範囲
- **公開履歴の除去 — 保留。** 2026-07-13 以降の履歴にアプリ名・Slack ID・
  `docs/decisions/` 等が残る。除去には `filter-repo`（パス指定＋`--replace-text`）と
  force-push が必要で、全 SHA が変わるため組織調整が前提。上記 2 件を保留した結果、
  今実行しても部分的な除去にしかならない（人名・会議名は残る）。後から追加で
  force-push を繰り返すのは public リポジトリでは悪手なので、除去範囲が確定してから
  1 回で実施する

**その他の懸案**:
- `~/.claude/settings.json` の GitHub PAT 失効（PM 作業、未実施）
- `argus_config.yaml` の `indices.pm-all.channels` は 55 件。旧ハードコード（57 件の
  和集合）との差分 2 件は PM 判断で現状維持
- **enforce の外に残っているもの**（「全経路が覆われた」と読まないこと）:
  ① `box` CLI（Node の別プロセス）— net_guard は subprocess を覆えない（根本対処は
  OS レベルの強制。上記 5 と同一課題）
  ② 手動実行の録音・TTS 系ラッパー（観測が溜まっていないため除外リスト）
  ③ Patrol（cron から意図的に外してある。DM の信頼性の問題が別途未解決のため。
  経緯は LOG.md 参照）

### 議事録生成への Kimi-K3 視覚入力の組み込み（2026-07-31 着手）

**ステータス**: **中断（2026-07-31、セキュリティ懸念による PM 判断 — LOG.md 参照）**。
実装（call_vision_llm / --slide-images / minutes_ab）はレビュー済み・テストグリーンで
**コミット済み（既定 OFF の opt-in）**。有効化の判断のみ保留。
ベンチは 20 本中 8 本完了時点で停止（data/eval/minutes_ab/ に保存）。
再開時は本エントリの下記内容がそのまま有効。詳細計画は `~/.claude/plans/rustling-pondering-starlight.md`。

**内容**: 文字起こし + スライド画像を直接 kimi-k3 に渡す議事録生成。
(1) llm.py に call_vision_llm 新設（複数画像・常時ストリーミング・usage/image_tokens 記録・
ctx 超過時の画像間引き梯子）、(2) generate_minutes_local に `--slide-images` +
`MINUTES_VISION_LLM_*` opt-in（Stage 2/3 のみ視覚化・失敗時テキストフォールバック・既定不変）、
(3) scripts/eval/minutes_ab.py で 4 アーム盲検 A/B（A=glm+OCR / B=K3+OCR / C=K3+画像 /
D=K3+画像+OCR、judge は中立の DeepSeek-V4-Flash）。
**Phase 2（本番 opt-in 配線）はベンチ勝利（C or D が A に ≥60% + 形式崩壊 0）が条件。**
OCR は視覚化後も廃止しない（Whisper initial_prompt の terminology が依存）。

### Kimi-K3 の実力を引き出す investigate 実装の検証（one-shot 長文脈 2×2 実験）

**ステータス**: 検証完了・**K3 は一旦停止（2026-07-31、セキュリティ懸念 — LOG.md 参照）**。
qa デーモンの K3 override（ARGUS_ONESHOT_LLM_MODEL）は除去済み。**one-shot 経路自体は
glm-5.2 で本番継続中**（ARGUS_ONESHOT=1、実測で現行超えのため）。評価記録・実装は再開時の
資産として維持。以下は経緯の記録（2026-07-29 着手）。
詳細計画: `~/.claude/plans/rustling-pondering-starlight.md`、背景は
docs/decisions/rikyu_argus_model_eval.md（investigate 単発品質のみ K3 が glm 超えという評価事実）。

**仮説**: K3-native な investigate は「補助 LLM 呼び出しゼロ + 決定的 broad-recall +
太い文脈 1 回渡し（one-shot）」。{現行ループ, one-shot} × {glm-5.2, kimi-k3} の 2×2 を
盲検 A/B（investigate_ab.py 拡張、4 ペア）で検証する。

**実装済み（全て opt-in・既定挙動不変、未コミット）**:
- retrieval.py: retrieve_chunks_hybrid の vector_k パラメータ化
- pm_argus_agent.py: `ARGUS_ONESHOT` one-shot 経路（LLM 1 回、rewrite/HyDE/re-rank バイパス）
- investigate_ab.py: ARM_PRESETS（glm-loop/glm-oneshot/k3-loop/k3-oneshot）+ stderr メトリクス
- 多段設問候補 9 問: data/eval/investigate_gold_candidates.yaml（**PM キュレーション待ち**）

**進捗（2026-07-30）**: 実装・レビュー対応・N スイープ・本走 5 ペア × gold 8 問まで完了
（40 件・error 0）。結果は eval doc 追補と LOG.md 2026-07-30 を参照。要点: search では
glm-oneshot が現行超え（83.3%・31s）、k3-oneshot はさらに上（66.7%）、docqa は one-shot 不適。
one-shot N=50 確定（RIKYU nginx 600s timeout 制約）。

**経過（2026-07-30 後半）**: mh- 9 問を gold に追記（計 17 問）し 5 ペア追加実行を開始したが、
敗因深掘りで検索段バグ 2 件を発見（sanitize の全角括弧未対応 → FTS 全段不成立、日付フォールバックが
RRF で vector 候補を押し出す）。修正・テスト済み（940+ 件グリーン）。汚染レコード 18 件を
investigate_k3_pre_sanitize_fix.jsonl に隔離し、11 問 × 5 ペアを修正済みコードで再計測中。
knowledge_context.py の重複 sanitize も一本化済み。

**方針アップデート**: PM 作成の設計メモ **docs/kimi-k3-migration.md**（2026-07-30）を統合。
「差し替えでなく役割分担」— K3 は視覚（Pass 1 文書読解）・長時間自律（Pass 3）・
preserved thinking の 3 軸で活かし、GLM-5.2 は高頻度テキストパスに残す。
実測との突合注記は同メモ末尾（RIKYU 配信では reasoning_effort 無効、nginx ~600s、
one-shot が loop より優位、の 3 点が設計前提に効く）。

**残作業**（メモ v2 のステップ0〜2 と整合）:
1. ~~再計測~~ **完了（2026-07-30）**: 55 エントリ全損ゼロ、バグ修正で隔離 18 件中 9 件の勝敗が
   反転。最終結論 = 経路は設問型依存（単発→one-shot / 多段→loop）、モデル軸は K3 一貫優位、
   多段品質首位は k3-loop（71.4%、455s）。eval doc 追補2 に記録済み。
   残欠陥: glm-loop の tool_calls メトリクス抽出疑義 / K3 の 27 字エラー様応答（answer 非 None で
   error 記録されない）への最小回答長ガード — investigate_ab.py の小改修 2 件
2. ~~ステップ0 プローブ~~ **ほぼ完了（2026-07-30、詳細は kimi-k3-migration.md 突合注記）**:
   (b) reasoning はトークン単位ストリーム確認 / (d) **image_url 受理・視覚動作確認**（優先度 2
   成立）/ (e) **prefix caching 自動有効**（cached 99.7% 実測）/ (c) 本番 504 実績 6 件（RiVault
   側）確認。**(a) 600 秒の種類のみ未確定** — 実ケースのストリーミング再現は 393s で完走
   （弱い正の証拠）。確定は運用側への直接確認（打診項目に追加）に委ね、追加プローブはしない。
   reasoning_effort は実測が矛盾（不安定扱い、設計で信頼しない）
3. **K3 配線 第 1 弾（承認済み、ステップ0 の結果を反映して実装）** — investigate one-shot
   限定の K3 モデル override（`ARGUS_ONESHOT_LLM_*` env、_run_oneshot 内でのみ消費、glm 自動
   フォールバック付き）。600 秒が無通信型ならこの経路は **stream 受信を既定に**（504 全損対策）。
   brief/risk/today は glm のまま（役割分担）
4. **API クライアント層の再設計（メモ v2 優先度 1 の a〜d）** — reasoning_content 往復 /
   ストリーミング既定 / 逐次永続化 / partial mode 再開 + ツール冪等化。多ターン経路
   （長時間セッション・優先度 3）の前提として第 1 弾とは分離して設計
5. **議事録生成ベンチ（メモ ステップ2）** — GLM vs K3、トークン単価・レイテンシ・
   スライド画像込み抽出精度の 3 点。(d) が通ることが前提
6. **PM 側の確認事項** — RIKYU タイムアウト緩和 / nginx 迂回経路の打診（600 秒がどの directive
   かの確認込み）、Kimi K3 License の機関確認、常駐割当・キュー待ちポリシー、
   ~/.claude/settings.json の GitHub PAT 失効・除去
7. ~~コミット + qa デーモン再起動~~ **完了（2026-07-30、K3 override 有効化済み）**。
   残る小課題: (a) recall_eval が _init_sudachi() を呼ばず fts_tokens 段が測定から素通りする
   計測ギャップの修正、(b) investigate_ab の glm-loop メトリクスは initial_search_calls を
   参照（tool_calls_total は STEP ループ内のみ）、(c) K3 実運用の観察（品質・レイテンシ・
   [oneshot][FALLBACK] 率）→ 数日問題なければ本エントリを LOG.md へ圧縮して削除

**ステータス**: 静的検査（tests/selfcheck/）は pre-commit で毎コミット自動実行中。
データ不変条件（pm_selfcheck.py）は手動実行で exit 0 を確認済み。

**残作業**:
1. **cron 登録（PM 実施）** — 以下を `crontab -e` で追加すると平日朝に自動実行される:
   `30 6 * * 1-5 /lvs0/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/bin/pm_selfcheck.sh`
   （違反時は logs/pm_selfcheck.log に一覧が残る。Slack 通知はまだ無い —
   数日運用して誤検知が無ければ通知配線を検討）
2. **数日の運用観察** — 静的検査が正当なコミットを誤って弾かないか、
   pm_selfcheck の誤検知（特に rollback_pattern / date_reversal_close）が無いか。
   問題なければ本エントリを削除して LOG に一行記録

### brief/risk 全文脈化の事後観察（本体は適用済み — LOG.md 2026-07-23 参照）

**ステータス**: PM 判断により brief/risk を全文脈 single-shot に統一・本番適用（2026-07-23）。
qa デーモン再起動後に有効。opt-out は `ARGUS_DISABLE_FULLCTX=1`、失敗時は従来切り詰め方式へ
自動フォールバック（1 回）。

**残作業**:
1. ~~翌朝の cron 確認~~ — **確認済み（2026-07-24）**: 07:47 の cron は brief/risk とも全文脈方式で
   実行、フォールバック warning 0 件、Canvas 投稿成功（replace方式）。なお total chars/est_tokens の
   計測行はログに出ていなかった（成否には影響なし。次回この観察を閉じる際に verbose 配線を確認）。
   同日 09時台の実行で temp canvas 削除の `canvas_editing_locked` WARN を 3 件観測 —
   投稿自体は成功しており実害なしだが、replace 方式の残骸掃除が単発失敗しうる点は Canvas
   エントリの既知挙動として記憶しておく
2. **数日の品質観察** — /argus-brief・/argus-risk・毎朝 Canvas の体感品質。
   問題があれば `ARGUS_DISABLE_FULLCTX=1` を qa デーモン起動環境と cron に設定して即戻せる

（A/B テスト成果物の平文ファイル 4 点は 2026-07-24 に削除済み。再現は argus_ab.py build-ctx で随時可能）

### read_document のツール選択率（運用は --file 一本化で決着・様子見）

**ステータス**: 決着（2026-07-20）。read_document ツール本体＋map 4並列化は実装・マージ済み
（経緯は LOG.md 2026-07-20）。

`read_document` は glm-5.2 のツール選択依存で発火が不安定。決定論的な自動エスカレーション案
（初期検索が単一 Box 文書に集中したら全文読込へ切替）は実装したが、実データ（類似報告書の
分散・box偏重コーパスでの誤爆・恒常 embedding コスト）で費用対効果が見合わず棄却（LOG参照）。
**運用方針**: 横断・数値集約が要る質問は `--file` 直指定（並列化済み・確実）に一本化。
read_document は「呼ばれれば効く」補助として残置。再燃時の残案は forced-synthesis 前ナッジ／
プロンプト強化だが、当面は着手しない（--file で十分回る想定）。

### 全文読込QA（--file）の汎用性補強（保留・様子見）

**ステータス**: 保留（2026-07-18、ユーザー判断で実運用の様子見）。

機構は汎用（アプリ名等のハードコード無し）だが、既知の限定が2つ:
(1) 偽「関連情報なし」ガードのエンティティ抽出が ASCII 限定（`[A-Za-z0-9/-]{3,}`）で、
日本語のみの固有名詞質問では強いガード（制限事項記録・記載なし断言禁止）が効かず
フォールバック（5,000字窓の却下リトライ）頼み。(2) e2e 実証が「アプリ評価報告書×性能系
クエリ」に偏在。再開時は日本語エンティティ抽出の拡張（カタカナ連続・「」引用語）と、
別文書×別質問型の検証スイープを実施。観測ログ（窓別応答字数・所要・エンティティ）は
整備済みで、実運用で問題が出れば診断は即可能。

### 休眠パス + gemma4 残滓監査への対応（詳細は docs/audit_20260724.md）

**ステータス**: **A〜D 全パッケージ完了**（A: 2026-07-24、B/C/D: 2026-07-27）。経緯は LOG.md、
監査記録の総括は docs/audit_20260724.md 冒頭を参照。残るは事後観察のみ。

**残作業（観察のみ）**:
1. **パッケージ B の事後観察** — (a) 次回以降の patrol 巡回で期限超過/停滞の再送がないこと
   （cooldown 7/14 日。`grep "期限超過リマインダー\|長期停滞検出" logs/pm_argus_patrol.log`）、
   (b) 管理者へのリダイレクト DM の量が運用に耐えるか（初回 104 件、以後は新規発生分のみ）。
   量が多すぎる場合は stale_days/cooldown_days の引き上げ、または enabled: false で即戻せる。
   **リダイレクト解除（実担当者への直接 DM 化）は効果を見て別途 PM 判断**
2. **パッケージ C の事後観察** — 次回 17:00 cron の today 全文脈経路、次回議事録処理の
   「Stage 1 をスキップ」ログ、次回 Slack ingest の抽出品質（トリアージ統合で `[TRIAGE]` DROP
   ログが消え 1 呼になる）、investigate/メンション応答の体感品質とレイテンシ
   （top10/1200字化の影響。退避: env `ARGUS_TOP_K_RERANK=5` `ARGUS_SEARCH_EXCERPT_CHARS=400`
   `ARGUS_RERANK_PREVIEW_CHARS=400` を qa 起動環境に設定して stop/start qa）。
   再測定はいつでも `scripts/eval/investigate_ab.py run`（gold 8 問・約 12 分）で可能

### 検索品質改善 2 件（LLM 第一段 + LLM re-rank）の事後観察（本体は完了 — LOG.md 2026-07-24 参照）

**ステータス**: 両方とも測定合格・既定有効で本番投入済み（2026-07-24）。
残るは実運用ログの観察のみ:
1. **LLM キーワード抽出** — `grep "キーワード(" logs/pm_from_slack_daily_*.log` で
   `(llm)` が主であること（`(sudachi)` 多発は LLM フォールバック多発の合図）。
   退避: `ARGUS_DISABLE_LLM_KEYWORDS=1`
2. **LLM re-rank** — investigate の体感レイテンシ悪化がないか、qa ログの
   `re-rank選択:` 行の出現。退避: `ARGUS_DISABLE_LLM_RERANK=1`（qa デーモン起動環境 +
   stop/start qa）。rivault(Kimi) フォールバック時は静かに先頭切りへ退化する仕様
3. 数日問題なければ本エントリ削除（LOG.md 記録済み）

### WhisperX 既定エンジン切替の事後観察（本体は完了 — LOG.md 2026-07-24 参照）

**ステータス**: 2026-07-24 に PM 判断で既定切替・本番投入済み。`pm_from_recording.sh` /
`/argus-transcribe` とも既定 `WHISPER_ENGINE=whisperx`（whisperx-blackwell.sif +
whisperx_pyfix オーバーレイ）で動作し、reconcile に決定論的話者名寄せが入る。
旧エンジンへの緊急ロールバックは環境変数 `WHISPER_ENGINE=transformers`（qa デーモン分は
起動環境に設定して stop/start qa）。whisper.sif・旧エンジンコードはフォールバックとして維持。

**残作業（観察のみ）**:
1. **実会議数本での品質観察** — 話者名寄せの確定率（[INFO] 話者名寄せ ログ）、未確定クラスタの
   LLM 推測品質、所要時間（60分会議で13-15分想定、うち約3分はプロセス初回ウォームアップ固定費）
2. **Stage 3 空応答ガードの発動頻度観察** — `[WARN] Stage 3 .*集約が空/不正形` の出現率。
   高頻度なら glm-5.2 の think=True 集約プロンプト側の見直しを検討
3. 問題なければ本エントリを削除（LOG.md に記録済み）

### initial-search 既定ON の事後観察（期限 2026-07-27）

**ステータス**: 観察中（2026-07-13〜）。コミット `6855533` で investigate/メンション応答の
全経路に初期 retrieval シードを既定ON化したが、検証は Q-Helix 1クエリの before/after のみ。
n=1 デプロイのため、実トラフィックで2週間観察してから定着/巻き戻しを判断する。

**確認コマンド**（`logs/pm_qa_server.log` に対して。qa デーモン稼働ホストで）:
- 発火実績: `grep -c "\[initial-search\]" logs/pm_qa_server.log`
- 所要分布: `grep "\[initial-search\] 完了" logs/pm_qa_server.log`（各行に `(X.Xs, N件)`）
- 失敗率: `grep -c "\[initial-search\] 事前検索スキップ" logs/pm_qa_server.log`

**判定基準**: メンション応答の体感遅延に不満が出る / シード所要が恒常的に 30s 超
→ qa デーモン起動環境に `ARGUS_DISABLE_INITIAL_SEARCH=1` を設定して opt-out（要デーモン再起動）。
問題なければ本エントリを削除し LOG.md に1行「観察完了・定着」を記録。

### アクションアイテム自動クローズの事後観察（本番稼働中 — 経緯は LOG.md 2026-07-24）

**ステータス**: 本番稼働中（`auto_close_enabled: true`）。2026-07-27 に 2 つの重大事故を修正
（経緯は LOG.md 同日エントリ×2）: (1) **日付逆転バグ** — 発生日より古い証拠での誤クローズ
15 件を特定・再オープンし、3 層防御を導入（0c63176）。(2) **xlsx_sync 巻き戻り再発** —
再エクスポート緩和策が box CLI PATH 欠落で 7/24 から無効だったのを修復（18:00 サイクルで
成功実証済み）し、pm_xlsx_sync に**シート鮮度ガード**を導入（d55e397。シートより新しい
pm.db 変更のフィールドは同期せず WARN、--force で明示上書き）。

**残作業（観察のみ）**:
1. **HIGH 判定の精度観察** — 今後の自動クローズ（リーダーチャンネル事後通知）を数週間分
   確認し、誤クローズがあれば `auto_close_min_confidence` / プロンプト抑制文を調整。
   誤りが目立つ場合は `auto_close_enabled: false` で承認ボタン運用に即戻せる。
   特に**日付逆転の残存リスク**（box の held_at は Box 側 modified_at のため「古い内容の文書が
   発生後に更新された」ケースは通過し得る）に該当する誤クローズが出ないか
2. **exempt_box=False 化後の完了検出の証拠品質** — patrol ログの「完了シグナル検出」件数が
   極端に減っていないか（発生日で box を絞ったことで証拠 recall が変わる。18:00 サイクルは
   正常に 2 件クローズ）
3. **鮮度ガードの WARN 観察** — `grep "シート編集" logs/` 系で人手編集が不当にスキップ
   されていないか。正当な編集が WARN された場合は最新シートで編集をやり直す運用
4. **Slack 抽出の背景知識 90 日窓化の影響**（since_date 修正の副次効果、LOG 参照）—
   次回 ingest の抽出品質に劣化がないか
   （旧 5「AI #2583 未来日付」は 2026-07-27 の selfcheck 導入時に原因特定・6 レコード修正済み — LOG.md 参照）

### 議事録転記トリアージの事後観察 + 既存データ精査（本体は 2026-07-28 導入 — LOG.md 参照）

**ステータス**: 転記時 3 ゲートトリアージを既定有効で導入。既存データは pm_screen --triage の
一括審査 CSV を生成済み（PM の精査・適用待ち）。

**残作業**:
1. **既存データ精査の適用（PM 実施）** — 一括審査 CSV（DROP 候補 + 理由）を確認し、
   誤判定を deleted=0 に直してから `pm_relink.py --import <csv> --dry-run` → 本適用。
   LLM 判定は false positive を含む前提（「共有する」で終わる実質的技術タスク等）
2. **転記トリアージの品質観察** — 次回以降の `pm_ingest.py minutes` ログの `[TRIAGE]` DROP 行を
   数回分確認し、正当な項目が落とされていないか（落とされていたら Web UI で deleted=0 に復活
   させれば以後 human_kept で保護される）。退避: `ARGUS_DISABLE_MINUTES_TRIAGE=1` または
   `--minutes-no-triage`
3. **二重トリアージの recall 影響** — 録音経路は生成時（generate_minutes_local）＋転記時の
   二重審査になる。会議の実項目が痩せすぎる場合は生成時 OFF（--no-triage）+ 転記時 ON への
   一本化を検討

### 実績DB（achievements ledger）週次 populate の初回観察

**ステータス**: 運用整備 4 件は 2026-07-24 に全消化（経緯は LOG.md 同日エントリ）。
残るは初回の週次実行観察のみ: **次の月曜（2026-07-27）02:00 の `pm_box_update.log`** で
「週次achievements populate」ステップの成功を確認し、問題なければ本エントリを削除。
スキップしたい場合は `ACHIEVEMENTS_WEEKLY=0`。

---

## 保留中の構想

着手判断待ちの計画。動かすときは「現在進行中の計画」セクションに移動して詳細化する。

### 2. Argus Phase 2: `/argus-do` 自動実行

**ステータス**: 保留中。LLM の JSON 構造化出力品質が安定したら着手。

**設計方針**:
- `/argus-brief` のアクション提案に `action_id` を付与
- 提案内容を `secretary_proposals` テーブルに保存
- `/argus-do a1` で対応する提案を pm.db に反映（assign_item, close_item 等）
- 実行前に対象アイテム ID をユーザーに確認表示する安全策を必須化

**未着手の理由**: 自動実行は誤りの影響が大きいため、まず Phase 1（提案・草案）の品質と
ユーザー受容を確認してから着手。

### 2.5 Argus 垂直軸 R3（流入モードA: argus-transcribe の決定捕捉拡張）

**ステータス**: 保留中（2026-07-05 の抜本見直し時に明示的に見送り）。垂直軸本体
（Phase 1〜3 + R1/R2 + /argus-direction）は完了・観察済み（LOG.md 2026-07-24 ほか参照）。

設計書§4が「最大レバレッジの一手」とする、決定確定の場での捕捉。argus-transcribe を
議事録生成器から決定捕捉器へ拡張し、決定の責任者にその場で2〜3行の確認
（理由・捨てた案・覆す条件）を求める。遡及エンリッチ（モードB）より確度の高い
台帳エントリが得られ、reversal_condition（覆す条件→レビュー発火）の実運用も
これで初めて成立する。**会議運用の変更を伴うため、R1+R2 の効果を見て PM が着手判断する**。
（Web UI への対話型グラフ追加は別件で見送り中 — PNG 静止画像で当面のニーズは充足）

### 5. investigate の retrieval recall 限界（主題外の固有名詞に埋もれた事実）

**ステータス**: 保留中（2026-07-13）。診断済み・軽量対策は失敗確認済み。

**問題**: investigate が「主題（例: GPU化・性能評価）から意味的に離れた語彙で書かれた事実」を
取りこぼす。実例: Q-Helix レポートで benchpark/AppTheta の完了状況（qa_index.db
id=22835[2026-05-15 Yamaura], 22877[2026-06-16], 17624[2026-06-16 Status表「AppTheta/fj/6」]、
いずれも `--since 2026-03-01` の在窓）を拾えず「確認できなかった」と過小報告。原因は
(a) 該当チャンクが簡潔な進捗ボックスノート／ステータス表で `GPU` 語を含まない、
(b) 文書側が英語 `benchpark`/`AppTheta`＋レベル番号、クエリ側が片仮名「ベンチマーク」＋GPU寄り、
という主題・日英表記のミスマッチ。

**試して失敗した案（2026-07-13）**: 案A＝rewrite プロンプトにドメイン同義語（英表記・略語・環境名）
併記の**汎用**ガイダンスを追加。→ 英語表現(status/porting)や富岳は入ったが、質問に無い固有名詞
（AppTheta/Benchpark/FX700/GH200/Genoa）までは LLM が生成せず、該当チャンクは依然未取得。
かつ latency が 2分→9分に増。**汎用の語彙拡張ではこの種の miss は解けない**と結論し、変更は破棄
（コミットせず）。

**却下した案（個別最適化のため不採用）**: 特定フォルダ（`22_進捗報告`）やステータス表を検索で
優先する案は、Q-Helix（特定の文書配置）に着目した**個別最適化**であり、他エンティティ・他の
フォルダ構成では効かず他クエリの精度を歪めるだけなので採らない。

**汎用の方向性（特定アプリ/フォルダ非依存）**:
- **索引由来の共起語拡張** — ❌ **試して失敗（2026-07-13, Stage1）**。qa_index.db に
  entity_cooccurrence を構築し retrieve_chunks に opt-in 配線して baseline-v1 と Δ 測定した結果、
  topic hit@k は改善せず（Δ≤0、悪化複数）。原因: エンティティの大域共起は「アプリ一覧に併記される
  他アプリ名・領域専門語」に支配され、狙った具体的関連語（AppTheta/Yamaura 等）は 70〜900 位に埋もれる。
  ＋FTS 暗黙 AND で変種がノイズ化。コード・テーブルは破棄済み（コミットせず）。
- **エンティティ起点の網羅パス** — ⏸ **検証したが保留（2026-07-13, Stage1）**。entity 検出＋recency
  取得は正しく動き、reserve slots(M=8) で **fts 層は topic hit@60 +0.071 と新規 recall を開けた**
  （メカニズムは実証）。しかし2つの壁で**実運用 hybrid では効果が正味ゼロ**: (1) hybrid 外側の
  fts+vector 融合(_rrf_merge)で anchor 候補が再クラウドアウト、(2) 動機ケース(Q-Helix/22877)は
  anchor recency 20位で M=8 に届かず、届かせる M 拡大は base 排除(precision 退行)と表裏。
  再開時の残作業: reserve を hybrid/hyde 最外層へ移す＋M を数水準で振り precision コストを実測して
  recall↔precision を判断。コードは破棄済み（コミットせず、ハーネス・baseline は温存）。
- **source_type 多様化（未着手）**: top-k を source_type（議事録/Slack/box）で分散させ、構造化文書が
  narrative に押し出されないようにする（特定フォルダ優先ではなく「型の多様性」を担保）。

いずれもランキング／取得ロジックに触るため、着手時は before/after の回帰測定が必須。当面は
安全側のヘッジ（「確認できなかった」）で運用継続。
（回帰測定の土台＝recall 評価ハーネス `scripts/eval/recall_eval.py`・baseline-v1（run_id 3）を
2026-07-13 に整備。以後の recall/precision 改善は本ハーネスの Δ で合否判定する。）

