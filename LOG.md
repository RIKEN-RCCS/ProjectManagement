# 変更ログ (LOG.md)

判断の経緯・破棄された案・方針転換を残す journal。運用ルールは `CLAUDE.md` を参照。

**フォーマット**: `## YYYY-MM-DD <一行サマリ>` → 本文に **背景 / 決定 / 影響** を 1-2 行ずつ。
新しいエントリを上に追加する。1 エントリ 3-5 行を目安に、長文は `docs/` 側に逃がす。

---

## 2026-08-03 アンカーの push を運用に載せた — 専用ブランチと plumbing で main を守る

**背景**: 外部アンカーは**ファイルがマシンの外へ出て初めて意味を持つ**（同じ FS 上にある限り、
台帳を書き換えられる攻撃者はアンカーも書き換えられる）。PM 判断で commit + push を運用に載せた。
**踏みかけた落とし穴**: `main` に**未 push のコミットが 26 件**あった。cron から素朴に
`git push` すると**開発者の意図しないコミットまで public リポジトリへ出る**。
**決定**: 専用ブランチ `anchors` を作り、**git の plumbing のみ**
（`hash-object` → `mktree` → `commit-tree` → `update-ref` → `push origin anchors:anchors`）で載せる。
`git add` / `commit` / `checkout` を使わないので**作業ツリーもインデックスも汚さない** —
共有の作業ツリーで cron が走る以上、ここは譲れない。アンカーファイルは `main` の追跡から外した
（日次コミットが開発者の作業と衝突するため）。
**push されたかを検査する**（`anchor_pushed`）。ローカルにだけあるアンカーはアンカーではないので、
「実装したが効いていない」を防ぐには push の成否まで機械的に見る必要がある。
**証明範囲**: push 済みのアンカーより後に、それ以前の記録が書き換えられていないこと**だけ**。
**記録された内容が真実かどうかには何も言わない**（嘘を正直に記録した場合は通る）。
公開されるのは `ts` / `rows` / `entry_hash` のみだが `rows` は活動量を露出する。
`git push` は subprocess なので `net_guard` の対象外である。

## 2026-08-03 Phase 3 と 5b を実装 — 5b を止めていた理由が誤りだった

**背景**: 流出側の残り（層3・承認フロー・外部アンカー・能力分離 5b）をまとめて実装した。
**5b について訂正**: 「調べながら中間結果を投稿する使い方が消えるので倒せない」と説明していたが
**誤り**。①メンション経路には Bolt の `respond` が無い ②`run_agent` は
`_make_progress_updater(None)` と**受け取った `respond` を握りつぶしていた**（2026-05-26 以来）。
つまり失われる進捗更新は最初から存在しなかった。**型: コードにある引数を、それが
使われている証拠として扱った。** さらに `run_in_read_plane` の呼び出しは**メンション経路
1 箇所だけ**で、`/argus-investigate` は常に in-process だった（被覆も想定より狭かった）。
握りつぶしを直し、親子の進捗中継（子は stdout に JSON、**トークンを持つ親が投稿**）を入れ、
スラッシュコマンド経路にも通して既定 ON にした。`--file` 経路は未分離。
**層3 は warn で入れた** — ephemeral はコマンド実行チャンネルへ返るので正当な宛先集合を
いま確定できない。**観測してから enforce を判断する**（net_guard・model_pin と同じ段階導入）。
**承認フローは Slack のボタンではなく CLI にした** — 投稿先の決めが要らないため。
**外部アンカーの限界を明記**: ファイルがマシンの外へ出て初めて意味を持つ。同じ FS 上にある限り
台帳を書き換えられる攻撃者はアンカーも書き換えられる。**push は自動化していない**ので、
証明できるのは「push 済みのアンカーより後に過去分が書き換えられていないこと」に限る。

## 2026-08-03 モデル pin を実態へ合わせて enforce — 宣言と本番構成が逆だった

**背景**: pin の enforce 化にあたり「実際にどのモデルへ解決するか」を確かめたところ、
**宣言と実態が逆**だった。`RIVAULT_MODEL=deepseek-ai/DeepSeek-V4-Flash` が本番デーモンに
設定されているのに、pin では `production: false`（評価専用の judge）。`_try_rivault` は
`model` 引数を渡さないためこれが使われる。**そのまま enforce にすれば RiVault 経路が全滅**
していた。第2系統（Llama-4-Scout / gemma3）も宣言が無く、同じく全滅するところだった
（`sensitive_terms.yaml` には「model_pin.yaml の宣言と一致していること」と書いてあった）。
**決定**: PM 回答（RiVault 本番は DeepSeek。Kimi-K2-Thinking は thinking のログが滲み出て退役）
に基づき pin を実態へ合わせ、本番 6 モデルが enforce で通ることを実測してから切り替えた。
**踏んだ誤り**: 一度「Kimi 退役により R8 の集中リスクは解消」と書いたが**誤り**。R8 は
個々のモデルの性質ではなく**依存が一系統に寄っていること**であり、退役後の本番構成
（Zhipu / DeepSeek / Alibaba）は依然すべて同系統。**集中リスクは解消ではなく移動した。**
**副次的な発見**: OCR 経路（`_ocr_image`）が pin を素通りしていた（`/chat/completions` を
直接組み立てる唯一の本番経路）。また DeepSeek を「中立ジャッジ」として選んだ評価の前提は、
本番モデルになったことで**今後の評価には当てはまらない**（過去の評価は当時候補外なので有効）。

## 2026-08-03 net_guard の enforce を cron へ展開 — 忘却と意図的除外を区別させる

**背景**: enforce が掛かっていたのはデーモンだけで、crontab の 5 本は warn のままだった。
前日の観測修正で全経路の宛先が記録されるようになり、実測 **1445 件すべて allow / deny 0**
（宛先 6 種、いずれも allow-list 内）を確認できたので展開した。
**決定**: 5 本のラッパーに `${ARGUS_NETGUARD:-enforce}` を入れ、**契約テストを同時に入れた** —
python3 を起動する `scripts/bin/*.sh` は enforce を設定するか、**理由付きで除外リストに
載っている**かのどちらかであること。**新しいラッパーを足したときの書き忘れは、これまで
何度も踏んだ型**（呼び出し側の作法に依存した制御が 1 箇所の忘れで無効化される）なので、
除外に理由を書かせることで忘却と意図的な除外を区別する。
**覆えていないものは明記した**: `box` CLI（別プロセス）／手動実行の録音・TTS 系（観測なし）
／Patrol（cron から意図的に除外）。**「全経路が覆われた」と書かないこと。**

## 2026-08-02 監視自身が P8 を踏んでいた — 「warn で 1 周」が cron の 2 本で観測不能だった

**背景**: cron 経路を enforce にする前段として warn 期間のログを確認したところ、
`pm_argus_daily` と `canvas_report` の **NETGUARD 記録が 0 行**だった。net_guard は入っており
遮断は効くが、`logger.info` の記録が出るかは**エントリスクリプトが `logging.basicConfig()` を
呼んでいるか**次第で、この 2 経路のスクリプト（`pm_argus.py` / `pm_xlsx_*.py`）は呼んでいなかった
（ルートロガーの既定 WARNING で INFO が捨てられる）。
**決定**: エントリスクリプト 5 本に `basicConfig` を足すのではなく、**`net_guard` 自身が
`install()` 時に自分の記録の可視性を保証する**形にした（他にハンドラが無く INFO が捨てられる
状態のときだけ StreamHandler を付け、`propagate=False`）。実測 0 件 → 4 件。
**理由**: 5 本に足す案は「新しいエントリを足すたびに書き忘れる」構造を残す。**同じ型
（呼び出し側の作法に依存した制御が 1 箇所の書き忘れで静かに無効化される）を今日すでに
複数回踏んでいる。**
**影響**: **遮断が効いていても、観測できなければ enforce へ進む判断ができない。**
これで初めて全経路で「warn で 1 周」が成立する。平日 1 周が enforce の前提。
あわせて **`net_guard` が subprocess を覆えない**ことを明記した（`box` CLI は Node の別プロセスで、
Box への通信は 1 バイトも通っていない）。危険度は低いが、被覆範囲の誤読を防ぐため（P10）。

## 2026-08-02 top_k の被害半径を実測 — 運用値 50 が環境変数任せだったことも判明

**背景**: P7（文脈軸）の「投入チャンク数＝汚染文書がモデルの文脈に入る機会」を数字にした
（`recall_eval.py` に `exposure` サブコマンドを追加。ゴールド 17 問 × k=50/100/200）。
**結果**: 1 クエリの露出は k=50 で 24 文書 / k=100 で 40 / k=200 で 62。17 問の union 到達率は
コーパス 3,498 文書に対し 7.1% / 10.1% / 14.6%。k を 200→50 に絞ると被害半径は約半分になるが、
k=200 で拾えていた正解チャンクの 19% を落とす（k=100 なら 9.5%）。
**副産物の発見**: 運用値 50 は 2026-07-30 に確定していたのに、**コードの既定は 200 のままで、
実際の 50 は qa デーモンを起動したシェルの環境変数にしか存在しなかった**
（`~/.secrets/` にも `pm_daemon.sh` にも `.bashrc` にも無い）。**別シェルから再起動すると
黙って 200 に戻り、被害半径が倍以上に広がる**。既定値を 50 に変更した。
**決定の型**: 「確定した運用値」が環境変数にしか無い状態は、確定していないのと同じ。
**既定値は、環境が欠けたときに倒れてほしい側に置く。**
**証明していないこと**: 汚染文書が実際に上位 k 件に入るかは、その文書の埋め込み類似度次第で
この測定からは分からない。到達率は「拾われうる上限」の目安にすぎない。

## 2026-08-02 「実装済み」と「効いている」は別だった — 制御 4 件が沈黙していた

**背景**: 残作業を進める中で、実装もテストもドキュメントも揃っている制御が、**production では
一度も動いていない**ケースを 4 件見つけた。①Slack 出力ファネルの canary 検査（`_guard()` が
`conn` 未指定時に検査と記録を丸ごとスキップする実装で、呼び出し 25 箇所すべてが未指定）
②第2系統トリアージ（production の呼び出し元が無い。退役した `two_stage` 形式向けに作られていた）
③監査台帳の append-only（後述の別エントリ）④net_guard enforce（掛かっているのはデーモンのみ、
cron 5 本は warn のまま）。
**共通のシグナル**は「動いている証拠が 1 件も無い」ことだけだった。テストも lint も通っていた。
**決定**: `pm_selfcheck` に `silent_control` を追加し、`tool_calls` に期待される種類の記録が
観測期間内に 0 件なら報告する。**ただし沈黙を違反にしない**（運用を止めていれば沈黙は正常。
既定は警告のみ、`--silence-strict` で違反扱い）。台帳が観測期間より新しければ「判定不能」と明示する
— 運用開始直後に必ず誤警報になる設計は、監視を無効化する方向に圧力をかけるため。
**影響**: ①はファネル側が自分で pm.db を開く形（canvas_utils / box_cli と同じ）に直した。
呼び出し 25 箇所に `conn` を渡させる形は同じ間違いを再発させるので採らなかった。

## 2026-08-02 監査台帳の追記専用性は「事故」で成立していた

**背景**: 監査記録が呼び出し側の未コミット作業を勝手に確定させる問題（`ensure_tool_calls_table` の
`executescript()` が保留中トランザクションを暗黙 COMMIT する）を直したところ、append-only の
テストが落ちた。調べると **`open_db()` の `schema.split(";")` がトリガ本体の `;` で分割を壊し、
`tool_calls_no_update` / `no_delete` を静かに作り損ねていた**。本番 pm.db にトリガがあるのは、
`ensure_tool_calls_table` の無条件 `executescript()` が**毎回それを偶然治療していた**ため。
**決定**: `open_db()` を `executescript` に一本化（根本）。加えて `ensure_tool_calls_table` は
テーブルだけでなくトリガの存在も見て、欠落していれば**警告つきで**再作成する。静かに直すと
「壊れていたことに誰も気づかない」状態が再発するため。
**影響**: 事故に支えられた保証は、事故が消えた瞬間に静かに消える。**副作用に依存していた不変条件が
他にも無いか**は未確認（`executescript` を使う DDL は 4 箇所すべてに存在確認ガードを入れた）。

## 2026-08-02 第2系統を「生存項目の再審査」から「欠落の検出」へ作り直した

**背景**: `apply_second_opinion` は主系統が KEEP した項目に第2系統を当てる設計だったが、
トリアージが `integrated`（抽出プロンプトに統合）へ移行して呼び出し元が消えていた。さらに
integrated では **DROP された項目は出力に現れない**ため、生存項目をいくら再審査しても
R8 が恐れる欠落（omission）は**原理的に見えない**。
**決定**: 同じ生入力を第2系統に独立抽出させ、主系統の出力に**無い**項目を探す形にした
（Slack Pass 1 抽出）。Box relevance は `noise` 判定のみを対象にした — **索引から落とす判定だけが
欠落を作る**（core/related の誤りは検索すればいずれ見つかるが、noise は二度と出てこない）。
**影響**: 主系統は上書きしない方針は維持（能力差による誤りが混ざるため、フラグを立てるに留める）。
退役形式向けの `apply_second_opinion` は削除せず docstring に「production からは呼ばれない」と明記した。

## 2026-08-02 FTS5 予約文字の列挙をやめた — 同じ型を 2 度踏んだため

**背景**: top_k の被害半径を測る過程で、`/` を含む語を含むクエリが
`fts5: syntax error near "/"` で落ち、**全文検索段が丸ごと不成立**になっていることが判明した
（ゴールド 17 問中 2 問）。呼び出し側が `OperationalError` を「ヒットなし」に丸めるため
**静かに縮退していた**。原因は `_fts5_escape_token` の予約文字がブラックリスト方式だったこと。
**2026-07-30 にハイフンで全く同じ事象を踏んでいた**（コメントにその経緯が残っていた）。
**決定**: bareword として安全な形（`\w+`）でなければ**無条件に引用符化**するホワイトリスト方式へ。
余分な引用符は無害（1 トークンのフレーズ = bareword）で、列挙の取りこぼしで検索が静かに壊れるより
はるかに安全。あわせて握りつぶし時に必ず WARNING を出すようにした。
**影響**: 列挙で守る設計は、列挙し忘れが出るたびに再発する。**取りこぼしの代償が非対称なら
安全側の広い規則を採る**。

## 2026-08-02 本番 tool_calls の 78 行はテスト由来 — 証拠として数えないこと

**背景**: Slack ファネルが自分で pm.db を開くようにした結果、**テストがファネルを呼ぶと本番の
監査台帳に行が入る**ようになった。実測で本番 `tool_calls` の全 78 行（2026-08-01T14:27〜15:50 UTC、
`args_json` の channel が `C0XXXXXXX`）がテスト由来と判明。本物の記録は 0 件だった。
**この汚染のせいで `silent_control` が `egress_slack` を「健全」と誤判定していた** — 制御の沈黙を
検出するために作った検査が、その主目的のキーでテストデータに騙された。
**決定**: 経路は塞いだ（`tests/conftest.py` の autouse フィクスチャ + `PYTEST_CURRENT_TEST` 下の
fail-closed ガード）。**汚染した 78 行は削除しない** — 追記専用の台帳から行を消して見た目を整えるのは、
その台帳が守ろうとしている性質そのものを壊す行為だから。全行はスクラッチへ退避済み。
**影響**: **この期間の `egress_slack` の非沈黙は証拠にならない。** 台帳が本物の記録で満たされるのは
2026-08-02 以降の運転から。

## 2026-08-01 qa_index.db を暗号化へ移行 — migrate_db が FTS5 を扱えないことも判明

**背景**: canary の生存確認を実装した際に平文と判明した（同日の別エントリ）。
`chunks.content` に議事録・Slack・Box の本文が入る 321MB の索引が、元 DB は暗号化されているのに
平文という**保護レベルの逆転**だった。
**踏んだ問題 3 件**:
①**`migrate_db` が FTS5 を移行できなかった** — テーブル単位の CREATE+INSERT では、
仮想テーブルの CREATE が影テーブル（`fts_data` 等）を自動生成するため後続の INSERT が衝突する。
**`sqlcipher_export()` 方式に書き換えた**（SQLCipher 自身の複製機能で仮想テーブルも忠実に写る）。
②**`except sqlite3.OperationalError` が暗号化 DB で効かない** — `pm_embed` の DDL 冪等化が
素通りして落ちた。**同じ型を今日 2 度踏んだ**ので `db_utils.operational_errors()` に共通化した。
③**`conn.row_factory = sqlite3.Row` の上書きで TypeError** — sqlcipher3 のカーソルと型が合わない。
`open_db` が既に設定しているので上書き自体が不要だった。
**設計判断**: 接続を `encrypt=True` 固定にせず `open_maybe_encrypted`（暗号化優先・平文なら
WARNING）にした。テストの一時 DB は平文で作られるため固定すると 34 件落ちる。**平文を黙って
受け入れないよう警告は必ず出す** — 今回まさに「平文のまま運用されていたのに気づかなかった」
のが問題だったので、次は気づけるようにした。
**性能**: FTS5 2.4→4.8ms、embedding 読み 23.5→33.9ms。investigate は秒単位なので許容範囲。

## 2026-08-01 残作業を一括で進めた — 承認フローと外部アンカーの cron 化だけ残した

**実施**: Canvas / Box のファネルへの検査追加、canary の植え付けと生存確認、
`tool_call_anchor()`（外部アンカーの材料）、Read Plane 切替フラグ（既定 OFF）、
qa / web デーモンの再起動（ファネルと enforce の実効化）。
**判断して止めた 2 件**: ①**承認フローの受け皿** — 「誰がどこで承認するか」は UX の決めで、
私が決めるものではない。`EgressPendingApproval` を投げるところまでに留めた
②**外部アンカーの cron 化** — 投稿先チャンネルの決めが要る。材料（連鎖の頭を取り出す関数）
だけ用意した。
**触らなかったもの**: `~/.secrets/` 配下（cron への enforce 展開）。**読まない約束のファイルを
書き換えるのは筋が悪い**。cron 側の enforce は PM 作業として残す。
**再起動の確認**: qa は Socket Mode 接続まで到達し、`verdict=allow` のみ。起動時の
「vLLM モデル自動検出に失敗（401）」は**今日の変更とは無関係の既存警告**（7/31 以前から
49 回記録あり）で、RIKYU の `/v1/models` にトークンの種類が合っていない。実害は
モデル名の自動検出のみで、`model_pin --check` で見つけたのと同じ型。

## 2026-08-01 canary を実際に植え、生存確認を実装 — その過程で qa_index.db が平文と判明

**背景**: 出力側の検知点（ブローカー／`tool_calls`／思考トレース）が揃ったので、保留していた
canary の植え付けを実施できる状態になった。
**実施**: `docs-d1f66cf0.internal-check.invalid` を発行し、`box_docs.db` に自己検査用の
ダミー文書として植えた（`relevance='related'` を明示して LLM 判定の対象外にし、
`box_file_id` は Box に実在しない値にしてクロールの自動削除を避けた）。`pm_embed` で
索引化し、qa_index に 1 チャンクとして入ったことを確認。**本文は自己説明的にした** —
investigate の回答に引用されても人が誤解しないように。
**生存確認を実装**（`pm_selfcheck` の `canary_alive`）。**発火検知だけでは餌が腐っても
気づけない**ため対で要る。
**その過程で `qa_index.db` が平文だと判明した。** 生存確認が「判定不能」を返したので調べたら、
`encrypt=True` で開けなかった。`docs/architecture.md` の DB 表は「✅ 暗号化」と書いていたが誤り。
**`chunks.content` には議事録・Slack・Box の本文がそのまま入る**（29,313 チャンク）。
`data/processing/` の会議録音 mp4 が平文だったのと**同じ型** — 元データを暗号化しても、
**そこから作った派生物の保護レベルが下がっていると意味がない**。
**設計を1つ変えた**: 生存確認は「判定不能」も違反として報告する。**黙って通すと、検査が
動いていないのに動いているように見える** — canary を植てる目的そのものを損なう。
最初の実装がまさにそれで、hmac エラーを出しながら「違反なし」を返していた。

## 2026-08-01 Slack にファネルを新設し第1陣を移送 — 「輸送層ではない」を輸送層にした

**背景**: 設計文書 §4.2 が実測で「`slack_post.py` は輸送層ではない。中身は整形ヘルパ2関数だけで
投稿関数を持たない」と指摘していた箇所。Slack だけファネルが無く SDK 直叩きが 25 箇所あり、
Canvas / Box と違って**ブローカーを1箇所に入れても覆えない**状態だった。
**決定**: `slack_post.py` に `post_message` / `post_ephemeral` / `update_message` /
`upload_file` を新設し、送信前検査（canary・ゼロ幅文字）と egress ログを集約する。
**kwargs をそのまま透過する形にした** — `client.chat_postMessage(**kw)` →
`post_message(client, **kw)` の機械的置換で移送できるようにするため。
**順序の判断**: パイプライン型（自動投稿）から移送した。設計文書が「**出力量の大半かつ
人間の介在なし**」と名指しした側で、対話型（ephemeral 応答）より先に覆う価値が高い。
移送 10 箇所、**残る直叩きは 15 箇所**（narrate 9 / qa_server 4 / transcribe 1 / confirm 1）。
**同日中に残り 15 箇所も移送し、直叩きはゼロになった。** 完了を待って pre-commit の
`slack-egress-funnel`（SDK 直叩き禁止）を有効化した。**未完了のうちに lint を入れない**のは、
恒常的に落ちる lint が「無効化する」方向の圧力を生むから。lint は達成した状態を固定する
道具であって、これからやることを表明する道具ではない。
**`/argus-narrate` の順序制約も実装した** — TTS へ渡す前に `scan_text_for_egress` を通す。
mp3 になった後では canary もゼロ幅文字も検出できないので、**合成前が唯一の検査点**になる。
このために検査関数を公開関数として切り出した（ファネル内部に閉じたままだと合成経路から呼べない）。
**限界**: 検査するのは canary とゼロ幅文字だけ。**自然な散文に符号化されたものは通る**
（TrojanStego 型）。また `upload_file` はファイル本体を検査していない — mp3/mp4/xlsx の中身には
canary もゼロ幅文字も適用できないため、**テキスト以外は生成元テキストの検査をもって代える**
（§4.2 の原則）。

## 2026-08-01 出力ブローカーの第1スライス — canary が初めて「遮断する仕組み」になった

**背景**: §4.3 の検知点3つのうち①（ブローカー＝送信前）だけが空だった。canary を植えても
出力側で止める場所が無く、`tool_calls` は「モデルが何を試みたか」しか見ていなかった。
**決定**: `output_broker.py` を輸送層の1段手前に置く。宛先は識別子で選ぶだけにし、送信前に
canary・ゼロ幅文字・自由文可否・承認要否を検査する。**canary を検出したら記録ではなく遮断する** —
canary は本来どこにも現れない文字列なので、出ようとしていること自体が異常だから。
**設計上の変更**: 設計文書 §4.2 の例は `canvas_id` / `channel_id` を直書きしていたが、
**origin が public で `no-slack-id-literals` の lint もある**ため、`egress_targets.yaml` は
`argus_config.yaml` への参照（`config_ref`）にした。**方針（外部可視性・自由文可否・承認要否）は
git 差分に出て、実値だけが外に出ない**という、元の設計より制約に合う形になった。
**新テーブルを作らなかった** — 送信の記録は `tool_calls` に入れる。連鎖が分かれると
「どちらが先か」が言えなくなるため、ハッシュ連鎖は1本に保つ。
**被覆率の明示（P8）**: 覆えているのは Canvas と Box だけ。**Slack は SDK 直叩き 25 箇所の
移送が未完了**で、`_dispatch` は slack を明示的に拒否する（黙って成功させると「ブローカーを
通った」という誤った記録が残る）。docstring に「ブローカーがあるから守られている、と
読んではいけない」と書いた。
**副産物**: canary の植え付けを保留していた理由（出力側の検知点が無い）が解消に近づいた。
Slack の移送が終われば、植える価値が実際に出る。

## 2026-08-01 能力分離 5b の第1スライス — Read Plane をトークン無しの別プロセスに割る

**背景**: 設計文書 §3.2 が「**Read Plane が Slack トークンを持たないという一点で P1 の大半が
達成される**」と書いている箇所。これまで `pm_qa_server.py` が全トークンを持ったまま調査も投稿も
担っていた。
**決定**: `pm_read_worker.py` を新設し、調査を別プロセスで走らせる。分離は2重にした。
(1) **トークンの不在** — `scrub_env()` で SLACK_* / BOX_* / PM_BOX_* / CANVAS / TTS / GITHUB_* を
除く。**DB 鍵と LLM トークンは残す**（Read Plane の仕事は pm.db を読むことで、LLM トークンは
read_plane の宛先にしか使えない）。(2) **到達性** — `net_guard` に `ARGUS_NETGUARD_PLANES` を
足し、`read_plane` だけを許可集合に入れる。allow-list が元から平面別だったのでほぼ無改造で済んだ。
**ゲートを実測で確認**: Read Plane プロセスから `slack.com` / `api.box.com` は resolve 段で遮断、
`api.rikyu.r-ccs.riken.jp` は到達可能。
**設計判断**: 子プロセス側にも自己検査を置いた（トークンが残っていれば起動を拒否）。**分離を
「呼び出し側が scrub する作法」に依存させると、1箇所の書き忘れで静かに崩れる** — P8 と同じ型。
また親が失敗を握りつぶして in-process へフォールバックしないよう、`run_in_read_plane` は
例外を上げる。**「分離があるように見えて無い」状態が最悪**だから。
**限界**: `net_guard` は同一プロセスの socket フックなので、subprocess（box CLI）とフック解除には
効かない。**OS レベルの強制（iptables / network namespace）が本来の姿**で、そこは運用調整が要る。
**未切替**: `pm_qa_server` の investigate は in-process のまま。UX 影響（「調べながら中間結果を
投稿する」使い方が消える）を tool_calls ログで確認してから倒す — その判断材料を得るのが
Phase 5 をここに置いた理由でもある。

## 2026-08-01 R8（改竄の集中リスク）へ第2系統を投入 — 全件ではなくフラグ付き項目に当てる

**背景**: 設計文書で唯一「受容しない」と書いた項目。RiVault の Kimi-K2-Thinking が K3 と同一
提供元で、かつトリアージ（Slack・議事録の両方から呼ばれる単一関数）を担っている。
**決定**: 独立系統は **`meta-llama/Llama-4-Scout-17B-16E-Instruct`（Meta）**。**全件ではなく
フラグ語が立った項目だけ**に当てる — 利用できる非中国系モデルは 17B/12B と小さく、全件だと
**能力差による不一致が支配的**になって信号が埋もれる。懸念しているのは平均的な性能差ではなく
**特定の語で挙動が変わる条件付きの偏り**（CAISI/CrowdStrike が観測したのはこれ）で、弱いモデルでも
その条件は共有していないため、**トリガ付き入力での不一致こそが探している信号**になる。
**捨てた案**: 第2系統の判定で主系統を上書きする案。能力差による誤りが混ざるので、**自動で
覆さずフラグを立てて人が見る**に留めた。件数が小さいので全件レビューできる。
**一致も記録する** — 不一致だけ残すと「何件中の不一致か」が分からず、雑音の割合を後から
評価できない。
**あわせて fail 方向を統一**: `triage_items` の `missing_verdict` 既定が `DROP`、
`triage_items_batched` が `KEEP` と**関数によって逆向き**だった。判定不能時に欠落を作らない
`KEEP` に揃えた（欠落は敵対的でなくても失敗モードとして起きる）。**Slack 抽出の誤抽出が
そのぶん増えるが、静かに消えるより見えるノイズの方がよい**という判断。
**実測**: 「中国製モデルの採用可否を法務と整理する」でフラグ 2 語、第2系統は KEEP を返した。

## 2026-08-01 供給網の固定（§4.6）を実装 — 照合が eval の judge 名の不一致を検出した

**背景**: K3 のオフライン評価を再開するなら、その前に「どのモデルを使ってよいか」の宣言が要る
（§4.6）。Phase 0 の項目でもある。
**決定**: `config/model_pin.yaml`（宣言）＋ `scripts/utils/model_pin.py`（照合）。**検証できるものと
申告にすぎないものを構造として分けた** — `served_model_name` は `/v1/models` と照合できるので
判定に使い、`declared_revision` / `declared_trust_remote_code` / `declared_engine` は**取得手段が
無いので判定に使わない**（R12）。使うと「pin が通った＝安全」という誤った確信を与える（P10）。
実効は「id の一致」と「申告値の変更が git 差分に出ること」の2つに限られる。
**照合が実バグを1件検出**: `minutes_ab.py` の judge 既定が `DeepSeek-V4-Flash` だったが、
RiVault の実 id は `deepseek-ai/DeepSeek-V4-Flash`（org 接頭辞つき）。`argus_ab.py` は正しい方を
使っており、**同じモデルを指す2つの表記がリポジトリ内で食い違っていた**。K3 視覚議事録ベンチを
再開したら judge 段で失敗していた。**自分の実装にも同型の穴があった** — 最初の `--check` は
RIKYU で 401 になった。トークンをエンドポイントごとに持たせず `RIVAULT_TOKEN` 決め打ちだったため。
`token_env` を pin のエントリに足して解決。**型は「1つの資格情報が全エンドポイントに通ると仮定した」**で、
docling の「1経路の値を全経路の代理として扱った」と同じ形。
**K3 の扱い**: id の実在は確認できたが `declared_engine` / `declared_trust_remote_code` は未確認の
ため **`production: false` のまま**。K3 は HuggingFace で `custom_code` タグ付きで、transformers 経路
なら `trust_remote_code=True` が必須になる（§1.3 攻撃者2）。理究が vLLM/SGLang ネイティブで
動かしていることの確認が取れるまで本番不可、という状態を pin に固定した。

## 2026-08-01 tool_calls 台帳を実装 — セキュリティの残作業と K3 優先度1 が同一コードパスだった

**背景**: 松岡先生の指摘に始まる対策で流出面（層1・層2）は enforce まで到達したが、**検知は
`logs/` しか見ておらず、流出が実際に現れる出力側＝ツール引数を覆えていなかった**（§4.3 検知点②）。
Kimi-K3 再開の可否を検討したところ、**K3 の優先度1（APIクライアント層の再設計）が
`tool_calls` / `reasoning_traces` と同一コードパス**で、両者の第一歩が一致すると分かった。
**決定**: pm.db に `tool_calls`（§4.4 のスキーマそのまま）＋追記専用トリガ＋ハッシュ連鎖を実装し、
`execute_tool` で記録する。**MCP を廃止してチョークポイントを1箇所にした結果、計装点も1箇所で済んだ** —
分離の副産物。**拒否された呼び出しこそ記録する**（`blocked` + `block_reason`）。モデルが EGRESS を
試みた事実が残るため、`netguard_deny` が宛先しか見ないのを補える。
**捨てた案**: ツールごとに接続を共有して高速化する案（並列実行でハッシュ連鎖が壊れる）。
ロックで直列化し、接続はツールごとに開閉する。**記録の失敗はツール実行を止めない**（fail-open。
監査ログの不調で本番を落とす方が損失が大きい）が、例外は必ずログに出す（P6）。
**限界の明示**: 連鎖が検出できるのは**事故による破損**まで。検証者が改竄されうる側と同じプロセス・
同じ UNIX ユーザで動くため、コード実行を取られれば両方書き換えられる。外部アンカー（日次の
ハッシュ投稿）は Phase 3 のブローカー待ち。
**続けて `reasoning_traces` も実装**（同日）。ループは既に `return_reasoning=True` で思考トレースを
取得していた（`preserve_reasoning` 時）ので、**保存先を足すだけで済んだ** — K3 優先度1 の
「逐次永続化」はここに接続する。`tool_calls` は sha256 だけを持ち本体は別テーブル。
**これで「EGRESS を試みる直前に何を考えていたか」が追跡できる**（実測で紐づけを確認）。
**保持期間を既定90日にした** — このテーブルはモデルが見た機微データがそのまま入る新しい機微
データストアであり、容量の問題として扱わない。canary 発火時は `keep_sessions` で該当セッションを
保全する。§1.2 の機微データ棚卸しにも追加した。

## 2026-08-01 net_guard を enforce へ（デーモン先行）— 初回の deny は綴り不一致だった

**経緯**: warn 1 周（cron を手動で全実行）で deny 0 を確認し、`pm_daemon.sh` に qa/web 限定で
`ARGUS_NETGUARD=enforce` を既定化。cron は各自が `~/.secrets/*.sh` を source する別経路なので
影響を受けない（デーモン先行 → 1 日観測 → cron の段階導入）。両デーモンは
`EndpointMismatchError` を出さず起動し、embedding は `localhost:8001` エントリで
resolve→connect の連鎖が enforce でも通ることを実接続で確認した。
**enforce 初日の唯一の deny**: `/argus-brief` 実行時に `host=localhost port=50021`
（VOICEVOX）が resolve 段で遮断された。原因は `pm_tts.VOICEVOX_HOST` が
`http://localhost:50021` のハードコードで、allow-list は `127.0.0.1:50021` だったこと。
**判断**: allow-list に `localhost` を足すのではなく**コード側を 127.0.0.1 に正規化**した
（許可対象を増やさない、`/etc/hosts` に依存させない）。同じ綴り不一致は
`DOCLING_SERVE_URL` / `FISH_TTS_HOST` でも起きており、これで 3 件目。
**教訓**: net_guard の照合は名前解決をしない文字列一致なので、**ローカルサービスの宛先は
コード・環境変数の両方で `127.0.0.1` 表記に統一する**。数値 IP リテラルは resolve 段を
スキップし connect 段のリテラル一致で通るため、綴りの揺れ自体が起きない。
**別件（enforce とは無関係）**: 同じ `/argus-brief` で fish-speech への接続が
`Connection refused` になった。これは allow-list 済み宛先へのサービス未起動であり遮断ではない。
`FISH_TTS_HOST` が設定されている間 pm_tts は fish を選ぶため、fish 停止中は TTS が失敗する。

**監視のノイズ対策（同日、上の deny をきっかけに判明）**: 修正済みの deny がログに残る限り
`netguard_deny` が `--days 7` の窓で鳴り続ける設計だった。**監視がノイズになると見られなく
なり、監視が無いのと同じになる**ため 2 点入れた。(1) ログ走査の窓をデータ検査から分離
（`--security-days`、既定 1 日）、(2) `config/netguard_ack.yaml` に解消済みを申告する仕組み。
ack は**恒久 mute ではない** — 抑制するのは `fixed_at` より前のタイムスタンプを持つ行だけで、
修正後の再発は必ず報告される。タイムスタンプを読めない行は抑制しない（判断材料が無いのに
黙らせると再発を見逃す）。抑制件数は毎回 stderr に出す（黙って減らすと「静かになった」と誤読
される）。**「宛先が正当だった」場合は allow-list に足すのが正しく、ack に書いて黙らせない** —
混同すると allow-list が実態と乖離する。

## 2026-08-01 pm_web_fetch.py を廃止 — 認証境界の外へ出る経路が無くなった

**背景**: §4.5 の判断（隔離ではなく廃止）を実行。外部 URL の取得は攻撃者が管理しうるサーバへの
外向きリクエストであり、URL・パラメータ・タイミングがそのままチャネルになる。Box/Slack が
認証で守られていることは宛先が境界の外なので一切の助けにならない。
**決定**: `pm_web_fetch.py`（+ 旧パス symlink・`pm_web_update.sh`）を削除。**`web_articles.db` は
残す** — 取得を止めた時点でリスクは消えるため、蓄積データのパージは不要。既存チャンクは
`search_text` で引き続き引ける。`pm_embed.py` はコード変更なし（追記元が消えただけ）。
**再発防止**: `test_web_fetch_scripts_are_gone` で 3 ファイルの不在を固定し、
`net-guard-import-required` lint の除外リストから pm_web_fetch を削除（除外を残すと、同じ名前で
新しい実装が入ったとき net_guard 無しで素通りする）。
**代替**: 記事が必要なら人が Box に置き `pm_box_crawl.py` の既存経路に乗せる（人間の判断が入る）。
**副産物**: canary のシグナルが明確になった。正当なフェッチャーが存在しなくなったので、
canary ホスト名への名前解決が観測されたら無条件に異常。

## 2026-07-31 canary + 監視を実装 — 既存の selfcheck ジョブに同乗させ、新デーモンを増やさない

**背景**: §4.3 の canary は「安価で既存コードをほとんど触らない」ことが採用理由。専用の
監視デーモンを立てると、その分だけ守る対象が増えて本末転倒になる。
**決定**: 検知を `pm_selfcheck.py`（既に cron 06:30 平日・読み取り専用・違反で exit 1）に
`canary_hit` / `netguard_deny` として追加。canary の発行・失効は `net_guard.py` の CLI に置いた
（allow-list との衝突検証を同じモジュールで持てるため。db_utils は遅延 import で循環回避）。
ホスト名は `.internal-check.invalid`（RFC 2606 予約 TLD）— 実在ドメインだと「canary への到達」が
本物の外部 DNS クエリになってしまう。
**踏んだバグ**: テーブル未作成の吸収を `except sqlite3.OperationalError` で書いたところ、
**SQLCipher の例外は標準 sqlite3 の派生でないため暗号化 DB でだけ落ちた**（平文のテストは全部通る）。
`table_exists()`（sqlite_master 参照）に置き換えた。暗号化 DB を扱う箇所で例外クラスに依存する
判定を書かない、が教訓。
**判明**: 稼働中の qa デーモンは net_guard 導入（20:28）より前の 11:48 起動で、`[NETGUARD]` 行が
1 本も出ていない。**warn 期間はまだ始まっていない**（再起動が前提条件）。
**保留**: canary の実データへの植え付け。人間向けレポート経路の `is_canary` 除外が無い状態で
pm.db に植えると架空のアクションアイテムが PM のレポートに出るため、box_docs 側から始める。

## 2026-07-31 MCP Server（pm-multi-agent）を丸ごと廃止 — 2 箇所目のチョークポイントを消す

**背景**: `investigate` ループには `COMMAND_TOOLS` allow-list を入れたが、`pm_mcp_server.py` は
`registry_for()` を通らず 14 ツール（READ 10 + **EGRESS 3**）を `@mcp.tool()` で公開しており、
**READ と EGRESS が同一プロセスに同居する唯一の経路**として残っていた。加えてこの経路の推論主体は
ローカル LLM ではなく Claude であり、`search_text` が返す議事録・Slack 本文が Anthropic API に
渡る構造で、機密ファイルを Claude に読ませない運用と整合していなかった。
**判明した事実**: `claude mcp list` は "No MCP servers configured"。登録先が `.claude/settings.json` の
`mcpServers` で、Claude Code が読むのは `.mcp.json` か `~/.claude.json` のため、**2026-06-20 の導入以降
一度も接続されていなかった**（`enabledMcpjsonServers` も空、プロセスも不在）。EGRESS 3 だけ外す部分
対応も検討したが、実利用ゼロ・代替（`pm_argus_agent.py --investigate` + `--to-*` フラグ）が揃って
いるため **丸ごと畳む**判断（PM）。
**実施**: `pm_mcp_server.py` と `.claude/settings.json` を削除、`pm-multi-agent` Skill を撤去、
再登録を防ぐ pre-commit lint `no-mcp-server-registration` を追加（`.mcp.json` / `mcpServers` /
`@mcp.tool` を検出）。`mcp_tools.py` / `output_tools.py` は Slack Bot 側と `pm_exec_summary.py` が
使うため据え置き（ファイル名の "mcp" は旧サーバ由来という注記を docstring に追加）。
**副作用**: `docs/kimi-k3-migration.md` の優先度6「既存MCP資産をそのまま活かす」は前提が消えたため無効化。

## 2026-07-31 net_guard warn フェーズの前提を実 crontab と突合 — docs の CRON 表が実態とズレていた

**背景**: `enforce` へ倒す前提の「warn で 1 周」を具体化するため実 crontab を確認したところ、
`docs/architecture.md` / `argus_outcomes.md` の CRON 表と**時刻も本数も一致していなかった**
（実際は 02:00 box / 06:30 selfcheck / 07:00 canvas_report / 07:47 argus_daily / 16:00 from_slack）。
**判明した 2 点**: (1) `pm_web_update.sh` は cron に載っていない — 認証境界の外へ出る唯一の経路が
そもそも定期実行されていないため、`pm_web_fetch.py` 廃止はコード削除だけで済む（運用の穴埋め不要）。
(2) 月〜金限定ジョブがあるため warn フェーズの下限は 24 時間ではなく**平日 1 周（実質 1 週間）**。
**あわせて修正**: `network_allowlist.yaml` の docling `127.0.0.1:5001` に付いていた「プレースホルダ」
注記は誤り。接続側 `pm_box_update.sh` の既定と待受側 `pm_daemon.sh` の bind 両方に一致する確定値
だった（allow-list を環境変数 grep だけで書き、シェルスクリプトを見ていなかったため）。

## 2026-07-31 docs/decisions/ を追跡から外す — public リポジトリへの機微情報混入

**背景**: `.gitignore` には以前から `docs/decisions/` があったが、**追跡開始が先だったため無効**
だった（gitignore は追跡済みファイルに効かない）。origin は public な GitHub リポジトリ。
**決定**: `git rm --cached` で追跡から外す。ファイル実体は手元に残す（スクリプトは FS から直読）。
**未解決**: **HEAD から外しても公開履歴（2026-07-13 以降 8 コミット）には残る。** 履歴書き換え
（filter-repo + force-push）は組織調整が必要なため別途判断。あわせて棚卸しした結果、
`docs/project.md` / `argus_config.yaml` / `goals.yaml` は**一度も commit されておらず**、
履歴にある `data/minutes/*.db` は **SQLCipher 暗号化済み・鍵は未コミット**であることを確認した。

## 2026-07-31 出力と外向き通信の宛先を allow-list 化（deny-list からの転換）

**背景**: 松岡先生経由の LLM バックドア脅威の一次整理を受け、脅威モデルと目標構成を
`docs/security-architecture.md` に定義。実装との突合を 4 回繰り返し、**同じ型の誤りを 4 回**
犯した — いずれも「エージェントのツールレジストリが漏斗である」という誤った前提だった
（実際の主流は cron とパイプライン）。原則 P8（被覆率を実流量で検算）と P10（対策が何を
証明するかを明示）はこの反省から立てた。
**決定**: (1) ツール公開を `COMMAND_TOOLS` の allow-list に転換（deny-list では新 EGRESS
ツールが既定で露出する）、(2) 外向き通信を `net_guard` で socket 層フックし宛先を照合
（呼び出し箇所ごとのチェックは書き忘れが素通りするため採らない）、(3) Box 共有リンクを
`--access open` から `collaborators` へ。
**捨てた案**: ブローカーをツール層に置く案（実際の出力量の大半を占める cron/Patrol の自動
投稿を覆えない）、MUTATE の構造化フィールド制約（MUTATE ツールは 0 件で被覆率 0%）、
引用スパンによる「捏造の原理的排除」（証明できるのは根拠の実在のみ）。
**影響**: net_guard は既定 `warn` で記録のみ。allow-list の実値確定後に `enforce` へ倒す。
box CLI 等の subprocess と MCP 経路は対象外（範囲は設計文書に明記）。

## 2026-07-31 Kimi-K3 の Argus 活用を一旦停止（セキュリティ懸念・PM 判断）

**背景**: PM からセキュリティ上の懸念が示され、Kimi-K3 の活用を一旦取りやめる判断。
**実施**: (1) 進行中だった議事録視覚ベンチ（minutes_ab、20 本中 8 本完了時点）を停止、
(2) qa デーモンの K3 override を無効化（settings.json から ARGUS_ONESHOT_LLM_MODEL を除去し
再起動。one-shot 経路自体は glm-5.2 で継続 — 実測で現行超えのため）。
**残置**: 視覚議事録の実装（call_vision_llm / --slide-images / minutes_ab）は
**2026-07-31 にコミット済み・既定 OFF で凍結**（opt-in 設計で K3 非依存。未コミットで放置すると
失われるためコミットまでは進め、有効化の判断のみ保留）。ベンチ素材・部分結果は
data/eval/minutes_ab/ に保存。K3 の評価記録（rikyu_argus_model_eval.md / kimi-k3-migration.md）は
再開時の資産としてそのまま。
**再開条件**: セキュリティ懸念の解消（PM 判断）。残っていた URL/TOKEN の export
（~/.secrets/rikyu_token.sh 7-8 行目）の削除は PM 作業。

## 2026-07-30 Q-Nova 本番障害からクエリトークン品質を修正 — FTS5 ハイフン=NOT の潜在バグも発見

**背景**: K3 有効化直後の本番 investigate「Q-Nova の停滞理由」が検索破綻。Sudachi トークンが
Q-Nova→Nova に縮退、段階的 AND が文順先頭の「今年度」1 語まで落ち低関連 70 件が RRF で vector を
全滅、さらにハイフン付きトークンは FTS5 の NOT 解釈で silently SQL エラー、の 3 連鎖を特定。
**決定**: 複合エンティティ保持 + FTS5 エスケープ + 機能動詞除去/汎用語降格 + 1 語縮退段の RRF 遮断。
**ボツ**: 単独 ASCII 語抽出（AppGamma7 注入が AppBeta の 4 語 AND を破壊し rank1→43）と、エンティティの
弱段解除（AppBeta は語形はエンティティでもコーパス内低選択性、hit@60 -0.074）— いずれも recall_eval
実測で撤回。語形からコーパス内選択性は判定できない、が教訓。
**副産物**: recall_eval が _init_sudachi() を呼ばず過去の recall 測定は fts_tokens 段を素通り
していた計測ギャップを発見（要修正、PLAN 参照）。テストは運用フラグを conftest で密閉化。

## 2026-07-30 検索段バグ 2 件を発見・修正 — 全角記号で FTS 全滅→日付フォールバックが RRF を汚染

**背景**: one-shot 検証（下記）で K3 の敗因を深掘りした結果、モデルでなく検索段に原因を発見。
(1) sanitize_fts_query が全角括弧・読点等を除去せず日本語質問で FTS 4 段が全滅、
(2) 最終段「日付降順フォールバック」（関連度ゼロの最新記録）が RRF で vector 候補 50 件を
数学的に押し出す。生質問で検索する経路が系統的に破綻していた（gv-nvl72 実測: 証跡 0/7）。
**決定**: sanitize の全角対応 + 日付フォールバックの RRF 遮断（vector 脚が空の時のみ最終手段として
温存）。knowledge_context.py の重複 sanitize 実装も削除し retrieval 側へ一本化。
**影響**: 隔離した評価 18 件の再計測で勝敗 9 件（50%）が反転 — 修正前の A/B は判定不能だった。
本番の search_text / investigate / patrol 証拠検索 / 実績抽出も全て受益（要 qa デーモン再起動）。

## 2026-07-30 one-shot 長文脈経路の 2×2 検証（gold 8 + mh- 多段 9 問）— 経路は設問型依存、モデル軸は K3 一貫優位

**背景**: kimi-k3 見送り（下記エントリ）の敗因が実装ミスマッチにある仮説を検証するため、
補助 LLM ゼロ + broad-recall + 1 回渡しの one-shot 経路（`ARGUS_ONESHOT`、opt-in・本番不変）を
実装し {ループ, one-shot} × {glm, k3} + 直接対決の 5 ペア × 17 問を盲検 A/B した（検索バグ
修正後の再計測込み）。
**結果**: (1) 経路の優劣は設問型に依存 — 単発 search では one-shot 圧勝（glm-oneshot 83.3%・31s）、
多段変遷型（mh-）では逆転しループ優位（glm-oneshot 22.2%）。broad-recall 1 回では 1 年超の変遷の
中間段階を拾えない。(2) モデル軸は経路によらず K3 > GLM（one-shot 直接対決 62.5% × 2 セット、
k3-loop は mh- で 71.4% 合格）。(3) 多段品質首位は k3-loop、ただし中央値 455s + rerank
フォールバック 18 件。docqa は one-shot 不適（--file 経路維持）。RIKYU nginx ~600s も発見
（非ストリーミング観測のみ — ストリーミング仮説は設計メモ v2 のステップ0 で判定予定）。
**次**: K3 の本命はメモ優先度 1（API クライアント層再設計）後の k3-loop。単発型は one-shot が
即戦力。経路使い分けの設問型判定が新課題。詳細は docs/decisions/rikyu_argus_model_eval.md
追補・追補2、設計は docs/kimi-k3-migration.md。

## 2026-07-29 RIKYU kimi-k3 を評価（HF 推奨条件での再評価含む）— glm-5.2 継続、見送り

**背景**: RIKYU に kimi-k3 が追加配信され、既存のモデル評価（2026-07-13 の 3 モデル）に加えた。
初回評価後、HF モデルカード推奨（temp1.0 / reasoning_effort / preserved thinking）との食い違いが
判明し、補正条件で再評価まで実施。
**決定**: **補正しても見送り**。k2.6 比では大幅な世代改善（総合 3.15→4.79）だが、brief/risk は
glm-5.2 に届かず latency 7〜8 倍。決め手は「`reasoning_effort` も thinking 抑制に効かない」こと
（risk の reasoning 長が low 指定でむしろ増加、truncation 悪化）— kimi 系の思考は 3 手段すべてで
制御不能。investigate 単発品質のみ一貫して glm 超え（7-0-3 / 7-1-2）だが、初回のループ実走で
3 問中 1 問が予算枯渇で完全失敗、再評価では全問単発完結で失敗モードが未検証のまま。
**再訪条件**: tool_call を要する複雑な investigate 質問でのループ安定性検証。評価用 opt-in 機構
（ARGUS_REASONING_EFFORT / ARGUS_PRESERVE_REASONING、既定 OFF）は本番コードに残置。
詳細は docs/decisions/rikyu_argus_model_eval.md 追補・再評価節。

## 2026-07-29 canvases を pm_embed で索引化 — source_type=slack_canvas

**背景**: 同日追加の slack.db canvases テーブルは書き込み専用で、investigate/brief の検索に乗らなかった。
**決定**: pm_embed.py に index_slack_canvases を追加（config の channels 定義を再利用、新設定なし）。
source_db は `slack.db#canvas#{channel_id}` — slack_raw の `{channel_id}.db` と名前空間を分けたのは、
delete_source_chunks が source_type を見ず source_db+index_name で消すため、共用すると生メッセージの
チャンクを巻き込むから。Canvas はストック情報なので purge_stale_record_chunks で旧版チャンクを都度掃除。
**見送り**: 削除済み Canvas の索引掃除・複数チャンネル同一 Canvas の dedup・鮮度スコアの Canvas 優遇是正は
運用実測後に判断（Canvas の held_at は常に新しく、一次証拠を押し出す懸念あり）。

## 2026-07-29 チャンネル Canvas の内容を slack_pipeline で取得 → slack.db canvases テーブルへ

**背景**: チャンネルタブの Canvas に会議資料一覧等の恒常情報が置かれるようになり、メッセージ取得だけでは拾えない。
**決定**: 新規スクリプトは作らず slack_pipeline.py に取得ステップを追加。タブ列挙は `bookmarks.list` でなく
`conversations.info` → `properties.tabs`（Canvas タブを返すのは後者のみ）。`type=="canvas"` フィルタ必須 —
Slack List（type=="list"）タブも file_id を持ち、生 JSON が混入する事故をスモークテストで実測。
差分判定は `files.info` の `updated` 比較（0 は判定不能として常に再取得）、空本文は UPSERT せずスキップ
（失敗時の空上書きが恒久化する病理を防止）。ダウンロード処理は pm_sync_canvas.py と canvas_utils.py に共通化。
**既知の制限**: ingest_slack は canvases 未対応（決定事項・AI の抽出元にはならない。索引化は同日対応 — 上記エントリ参照）。

## 2026-07-28 「グラフエンジニアリング」の Argus 導入を見送り（ボツ案）

**背景**: 2026 年央に流行の同語を調査。実体は ①オーケストレーショングラフ（LangGraph 型の
処理フロー設計）と ②コンテキストグラフ（GraphRAG 型の知識グラフ化）の 2 概念の混在。
**決定**: ①は Argus が既に実装済みの構成（Orchestrator-Worker / agent ループ / Patrol の Human Gate）に
後から名前が付いたものでフレームワーク導入の価値なし。②も現時点では見送り — 品質優先項目
（P8.1 話者同定 > P2 達成証跡）が先。
**再訪条件**: investigate で「決定の変遷を辿る質問」（方針がいつどの決定で覆ったか等）の回答品質が
問題化したら、related_ids の型付け（supersedes/depends_on）＋ SQLite 再帰 CTE 探索（depth≤2）を
enrich_items.py / investigate ツールの小改修で検討。Neo4j 等の新規インフラは不要と判断済み。

## 2026-07-28 議事録転記に 3 ゲートトリアージ導入 — 非本質項目（連絡・会議運営）の混入源を封鎖（PM 指摘）

**背景**: 「語尾だけの機械抽出で連絡事項が混入していないか」との PM 指摘を受け pm.db を全数調査。
疑義率は action_items 20%（82/405）・decisions 8%、混入源の大半は **meeting 経路**で、Slack 側の
トリアージ強化（6/8・7/27）後も議事録→pm.db 転記（ingest/minutes.py）が機械コピーのみのため
疑義率 22.6% と改善していなかった（真の穴は素の Markdown 取込経路。録音経路には生成時トリアージあり）。
**決定**: transfer_meeting に Slack と同じ 3 ゲート審査（マイルストーン関連・代替可能性・影響範囲）を
既定適用。DROP は削除でなく **deleted=1 + audit_log（source='minutes_triage'、理由付き）で永続化**
（復活可能・--force 冪等・監査可能）。Web UI 編集保存（pm_minutes_publish）は triage=False —
人間の最終判断を LLM が覆さない原則。人間が復活させた行は minutes_human_kept 監査行で保護を持続。
フェイルオープン 3 種（milestones 空→スキップ、応答欠落→KEEP、チャンク単位スキップ）を選んだのは
「後段の審査が正解を殺す」病理（consensus 3→1 の教訓）の再発防止のため。既存データは
pm_screen --triage（一括 LLM 審査→ pm_relink 互換 CSV）で洗い出し、削除適用は人間の精査後。
既知の制限: 録音経路は生成時＋転記時の二重トリアージになる（recall への影響は運用観察で判断）。
**較正**: 初回の一括審査で decisions の DROP が 123/396（31%）と過剰 — ゲート1（マイルストーン
関連性）が AI 向け設計のまま決定にも適用され、正当な資源配分・技術決定（NVL72 レンタル範囲、
AWS GB200 測定方針、AppBeta 測定パラメータ等）を「マイルストーン非関連」で落としていた。
TRIAGE_PROMPT に決定事項向け例外（EXTRACT_PROMPT の分類ゲート 3 問と整合: 覆すとやり直し／
選択肢排除／資源・方向確定なら KEEP、ただし会議運営・連絡共有は DROP）を追加し 50/396（12.6%）
に収束。実データで較正してから ingest 適用できたのは pm_screen --triage を先に走らせた副産物。

## 2026-07-27 バグクラス再発防止の selfcheck 検査ジョブを新設（PM 指示）— 初回実行で実問題 8 件を検出

**背景**: 単純バグ・ロジックバグの続発を受け、発見済みバグを 5 クラスに一般化して事前検出する
検査を PM 指示で整備。静的検査（tests/selfcheck/、pre-commit の pytest で毎コミット自動実行）+
データ不変条件（scripts/quality/pm_selfcheck.py、読み取り専用・cron 日次想定）。
新規スクリプトは pm_screen（重複検出）と責務が異なるため新設を正当と判断。
**決定と成果**: 初回実行だけで実問題 8 件を検出 — box CLI の PATH 欠落が**さらに 3 本**
（pm_from_recording/auto/pm_from_slack、patrol と同一クラス）、db_utils --help 死、
飾り引数 --no-stream、未来日付 6 レコード（2026-09-10 バッチ→ 6/10 に修正）、
**未解消の xlsx_sync 巻き戻り 2 件**（#3248/#3251、7/26 発生・掃討調査で人手 reopen と
誤認していたもの → 根拠 note ごと復元）。全件修正済み（a5ebf24）。
レビューが私の日付パーサ「最先頭優先」案の回帰（GMT 形式 32 件で処理日を返す）を実測で
検出し、「GMT 族絶対優先 + 族内最先頭」に修正 — 検査も修正もレビュー必須の好例。
box 依存ラッパー 7 本に command -v box のランタイム警告を追加（無音失敗クラスの根絶）。

## 2026-07-27 xlsx_sync 巻き戻り再発 → シート鮮度ガード導入（緩和策は PATH 欠落で 7/24 から死んでいた）

**背景**: patrol が 17:00 に根拠付き自動クローズした 3 件を、16:00 cron 由来の pm_xlsx_sync が
16:45 時点の古いシートで open に巻き戻し note も消去（7/24 に対策したはずの衝突の再発）。
調査の結果、緩和策「クローズ後の XLSX 再エクスポート」は pm_argus_patrol.sh だけ box CLI の
PATH 補正行が無く、導入以来毎回 FileNotFoundError で失敗していた（warning 止まりで無音）。
**決定**: (1) PATH 補正を追加（18:00 サイクルで再エクスポート成功を実証）。(2) 構造的対策として
pm_xlsx_sync に**シート鮮度ガード**を新設 — シートのエクスポート打刻（XLSX docProps created、
フォールバック Box modified_at → mtime）より新しい pm.db 変更がある**フィールド**は同期せず
WARN、--force で明示上書き。行単位でなくフィールド単位にしたのは、無関係な note 追記で
シートの人手編集（担当者変更等）が巻き添え破棄されるのを防ぐため。巻き戻された 3 件は
audit_log の old_value から note ごと復元・再クローズ。教訓: **warning 止まりのフェイルセーフは
発動確認をタスク化しないと死んでいることに気づけない**。

## 2026-07-27 自動クローズの日付逆転バグ（AI #3056）— 発生日より古い証拠は完了の証拠にならない

**背景**: patrol が extracted_at=2026-06-09 のアイテムを 2026-05-18 の Box 文書を根拠に自動
クローズ（PM 指摘）。evidence_since_extracted ガードは存在したが、(a) hybrid 検索の vector 経路が
since_date を無視、(b) FTS 側も box_document を日付フィルタから免除（検索用途の意図的仕様）の
合わせ技で両経路とも素通りだった。掃討調査で同パターンの誤クローズを追加 14 件特定
（note 内の根拠日付 vs extracted_at の突合）、計 15 件を audit_log 付きで再オープン。
**決定**: 3 層防御（0c63176）— vector 経路に since_date 実装 + `exempt_box` フラグ新設、
patrol 側は exempt_box=False で box も発生日で絞り検索実装非依存の held_at post-filter を主防御に、
LLM 判定プロンプトへ「発生日より前の情報は証拠にならない（アイテム化時点で既知）」を明記。
スレッド返信も max(60日窓, 発生日) でカット。残存リスク: box の held_at は Box 側 modified_at
のため「古い内容の文書が発生後に更新された」ケースは通過し得る（日付粒度は日単位）。
副次影響: vector 修正により Slack 抽出の背景知識検索（since_days=90）が実質 90 日窓化
（従来は vector 経由で古い knowledge が混入していた。precision 向きの変化と判断、要観察）。

## 2026-07-27 パッケージB完結: Patrol 検出器 3 種を有効化（cooldown バグを修正してから）

**背景**: 監査対応の最終項目。有効化前の通知量見積もりで、cooldown の**キー名不一致バグ**を発見
（検出器は overdue_reminder/stale_alert で判定、送信側は overdue/stale で記録 → 抑制が永遠に
効かず、有効化すると 30 分毎に全件再送される状態だった）。d519c18 で記録側にマッピングを適用し、
判定キーとの一致をソース解析で検証するテストを追加してから有効化。
**決定**: overdue_reminder / deadline_warning / stale_detection を enabled: true に（PM 判断、
2026-07-27）。初回発動は見積もりどおり期限超過 38 件→17 担当者・停滞 66 件・期限接近 0 件の
計 104 アクション、エラーなし。`dm_redirect_user` 設定済みのため全 DM は管理者へ転送される
確認モードで稼働（チャンネルに出るのは停滞サマリ 1 通のみ）。cooldown 記録も修正後キーで
38+66 件を確認。**リダイレクト解除（実担当者への直接 DM）は別途 PM 判断**。

## 2026-07-27 パッケージD完結: 休眠パスは「配線より削除」、ただし監査前提の誤り 2 件は残置

**背景**: 監査の小粒清掃。孤立関数 11 個（build_risk_prompt/build_brief_prompt 含む）・
goals_print.py・飾り引数 2 件・.bak 2 件は全数 grep + crontab + vulture で参照ゼロを確認し削除
（機能は git から復元可能、brief/risk の assignee/topic フォーカスは現行経路が自前処理済みで
機能喪失なし）。screen ジョブに --semantic を配線（ただしジョブ経路自体はフロント未参照の休眠。
プレビュー API と既定を揃えるための整合修正）。pm_ingest --help の ValueError（help 内の生 %）、
llm.py docstring、スキルの claude_code 残記述も修正。
**決定（監査前提の訂正 2 件）**: (1) 「Web の terminology/glossary 削除不可」は誤り — save フローの
deleted フラグで削除 UI 実装済みだったため、フロントは無変更とし孤立していた単発 /delete
エンドポイント 2 本を削除。(2) 「fish TTS は移行残骸」も誤り — /argus-narrate の英語話者クローンが
依存する現役設計（不通時自動フォールバック）のため残置し、未配線だった default_text_limit() のみ
配線（fish 疎通時 400 字上限が有効に）。

## 2026-07-27 パッケージC完結: investigate 検索パラメータを拡大既定化、--file 窓は据え置き

**背景**: gemma4 期の固定値（top5 / 抜粋400字 / プレビュー400字 / --file窓24k字）を env 化し、
e2e A/B ハーネス `scripts/eval/investigate_ab.py`（pm_argus_agent を 2 アーム実行、参照事実付き
Kimi judge、調査予算 1200s 統一、budget_truncated 記録）を新設して実測。
**決定**: search 系 3 値は expanded（top10/1200字/800字）が 4勝2敗 66.7% で合格 → 既定昇格
（コミット e34a9c3、qa デーモン再起動済み）。勝因は出典の精密化とカバレッジ向上、
レイテンシ +4.5s。**--file 窓 24k→150k は 0/2 で却下** — 150k 窓は 3 倍速いが map 段が
17→3 回に減り数値の網羅性が低下。「大窓=高品質」は成立しないと実測で確定
（哲学: brief/risk の全文脈化が勝ったのは切り捨ての解消であり、map-reduce の窓拡大は
抽出機会の削減。同じ「拡大」でも機序が逆）。これで監査パッケージ C は 6/6 完了。

## 2026-07-27 Slack 抽出トリアージを 2 段 → 1 パス統合に既定変更（LLM 1 呼削減 + 取りこぼしパス除去）

**背景**: Extractor→Triage の 2 段分離は弱モデル（gemma4）前提の設計で、2 段目には「triage
レスポンスに候補が欠落→保守的 DROP」という構造的な取りこぼしパスがあった。抽出プロンプトに
3 ゲート自己審査を織り込む integrated 方式を追加し A/B（knowledge_ab --compare triage、
狙い撃ち item-bearing 10 + ランダム 15、Kimi judge 順序スワップ）で実測。
**決定**: integrated 勝ち 3・tie 19・**負け 0**（勝ち+引き分け 100%、両層とも）で既定を
integrated へ（--slack-triage-mode two_stage で復帰可）。勝因はすべて「2 次審査が実在の
決定・AI を握り潰すのを統合版が回避」— consensus 廃止と同じ「後段の審査が正解を殺す」病理。
item-bearing スレッドの LLM 呼び出しが 2→1 に半減。狙い撃ちサンプリングは knowledge_ab の
--item-bearing として恒久化（pm.db source_ref 起点、report は sampling 層別）。

## 2026-07-26 Slack 抽出の consensus 既定も 3→1 — 多数決が実在アイテムを握り潰す実例を確認

**背景**: 議事録側の N=1 化に続き Slack 抽出側を実測。ランダム 15 スレッドでは両構成とも
全件ゼロ抽出で完全一致（= 大多数のスレッドで N=3 の 3 倍コストが無意味。「Slack は重要決定の
場でない」という運用実態どおり）。過去に items を生んだスレッド狙い撃ち 10 件では
consensus1 勝ち 2・tie 7・parse_failed 1（勝ち+引き分け 100%）。
**決定**: 既定を 1 に変更（--slack-consensus 3 で復帰可）。決定打は、consensus3 が
実在する決定 2 件+AI 1 件を**多数決で 0 件に握り潰し**、consensus1 は正しく抽出した実例
— Stage 3 空応答バグと同族の「集約が正解を殺す」病理で、gemma4 の出力揺れ対策だった
多数決は glm-5.2 では品質を下げる方向に働くことが確定的になった。

## 2026-07-26 パッケージC前半: today/draft 全文脈化・consensus N=1・議事録 Stage 1 スキップ

**背景**: gemma4 残滓の近代化。today/draft は brief/risk 全文脈化（07-23）の続き（チャンネル 2 万字
切り捨ての解消、CLI --today-only 経路も追随）。議事録 consensus N=3 は導入根拠（gemma4 の出力揺れ）が
失効しており、実会議の盲検 A/B（順序スワップ 2/2 一致）で N=1 が品質同等以上・決定+1 件・
LLM 21→3 呼・3m41s→42s を確認して既定を N=1 へ。
**決定**: chunk-minutes は当初案の「90 分固定」ではなく **1 チャンクに収まる会議は Stage 1 自体を
スキップして全文を Stage 2/3 へ投入**（Opus レビューが「全文→800字要約→展開」の情報ボトルネックを
指摘、実測でも生成物が最も充実: 決定 4 件・10.8KB・LLM 2 呼・64 秒）。90 分超の長会議のみ
Stage 1 が残り、字数目標とタイムアウトをチャンク長に比例スケール。combined 全滅時は空議事録を
防ぐため非ゼロ終了。旧構成は --chunk-minutes 10 --consensus 3 で再現可（sh にパススルー追加）。
**限界の記録**: A/B は 1 会議（68 分 Leader）× 判事 2 票のみ。90 分超会議での品質は未実測（PLAN 観察）。

## 2026-07-25 休眠パス+gemma4残滓の全体監査とパッケージA実施 — 「実装済みだが動かない」を系統的に掃除

**背景**: re-rank no-op の発見を受け「同型の休眠バグ」と「gemma4 前提の設計残滓」を全 scripts/ で
監査（vulture + 呼び出し元全数 grep）。休眠パス 15 件・gemma4 残滓 11 系統を特定
（一覧は docs/audit_20260724.md、対応は A〜D パッケージに分割し PLAN 管理）。
**決定（パッケージA=即日低リスク実施分）**: --dry-run の no-op 実装（付けても DB に書く危険バグ）、
think が rivault ルートで伝播しない件の明示化（実挙動: kimi 系=常時 thinking、非 kimi=disabled
強制 — 当初の「常時 thinking」認識はレビューで訂正）、reasoning 系で空応答になる小 max_tokens
8 箇所の 4096 化（_rewrite_query 含む）、議事録 Stage 1 の 1024 分岐撤廃（Stage 3 修正と同型）、
LOCAL_OCR_ prefix 欠落修正（web デーモン経由の図 OCR 無言スキップ解消）、要約系の暴走ガード、
docs の LLM 記述近代化（gemma4 優先/OPENAI_API_BASE/re-rank無効の記述は実装と真逆だった）。
**残**: B=Patrol 3 検出器の有効化（PM 判断）、C=評価駆動の近代化（consensus N=3 ほか）、D=小粒清掃。

## 2026-07-24 LLM re-rank を修理し既定有効化 — investigate hit@5 最大3倍、extraction A/B 92.9%

**背景**: rerank_chunks が本番全経路（investigate の search_text / Slack 抽出）で no-op と判明
（openai_base 未配線）。さらに評価側 _stage_rerank も top_k=50 の早期 return で LLM が呼ばれず、
max_tokens=30 も選抜に不足 — 「測定器ごと壊れていた」状態だった。
**決定**: 測定器を修理（top10 選抜+残候補後置の安定並べ替え）した上で両側を実測 —
recall_eval（gold 28クエリ）: literal hit@5 0.231→0.692（3倍）・topic 0→0.286・hit@30/60 悪化ゼロ・
MRR 6倍。knowledge_ab（実スレッド30件）: rerank 24勝/2敗/2分（92.9%）。両側合格により
**既定有効**（退避は `ARGUS_DISABLE_LLM_RERANK=1`）。コストは検索1回 +LLM 1呼（2秒級）。
**留意**: rivault フォールバック先が Kimi-K2-Thinking の場合 thinking がトークンを食うため
max_tokens=4096 固定（上限であり非 thinking モデルの消費は不変）・timeout 30s に設定。
rerank 失敗時は従来の先頭切りへ静かに退化する（クラッシュしない）設計。

## 2026-07-24 Slack 抽出ナレッジ検索の第一段を LLM 化 — A/B 90% で既定有効ロールアウト

**背景**: 旧「同種バグ調査」の最後の残件。第一段が SudachiPy 出現順先頭 15 名詞のため、
長スレッドで冒頭の雑談名詞が検索スロットを食い複数話題が混線していた。精査で
retrieve_chunks_hyde が既に extract_search_keywords（質問向け LLM rewrite）を無条件に
呼んでいる二重構造も判明。
**決定**: extract_topic_keywords_llm（スレッド向けプロンプト・head+tail 4000字切り詰め・
失敗時 SudachiPy フォールバック）を新設し、成功時は下流の質問向け rewrite をスキップ
（総 LLM 呼び出し数は現状維持の 2 回）。実スレッド 30 件の A/B（judge=Kimi、順序スワップ付き）
で **LLM 版 18 勝 / SudachiPy 3 勝 / 引き分け 9（勝ち+引き分け 90%）** → 既定有効で投入。
退避は `ARGUS_DISABLE_LLM_KEYWORDS=1`（LLM 全断時は劣化経路で呼び出しが 3 回に増えるため、
不調が続く場合はこの env で止める）。共有関数 extract_topic_keywords は他 4 箇所が使うため不変更。
**副産物・留意**: (1) extraction 経路の rerank_chunks は openai_base 未指定で no-op
（BM25+鮮度順の先頭切り）と判明 — 将来課題。(2) 評価は scripts/eval/knowledge_ab.py
（gitignore 許可リスト追加）。eval JSONL にはキーワード（人名を含みうる）が残るため
data/eval/ のローカル限定運用を維持。(3) judge の Kimi は max_tokens 512 だと think で
使い切り全件 parse_failed になる罠を再確認（既定 4096 に修正）。

## 2026-07-24 日程調整 Agent・RiVault embedding バグ報告・Web UI 認証を PM 判断で取り下げ

**背景**: 日程調整 Agent（/argus-schedule、2026-05-26 起票・Modal 案まで検討済み）、
RiVault embedding バグの運用者報告（実害は 2026-07-20 のローカルサービング移行で解消済み）、
Web UI 認証・ログインノード移設（Slack OAuth 設計済み・情報セキュリティ部門確認待ち）の
3 件が PLAN に残っていた。
**決定**: PM 判断で 3 件とも取り下げ（2026-07-24）。日程調整の UI 検討履歴、RiVault 再現材料
（scripts/eval/embedding_duplicate_repro.py）、Web UI 認証の設計
（~/.claude/plans/pm-db-slack-web-hazy-dewdrop.md）は再開する場合に備えて残置。
Web UI は引き続き SSH ポートフォワーディング経由・認証なしの現行運用を継続する。

## 2026-07-24 Docling 統合と Argus 垂直軸の観察完了 — PM 体感確認で問題なしと判定しクローズ

**背景**: 両計画とも本体は完了済みで観察のみ残っていた（Docling: 夜間バッチ確認済み +
検索品質の体感待ち／垂直軸: /argus-direction の実 Slack 投稿確認待ち）。
**決定**: PM が体感確認して問題なしと判定（2026-07-24）。PLAN から両エントリを削除。
垂直軸の R3 構想（argus-transcribe の決定捕捉拡張）のみ保留構想として存続。

## 2026-07-24 自動クローズ×XLSX逆同期の衝突を発見・復元 — 巻き戻し33件、恒久対策は再エクスポート

**背景**: 自動クローズは 7/22 の config 投入で観察フェーズを経ず本番化しており（64 件クローズ、
判定品質は note サンプル確認で妥当）、うち **33 件が pm_xlsx_sync に巻き戻されていた**。原因は
クローズ前に出力された古いシートの open 値による last-writer-wins 上書き（人間の差し戻しではないと
PM 確認済み）。patrol の冪等性により再判定されず、証拠 note ごと恒久消失する構造だった。
**決定**: (1) audit_log から 33 件を note 込みで復元（source=restore_autoclose_20260724）、
(2) 恒久対策は「自動クローズ後に patrol が XLSX を再エクスポート」— closed 行はシートから
消えるため構造的に巻き戻し不能になる。xlsx_sync 側の防御（案b）・auto_close 停止（案c）は不採用。
**副産物**: pm_minutes_publish の楽観ロックが「アップロード後に時刻比較」で無効（偽陽性 Skipping
表示のみ・ロールバック無し）だったのを「アップロード前比較で中止」に修正。Box 障害時に
Stage 1（pm.db 同期）を巻き添えにしない縮退も追加（Opus レビュー指摘）。

## 2026-07-24 achievements ledger の運用整備 4 件を消化 — 週次 cron 化と抽出時 title 正規化

**背景**: PLAN 起票（07-16）の 4 件のうち 2 件は点検で解決済みと判明（rejected 再提案抑止は
known_titles の status フィルタ撤廃で対応済み・rejected 現存 0 件、evidence_ref の出典ラベルは
実測 0/53 が日付のみ）。実残は per-app commit と title のアプリ名重複（16/53 件）のみだった。
**決定**: per-app commit 化（途中失敗時の全損防止）、抽出時の `_strip_app_name_prefix` 正規化
（既存 confirmed 行は人間承認済み台帳のため不変更）、populate は pm_box_update.sh 内で
**月曜のみ週次実行**（embed 後に依存、`ACHIEVEMENTS_WEEKLY=0` で無効化）。毎晩は実績の発生
頻度に対し LLM コスト過剰と判断。AppDelta 1 アプリの dry-run で e2e 動作確認済み。

## 2026-07-24 「V4-Flash 切替の follow-up」計画を obsolete としてクローズ — glm-5.2 移行で前提消滅

**背景**: 2026-06-05 起票の残課題 3 件を点検。(1) Pass1 抽出の call_local_llm 直叩きは
現行 slack.py で全経路 call_argus_llm 化済み、(2) think 再検証は 2026-07-18 に glm-5.2 で
A/B 実測済み（investigate think=False 既定で決着）、(3) gpu_memory_utilization 0.5→0.8 は
チャット系 LLM が RIKYU リモート glm-5.2 へ移行しローカル vLLM が bge-m3 のみとなったため
前提消滅（GPU 逼迫なし、WhisperX ピーク 7.1GB/121GB）。
**決定**: 3 件とも解消済み/前提消滅のため PLAN から削除。V4-Flash 固有のチューニング知見
（AI 過剰抽出対策の個数上限等）はコードに残置で問題なし。

## 2026-07-24 enrich の恒久未回収を backfill 機構で解消 — 「同種バグ調査」残課題 3 件を決着

**背景**: PLAN 保留構想「argus-investigate と同種バグ（途中結果の静かな破棄）」の残 3 容疑を
実測で決着。slack.py 単発抽出の ValueError は呼び出しループの guard + 未抽出マーカー経由の
次回リトライで既に緩和済み（本番は consensus=3 で当該分岐を通らない）。HyDE 過剰展開は
recall ハーネスでの回帰測定が必要なため保留継続（PLAN に単独項目化）。
**決定**: 実害があったのは enrich のみ — 自動エンリッチが新規 ID 範囲しか見ず、LLM 失敗行が
恒久放置（glm-5.2 移行後 81 件滞留を実測）。`related_ids IS NULL` を未エンリッチマーカーとし、
pm_ingest の自動エンリッチに古い順・上限 10 件/回の backfill を相乗り + `--backfill N` CLI を追加。
5/28 のボツ案（失敗時の即時リトライ）は踏襲して不採用。バックログ 81 件は全回収済み（失敗 0）。

## 2026-07-24 WhisperX を既定転写エンジンへ切替 — 「8倍遅い」は計測誤診、話者名寄せを決定論化

**背景**: 7/6 エントリの「ctranslate2 未対応で転写 8 倍遅い」は誤診と判明。遅いのはプロセス
初回推論のウォームアップ約 3 分のみ（cuBLAS/cuDNN 初期化、JIT キャッシュでは消えない）で、
定常転写は実時間の 12 倍速・68 分会議 13.5 分＝旧エンジン同等。5 分クリップ×新プロセスの
ベンチはウォームアップに支配されるため今後のエンジン判断に使わないこと。
**決定**: `WHISPER_ENGINE` 既定を whisperx に切替（rollback は env で transformers）。同時に
reconcile の話者帰属を LLM 任せから決定論化（SPEAKER_XX×VTT 時間重なり多数決 + 正規表記
テーブル注入）し、68 分会議で表記ゆれ 17 変種 → 0 件を実証。LLM は未確定クラスタの推測のみ担当。
**影響**: P8.1（話者同定）の主要因を解消。HF トークンは SINGULARITYENV 経由（argv 非露出）。

## 2026-07-24 議事録 Stage 3 の空応答ガード追加 — 投票通過クラスタが「（なし）」に化けるバグ

**背景**: WhisperX e2e デモでアクションアイテム 0 件が発生。4 条件切り分けで、トリアージでも
VTT 肥大でもなく、`_consensus_stage3()` の集約 LLM（glm-5.2 think=True）が確率的に本文 0 文字を
返すのに例外時フォールバックしか無いことが真因と特定（投票通過 8 クラスタが全損した実例）。
**決定**: 集約結果がパース 0 件なら LLM 不使用フォールバック（クラスタ代表 bullet/行）で代替。
正当な 0 件（サンプル不抽出・投票不通過）の経路は不変。実データで再発時に 8 件救出を確認。

## 2026-07-24 Canvas 更新を全文 replace 方式へ移行（表の残骸・前日コンテンツ残存の解消）

**背景**: brief/risk Canvas に旧コンテンツ（前日見出し・id なし `<table>` 10 個）が残存。原因は
post_to_canvas の「全セクション 8 並列削除 → insert_at_start」方式で、(1) 並列削除が
`canvas_editing_locked` で大量失敗（Canvas は同時編集ロック）、(2) id なし `<table>` は
セクション API で削除不可、の 2 点が構造的。
**決定**: `canvases.edit` の `operation:replace`（section_id なし = 文書全体を 1 呼び出しで
アトミック置換）へ移行。使い捨て Canvas で検証後に本番適用し、両 Canvas クリーン化を確認。
replace が稀にセクションを取り残す事象を 1 件観測（ロック障害を経た個体）→ replace 後に
旧 ID の生存確認 + 逐次削除の自己修復ステップを追加。旧方式は replace 失敗時の
フォールバックとして温存。知見は docs/canvas_api.md と slack-canvas-api Skill に反映済み。

## 2026-07-23 brief/risk を全文脈 single-shot に統一（PM 判断で本番適用）

**背景**: 下記 A/B の結果、期間サマリー型（brief/risk）は fullctx が互角以上（risk 明確勝ち・
brief 4対5 僅差）。PM 判断で brief/risk のみ全文脈化を決定。
**決定**: 系統 A（Slack /argus-brief・/argus-risk）と系統 B（cron pm_argus_daily.sh →
--brief-to-canvas/--risk。**旧 orchestrator-worker が実は生き残っていた**）を共有関数
`generate_brief_report`/`generate_risk_report`（全文脈 single-shot、budget 350k 字、
`ARGUS_FULLCTX_CHAR_BUDGET` で調整可）に統一。worker/orchestrator 実装（2 関数 + prompts 9 定数）
は削除。失敗時は従来切り詰めプロンプトへ 1 回自動フォールバック、`ARGUS_DISABLE_FULLCTX=1` で
常時従来方式。investigate は検索型が優位のため不変。
**適用直後の調整**: e2e で glm-5.2 の反復退化（成功応答のままゴミ）を確認 → 退化ガード
（同一文字100連続/final_answer未クローズ/有効文字率<50% でフォールバック）を追加。ユーザー FB
「長すぎる・画面1枚でないと読まれない」→ 指示部を 2,000 字以内・上位3〜5項目に変更、
max_tokens 32768→8192。短縮後 e2e: brief 2,069字/23s・risk 1,230字/16s、全文脈経路で退化なし。
**影響**: qa デーモン再起動が必要（Slack 経路、22:24 実施済み）。cron は次回実行から自動反映。

## 2026-07-23 全文脈投入（OpenWebUI 風）vs 現行 RAG の盲検 A/B — 現行 10 勝 4 敗で現行優位

**背景**: gemma4 前提の「検索 top-5 + 400字切り詰め」を glm-5.2 移行後も継続中。OpenWebUI の
「文脈丸ごと」の体感品質を受け、期間 30 日の全データ（Slack 全ログ+議事録全文+decisions/actions
全件+Box、33.7 万字 ≈ 17.1 万 tok）を 1 プロンプト投入する方式を盲検比較（argus_ab.py 拡張、
本番無変更・`data/eval/ctx_ab.db`）。
**結果**: Kimi 盲検 14 ペアで current 10 勝 / fullctx 4 勝。ただし**構造的非対称**あり — fullctx は
30 日窓の外（2026-05 の Yamaura boxnote 等）を見られず、窓外に正解がある質問（gold 由来の過去事実
QA）は原理的に不利。窓内が正解の質問と risk では fullctx が勝ち、brief も 4 対 5 の僅差。
**知見**: (1) RIKYU 長文プレフィルは日中スタックし得る（20 分無応答）が、同一コンテキスト共有なら
vLLM prefix cache で 2 件目以降数秒。(2) temperature 0.3 + 32k 生成で「!!!!」反復退化 → 本番同値の
0.8 で解消。(3) Kimi judge は max_tokens 2048 では think で尽き parse 失敗 50% → 8192 で全件成功。
**評価の限界**: judge は原データ非提示のため factual 軸は不完全（gold 目視で補完）。
**判断**: アーカイブ横断 QA（investigate）は検索型が優位。全文脈投入は「期間サマリー型」
（brief/risk/today）への適用が有望で、採否は PLAN.md の残項目として検討。

## 2026-07-23 Docling 統合 — Box 抽出品質の底上げと見出し考慮チャンク分割

**背景**: OpenWebUI RAG（Docling + bge-m3）の高品質を確認。embedding・チャンクは Argus と同等で、
品質差の主因は Docling の抽出（表構造化・レイアウト・OCR）と特定。
**決定**: pm_box_crawl に `DOCLING_SERVE_URL` ゲートの Docling 経路を追加（失敗時は既存経路へ自動
フォールバック、pptx は Docling 本文＋既存マルチモーダル OCR の図言語化を併用）。docling-serve は
pm_daemon 管理（port 5001、`venv:` 記法）に一本化し tmux 手動運用を廃止。pm_embed に
`split_into_chunks_by_heading`（P2 の議題単位分割＋見出しパス付与）を実装し全件再索引。
**捨てた案**: OCR=rapidocr（docling 同梱モデルが中国語/英語/ラテンのみで日本語かな非対応と実機確認
→ easyocr ja,en に変更）。noise 含む全件 --force 再変換（1660 件は索引対象外で無駄が大きいと
ユーザー判断 → non-noise 208 件の個別再変換に切替）。
**結果**: non-noise pdf/docx/pptx の 96%（267/279）が docling 系 method、図言語化セクション入り
チャンク 3,109 件、索引 27,271 チャンク（見出しプレフィックス付与済み）。権限制限 PDF は pikepdf
空パスワード復号の前処理で対応（poppler のみ読める特殊暗号化 PDF 8 件は pdftotext 残置が正）。

## 2026-07-22 terminology 辞書を 1216→72 語に浄化、slide_ocr 抽出に LLM フィルタ追加

**背景**: `slide_ocr.extract_terminology()` が正規表現のみ（大文字語・カタカナ4文字以上）で抽出
するため、一般語（THE / README / アプリケーション等）や OCR 誤認識（NVDIA / NIKEN 等）が録音処理の
たびに pm.db terminology へ流入。一般語ほど frequency が伸び、Whisper initial_prompt の 224 トークン
枠を占有していた。
**決定**: プロジェクト固有語のみ残す keep リストで一括削除（バックアップ:
`data/terminology_backup_20260722.csv`）。再流入防止として slide_ocr に `filter_terminology_llm()`
（call_argus_llm 経由、hallucination ガード付き）を追加。判断基準は「LLM/Whisper が通常認識できる
語は登録不要」。
**留意**: LLM ルート（rivault/local）不達時は fail-closed で当該回のスライド用語が空になる
（DB 由来の initial_prompt は別経路で維持）。escape hatch は `--no-llm-filter`。

## 2026-07-22 Patrol に方針転換（obsolete）検出器を追加、証拠検索を2クエリ化

**背景**: AI#2983（Megatron-DeepSpeed の Benchpark 統合）が「やらない方針に転換」で実質決着して
いたが、完了検出は完了の証拠しか見ないため拾えない類型と判明。
**決定**: 新検出器 `obsolete` を追加（qa_index 証拠→LLM が方針転換を判定→クローズ確認DMのみ、
自動クローズなし。1巡回 LLM 50件・DM 5件上限、NOT判定は7日再チェック）。あわせて
`_get_activity_evidence` を「本文そのまま＋キーワード抽出」の2クエリマージに改善 —
キーワード分解は "Megatron-DeepSpeed" 等の固有名詞句を壊し決定的証拠が圏外になる実測に基づく
（完了検出も同ヘルパで恩恵）。実データ先頭50件で誤検出0を確認。
**限界**: #2983 自体は改善後も両検出器で NO — 明示的な転換発言（6/11 Slack）がアイテム本文と
語彙が重ならず検索到達不能、到達可能な 6/18 議事録だけでは LLM が慎重側に判定。個別ケースへの
過剰適合を避けここで線引きし、#2983 は手動クローズとする。

## 2026-07-21 Patrol 運用開始: リマインダー3種停止・完了検出を横断証拠で本格化

**背景**: Patrol 初回実行で期限超過58件＋停滞335件のリマインダー DM が洪水化。一方、本来の主眼で
ある完了シグナル検出は 0 件 — 段階ロールアウト用の `evidence_from_index` が既定 false のままで、
証拠が同一 Slack スレッドの返信に限定されていたのが原因。
**決定**: overdue/deadline/stale の3検出器を無効化（ルールベースの督促は現運用では雑音と判断）、
`completion_detection.evidence_from_index: true` で qa_index（議事録・Box・Slack全体）横断の
LLM 完了判定を有効化。DM は全て `patrol.dm_redirect_user`（新設、actions.py）で管理者に集約。
dry-run で16〜25件を確信度 HIGH で検出、根拠妥当を確認。LLM 判定は temperature で件数が
実行ごとに±9件揺れるが、承認ボタンゲートがあるため実害なし。`auto_close_enabled` は品質確認後に
true 化する段階導入とした。cron ラッパーは `scripts/bin/pm_argus_patrol.sh`（flock 付き）。
**追記**: 承認ボタン押下が Slack 警告マークで失敗する不具合を修正。原因は pm_qa_server が
PatrolState（sqlite3 接続内包）を起動時スレッドで1個共有していたこと — Bolt ハンドラは別スレッド
実行のため sqlite3 のスレッド制約で ack 前に例外。押下ごとに開閉する方式へ変更（qa 再起動で反映）。
ボタン承認クローズは pm.db 直更新のため Web UI は再読込のみで反映、Box XLSX / Canvas は対象外
（必要なら handle_approve_close に xlsx publish ジョブ投入を追加する）。
**追記2**: auto_close 有効化後「LOW だけが DM で届く」ため判定が厳しく見える（実分布は HIGH 81%）。
LOW 6件を精査し、成果物の存在が確認できるのに完了宣言が無いだけのケース（アンケート集計済み・
報告書記載済み）が LOW に落ちていたため、確信度基準に「成果物そのものの存在が直接確認できる→HIGH」
を追加（detect.py プロンプト）。推測・部分完了は従来どおり LOW。合成ケースで昇格/棄却を検証済み。
さらに YES/NO 判定側も「明確な報告がある場合のみ」が成果物存在型を弾いていたため、
「(1) 明確な完了報告 または (2) 成果物そのものの存在確認」の2条件に緩和（言及のみ→NO は維持）。
**追記3**: AI#2987（共有Spack構築・配置）を教材に完了条件 (3)「稼働・利用記録の確認→LOW」と
「継続的役割を含む複合タスクは作業部分の完了で可」を追加。ただし #2987 自体は corpus に
富岳以外（AI4SS/R-CCSクラウド）への配置証拠が存在せず（未来形の言及のみ）、LLM の NO は
証拠に忠実な判定と確認 → 手動クローズが正解。**記録されていない完了はどう基準を緩めても
正直には拾えない**が本システムの原理的限界（緩めれば推測クローズの誤爆と引き換え）。

## 2026-07-21 GitHub（Issues/Projects/Actions）のPM運用導入はボツ

**背景**: アクションアイテムの Issue トラックや Actions/Projects 活用、AI 自動化との親和性を検討。
**決定**: 導入見送り。理由は (1) ステークホルダー（理研/富士通/NVIDIA 意思決定層）が GitHub 常用者で
なく「Slack/会議から自動吸い上げる」現設計思想に逆行、(2) pm.db 正本に対し Issues 双方向同期という
3系統目のカスケードが増え不整合の温床、(3) 機密データ（マイルストーン・体制）を SaaS に置けず
ガバナンス上の壁が高い。開発管理用途（Issues + gh CLI）のみ将来の再検討余地あり。

## 2026-07-21 exec summary 完了列: 尻切れ真因はデータ切り詰め、選抜は LLM 凝縮へ

**背景**: NVIDIA協業 PPTX の完了列で日付が尻切れ。描画側（TEXT_TO_FIT_SHAPE→spAutoFit）を
2段疑って修正したが再発し、PPTX 内テキストの直接検査で真因は `_MAX_CHARS=30` の**データ切り詰め**
（台帳 title は日付込み≒50字）と確定。レンダリング画像の「…」をクリップと誤読したのが遠回りの原因で、
以後この種の調査は**先に XML/生テキストを検査**する。あわせて完了列が古い実績ばかりになる問題も発覚
（`ORDER BY achieved_on LIMIT 5` 昇順＝最古5件固定）。
**決定**: 上限を60字に緩和（暴走ガードとして残置）＋noAutofit明示＋全列8pt。選抜は「直近5件」案を
実装後に撤回（古い重要合意が落ちるため）し、**confirmed 全件を LLM で5件に凝縮**する方式を採用
（`condense_confirmed_titles`、失敗時は直近5件フォールバック）。Q-Nova 10→4件・AppDelta 10→5件で
最古実績の保持と関連合意の統合を実測確認。
**影響**: 実績タブの「達成日空欄」報告も同根で調査 → DB/API は正常、ag-Grid v31+ の型自動推論が
月精度値（YYYY-MM）を dateString として不正扱いする**フロント表示バグ**と特定し `cellDataType:'text'`
で修正。日付系グリッド列を追加する際は型明示を忘れない。

## 2026-07-21 core docx にも図OCR適用＋Box relevance の大量 noise 化で索引を1/7に

**背景**: Box資料の relevance を精査し core だった大半を noise へ再判定（CSV `final_relevance` 上書き
→ `--import`）。`pm_embed.py` は noise のみ索引除外するため full-rebuild で box由来 1954→294文書、
qa_index 全体で noise漏れ0・取り込み漏れ0 を照合。狙いは投入ノイズを削り core/related の本質資料を
ヒットさせること。ただし残った core を点検すると、図OCR（図の言語化）は `_convert_pdf` 専用で、
core の約半数を占める **docx（61件）は LibreOffice テキスト抽出のみ＝埋め込み図表が索引に入らない**
穴が判明。
**決定**: docx を全ページ multimodal OCR（pptx方式）にはせず、`_to_pdf` で PDF化→既存 `_convert_pdf`
ハイブリッド経路に流す方式を採用。文字が密な仕様書で日本語テキスト精度を落とさず、`pdftotext+figures`
分類・`--figures-pending` バックオフ・リトライを全面再利用できるため。全ページOCR案は精度低下と
コスト増、既存バックフィル機構と非統合のため棄却。`_to_pdf` 恒常失敗時は `figures_attempted=True` を
返しバックオフを効かせる（毎晩の空振り防止）。
**影響**: 既存 core docx は `--figures-pending` で初回バックフィル可（`DOCX_TEXTONLY_METHODS` を対象に
含めた）。トレードオフとして docx 本文が LibreOffice-HTML→pdftotext 抽出に変わり表組みの再現性が
変化しうる。バックフィル後は `pm_embed.py` 差分更新まで検索に反映されない点に注意。

## 2026-07-20 action_items 意味的重複の棚卸し（embedding+LLM）＋Slack再抽出の復活穴を封止

**背景**: 「表現は違うが意図は同じ」action_item が蓄積。既存 `pm_screen.py` は正規化＋先頭一致
のみで意味的重複を取りこぼす。判定方式は純 embedding 単独と2段（embedding＋境界帯LLM審査）を
比較し、bge-m3 のコサインだけでは 0.85〜0.92 帯の言い換え/別言語の取り違えが残るため2段を採用。
**決定**: `detect_semantic_duplicates`（≥0.92 自動同一 / 0.85〜0.92 は `call_argus_llm` バッチ
審査・失敗時保守的に別物 / 境界ペアは上位200件キャップ）を追加。残置は enrich 充実度優先→
`extracted_at`→`id` 降順（＝古い薄い方を削除）。embedding はオンザフライ計算（専用テーブルは
件数増加時まで見送り）。削除は既存の論理削除（`deleted=1`＋audit）に一本化、物理削除は
`related_ids` ダングリングと audit 欠落のため不採用。CLI（`--semantic` 既定off で非破壊）と
Web Quality タブ（screen_for_web に `keep` フラグ）双方に露出。keep 付与は複数カテゴリ跨りの
共有dict変異で壊れたため浅コピーで修正。
**影響**: 削除復活の全経路点検で `slack.py save_slack_items` に穴を発見（`--force` 再抽出で
削除済み content を再INSERT、decisions/action_items 両方）。minutes.py と対称の「削除済み
content 退避→再INSERTスキップ」を移植し封止。通常運用（force なし・XLSX・Canvas・enrich）は
既に安全。過去の decisions 復活主因は `pm_xlsx_sync.py` の空セル→deleted=0 上書き（a9b4bb9 で
修正済み）。

## 2026-07-20 investigate に read_document ツール追加＋map段4並列化（発火はモデル依存の課題残）

**背景**: `--file` 全文読込QA が高品質なため通常 investigate にも波及させたい。全検索の
map-reduce 置換はコーパス規模（core+related 903文書≒740窓＝直列で数時間/質問）で棄却し、
検索で文書を特定→全文読込へエスカレーションするツール型ハイブリッドを採用。
**決定**: `read_document(file, question)` ツールを追加し `run_document_qa` にスコープ委譲。
併せて map 段を ThreadPool(4) 並列化（7窓 直列≒34〜61秒 → 並列≒32秒に短縮を e2e 確認）。
timeout 上限をツール実行側120sより小さい110sにそろえ成果破棄＋オーファンスレッドを防止、
`Semaphore(1)` で全文読込を同時1本に直列化し RIKYU 同時リクエスト増を抑制。
**影響**: `--file` 直指定の全文読込は良好（6アプリ数値付き内訳・並列で高速化）。一方 e2e で
**read_document は一度も発火せず**、glm-5.2 が0件ツール呼び出しに流れて「情報なし」誤答を1件生成。
7/18 に却下した「ツール非準拠は解決しない」懸念が的中した形。実装は「呼ばれれば動く」ことを
`--file` 経由で裏付け済み。数値確実性が要る単一資料は `--file` 直指定が確実、と docs に明記。

## 2026-07-20 検索集中の自動エスカレーションを実装・実データ検証で棄却

**背景**: read_document がモデル依存で発火しない対策として、初期検索が単一 Box 文書に集中
していたら決定論的に全文読込へ切り替える案（`_detect_document_concentration`）を実装。
**決定**: 実データ検証で棄却。(1) 当初の主目的「6アプリ実行時間内訳」は Box コーパスに
類似報告書（月次 Jan/Feb/Mar・基本設計・FY成果報告書等）が10種以上あり、上位が分散して
dominance 0.11（閾値0.6）で発火しない。単一資料に集中する質問でしか効かない。(2) 複数文書
版に拡張しても、決定事項質問（box_ratio 0.76）と6アプリ質問（0.88）を初期検索の集中度で
区別できず誤爆する。(3) 発火しない通常ケースでも毎回 embedding 3回の恒常コストが乗る。
以上より費用対効果が見合わず、未コミットで破棄（`git restore`）。
**影響**: read_document ツール＋map並列化は維持。横断・数値集約が要る質問は `--file` 直指定
（複数資料はカンマ区切り等）に運用を一本化。dedup キーの教訓（box_document は共通ヘッダ
`【folder/filename】`約95字で `content[:80]` が全チャンク同一になり潰れる。集計は chunk id で
dedup すべき）は今後の類似実装で再利用可能。

## 2026-07-18 `--file` QA を検索スコープ方式から全文読込（map-reduce）へ転換

**背景**: 決定論ピン方式は該当資料のチャンクを検索対象に固定するだけで、実際に返るのは
top-5×400字程度の断片。LLM のツール呼び出し非準拠と「文書を読む」ツールの不在が重なり、
「全文が取得できていない」まま回答してしまう事例があった。
**決定**: 1文書スコープの質問は「検索」ではなく「読込」が正しいプリミティブと判断し、
`run_document_qa` を新設して 24,000字窓（map）→統合（reduce）の全文読込QAに転換
（`search_text`系ツール・エージェントループを経由しない独立パス）。`read_document` ツールを
エージェントに追加する案は、モデルのツール非準拠自体が解決しないため却下。
**影響**: 6アプリ実行時間内訳で全アプリの数値到達を e2e 確認。処理限界（400,000字/3ファイル
上限・タイムアウト予算切れ・本文未取得）は回答末尾「## 制限事項」に機械的に明示。

## 2026-07-20 embedding を RiVault から DGX-Spark ローカル vLLM へ移行（衝突バグ解消）

**背景**: RiVault bge-m3 の同一ベクトル返却バグは運用側でしか直せず、被害124件の修復も
エンドポイント修理待ちだった。再現スクリプトでローカル vLLM（同一モデル・同一テキスト）では
衝突しないことを確認し、バグが RiVault サービング層に固有と確定。
**決定**: OpenWebUI/baai.sh（2026-05-18 に gpu-memory-utilization 既定0.9 のメモリ超過だけで
失敗していた）へ 0.10 を明示して ng-dgx-s-07 の tmux "baai"・port 8001 で vLLM サービング。
切替は EMBED_API_BASE/EMBED_MODEL の env のみ（コード無変更）。サービング実装が変わると
ベクトル空間の互換がないため全31,454件を再embedding（ローカルで8分）→ 被害124件も一掃。
**影響**: 真の異常グループ0件・単一ベクトル空間に統一。副産物: 空白差のみのテキストは
トークナイザ正規化で同一ベクトルになるのが正当と実測され、書き込みガードの誤検知を修正。
ノード再起動時は `bash OpenWebUI/baai.sh` の再実行が必要（gemma4 vllm.sh と同じ運用）。
RiVault への報告は任意として PLAN.md に材料を残置。

## 2026-07-18 夜間2パス化で新規core文書の図OCRを完全自動化（図言語化シリーズ完結）

**背景**: 全coreロールアウト後も新規文書は手動 --figures --force が必要で、夜間には
relevance 判定自体が入っていなかった（新規文書が永続的に未判定＝図OCR対象外）。
**決定**: 夜間を「scan+convert→relevance判定→--figures-pending→embed」の4段に。
図なし文書には pdftotext+nofig マーカー（毎晩の再OCR防止、LIKE判定を明示集合に全廃）。
OCR恒常失敗文書は last_figures_attempt_at の7日バックオフで毎晩→週1に上限化
（レビュー指摘。同スタンプが同一夜の二重OCRも自然に防ぐ）。
**影響**: 監督付き初回実行で backlog 74件（pending 49＋当夜のcore昇格分）を23分で処理、
真のpending 0件に収束。relevance未判定88件も解消（core +77）。以後は cron 完全委任。
残る手動作業なし。外れ値1件（2.05M字）はサイズガードで恒久除外。

## 2026-07-18 図言語化OCRの全core一括ロールアウト完了

**背景**: パイロット・ハードニング済みの図言語化OCRを全core PDFへ展開（ユーザー起動）。
**実施**: 256/256件成功（3時間32分、平均49.7秒/件、失敗0。外れ値xlsx由来PDF 1件は図価値低で
意図的除外）。208件に図キャプションが付与され本文+259万字、図チャンク 1,112→3,708。
再embedding 7,378チャンク（12分）。書き込みガードが異常同一ベクトル28件を検出・安全スキップ
（エンドポイント問題のライブ実証、PLAN.md 保留エントリに追記）。
**影響**: 全core報告書の図・グラフが --file なしの通常検索でも取得可能に。core PDF に
source_modified_at 基準が確立し、以後のBox側更新は夜間cronで自動追従（figures維持つき）。
副産物: 起動確認が「try-ALTER の例外型不一致で convert 全クラッシュ」バグ（当日混入）を
検知、cron 被弾前に修正（255baf6）。

## 2026-07-18 全文読込QAに「偽の関連情報なし」ガードを追加（モデル申告を信用しない）

**背景**: --file 全文読込で glm-5.2 が中身のある窓（AppBeta章等）を「関連情報なし」6字・サブ秒で
誤却下（7窓中4窓）し、統合段が「AppDelta/AppBeta は記載なし」と誤回答。同一6字×4窓のパターンは
embedding で確認済みのエンドポイントキャッシュ衝突の chat 版の疑いもある。
**決定**: モデルの「なし」申告を信用せず決定論検証を導入 — 質問中のエンティティが窓本文に
存在するのに却下されたらリトライ（nonce で文面を毎回変えキャッシュも回避）、再失敗は
「## 制限事項」に機械記録して「記載なし」の断言を禁止。エンティティ無し質問には
5,000字以上の窓の却下をリトライするフォールバック。プロンプト強化のみの案は
再発を防げないため却下。
**影響**: 実LLMで誤却下4窓すべて回復を確認。6アプリの定量情報が安定して出力されるように。

## 2026-07-18 investigate STEPループを think=False / 16384 に変更（A/B 9run）

**背景**: glm-5.2 が STEP ループ（think=True/32768）で tool_call を出さず即強制まとめに
落ちる症状。1ステップ135秒・21万字の思考のみで tool_call 未到達の失敗モードを実測。
**決定**: A/B 9run（think ON/OFF × 8k/16k/32k × 3クエリ）で think=True の tool_call 準拠率
18% に対し OFF は 44-65%。OFF/16384 が多エンティティ調査で唯一8ステップ完走・最多の
定量結果（68件）を出したため既定に採用。8192 は速度優位だが粘りで劣後（僅差・要再検証）。
環境変数 ARGUS_STEP_THINK / ARGUS_STEP_MAX_TOKENS で戻せる。
**影響**: gemma4 reasoning 前提だった 2026-05-14 の investigate think 運用は glm-5.2 では
成立しないことが確定。FFB図チャンクの数値未到達は embedding エンドポイント問題
（PLAN.md 保留エントリ）が残存。

## 2026-07-17 Box図言語化OCRの導入と、Box更新検知（鮮度問題）の解消

**背景**: pdftotext でテキストが取れる報告書はグラフ・図が索引に載らず investigate が視覚情報を
認識できなかった。また `--force` なしでは変換済みファイルは Box 更新されても永遠に再変換されない
鮮度問題が潜在していた（図言語化維持の設計中に発覚）。
**決定**: テキスト維持＋図OCR追加方式（ページ全体フル言語化案は本文品質劣化の懸念で却下）、
対象は relevance='core' のみ。更新検知は `source_modified_at` スナップショットの等値比較
（modified_at と extracted_at は TZ/形式が異なり大小比較は誤判定するため却下）。既存行の
NULL 基準は更新扱いにせず初回900件雪崩を防止（基準は --force 時に確立）。figures 済み文書は
cron でも自動維持し、OCR不可時は上書きせずスキップ（黙って剥がれる事故を防ぐ）。
**影響**: 別モデル（Fable）の追いレビューが、ページ対応の silent ズレ・OCR失敗の無音化・
cron 維持不整合の3件を検出し修正。全core一括ロールアウトはコスト大のため保留中（PLAN.md）。

## 2026-07-17 investigate の回答フォーマットを中立化（固定5セクション・コデザイン文脈を撤廃）

**背景**: investigate の system prompt / 強制まとめ prompt は、富岳NEXTのコデザイン評価フォーマット
（結論サマリ/詳細状況/コデザインへの含意/ボトルネック・リスク/仕様決定に向けて不足している情報の
固定5セクション）を流用したもので、あらゆる問いに同じ構成を強制していた。
**決定**: investigate は汎用調査エージェントであり問いの性質は多様なので、LLM が問いに応じて
重要と判断した情報を自由な構成で出力する方針に転換。プロジェクト固有のコデザイン文脈も
system prompt から完全削除し中立化した。terminology/glossary の用語辞書のみ中立見出しで存置。
**影響**: brief/risk（`pm_argus.py`）は今回の対象外で固定フォーマットのまま。

## 2026-07-17 argus-investigate に `--file` 特定ファイル(Box資料)スコープ検索を追加

**背景**: retrieval は各チャンクの `record_id` を SELECT するのに WHERE で未使用のまま放置されており、
embedding 済みの特定 Box 資料「1本だけ」に QA を仕掛ける手段が無かった。
**決定**: retrieval 層に汎用の `record_ids` フィルタ（`c.record_id IN (…)`）を追加し、
box_docs.db でファイル名 → box_file_id を解決。「このファイルだけ」を確実に保証するため
決定論的ピン方式を採用し、ピン時は pm.db/slack.db 系の非doc検索ツール（search_decisions /
search_action_items / get_slack_messages / search_mentions / get_milestone_progress 等9種）を
提示・実行の両面で封鎖した。ツール引数（LLM任せ）のみに委ねる案はスコープ保証にならないため却下。
**影響**: CLI/Slack 双方に `--file` を追加、0件解決時は停止。search_text/hybrid には LLM 用の
`file` 引数も追加（詳細は `pm-argus-commands` スキル参照）。

## 2026-07-16 実績DB（achievements ledger）を新設し「完了」列の検索依存を断つ

**背景**: 前エントリの通り「完了列をinvestigエージェントの都度検索に依存する」設計は run 毎に
薄い/空になるムラがあり（Q-Helix が 0 件になる実測）、根本原因は「過去の完了実績は一度確定
すれば変化しないのに毎回検索し直している」ミスマッチだった。
**決定**: pm.db に per-app の `achievements` テーブルを新設し、確定した実績は検索せず参照する
方式に変更。信頼モデルはハイブリッド（confidence=high→自動confirmed、low→proposedで人間が
Web UIの「実績」タブで検収）とし、全自動（誤り混入リスク）にも全人力（運用負荷）にも寄せなかった。
Box XLSXの「実績」シートは confirmed のみ・表示専用・逆同期なし — Web UI を編集の唯一の正路に
保つため。捨てた案: 完了列をライブ検索のまま維持し続ける案（上記の実測により棄却）。
**影響**: `pm_exec_summary.py` の完了列は「確定実績DB→ライブ検索フォールバック」に縮退、
`/argus` に `get_app_achievements` ツールを追加し investigate が自動参照。本番 pm.db に全6アプリ
39件投入済み。多層 dedup（既存title認識＋run内self-dedup＋embedding類似度0.85＋dedup_key）で
再実行冪等、人間の confirmed/rejected は再実行時に保護される。

## 2026-07-16 エグゼクティブサマリー「完了」列の充実と埋め込み索引の実装

**背景**: `pm_nvidia_collab_update.sh` の executive_summary pptx で「完了したこと」が薄く（一部1件・
手続き的メモ）、1.5年の活動成果に見えなかった。調査の結果、真因は pptx 表示ではなく上流にあった。
**判明した真因**: (1) 埋め込み索引化がハイブリッド検索導入コミット(7c8e4ab, 2026-06-12)から**未実装**で
`chunk_embeddings` が常に空 → 全 /argus 検索が約1ヶ月 trigram FTS のみに退化、(2) recency 重み過大
(0.4 / 半減期180日)で歴史的マイルストーンが synthesis から締め出し、(3) Pass2 の `--since` が直近
4.5ヶ月に限定。
**対応**: `pm_embed.py` に埋め込み索引化を実装（全26,902件構築）、`retrieval.py` の recency を緩和
(0.15 / 365日)、`.sh` の窓を 2025-04-01 に拡張、`pm_exec_summary.py` の完了列を
「recency非適用ハイブリッド検索＋LLM凝縮」の**決定的経路**に変更（next/vendor はレポート由来のまま）。
**捨てた案**: 完了列を investigエージェント出力に依存し続ける案（run 毎に空/薄のムラ、Q-Helix が
0件になる実測で棄却）、グローバル recency 無効化（PM 用途の新しさ優先を壊すため 0.15 の軽い重みに留めた）。
**要注意**: `retrieval.py` の定数変更は稼働中 qa デーモンに未反映 → 反映には `pm_daemon.sh` で qa 再起動。

## 2026-07-14 アクションアイテム自動消化検出（既存 Patrol 完了検出の拡張）

**背景**: 抽出は軌道に乗ったが消化状況の確認が手動。実際はアイテムは日々の活動で消化され会議/Slack で
報告されている。これを自動で突き合わせ済み化したい。既存 `detect_completion_signals`（slack同一
スレッド返信のみで完了検出→承認）がほぼ下敷きだった。
**決定**: 新検出器を並置せず**同関数を拡張**（同一アイテムに複数検出器が別々発火するのと dedup 分散を
避けるため）。対象を meeting にも広げ、証拠源を qa_index ハイブリッド検索（活動報告全般）へ拡大。
確定方式はユーザー選択で**完全自動 close**だが、安全弁として①HIGH確信度のみ自動②`auto_close_enabled`
既定 off の段階投入③note/audit_log(source=argus_auto)/リーダーチャンネル事後通知で可視化・再open可、
とした。**捨てた案**: CSV→pm_relink 経路（承認ボタンがある以上ムダ）、完了日専用列の追加（audit_log.
changed_at ＋ note で足り、Phase2 送り）。
**影響/要注意**: レビューで(a)auto_close無効時に旧承認フローが無音消失する退行→**YESなら無効時/LOW時は
send_completion_confirm へフォールバック**して解消（毎巡回LLM再実行も同時に抑制）、(b)pm.db一括commit
と state.db即時commitのクロスDB非原子性→close直後に`ctx.conn.commit()`してから record_notification、
(c)max_tokens=250 が rivault(Kimi think)で枯渇し判定不発→4096/timeout60 に増、を修正。検証時 glm-5.2 が
過剰に完了判定する傾向を確認、既定 off での観察をロールアウト前提に置いた（PLAN.md 参照）。

## 2026-07-13 非think local 呼び出しの reasoning-truncation を根治（reconcile 0字事故の対策）

**背景**: RIKYU glm-5.2 で録音ジョブが失敗（`reconcile_transcript.py` の VTT×Whisper 突合が
全チャンク0字→transcript を空で上書き→議事録生成が「セグメントが見つかりません」で停止）。
根本原因は「reasoning 既定モデルは非think指定でも内部思考し、低 max_tokens（reconcile=2048 等）を
思考で使い切り content=0字を返す」＝決定事項欠落と同一クラスのバグが別スクリプトで顕在化。
**決定**: 対症療法（各所の max_tokens 増）では地雷が残るため根治。`_call_local_llm_inner` で
**think=False 時に `enable_thinking:false` を送出**し `think` 引数が実際に reasoning を制御するように
（`no_chat_template_kwargs=True` のエスケープは維持）。reasoning が分類品質を上げる決定事項抽出
ステージのみ **think=True** に固定して品質を保持。`reconcile_transcript.py` は出力空時に元の
Whisper 文字起こしを保持し破壊的上書きを防ぐ防御を追加。
**影響**: think=False の全 local 呼び出し（brief/risk/investigate/ingest/議事録）が非reasoning化＝
高速化＋truncation解消（A/B で glm を選んだ非think条件と一致）。`llm.py` はコア共有のため反映には
qa デーモン再起動が必要。実機で think=False・max_tokens=2048 が非空を返すこと確認済み。

## 2026-07-13 Argus 本番 LLM を RIKYU glm-5.2 へ切替、議事録の決定/アクション欠落を修正

**背景**: RIKYU（新 OpenAI 互換サービング）の3モデルを A/B 評価（`argus_ab.py` に `--target rikyu` 追加、
中立ジャッジ DeepSeek-V4-Flash）した結果、glm-5.2 が総合品質最高（4.78/5）で採用。DeepSeek-V4-Flash
との直接対決（中立ジャッジ Kimi）でも品質・速度とも glm 優位（詳細 `docs/decisions/rikyu_argus_model_eval.md`）。
**決定**: routing_priority を local 優先に、`LOCAL_LLM_MODEL=glm-5.2` へ。議事録生成で決定事項/アクション
アイテムが消える不具合を修正 — 根本原因は「reasoning 既定モデルは非think指定でも内部思考し、
decisions 抽出の max_tokens=1024 を思考で使い切り content が0文字→セクション消失」。max_tokens を full 化、
空ガード追加、アクションアイテム規約の矛盾（担当不明時の扱い）を解消し（未定）で列挙するよう明確化。
OCR は非マルチモーダルな glm-5.2 を避けるため `LOCAL_OCR_MODEL`（qwen3.6-35b 等）で分離（`pm_box_crawl.py`）。
**影響**: brief/risk/investigate は glm が既定 reasoning するため latency 増（品質は良化）。`enable_thinking:false`
を非think local へ送る一般化は今回見送り（`llm.py` 未変更）。`argus_ab.py` は gitignore 対象でローカルのみ。

## 2026-07-13 recall 評価ハーネス baseline-v1 記録 — vocab-gap を定量化

**背景**: `scripts/eval/recall_eval.py`（recall 回帰ハーネス）のゴールドを 14 エントリに拡充
（エンティティ9種、source_type: minutes 5/slack 5/box 4、主題分散）し、baseline を測定。
**結果（run_id=3, gold sha256 66999e0f, chunks 26590）**: literal クエリ（文書語彙）は
hit@10≈0.54/hit@60≈0.69 と索引は健全な一方、**topic クエリ（主題語彙＝investigate rewrite が
出す語彙）は hit@10≈0.07〜0.12** と 4〜7 倍低い。「事実は DB にあるのに主題語彙では拾えない」
vocab-gap recall 欠陥が定量化された。fts と hybrid はほぼ同値、hyde/rerank も topic を大きく
改善しない（rerank は openai_base="" で実質 hyde 先頭[:5]）。
**決定**: 以後の recall/precision 改善（共起語拡張・source_type 多様化・rerank 再有効化等）は
本 baseline との Δ（特に topic hit@k）で合否判定する。**注意**: 結果DB `data/eval/recall_eval.db`
は git 管理外のためローカルのみ。再現は gold sha256 で同一性を担保する。

## 2026-07-13 investigate 出力への INFO ログ stdout 混入を修正

**背景**: `terminology.py` / `glossary.py` の診断 print が stdout に出ており、investigate の
stdout をそのまま Box レポートにするバッチ（`pm_nvidia_collab_update.sh`）で公開文書冒頭に
`[INFO] terminology/glossary` 行が混入していた。**決定**: 両ローダーの print を `file=sys.stderr`
へ変更（文言・件数は不変、行き先のみ）。**影響**: Box レポートが見出しから始まるようになった。
反映には qa デーモン再起動が必要。PLAN.md の当該残課題（旧項目6）は解消につき削除。

## 2026-07-13 LLM re-rank が配線漏れで無効化されていた（デッドパス）と判明 — ドキュメントを実態に修正

**背景**: `retrieval.py` の `rerank_chunks()` は `openai_base` が空だと即座に
`chunks[:top_k]` を返す実装だが、呼び出し元 `mcp_tools.py:163`（`search_text`）・
`cli_utils.py:418` のどちらも `openai_base` を渡していない。2026-06-19 の責務別モジュール
分割（`2e3fe68`）で rerank をモジュールへ移設した際、有効化ゲートをモジュールグローバルの
接続設定から引数化したが呼び出し元の更新が漏れ、`openai_base` が単なる ON/OFF フラグとして
形骸化（vestigial）した配線ミス。意図的な無効化ではない。

**決定**: 今回はドキュメント（architecture.md / argus_system.md / argus_outcomes.md）を
実態（re-rank無効・最終順位は `_combined_score` = BM25 0.6 + 鮮度0.4 降順 top-5）に合わせるのみ。
再有効化は構築中の recall/precision 評価ハーネス（`scripts/eval/recall_eval.py`）で
baseline 完成後に before/after を測定してから判断する（今回は再有効化しない）。

**影響**: precision を上げる最終選別段が現状欠落しており、BM25/鮮度スコアが高いだけの
無関係チャンクが上位に残るリスクがある。recall 自体は HyDE 拡張＋ベクトル検索＋鮮度
スコアリングで維持されている。関連コミット `2e3fe68`、根拠 `retrieval.py` L526-527。

## 2026-07-13 investigate/メンション応答に初期 retrieval シードを既定追加（検索0件で断定する問題の是正）

**背景**: investigate 実走検証で、Pass2（`--context-file` 注入時）に DeepSeek が STEP1 で
ツール呼び出し0件のまま単発生成し、検索せずに具体名・数値を断定する挙動を確認。DB 照合では
今回は幻覚0件だったが「たまたま内部知識が実在と一致した」だけで、検索省略のプロセス欠陥は残存
（かつ AppTheta 完了・EEA 成熟度など DB にある最新情報を取りこぼしていた）。

**決定**: `run_agent()` のループ開始前に、rewrite が生成した検索クエリ上位3件を既存 `search_text`
経由で事前実行し history に投入する「初期 retrieval シード」を**既定ON**で追加
（`ARGUS_DISABLE_INITIAL_SEARCH=1` で opt-out）。opt-in 案も検討したが、接地品質を優先し
investigate・メンション応答（`run_agent` 共有の全経路）で既定有効化する判断。Q-Helix で
再走し、出典引用付き・未確認事項の明示・より新しい事実の捕捉へ改善を確認（latency 2m→5m 程度増）。

**影響**: 全 investigate/メンション応答に事前3クエリ検索(HyDE+rerank)が1ラウンド加わる（並列・
120s上限）。シードは try/except で握りつぶし、失敗時は従来挙動にフォールバック。patrol は
run_agent 不使用で影響なし。**反映には qa デーモン再起動が必要**。残課題: 強制版でも AppTheta を
「確認できなかった」と留保する retrieval recall の取りこぼし、INFO ログが stdout に漏れ Box
レポート先頭に混入する既存バグ（別途）。検証詳細は `docs/decisions/rivault_model_eval_2026-07.md`。

## 2026-07-13 Argus 主力LLM は全用途 DeepSeek-V4-Flash 単独運用に確定（Qwen 見送り）

**背景**: 2026-07-11 評価では対話系→Qwen3.6-35B-A3B-FP8 / 集約分析→DeepSeek の
ハイブリッドを推奨としていた。これを詰めるため実際の `pm_argus_agent.py --investigate`
（マルチステップ tool-call ループ）で Q-Helix を2パス実走し両モデルを比較
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

**[2026-07-13 追記・証拠強度の是正]** 上記「質は DeepSeek がわずかに優勢（15-12）」は
証拠強度の過大申告だった（Fable 5 監査指摘）。単一 judge・tie 0件・swap 19:8 偏りという
手法限界を踏まえると、15勝12敗（n=27, 二項検定 p≈0.7）は**有意差なし**であり、序列ではなく
「同等」と読むべき。推奨（用途別ハイブリッド）自体は後続の 2026-07-13 エントリで DeepSeek
単独運用に上書き済みのため運用への実害なし。詳細は `rivault_model_eval_2026-07.md` の
「手法上の限界」節。

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

**背景**: 直前のエントリの時点で、G-NS（最上位目標）の出所のうち「松岡センター長のAI活用に関する
最上位目標の指令」は一次情報が未確認のまま残っていた。ユーザーがMEXT・松岡センター長・
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

**影響**: 実データ（AppDelta/AppEpsilon）での動作確認済み。分類ゲートが証拠なき「完了」を
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
