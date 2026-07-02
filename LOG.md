# 変更ログ (LOG.md)

判断の経緯・破棄された案・方針転換を残す journal。運用ルールは `CLAUDE.md` を参照。

**フォーマット**: `## YYYY-MM-DD <一行サマリ>` → 本文に **背景 / 決定 / 影響** を 1-2 行ずつ。
新しいエントリを上に追加する。1 エントリ 3-5 行を目安に、長文は `docs/` 側に逃がす。

---

## 2026-07-02 アプリ評価エグゼクティブサマリー PPTX 生成を追加

**背景**: `pm_nvidia_collab_update.sh` はアプリ単位の Markdown レポートを生成するが、
全アプリを俯瞰する1枚物が無かった。全情報の網羅は不可能なため
「完了したこと/これからやること/ベンダー連携」の3カテゴリへの凝縮が要点。

**決定**: `scripts/reporting/pm_exec_summary.py` を新規作成。LLM で各アプリのレポートから
3カテゴリJSONを抽出（証拠のない「完了」は next へ回すゲート付き）→ `pptx_theme.py`
（2026-06-25 に旧 pm_biweekly_report.py 削除後、初の利用者）でアプリ×3カテゴリの
グリッド1枚に作図 → 日英2版を `box_upload_file` でアップロード。
`pm_nvidia_collab_update.sh` の末尾に統合、失敗しても個別レポート本体には影響しない。

**影響**: 実データ（GENESIS/SALMON）での動作確認済み。分類ゲートが証拠なき「完了」を
正しく除外することを確認。マルチカラムのpptx生成パターンが今後の同種レポートに再利用可能。

---

## 2026-06-25 pm_biweekly_report.py 廃止

**背景**: 隔週 pptx レポート (`reporting/pm_biweekly_report.py`) は現在
運用されておらず、`db_utils.open_knowledge_db` を import していた箇所が
knowledge.db 廃止 (2026-06-16) 後の取り残しで実際にクラッシュしていた。
ruff 導入の smoke test で発覚。

**決定**: 修復ではなく削除を選択。`scripts/reporting/pm_biweekly_report.py` /
symlink / `scripts/bin/pm_biweekly_report.sh` を削除し、docs/reports.md
(§2 と運用例)、docs/architecture.md、CLAUDE.md (pm-reports skill 参照)、
utils/pptx_theme.py の docstring からも除去。pptx 生成は他の用途で残るので
pptx_theme.py は据え置き。

**影響**: レポート系コマンドは pm_report / pm_insight / canvas_report.sh の
3 本に縮約。隔週 pptx が必要になった場合は git history から復元可。

---

## 2026-06-20〜22 pm-multi-agent (MCP) 導入・出力スキル統合・agent ループ全廃

**背景**: Claude Code から pm.db 検索・分析・Box/Slack/Canvas 出力を直接呼び出せる
MCP サーバーの要求。同時に Slack Bot (/argus-investigate) と挙動が異なり品質差が生じていた。

**決定**:
- `argus/mcp_tools.py` / `argus/output_tools.py` を新設 — MCP 全ツールの実装本体を
  pm_mcp_server.py と agent_tools.py で共有。pm-commands と pm-argus-commands は同一ツール群を提供
- agent ループ（`run_agent` のマルチステップ ToolCall→実行→ToolCall ループ + 重複防止 + 過剰呼び出し制限 +
  強制合成）を全廃し、single-shot 実行に変更。LLM の内部 reasoning に委譲
- 同様に `_run_brief` / `_run_risk` の Worker+Orchestrator パターンを廃止、single-shot 化
- `call_argus_llm()` のルーティングに claude_code ルートを追加（ANTHROPIC_BASE_URL 最優先、
  ARGUS_PREFER_RIVAULT より上位）。pm_daemon.sh は .claude/settings.json の env を自動読み込み
- --to-box / --to-slack / --to-canvas 出力先フラグを CLI と Slack コマンドの両方に追加
- pm-multi-agent / pm-argus-commands Skill を新規作成。argus-system Skill を更新

**影響**: argus-investigate / brief / risk の制約（1200字・5ステップ・早期終了）を全解除。
出力ツールは MCP と Slack の両方から使用可能。回答品質は使用する LLM に依存
（claude_code > rivault > local）。従来の agent_tools 専用ツール（get_weekly_trends,
get_unacknowledged_decisions）は廃止。

---

## 2026-06-19 Argus モジュール責務分割（テスト基盤 + Phase 2/3 リファクタリング）

**背景**: Opus 4.7 が完了した scripts/ 再編は「ファイルを正しい場所に移す」段階まで。`pm_argus.py`(2832行)・`pm_qa_server.py`(1805行)・`pm_argus_agent.py`(1465行)・`cli_utils.py`(1118行) が依然として巨大モノリスで、LLM 出力の非決定性・Slack API 副作用・SQLite スキーマ変更に対するレグレッションガードが皆無だった。リファクタリングを安全に進めるにはテストが前提と判断。

**決定**: テスト基盤（pytest 102 件）を先に整備し、通過を確認しながら 6 ステップで責務分割を実施。
- Phase 2（横断レイヤー）: `slack_post.py`・`retrieval.py`・`llm.py` を新設し、mrkdwn ヘルパ / FTS5+ベクトル検索 / LLM ラッパを抽出
- Phase 3（縦割り）: `prompts.py`・`agent_tools.py`・`transcript.py` を新設し、プロンプト定数 / ツール実装群 / Whisper パーサを分離
- 後方互換のため各元ファイルは `from <新モジュール> import *` で再 export し、既存の CRON・シェルスクリプトへの影響はゼロ

**副産物バグ修正**: `_fts5_search` / `_fts_tokens_search` の SELECT で `c.id` が欠落しており、ハイブリッド検索時に `_rrf_merge` で `KeyError: 'id'` が発生していた（テスト作成で発覚）。

**影響**: 削減行数 — `pm_argus.py` 543行、`pm_argus_agent.py` 526行、`pm_qa_server.py` 560行、`cli_utils.py` 524行。次のリファクタリング（pm_qa_server の Bolt ハンドラ分離、pm_argus の Orchestrator / TTS 分離）はテスト保護が整った状態で着手できる。

---

## 2026-06-16 knowledge.db 全廃、背景知識を pm.db.decisions に集約

**背景**: 1,801 件中 active 1,552、うち 66% が superseded、人手編集 0 件、Patrol の conflicts_with 検出も 0 件。実消費は brief/risk のプロンプト同梱と investigate の search_knowledge のみで、knowledge.db ⊃ pm.db.decisions の関係が二重化していた（KN-1794 と D-1254 など）。蒸留は毎日 68 回の LLM 呼び出しを消費していたが、その出力の上位は既に pm.db.decisions に rationale 付き 78% で記録されていた。

**決定**: 「目的（判断の背景知識を提供）は維持、実装としての knowledge.db は廃止」の方針で、4 段階で全撤去：
- Stage 1: `fetch_background_knowledge()` を新設し、brief/risk から `pm.db.decisions` (rationale 付き) を引いて Markdown 化
- Stage 2: search_knowledge / get_knowledge ツール、detect_knowledge_conflicts、`/argus-knowledge` を全削除
- Stage 3: pm_box_distill.py + pm_knowledge_* を scripts/archive/knowledge_db_deprecated/ へ、_KNOWLEDGE_SCHEMA / open_knowledge_db を削除、data/knowledge.db.deprecated_20260616 にリネーム
- Stage 4: docs/architecture.md（4 層 → 3 層）、CLAUDE.md、pm-distill-policy Skill、docs/distill_policy.md 削除

**影響**: brief/risk 同梱の背景知識は KN-XXXX → D-XXX 形式に変化。`/argus-knowledge` Slack コマンドは消滅。Patrol の knowledge_conflict 検出器は消滅（30 日間ゼロ検出だったため実害なし）。LLM 蒸留コスト約 2,000 req/月 削減。cron の 04:00 daily pm_box_distill.sh エントリの crontab 編集はユーザー手動対応。

**他案の検討**: 案 Y (pm.db に policy_constraints 新設)・案 Z (Markdown を git で人手承認制) も検討したが、pm.db.decisions の rationale 78% カバレッジで brief/risk の品質が十分担保できることが実測でわかり、最小実装の案 X を採用。

---

**背景**: `scripts/` 直下に Python 28 + shell 16 がフラットに並び `docs/architecture.md` の論理分類と乖離。また `pm_box_update.sh` の cron が暗号化 PPTX を毎回再変換しようとして失敗ループ、OCR が gemma-4 で動かず RiVault に流れていた。

**決定**:
- Phase 1: Python を機能別に 7 サブディレクトリ集約（utils/, data-pipeline/, minutes/, reporting/, quality/, web/, tts/）。後方互換のため scripts/ 直下に symlink を残す方針を採用。Phase 2（symlink 解除）は data-pipeline がハイフン名なこと・CRON 影響が大きいことから費用対効果が悪く skip。
- Phase 3: pm_xlsx_report / pm_xlsx_sync / pm_minutes_catalog / pm_minutes_publish で重複していた Box CLI ヘルパー (`box_find_file` 等) を `utils/box_cli.py` に統合（純減 75 行）。
- 暗号化 OOXML 検出を `pm_box_crawl.convert_to_markdown` 先頭で実施し placeholder 行を書いて再変換ループを止めた。LibreOffice 並列起動には `-env:UserInstallation` を付与し silent fail を回避。
- OCR endpoint 選択を反転：LOCAL_LLM_URL があれば localhost (gemma-4) 優先、なければ RiVault フォールバック。

**影響**: 既存 cron / シェルスクリプトは symlink 経由で動作継続。Phase 2 は将来必要になったとき再着手（`data-pipeline` → `data_pipeline` リネーム + 全 import 書き換え + CRON 更新）。詳細計画 `~/.claude/plans/plan-stateful-curry.md` は破棄。

---

## 2026-06-11 Admin Web Dashboard 実装

**背景**: 管理者が SSH + コマンド実行で行っていた全操作（録音処理→議事録生成、データ取り込み、ナレッジ蒸留、レポート生成、サービス管理）をブラウザから実行できるようにする必要があった。

**決定**: 既存の `pm_api.py` (FastAPI) + `scripts/static/` Web UI を拡張し、19 の `/api/admin/*` エンドポイント + 7 ページの SPA 管理ダッシュボードを追加。新規 npm/Node/Python 依存ゼロ。ジョブキューは SQLite 永続化 + スレッド実行（FastAPI 同期エンドポイント対応）。

**影響**: 
- PM_DB Editor の既存機能（AG Grid）は完全維持
- `data/admin_jobs.db` が新規作成される（ジョブ履歴の永続化）
- `scripts/web_admin.py` 新設、`web_admin.AdminJobQueue` で全管理操作を非同期実行

**副次修正**:
- `pm_ingest.py` に `--force` 共通オプション追加、`IngestContext.force` で全プラグイン統一
- 全 11 シェルスクリプトの Python venv パスを `uname -m` 自動判定に統一（aarch64/x86_64 両対応）

**背景**: `ARGUS_PREFER_RIVAULT=1` 時、`slide_ocr.py` が `RIVAULT_URL` を base_url に選び、
`_ocr_image()` が `RIVAULT_MODEL`（= `deepseek-ai/DeepSeek-V4-Flash`、テキスト専用）でリクエストして
400 Bad Request になっていた。`scripts/eval/slide_ocr_compare.py` の結果（`/tmp/slide_compare/report.md`）では
`gemma3:12b` が 0/7、`Qwen3.6-35B-A3B-FP8` が 7/7 であり、vision 対応モデルの明示指定が必要と確認済み。

**決定**: OCR 用に `RIVAULT_OCR_MODEL` 環境変数を新設。`slide_ocr.py` / `pm_box_crawl._ocr_image()` は
この変数が設定されている場合のみ RiVault を使い、未設定時は `ARGUS_PREFER_RIVAULT=1` でもローカル vLLM
（gemma-4）にフォールバック。RiVault で OCR したい場合は `rivault_tokens.sh` に
`export RIVAULT_OCR_MODEL=Qwen/Qwen3.6-35B-A3B-FP8` を追加する。

---

## 2026-06-08 別環境スクリプト持ち込みによる RiVault リグレッション修正

**背景**: Triage Agent 追加（同日）のスクリプトを別環境から持ち込んだ際、`3ccbfd7`（ARGUS_PREFER_RIVAULT=1 統一）
と `0b3752b`（Pass1 Slack 抽出を call_argus_llm 経由に変更）の修正内容が上書きされた。

**決定**: `scripts/ingest/slack.py`・`scripts/pm_from_recording.sh`・`scripts/recording/generate_minutes_local.py`
の 3 ファイルを再修正。slack.py は `call_argus_llm` インポートに戻し、`triage_items()` / `_sample_extractions()` /
consensus_n≤1 の全 LLM 呼び出しを置き換え。`base_t` を 0.6 → 0.4 に戻した。pm_from_recording.sh は
`ARGUS_PREFER_RIVAULT=1` 条件分岐と RiVault トークンコメントを復元し、`--url`/`--token` を空変数で
上書きしない条件付き渡しに修正。generate_minutes_local.py は `load_local_llm_endpoint()` の RiVault 分岐と
`main()` の `using_rivault` 検出ブロックを復元。

**再発防止**: 別環境から持ち込むスクリプトは `git diff` で RiVault 関連パターン（`call_argus_llm` /
`ARGUS_PREFER_RIVAULT` / `load_local_llm_endpoint`）の欠落を事前確認すること。

---

## 2026-06-08 抽出・転記パイプラインに Triage Agent を追加

**背景**: EXTRACT_PROMPT は5基準+do-not-extract リスト+few-shot で「大半のスレッドは空配列が正しい」と
指示しているにもかかわらず、些末な項目が pm.db に大量に漏れ出ていた。これは単一LLM呼び出しで
「抽出」と「意義判定」を同時に行うことの構造的限界（DevNous 論文の "engineered bias towards action"：
NO_ACTION F1=0.308）が原因。

**決定**: Extractor → Triage の2段階分離を実装。Extractor（既存 EXTRACT_PROMPT）は高リコールで
候補を拾い、新設の Triage Agent（TRIAGE_PROMPT）が3ゲート（マイルストーン関連性・代替可能性・
影響範囲）で審査し KEEP/DROP を判定。Triage はデフォルト有効、`--slack-no-triage` / `--no-triage`
で無効化可能。DROP理由は stderr にログ出力され、人間が監査可能。JSON パース失敗時はフェイルセーフ
（元の候補をそのまま返す）。議事録経路では `minutes.py`（転記時）ではなく生成パイプライン側
（`pm_minutes_import.py` / `generate_minutes_local.py`）でトリアージを挟む。転記時は既に上流で
フィルタ済みのため効果が薄いという判断。

**影響**: `scripts/ingest/slack.py` に TRIAGE_PROMPT・triage_items()・enable_triage パラメータを追加。
`scripts/pm_minutes_import.py` の `process_file()` にトリアージ導線を追加。
`scripts/recording/generate_minutes_local.py` にトリアージ導線と `_reconstruct_decisions_md()` を追加。
`pm_from_recording.sh` に `--no-triage` オプションを追加。

---

## 2026-06-05 RiVault 移行: 環境変数一本制御 + V4-Flash のアクションアイテム過剰抽出対策

**背景**: `ARGUS_PREFER_RIVAULT=1` で全 LLM 呼び出しを RiVault に切り替える実装を進めた際、
(1) 各スクリプトに `--rivault` CLI フラグを追加する案が出たが、フラグ増殖を嫌いユーザー判断で却下。
(2) V4-Flash は gemma4 より多弁で、アクションアイテムを 8-10 件抽出してしまう傾向が発覚。

**決定**:
- CLI フラグは一切追加せず `ARGUS_PREFER_RIVAULT=1` + `RIVAULT_URL/TOKEN/MODEL` 環境変数のみで制御。
  `call_claude()` / `call_local_llm()` / `detect_vllm_model()` / `slide_ocr` / `transcribe_pipeline` すべて
  この環境変数を見て分岐する。`pm_daemon.sh` は `rivault_tokens.sh` 読み込み後に自動 export。
- アクションアイテム過剰抽出は `DECISIONS_TEMPLATE` と `CONSENSUS_ACTIONS_TEMPLATE` に「通常 3-4 件、最大 5 件」
  の個数上限を明示して抑制。LLM の自己判断に任せると V4-Flash は寛容方向に振れるため明示的上限が必要。

**捨てた案**: `--rivault` フラグ — スクリプトごとに追加が必要で保守コスト大。環境変数ならデーモン起動時に 1 箇所で済む。

---

## 2026-06-05 Argus 主力 LLM を gemma4 → DeepSeek-V4-Flash に切替判断

**背景**: GB200 NVL4 で RiVault 経由の DeepSeek-V4-Flash が利用可能になり、現行 gemma4
(GB10 上 vLLM、Whisper と同居) と比べて品質・速度ともに乗り換える価値があるか検証した。
本番非影響で進めるため `scripts/eval/argus_ab.py` / `argus_ab_judge.py` を新設し、pm.db /
knowledge.db から brief/risk/investigate 30 件を合成、4 モデル × 2 judge で採点した。

**決定**: V4-Flash (Non-think) に全面切替。`call_rivault` の thinking 無効化分岐を V4 系にも
適用（`enable_thinking=False` がそのまま効く）。`~/.secrets/rivault_tokens.sh` で
`RIVAULT_MODEL=deepseek-ai/DeepSeek-V4-Flash` + `ARGUS_PREFER_RIVAULT=1` を設定し、
`pm_qa_server.py` を再起動するだけで切替完了。Pass1 抽出 (Slack/議事録) は
`call_local_llm` を直接叩いているため当面 gemma4 のまま (要 follow-up)。

**根拠** (judge 横断、5 段階 overall):
- DeepSeek-V4-Flash Non-think: 4.57 / think: 4.27 / GLM-4.7-Flash: 3.24 / **gemma4 think: 1.92**
- gemma4 vs V4-Flash 直接 A/B: V4-Flash 17 勝 / gemma4 3 勝 / tie 1 (think 同士)
- 速度: V4-Flash 1-7 秒, gemma4 think 62 秒 (8-10 倍速)
- think モードは brief/risk のような構造化タスクで Non-think より低スコアだったため Non-think 既定

**捨てた案**:
- GLM-5.1-NVFP4 (754B): GB200 1 ノードでは active 効率に対する重さがネックで V4-Flash 優位
- think モード ON 既定: -0.30pt の品質劣化 + 5 倍 latency。investigate のみ将来検証
- gemma4 の Non-think 検証: deep-research では gemma-3-27B GPQA 24.3 で見劣りが明白だったため省略

**影響**: investigate / brief / risk / patrol / Pass3 蒸留が V4-Flash に切替。GB10 の vLLM は
Whisper 単独で稼働するためメモリ余裕が出る (gpu_memory_utilization 上げ可)。検証データは
`data/eval/v4flash_ab.db` に保管、再評価可能。

---

## 2026-05-29 `/argus-narrate` — PPTX/PDF をスライド要約読み上げ mp4 化

**背景**: argus-today/brief/risk の音声化が好評。PPTX/PDF も全文読み上げは間延びするが、
スライドごとに 2-3 文の要約読み上げ + スライド画像を組合せた mp4 なら「概観の skim」用途に有効。

**決定**:
- `scripts/build_slide_video.py` を新設。各スライドについて (A) PPTX→python-pptx で本文+notes /
  PDF→pdftotext / PyMuPDF で抽出、(B) `slide_ocr.ocr_slide_image` でマルチモーダル OCR、両方を
  併記して LLM に投げ「(A) 優先・(B) は補完」で要約。`pm_tts.synth_chunk` / `concat_wavs` を直接
  使ってスライド粒度で WAV を作り、`ffmpeg -loop 1` で静止画+音声→セグメント mp4、concat demuxer
  で 1 本に結合。
- Slack エンドポイント `/argus-narrate <filename.pptx|pdf>` を `pm_qa_server.py` に追加。
  `_run_narrate` は `/argus-transcribe` を雛形にしつつ排他制御は `_narrate_lock` で軽量に。
  生成 mp4 は `_post_argus_video` (`_post_argus_voice` を mp4 用に派生) でチャンネルに投稿し、
  `voice_uploads.record_upload(kind="narrate")` で履歴記録。`:wastebasket:` リアクションと
  `/argus-delete` スレッド一括削除は既存コードで自動的に対象になる。
- OCR とテキスト抽出を併用したのは、画像 OCR 単独だと数式・表・小さい文字で誤認識が出るため。

**影響**: PPTX/PDF を Slack に上げて `/argus-narrate slides.pptx` を叩くと要約 mp4 がスレッドに
投稿される。Slack App 側で `/argus-narrate` の登録が必要。VOICEVOX エンジンが必須。

---

## 2026-05-29 argus 出力の音声化 (VOICEVOX) と削除 UX 整備

**背景**: `/argus-today` `/argus-brief` `/argus-risk` および議事録パイプラインの出力テキストを
通勤・移動中に聴き流したい、という要望。VOICEVOX エンジン (http://localhost:50021) はローカルで
稼働済み。素のテキストをそのまま合成すると(a) 数百チャンクで再生時間が延びる、(b) URL や記号が
不自然に読まれる、という問題。さらに「ephemeral と整合的に音声をどう届けるか」「削除手段は」も
個別に決める必要があった。

**決定**:
- `scripts/pm_tts.py` を新規追加。VOICEVOX `audio_query` → `synthesis` をチャンク化して呼び出し、
  `wave` で結合し ffmpeg で MP3 化。default speaker=74 (琴詠ニア) / speed=1.3。
- LLM 要約モードを 3 つ実装: `auto` (見出し/番号付き) / `minutes` (## 決定事項・## 議事内容→### 単位・
  ## アクションアイテム) / `priority` (`- **[優先度: 高/中/低]**` 単位)。argus-today=auto, brief・risk=
  priority, 議事録=minutes をハンドラ側でハードコード。要約は `cli_utils.call_argus_llm` で 1 セクション
  あたり 2 文 / 120 字以内に圧縮。
- 投稿先は当初 `conversations_open` で実行者 DM にしていたが、Slack の "App" セクションに隔離されて
  視認性が悪いとの指摘で `command.channel_id` への chat に変更（テキストは ephemeral・mp3 はチャンネル
  公開）。
- 削除はスラッシュコマンドではスレッド `thread_ts` が取れないため `:wastebasket:` リアクション式に変更。
  `voice_uploads.db` (新規・非暗号化) に file_id / message_ts / channel_id を記録し、
  `app.event("reaction_added")` で本人投稿または記録済みメッセージのみを `_delete_thread_files` で
  一括削除。bot メッセージ自体も `chat_delete`。
- VOICEVOX 利用規約遵守のため `pm_tts.credit_line(speaker_id)` を `/speakers` API から動的解決し、
  `initial_comment` に "音声合成に『VOICEVOX:話者名』を使用" を埋め込み。
- Slack section block は先頭スペースを表示しないため、入れ子箇条書きが Canvas と差が出ていた。
  `_to_slack_mrkdwn` を改修し `- ` → `•`、`  - ` → `　　◦`、`    - ` → `　　　　▪` に NBSP+Unicode
  ブレットで階層化。

**影響**: argus 系コマンド・議事録投稿に音声 mp3 が併投され、Canvas と Slack の見え方が揃う。
`pm_qa_server` 再起動が必要。Bot Token Scopes に `reactions:read` 追加と Event Subscriptions の
`reaction_added` 購読が前提。`pm_from_recording.sh` (ローカル CLI) は対象外で従来通り。各コマンドの
音声無効化用に `ARGUS_TODAY_VOICE` / `ARGUS_BRIEF_VOICE` / `ARGUS_RISK_VOICE` / `MINUTES_VOICE`
環境変数を用意。テスト用に `scripts/pm_tts_test_upload.py` を同梱。

## 2026-05-28 argus-today のチャンネル ID / ユーザー ID を表示名に解決

**背景**: `/argus-today` の出力でチャンネル ID (`Cxxx`) と Slack user_id (`U0xxxxxxxxx`) が
そのまま露出していた。原因は 2 つ。(1) `_build_channel_name_map()` が argus_config.yaml の
**コメント行**から `# Cxxx 名前` を拾う旧仕様で、df27935 で機密削除されコメントが除去されて以降
0 件返していた。(2) slack_pipeline.py の users_info 失敗時に user_id を user_name にフォールバックしており、
slack.db 上で 99 user_id 中 55 件が `user_name=user_id` のまま。Argus 側で逆引きが効かない。

**決定**: argus_config.yaml に `user_names:` セクションを新設し正本とする（slack.db はフォールバック扱い）。
更新は新規 `scripts/pm_users_sync.py` で `users.list` API を 1 回叩いて流し込む（既存値は --force なしで保護、
yaml の他セクションのコメント・順序はテキスト置換で温存）。`cli_utils` に `resolve_user_names()` /
`resolve_channel_names()` を共通実装し、`_build_channel_name_map()` をコメント抽出から正規キー読み込みに
置き換え。`_filter_mentions_for_user` でメンション本文中の `Cxxx` / `<#Cxxx>` / `<#Cxxx|name>` も
`#name` に展開。`## チャンネル: Cxxx` 見出しも `Cxxx (#name)` 形式に変更し LLM プロンプト全体で生 ID を減らす。
別案として「slack_pipeline 側の users_info 失敗時に user_id をフォールバックしない」も検討したが、
取り込みパイプラインを壊すリスクと既存 55 件への対応にならないため不採用。

**影響**: 表示名は yaml で一元管理・手動修正が容易に。argus_agent と argus-today で channel_names の
読み出しが統一された。pm_qa_server 再起動で新コードが反映される。

## 2026-05-28 議事録 Stage 3 集約のフォールバック修正（途中結果の破棄を解消）

**背景**: 4ba721c で修正した `/argus-investigate` の「ステップ上限到達時に最後のツール結果が捨てられる」
バグと同種のものが `recording/generate_minutes_local.py::_consensus_stage3` にあった。
embedding 失敗時に関数全体を `return max(drafts, key=len)` で抜けるため、決定事項側でエラーが
出ると AI 集約に進まず、AI 側でエラーが出ると既に合成した決定事項が捨てられる。LLM 集約失敗時も
`decisions_md=""` で「（なし）」化され、投票通過済みクラスタの中間情報がすべて消えていた。

**決定**: フォールバックを 4 種類に分離する。embedding 失敗 → 最長ドラフトから当該セクションだけ
抜き出す（他方の集約は通常通り続行）。LLM 集約失敗 → 投票通過済みクラスタの代表 bullet/行で
Markdown を直接組み立てる（LLM 不使用）。`_extract_section()` ヘルパーを追加。

**影響**: 議事録生成中に部分的な障害が起きても、可能な限り中間結果を保持して出力する。
ボツ案: 「失敗時に LLM をリトライ」は採らず（タイムアウト既に長く、二重に時間がかかる）。

## 2026-05-28 ドキュメント運用ルールを CLAUDE.md に集約、log.md → LOG.md にリネーム

**背景**: 運用ルールが PLAN.md / LOG.md の両方に重複していた。Claude が毎会話で参照するのは CLAUDE.md
だけなので、ルールは一箇所に集約した方が一貫性を担保しやすい。また他リポジトリの慣例（README.md / LOG.md）
に合わせて大文字統一。

**決定**: 運用ルールは CLAUDE.md の「ドキュメント運用ルール」セクションを正本とする。PLAN.md / LOG.md
の冒頭は「運用ルールは CLAUDE.md を参照」の一行のみに簡素化。`log.md` は `LOG.md` にリネーム（git 追跡前の
ためファイルシステム上の rename のみ）。

## 2026-05-28 CLAUDE.md スリム化 — ファイル構成セクション削除

**背景**: CLAUDE.md の「ファイル構成」セクション（80行強）は `docs/architecture.md` のスクリプト分類一覧と
ほぼ重複。CLAUDE.md は毎会話のコンテキストに自動展開されるため、重複情報はトークン浪費になる。

**決定**: スクリプト一覧は CLAUDE.md から削除し、`@docs/architecture.md` のインクルードに任せる。
DB 役割は要約形に圧縮（詳細は `pm-schema` Skill 経由）。日付付きの DB 統合経緯（2026-05-17 / 05-18）も
削除し、log.md / git log 側で参照する形にした。

**影響**: CLAUDE.md は約 250 → 約 130 行に縮小。詳細な情報が必要な場合は Skill か `docs/` 直接参照。

## 2026-05-28 PLAN.md を Argus から次の in-flight 計画に切り替え

**背景**: 旧 PLAN.md は 2026-04 時点の Argus（`/pm-brief` `/pm-draft` `/pm-risk`）実装計画で、
既に実装済み・コマンド名も `/argus-*` に変更されており、現状と乖離していた。

**決定**: PLAN.md は「進行中の計画のみ置く」という運用に統一。Argus 計画は完了扱いとして本 log に
1 エントリで残し、PLAN.md は次の候補（Phase 2 `/pm-do` 自動実行 / 日程調整 Agent / ナレッジ蒸留の品質改善）
を保留中項目として整理する形に書き換える。

## 2026-05-28 ドキュメント運用ルールを 3 ファイルに分離

**背景**: CLAUDE.md に変更履歴・経緯を書き込んでいくとコンテキストが肥大化し S/N が下がる。
PLAN.md と CLAUDE.md の境界も曖昧になっていた。

**決定**:
- `CLAUDE.md` — 現在のプロジェクト規約・禁止事項・ポインタ（変更頻度低、毎会話で自動展開）
- `PLAN.md` — 進行中の実装計画のみ（完了したら log.md に圧縮して PLAN.md からは削除）
- `log.md` — 完了した変更・方針転換・破棄された案（journal、新しいものを上）

**影響**: CLAUDE.md 冒頭に運用ルールを明記。今後 PLAN.md のエントリが完了したら本 log に
3-5 行で圧縮し、PLAN.md からは削除する運用に統一する。

---

## 過去の主要マイルストーン（要約）

git log で詳細は追えるが、判断の経緯として残す価値があるもの。

### 2026-04-16 Argus AI 実装完了（旧 PLAN.md）
- `/argus-brief` `/argus-draft` `/argus-risk` を `pm_qa_server.py` に統合（独立デーモンを立てない方針）。
- LLM は当初 GLM-4.7-Flash（200k context）採用 → その後 Kimi-K2-Thinking、`/argus-investigate` は
  2026-05-14 に gemma4 reasoning へ移行。長文コンテキスト処理は RiVault、軽量タスクはローカル gemma4 と棲み分け。
- 詳細は `docs/argus_system.md` と関連 commit。

### 2026-05-17 PM DB を `pm.db` に一本化
- `pm-hpc.db` / `pm-pmo.db` / `pm-personal.db` の分割を廃止。
- 出典チャンネルは `action_items.channel_id` / `decisions.channel_id` 列で保持する設計に変更。
- 分割は当初「組織別フィルタリングを楽にする」目的だったが、跨ぎ集計・Web UI 実装で
  複数 DB スキャンが負担になり、列フィルタの方が筋が良いと判断。

### 2026-05-18 Slack DB / FTS5 インデックスを統合
- `data/{channel_id}.db` 分割と `data/qa_pm*.db` 分割を廃止し、`slack.db` と `qa_index.db` に統合。
- FTS5 は `chunks` + `chunk_indexes(chunk_id, index_name)` の junction で論理 index を表現。
- 旧 DB は `data/*.db.bak` として保管。

### 2026-05-18 ナレッジ蒸留レイヤ（Pass 3）導入
- BOX 本文・議事録・決定事項を意思決定単位に蒸留 → `data/knowledge.db`。
- Stage 1（gemma4 抽出）→ Stage 2（bge-m3 類似度 + Kimi 審査）の二段ゲートで重複・ノイズを抑制。
- 採否ポリシーは `docs/distill_policy.md`、人手介入は `pm_knowledge_edit.py` / `/argus-knowledge`。
