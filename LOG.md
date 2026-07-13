# 変更ログ (LOG.md)

判断の経緯・破棄された案・方針転換を残す journal。運用ルールは `CLAUDE.md` を参照。

**フォーマット**: `## YYYY-MM-DD <一行サマリ>` → 本文に **背景 / 決定 / 影響** を 1-2 行ずつ。
新しいエントリを上に追加する。1 エントリ 3-5 行を目安に、長文は `docs/` 側に逃がす。

---

## 2026-07-13 Argus 主力LLM は全用途 DeepSeek-V4-Flash 単独運用に確定（Qwen 見送り）

**背景**: 2026-07-11 評価では対話系→Qwen3.6-35B-A3B-FP8 / 集約分析→DeepSeek の
ハイブリッドを推奨としていた。これを詰めるため実際の `pm_argus_agent.py --investigate`
（マルチステップ tool-call ループ）で SCALE-LETKF を2パス実走し両モデルを比較
（env `RIVAULT_MODEL`+`ARGUS_SKIP_LLM_SECRETS=1` で切替、コード無改修、本番非破壊）。

**決定**: **全用途 DeepSeek 単独運用**（現行主力を維持、Qwen 採用見送り）。理由: `llm.py`
の `call_rivault()` が Kimi 系以外で thinking を強制無効化するため、thinking 前提の
Qwen3.6-35B-A3B-FP8 は investigate の複雑な system prompt+tool-call 形式下で content 0 文字
となり**ループを駆動できない**（3回再現）。対話の速さより一本化の単純性と investigate での
確実動作を優先。DeepSeek は2パス完走・構造/証跡遵守良好（Pass1 1m57s / Pass2 2m6s）。

**影響**: ハイブリッド案は破棄。Qwen を investigate 対応させるには thinking ポリシー改修+
reasoning_content/content の扱い+tool-call 形式検証が必要で投資に見合わずと判断。
副次的に、investigate 実走で (a) Pass2 が max-steps 15 枠でツール未呼び出しの単発生成に
なる点（証跡の retrieval 裏打ちは要検証）、(b) INFO ログが stdout に漏れ Box レポート先頭に
混入し得る点を発見（別途対応候補）。詳細は `docs/decisions/rivault_model_eval_2026-07.md`。

## 2026-07-11 RiVault モデルの Argus 適性を再評価 — 用途別ハイブリッド運用を推奨

**背景**: 現行主力 `DeepSeek-V4-Flash`（2026-06-05 切替）が最適か、RiVault の他モデルと
2段階で再評価。Stage1 軽量ヒューリスティック（`eval_rivault_models.py`）は速度偏重で
質判定に使えないと判明（DeepSeek が速度減点で6位に沈む）。Stage2 で上位3挑戦モデル+
DeepSeek を LLM-as-judge 盲検 A/B（Kimi-K2-Thinking judge、既存30サンプル再利用、
max_tokens は 2048 だと thinking 予算切れで parse_failed 多発のため 4096 で再実行）。

**決定**: 質は DeepSeek がわずかに優勢（vs Qwen3.6-35B-A3B-FP8 で 15-12、overall 4.22 vs
4.00）だが突出して遅い。**用途でモデルを分ける** — 対話即応（brief/risk）は簡潔・指示遵守・
10〜20倍速の `Qwen3.6-35B-A3B-FP8`、広範な情報集約・分析の無人バッチ
（`pm_nvidia_collab_update.sh` 等）は網羅性・構造化で優る `DeepSeek-V4-Flash`。
Llama-4-Scout / GLM-4.7-FP8 は高速だが質で明確に劣後し除外。

**影響**: 切替はまだ未実施（本エントリは評価と推奨の記録）。DeepSeek の量子化は非量子化の
可能性が高いが未確定（LiteLLM Proxy 経由では dtype 不可視、運用者確認が必要）。A/B は
単発生成の評価でマルチステップ investigate ループは未検証。詳細は
`docs/decisions/rivault_model_eval_2026-07.md`。生データ `data/eval/stage2_ab.db`。

## 2026-07-06 WhisperX/GB10テスト完了 — 品質は優位・速度はctranslate2のBlackwell未対応がボトルネック、vLLMスケジューラ停滞も発見

**背景**: Whisper文字起こし+話者分離の高速化のため、ユーザーが用意した
whisperx-blackwell.sif（docker://mekopa/whisperx-blackwell）への対応をテスト。
SIFはアップストリームの時点で3箇所破損しており（numpy混在・torch2.6のweights_only・
NGC torchのSemVer非準拠バージョン文字列）、修復レイヤ `whisperx_pyfix/`
（PYTHONPATH shadow + sitecustomize + 環境変数）に集約して修復。SIF本体は無改変。

**決定**: whisper_vad.py に `--engine {transformers,whisperx}` を追加（デフォルト
transformers、既定動作・出力契約は完全無変更。Sonnet実装+Opusレビュー）。ベンチ
（5分音声、GPU非競合）: 旧110秒（ロード56/転写21/話者分離15）vs 新347〜397秒
（転写168-178/整列46-48/話者分離102-106）。**話者分離品質は新が明確に優位**（話者数
正解、旧は30秒チャンク境界で同一人物を誤分割。句読点付きで誤認識も少）。遅さの真因は
ctranslate2のBlackwell(GB10)カーネル未対応 — 参考記事（note.com/nob75note）方式で
ct2 v4.8.1 をcompute_90 PTXソースビルド（8分で完了、whisperx_pyfixに組込）しても
転写168秒と改善せず、**PTX JITでは埋まらないアーキテクチャ最適化の差**と結論。
公式pipのaarch64ホイールはCUDA非対応（実測）である点も記録。

**影響**: 既定エンジンは transformers を維持（この構成ではGB10で最速）。whisperx は
品質重視の会議向けopt-in（`--engine whisperx` 手動指定）として利用可能。wrapper への
配線は ctranslate2 が Blackwell 対応した時点で再ベンチして判断（PLAN.md に保留構想）。
副産物2件: (1) reconcile のタイムアウト480秒化が argparse 側 default=180 の見落としで
効いていなかったのを修正（関数デフォルトとCLIデフォルトの二重管理に注意）。
(2) **vLLM v0.19.0 のスケジューラ停滞を発見** — エンジンがアイドル（Running:0、
KV 0%）なのに Waiting のリクエストを永遠にスケジュールしない状態。生成途中の
クライアント切断・kill の繰り返しが引き金の疑い。vLLM再起動で解消し、5回目の
議事録再生成で reconcile 含む全工程が初めて完走（本文1+決定3+アクション6件）。
恒久対策はvLLMのバージョンアップ推奨（停滞シグネチャの機械検出も可能、未実装）。

---

## 2026-07-06 LLM接続設定を secrets ファイル一元化 — べた書きデフォルト全廃、議事録生成の二重障害から

**背景**: Argus Console からの議事録生成が全LLMルート失敗で空議事録を保存（admin_job_58805315）。
診断: (1) localLLM.sh の定義（8001/DeepSeek）は正しく参照されていたが 8001 の vLLM が
未起動（起動中は gemma4@8000 のみ）、(2) RiVault フォールバックも DeepSeek-V4-Flash
モデルグループがサーバー側 500（litellm が `context_management` kwarg を hosted_vllm へ
透過。クライアントは送っておらず RiVault 側問題 — 報告文 docs/decisions/
rivault_deepseek_500_report.md）。棚卸しで `http://localhost:8000/v1` のべた書き
デフォルトが Python 11箇所+シェル5ファイルに散在し、「secrets 未設定時に黙って
意図しないエンドポイントへ接続する」構造問題を確認（過去のgemma4誤接続事故と同根）。

**決定**: `llm.py` に `load_llm_secrets()` を新設し、**LLM呼び出し直前に毎回**
~/.secrets/{localLLM.sh,rivault_tokens.sh} を bash source して環境変数へ反映
（ファイルが正、mtimeキャッシュ付き、ARGUS_SKIP_LLM_SECRETS=1 でテスト用バイパス）。
べた書きデフォルトは全廃し未設定は明示エラー（→ルートフォールバックが拾う）。
`_is_route_available("local")` が常に True を返すバグも修正。デーモン起動時の
環境変数に依存しなくなったため、**secrets 更新はデーモン再起動なしで即反映**される。
CLI --url/--token の明示上書きは「上書き後に再sourceを無効化」で保護（Opusレビュー指摘）。

**影響**: 実装Sonnet/レビューOpusの委譲体制で実施（モデル運用ポリシー初適用）。
pytest 118件パス。空議事録（misc.db instances + pm.db meetings 各1行）と汚染キャッシュ
combined.txt を削除、元mp4+VTTは data/processing/ に残置し再生成待ち。
**再生成の前提**: localLLM.sh と実起動サーバー（現状 gemma4@8000 のみ）の不整合解消が必要
（ユーザー対応: localLLM.sh 更新 or 8001 で DeepSeek 起動）。RiVault DeepSeek 500 は
管理者報告待ち。

---

## 2026-07-05 Argus 垂直軸の抜本見直し — クラスタ表示から所見検出へ（R1+R2）

**背景**: PMから「実行結果は決定事項のクラスタリングにとどまり、知見を引き出すのが難しい」
との指摘。設計書・実装・本番データを突き合わせて診断した結果、設計書§4が予言した
失敗モード（「荷重を持つ決定だけを取り込む。これを怠ると、痕跡が台帳に蓄積し、細粒度の
文言の羅列という問題を一階層上で再生産する」）に正確に該当していた。診断数値:
選別ゲート未実装で345決定ほぼ全件に辺付与、G-NS（最上位・抽象）に157本の貢献辺
（enrichプロンプトが全goalを候補に見せていた）、G-REPROに議事録取りまとめ等の事務決定が
混入、制約C-*が違反検査でなく貢献先として誤用、前提#5に68決定が依拠扱い。この辺ノイズを
前提集合キーの非収束検出が拾い、識別要件5件全てが常に非収束判定＝情報量ゼロだった。

**決定**: R1（辺の品質）= 選別ゲート（decisions.ledger_gate、3問判定）を enrich に追加し、
貢献先候補を識別要件5件+TS2件に限定（G-NS直接貢献の禁止）、依拠前提は反実仮想テスト、
制約は may_violate 辺（違反疑い）、論点は blocks 辺として判定。全345件を
`--ledger-regrade`（1件コミット・再開可能）で遡及再判定。R2（検出器）= 所見5種
（停滞/未着手・制約違反疑い・論点ブロック・トレードオフ衝突・前提健全性）に再定義し、
レポートを所見型に再構成。**投入量Δ（貢献辺数と重みランクの次元の合わない比較）と
前提集合キー非収束（LLM辺付けの揺らぎを測るだけ）は廃止**。「非収束」の定義を
トレードオフ衝突（Aが捨てた案をBが採用）に置き換えた。R3（argus-transcribeの決定捕捉、
設計書の言う最大レバレッジ）は会議運用の変更を伴うため見送り、PLAN.mdに構想として記録。

**影響**: サンプル検証で既知の誤辺（d:1256議事録→G-REPRO等）はtrace化で消え、正しい辺
（d:1505コンテナ固定→G-REPRO、d:1527→前提#2依拠）は維持された。C-*の検査句を
ledger_seed.json の identification_test に転記（enrichの違反スクリーニングが参照）。
レポートは巨大なクラスタ羅列から所見一覧に縮小され、分量問題も実質解消。

---

## 2026-07-04 方向Δレポートに有向グラフの静止画像を追加（PNG、Slack投稿）

**背景**: PMから「有向グラフを文字だけで表現するのもありだが、グラフィカルに可視化できないか」
との要望。出力先は既存の `narrate.py`（TTS音声mp3を `files_upload_v2` でチャンネルに
アップロードし、DMは"App"セクションに隔離され視認性が悪いため不採用、という既存判断）と
同じパターン・トレードオフを踏襲しSlack静止画像添付を選択。Web UI（Argus Console）への
対話型グラフは新規APIエンドポイント要で今回見送り。

**決定**: `render_direction_graph()` を新設し、既存の `named_clusters`/`delta`/
`unaddressed`/`divergent`（build_executive_summaryと同じデータ、識別要件5件スコープ）
をそのままPNGに描画。`networkx`+`matplotlib`は新規依存追加不要（aarch64 venvに導入済み）。
実装中に2点の落とし穴を発見・対処: (1) matplotlibデフォルト（DejaVu Sans）は日本語が
豆腐になるため `font_manager.addfont()` でNoto Sans CJK JPを明示登録、かつ
`nx.draw_networkx_labels()` の `font_family` デフォルト値"sans-serif"がrcParamsを
上書きするため個別に明示指定が必要だった。(2) `nx.multipartite_layout` は各tier内の
ノードを均等配置するだけで親子関係を無視し、識別要件5件がクラスタ21件と同じ幅に
均等割り当てされて密集・重複した。各目標をその子クラスタ群の重心の真上に置く
手動レイアウトに置き換えて解決。

**影響**: `build_direction_report()` の戻り値を `str` から `tuple[str, Path|None]` に
変更（呼び出し元はpm_argus.py内の2箇所のみ、同時更新）。画像生成失敗時は`None`を返し
テキストレポートのみの従来動作に縮退（コマンド全体は失敗させない）。テキスト本文は
実行者のみのephemeralだが、グラフ画像はSlack仕様上ephemeral化できずチャンネル全員に
見える（narrate.pyと同じ既知のトレードオフ）。本番pm.dbのスクラッチコピーで
CLI end-to-end検証済み（実LLM命名込み）。

---

## 2026-07-04 方向Δレポートにクラスタ要約・目標別提案アクションを追加、問いかけ限定の制約を拡張

**背景**: PMから、決定クラスタが生の決定羅列のままで俯瞰できない、Δ（欠落）だけでなく
「目標とクラスタの構造」自体をエグゼクティブサマリに写してほしい、目標ごとにPMが取るべき
アクションを提案してほしい、という3点の指摘。従来はLLMの裁量を「命名」と「問いかけ形式の
論点整理」（2026-07-03追加）に限定していた。

**決定**: `summarize_cluster_with_llm()`（旧`name_cluster_with_llm`）でクラスタ命名と同じ
LLM呼び出しに1〜2文要約を統合。`build_executive_summary()`を、識別要件5件それぞれに
「現状」（クラスタ構造の言い換え、新規解釈禁止）と「提案アクション」（〜してはどうか／
〜を検討、断定禁止）を出す目標別構成に再構成。是正判断は人が行うという原則自体は維持しつつ、
問いかけのみだった制約をPM指示で拡張した（PM明示指示による認められた逸脱、`direction.py`
モジュールdocstring参照）。

**影響**: 対象範囲は識別要件5件に限定（非収束検出と同じスコープ、制約/前提条件はノイズに
なるため対象外）。クラスタ命名・要約は既存と同じくクラスタあたり1回のLLM呼び出しに収め、
エグゼクティブサマリーとクラスタ一覧セクションで結果を使い回すことで二重呼び出しを回避。
本番pm.dbをコピーしたスクラッチ環境でCLI実行し出力を確認済み。

---

## 2026-07-03 BOX公開フォルダ40件をクロール、G-NS出所をより直接的な一次資料に更新、OCRのマルチモーダル誤送信事故

**背景**: 既存BOXフォルダ「FugakuNEXT_Ext_機密性1_公開（公開情報）」（キックオフ会議・発表資料・式典資料40件）にも
台帳に有用な情報がある可能性を検討。クロール・変換を実行した。

**決定**: 変換時、直前の`~/.secrets/localLLM.sh`（DeepSeek-V4-Flash、非マルチモーダル）を
sourceしたままだったため、PPTX変換のOCR呼び出し（`_convert_via_multimodal`）がローカル
エンドポイントに画像を送り400エラーで全滅する事故が発生（ユーザーが「DeepSeek-V4-Flashは
マルチモーダル非対応、正常に実行できているか」と指摘し発覚）。`get_ocr_endpoints()`は
`RIVAULT_URL`+`RIVAULT_OCR_MODEL`設定時に自動フォールバックする設計だったが、
`~/.secrets/rivault_tokens.sh`をsourceしていなかったため機能していなかった。
両方をsourceし直し、local（DeepSeek、失敗前提）→RIVAULT（Qwen3.6-35B、マルチモーダル）の
フォールバックで再実行。ユーザーから「localのモデル定義を勝手に変えるな」と明確な指摘を受け、
gemma4への切替ではなくRIVAULTフォールバックの活性化が正しい対応と修正した。

**影響**: 40件全て実質的な内容を確保（pdftotext 19件、multimodal_ocr 21件）。副次的に
`_convert_via_multimodal`の「全ページOCR失敗時も非Noneを返しlibreofficeへフォールバック
しない」バグを発見・修正。RIVAULT側の413エラー（画像サイズ超過）・レスポンス解析エラーで
一部ページ（合計46ページ）のみ欠落したが文書単位の全損は無し（未修正の残課題）。
`20250822_富岳NEXT開発体制始動記念式典_松岡先生プレゼン資料final.pptx`（松岡聡センター長
本人によるプロジェクト発足式典でのプレゼン）に「AI for Scienceによる科学の推進」
「情報技術における主権の確保」を発見、G-NSの出所を前回引用したHPCI委員会資料から
こちらのより直接的な一次資料に更新した。G-REPROの出所は44文書中に該当なく未確認のまま。

---

## 2026-07-03 G-NSの「松岡指令」出所をBOX新規資料で確認（台帳の最後の未確認出所を解消）

**背景**: 直前のエントリの時点で、G-NS（最上位目標）の出所のうち「松岡センター長 AI4S
最上位目標 指令」は一次情報が未確認のまま残っていた。ユーザーがMEXT・松岡センター長・
富岳NEXTリーダーによる公開情報をBOX新規フォルダ「プロジェクト方針」に追加、
`box_sources.yaml`にも登録した。

**決定**: `pm_box_crawl.py`でクロール・変換（4件、下記バグ修正後に成功）した内容を読み、
2025-08-22付 R-CCS HPCI計画推進委員会資料（松岡聡センター長・近藤正章部門長）に
「AI for Scienceによる科学の推進」「日本の主権の確保」の記載を発見、G-NSの一次情報として
`ledger_seed.json`に反映し`--ledger-force`で本番投入した。同資料群からG-PHYS（PINNs
利用）・G-COUPLE（双方向データフロー連携）・C-ECOSYS（OSS通信ライブラリ）の補強根拠も
得た。G-REPRO（再現性の独立識別軸としての一次根拠）は4文書中に該当箇所が見つからず、
未確認のまま残る（出所主義：無理に埋めない）。

**影響**: 台帳10 goalsのうちG-REPROの一部を除き出所が一次資料で確定。BOX資料は
`pm_box_relevance.py --judge`で4件とも`core`判定、`pm_embed.py`でqa_index.dbに索引化し
`/argus-investigate`からも検索可能にした（「プロジェクトの方向性を示す資料」としては
台帳への手動反映が主経路、検索索引化は補助的な経路という位置づけ）。

---

## 2026-07-03 pm_box_crawl.py の暗号化PDF誤判定を修正（政府系公開PDF全般に影響の可能性）

**背景**: ユーザーがMEXT公開資料等をBOX新規フォルダに追加し取り込みを試みたところ、
4件全PDFが「暗号化されており抽出不可」としてスキップされた。しかし`pdftotext`で
直接試すと問題なく本文が抽出できた。

**決定**: `_is_encrypted_pdf()`はPDF trailerの`/Encrypt`有無のみで判定していたが、
これは「コピー・印刷禁止」等の権限制限のみ（オープンパスワード無し）のPDFでも真になる。
政府公開PDFにはこの種の権限制限が多く、実際は空パスワードで正常に開ける。
`convert_to_markdown()`を、事前ブロックではなく実際に`_pdftotext`等で抽出を試みた後、
本文が空だった場合のみ「暗号化」と判定する順序に変更した。

**影響**: 該当4件は`pdftotext`で14,903〜37,976文字を正常抽出できるようになった。
このバグは今回の4件に限らず、`box_sources.yaml`配下の他の政府系公開PDF全般で
同様に誤スキップが発生していた可能性がある（再クロール時に自然に解消される）。

---

## 2026-07-03 Argus 垂直軸 台帳の出所（source）を一次資料で確定

**背景**: Phase 1投入時、設計書§8が参照する別添JSON（`data/FugakuNEXT_Argus_designsheet.json`）が
リポジトリに見つからず、`data/ledger_seed.json`は設計書の表から手動再構成したものだった。
G-NS/G-REPRO/C-SOVEREIGN/C-ECOSYS等の出所（source）は「要・出所確定」のまま
`source_status='needs_source'`で先行投入し、判明次第更新する方針にしていた。ユーザーが
別添JSON本体を発見し提示。

**決定**: 別添JSONの一次資料引用（MEXT事業背景文書・計算科学ロードマップ・アプリケーション
セミナー総括等）を`ledger_seed.json`の全10 goalsの`source`に反映し`source_status`を
`needs_source`→`confirmed`に更新。ただしG-NSの「松岡指令」とG-REPROの「設計セッション
一次根拠」の2件は、別添JSON自体が「要・出所確定（一次情報の参照を要批准）」と明記しており
今なお未確認のため、その旨をsource文中に残した（出所主義：無い確証を作らない）。Q-FP64の
責任者・期限も別添JSONで「要割当」「要設定」と明記されており引き続き未確定のまま。重み・
5本のcontributesエッジは再構成版と完全一致し差異なし（再構成が正確だったことの裏付け）。
6本目の「ブロック」エッジ（Q-FP64→精度アーキテクチャ決定群）は対象の決定群がまだ台帳に
存在しないため未投入のまま。

**影響**: `pm_ingest.py ledger --ledger-force`で本番pm.dbの10 goals全件の`source_status`が
`confirmed`になったことを確認。Phase 1完了時点で残っていた唯一の既知ギャップが解消された。

---

## 2026-07-03 Argus 垂直軸 Phase 3 サマリー根拠の追加（トレーサビリティ）

**背景**: エグゼクティブサマリー導入後、Slack実行で改善版が反映されず旧コードのまま
出力される事故が発生（`pm_qa_server.py`デーモンが`direction.py`修正前に起動されており、
`from argus.direction import ...`が古いモジュールをプロセス内にキャッシュしていたため。
デーモン再起動で解消、以後コード変更時は再起動が必要）。再起動後の新レポートに対し、
PMから「サマリーが『G-UQは投入2件』と言っているが、具体的にどのdecisionを指すのか
近くに示してほしい」との指摘。

**決定**: `compute_direction_delta()`が集計時に`decision_ids`を保持するよう変更。
エグゼクティブサマリー本文の直後に`_format_summary_evidence()`（LLM不使用、SQL集計
から機械的に算出）でサマリーが言及した目標ごとのdecision_id一覧を追加。非収束の
表示も「方向1［d:x, d:y］/ 方向2［d:z］」の形でクラスタ単位のID一覧を明示するよう変更。
根拠はLLMの要約文とは独立に算出するため、要約が不正確でも根拠側は常に正しいIDを示す。

**影響**: 本番全件データで再検証。「G-UQは投入不足かつ非収束」という要約の直後に
「G-UQ — 投入不足: d:1828, d:1897」「G-UQ — 非収束: 方向1［d:1828］/ 方向2［d:1897］」
が機械的に表示され、要約の主張とその根拠が1画面内で追える状態になった。

---

## 2026-07-03 Argus 垂直軸 Phase 3 解釈性改善（エグゼクティブサマリー・非収束の対象限定）

**背景**: 本番全件データで`/argus-direction`をSlack実行したところ、PMから「決定事項の
羅列だけでΔの解釈ができない」との指摘。原因を調べると、非収束検出が重み未承認の
目標（最上位目標G-NS・制約C-*・前提条件TS-*）にも無差別にかかっており、傘概念の
G-NSだけで70行超のtrade_off羅列というノイズを生んでいた。また `G-*`/`C-*`/`TS-*` の
プレフィックスだけでは種別が分からず読みにくいとの指摘も受けた。

**決定**: `ledger_goals.layer`（top/identifying/constraint/tablestakes）を使い
全ての目標参照に種別ラベルを付与（`_goal_label()`）。非収束検出は
「重み承認済みの識別要件（layer='identifying' AND weight IS NOT NULL）」のみに
限定（G-NS等5件を除外、対象10件→5件に削減）。クラスタ命名プロンプトを
「話題」でなく「選んだ方向性」が伝わる表現に変更。加えて、PMの明示指示により
設計書の「LLMの裁量は命名のみ」という制約を一部緩和し、機械的所見から
「〜を確認してはどうか」という問いかけ形式のエグゼクティブサマリーを
LLMに生成させレポート冒頭に追加（`build_executive_summary()`）。断定を禁止し
是正判断はPMが行うという原則自体は維持。

**影響**: 本番全件データで再検証し、非収束セクションがG-REPRO(5)/G-COUPLE(10)/
G-PHYS(2)/G-UQ(2)/G-INV(2)の5件（識別要件のみ）に整理され、冒頭の3行程度の
サマリーだけで「どこを見るべきか」が把握できるようになった。レポートの総分量
（Slackメッセージ量）自体の問題は別課題として保留（PLAN.md参照）。

---

## 2026-07-03 Argus 垂直軸 Phase 3 完了（機能2: 決定クラスタ集約・方向Δ、過去分遡及enrich含む）

**背景**: Phase 3（`direction.py`/`/argus-direction`）はスクラッチ検証のみで、本番pm.dbには
decision起点の`contributes`/`depends_on`辺が1件も無く「集約対象なし」を返す状態だった。過去
332件のdecisionsのうち312件がPhase1/2実装前の取り込みで、自動エンリッチの対象外だった。

**決定**: `enrich_items.py --id d:...`を20件ずつ16チャンクに分割し遡及実行（1プロセス
一括だとcommitが全件処理後の一度きりで、失敗時に全損するため分割）。実行中、環境変数
`LOCAL_LLM_URL`未設定によりgemma4（意図しないモデル）が呼ばれる事故が発生、
`~/.secrets/localLLM.sh`をsourceしDeepSeek-V4-Flashに切替えて再実行。

**影響**: `contributes`辺296件・`depends_on`辺109件を生成（332件中252件に辺付与、
残り80件は「明確な関連なし」とLLMが判断した正当な空）。`/argus-direction --dry-run`を
本番全件で実行し、G-NS等で複数クラスタ併存（非収束）・投入不足領域を実データで検出、
クラスタ命名も妥当に機能することを確認。新規議事録は`pm_ingest.py`のPass2自動エンリッチで
以後追加操作不要と実証済み（8件のAreaLeaderTechnical新規取込みで確認）。

---

## 2026-07-03 Argus 垂直軸 Phase 2 完了（機能1: 外部シグナル検出の着地処理3作用）

**背景**: Phase 2 第一実装（検出器8・LLM判定・monitor_target実データ投入）は完了していたが、
設計書§5の着地処理3作用のうち「既存決定への警告」だけは `decisions →depends_on→ 前提` 辺の
生成経路が無く未発火だった。

**決定**: `enrich_items.py::enrich_decision()` に `contributes_to_goals` と同型のパターンで
`depends_on_assumptions` を追加（`ledger_assumptions` 一覧をプロンプトに提示しLLMに選ばせ、
実在ID検証後 `ledger_edges` へ `depends_on` 辺をUPSERT）。実LLM呼び出しで検証中、
`_split_monitor_terms()` の重大なバグを発見: 区切り文字（, 、 ・ / 空白）分割では
日本語の自由文（「KDDIによるGB200 NVL72サービスの正式な提供開始時期」等、単語間に
空白が無い）がほぼ分割されず、記事本文と一切マッチしなかった。英数字固有名詞の正規表現抽出 +
`retrieval.sudachi_tokenize_query()`（既存FTS5検索と同じSudachiPy形態素解析）の併用に修正。

**影響**: 修正後、実LLM呼び出しで「前提を否定する記事の検出→depends_on辺を辿って
依拠する決定への警告表示」まで一気通貫で動作確認済み。設計書§5「共通の着地処理」の
3作用（確信度更新・既存決定への警告・監視継続）が全て実発火する状態になった。

---

## 2026-07-03 Argus 垂直軸 Phase 1 完了（前提・意思決定台帳、本番投入まで）

**背景**: 2026-07-01に設計書（v0.1・要批准）を読解し、台帳スキーマ
（ledger_goals/assumptions/issues/edges）・シード・流入拡張（rationale/trade_off/
reversal_condition のブラケットタグ）を実装。本番投入は重み・出所が「要批准」のため保留していた。

**決定**: end-to-end動作確認は議事録一括再生成バッチ（65件）とSlack抽出の実運用で完了
（meeting経由decisions 165件中rationale99%、slack経由145件中98%）。goals/issues/edges の
本番投入は、識別要件5件の重み（高/高/高/中/中）をPM承認により確定（provisional→ratified）、
一方でG-NS/G-REPRO等の一次出所とQ-FP64の責任者・期限は情報が無いため
`needs_source`/未定のまま先行投入する判断とした（出所主義：無い情報は無いまま記録し、
判明次第 `--ledger-force` で更新する）。`ledger_assumptions` は別途「LLM提案→人承認」の
新機構（`--ledger-suggest-assumptions`）で5件を承認・投入。

**影響**: 本番pm.dbに台帳（goals 10・issues 1・assumptions 5・edges 5）が揃い、
Patrol検出器8（機能1・外部シグナル検出）が実際に監視対象を持つ状態になった。
Phase 2の残課題（depends_on辺の生成経路）とPhase 3（機能2）は引き続きPLAN.md参照。

---

## 2026-07-03 議事録一括再生成65件の pm.db 転記漏れ6件を発見・修復、重複判定を恒久修正

**背景**: バッチ完了後、65件全てが本当に pm.db に反映されたか確認するため、
各会議の `meetings.parsed_at` を今回バッチ実行日時（2026-07-02/03）と突き合わせ検証した。

**発見**: 6件（2026-05-07/05-26/06-05/06-09/06-11/06-19）が新しい高品質な議事録
（`data/minutes/{kind}.db`）を持ちながら pm.db には**古い旧エントリのまま**だった。
原因は `scripts/ingest/minutes.py::transfer_meeting()` の重複判定が `meeting_id`
ではなく `(held_at, kind)` で行われるため、`--force` なしでは「同じ日付の会議は
既にある」と判定してスキップし、しかもスキップは正常終了扱いでログ上は
「保存し削除しました」と成功のように見えていたこと。**旧エントリは空ではなく、
5/6件が実質的な決定事項・アクションアイテム（各3-4件・5-7件）を保持していた**
（当初「空の重複」と誤認して報告したため訂正）。

**決定**: 個別6件を `--minutes-meeting-id <新ID> --minutes-force` で再転記した後、
ユーザー確認の上で旧エントリ（decisions 16件・action_items 29件・meetings 6件）を
削除（related_ids からの軟参照が数件ダングリングになるが実害は軽微と判断）。
恒久対策として `transfer_meeting()` の重複判定を `meeting_id` 基準に変更し、
同一 `(held_at, kind)` の別 `meeting_id` が残る場合は内容が空なら自動削除・
内容があれば `[WARN]` ログのみ（実データの誤自動削除を避ける）とした。

**影響**: 65/65件で pm.db 転記完了（decisions/action_items とも非ゼロ、重複なし）を
スクラッチ環境の3ケース（空の旧レコード自動削除／内容ありの旧レコードは警告のみ・
新規転記／同一IDの再転記はスキップ）で検証済み。`docs/commands.md` に
`--minutes-meeting-id` の記載漏れも合わせて追記。

---

## 2026-07-03 pm_minutes_catalog.py の無言終了を調査、未反映分を解消

**背景**: 議事録一括再生成バッチ（65件）中、Canvas目録更新ステップが
トレースバック・警告一切なしで異常終了する事象が発生（既知の `canvas_editing_locked`
→`sys.exit(1)` とは別に、出力ゼロで落ちるケース）。バッチ完了後に調査。

**調査**: `dmesg`/`syslog`/`journalctl` は権限不足（`adm`/`systemd-journal` 未所属、sudo不可）で
直接確認できず。手動再現では `canvas_editing_locked` は頻発するもののリトライで毎回回復し
(メモリ使用量も30MB程度と僅少)、正常系では警告ログが必ず出力されることを確認。実際の障害時は
警告すら一切出ていなかったことから、**SIGKILL（stdout非フラッシュのまま強制終了、python の
パイプ時ブロックバッファリングにより緩衝済みログも消失）の可能性が高いと判断**。GB10 Unified
Memory 上で vLLM(gemma4, 常駐53GB) と Whisper が同居しており OOM killer が有力な原因候補だが、
カーネルログ非開示のため確証には至らず。

**決定**: 原因特定は権限的limitationで打ち切り、実害（Box/Canvas未反映）の解消を優先。
`pm_minutes_catalog.py --upload --catalog`（全会議種別）を再実行し未アップロード分・
目録未更新分を解消。`pm_minutes_publish.py --xlsx-only` は Box 側で別プロセスによる
直近更新と衝突検知し2回ともスキップ（安全機構が正常動作、実害なし）。

**影響**: Box議事録・Canvas目録は最新化済み。根本原因（OOM疑い）は未解決のまま観察継続。
再発時は sudo権限を持つ管理者に `dmesg -T | grep -i "killed process"` の確認を依頼するのが
次の一手。

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
