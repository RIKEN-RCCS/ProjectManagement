# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

### セキュリティ対策（docs/security-architecture.md）— 流出面を優先

**方針**: PM 判断により、改竄（R5）より**流出**を優先。改竄向けの項目（出自の独立した
第2系統・引用スパン照合・二段抽出・挙動指紋）は保留。

**完了**: ツール allow-list（`COMMAND_TOOLS`）/ 外向き通信 allow-list（`net_guard`、既定 warn）
/ Box 共有リンクの `collaborators` 化 / pre-commit による不変条件の固定（lint 4 本）/ 認証境界の棚卸し
（Slack Connect・ゲスト不在、パートナーチャンネルには非投稿、Box 実効 `company`）/
**MCP Server（`pm_mcp_server.py`）の廃止** — チョークポイントは `agent_tools.py` の 1 箇所に集約。

> [!note] Kimi-K3 再開との関係（2026-08-01）
> K3 の6提案のうち**優先度1（APIクライアント層の再設計）はこの節の `tool_calls` /
> `reasoning_traces` と同一コードパス**であり、セキュリティ側の残作業と K3 側の第一歩が
> 一致する。一方**優先度2（視覚入力）・優先度3（長時間自律）は Phase 5（5b）完了がゲート**で、
> R8（改竄の集中リスク）も未着手。**「対策が完了したから K3」ではなく「K3 の本命機能を
> 使うために 5b と R8 をやる」**という順序（設計文書 §6）。

**次にやること**（優先順）:
1. **`config/network_allowlist.yaml` の実値化** — `ingest_plane`（docling `127.0.0.1:5001`）は
   pm_box_update.sh の既定と pm_daemon.sh の bind 両方に一致することを確認済み（確定値）。
   残りは read_plane / write_plane。`net_guard.py --print-env-hosts` で既知分を
   埋める → qa 再起動 → **warn で 1 周**運転 → `cat logs/*.log | net_guard.py --summarize-log -`
   で未知の宛先を `caller=` を確認しながら追加。
   `stage=resolve` / `stage=connect` の両方で deny がゼロになったら `ARGUS_NETGUARD=enforce`。
   **「1 周」の定義**: 月〜金限定の cron があるため**平日 1 周（実質 1 週間）が下限**。
   加えて cron に載っていない経路は手で 1 回ずつ叩く必要がある — qa デーモンの各 `/argus-*` と
   メンション応答、録音パイプライン（`pm_from_recording.sh`）、`/argus-narrate`（TTS。
   `FISH_TTS_HOST` 系はこの経路でしか出ない）、Web UI 経由のジョブ（embed / xlsx / minutes publish）、
   Canvas `--recreate`、Box 共有リンク作成。Patrol は 30 分間隔なので放置で出る。
   **飛ばすと、稀にしか動かない経路が enforce 後に初めて落ちる**（warn 中は何も止まらないので
   「1 周した」の判定はログでしかできない）

   **warn 初日（2026-08-01、qa 再起動後）に出た起動時照合 NG 2 件 — enforce 前に必須で解消する**
   （enforce では `EndpointMismatchError` で**デーモンが起動しない**）:
   - `FISH_TTS_HOST` expected=`127.0.0.1:8080` actual=`localhost:8080` — 綴り不一致
   - `DOCLING_SERVE_URL` expected=`127.0.0.1:5001` actual=`localhost:-` — 綴り不一致 + **ポート無し**
   docling は `pm_box_update.sh` の既定（`http://127.0.0.1:5001`）とは一致するが、
   **qa デーモンの環境では別の値（localhost・ポート無し）が入っている**。つまり
   「127.0.0.1:5001 が確定値」は box crawl 経路についてのみ正しかった。
   対処は (i) allow-list に `localhost` 綴りのエントリを併記するか、(ii) 環境変数側を
   `http://127.0.0.1:5001` に正規化するかのどちらか。**(ii) を推奨**（許可対象が増えないため）
2. ~~**`pm_web_fetch.py` の廃止**~~ → **2026-08-01 完了**（`pm_web_fetch.py` / 旧パス symlink /
   `pm_web_update.sh` を削除。`web_articles.db` は保持し既存チャンクは検索可能のまま。
   復活は `test_web_fetch_scripts_are_gone` が検出）。
   **これで Argus の推論経路から認証境界の外へ出る経路は無くなった**
3. **hostname canary + 監視** — 2 の後は `verdict=deny` が無条件に異常シグナルになる。
   **機構は実装済み（2026-07-31）**: `canary_tokens` テーブル（pm.db）、`net_guard.py` の
   `--plant-hostname-canary` / `--list-canaries` / `--revoke-canary`、`pm_selfcheck.py` の
   `canary_hit` / `netguard_deny`（既に cron 06:30 平日で回っているジョブに同乗。違反時 exit 1）。
   **残っている運用手順**:
   - (a) ~~qa/web デーモンの再起動~~ → **qa は 2026-08-01 に再起動済み**（`[NETGUARD]` 行が
     出始め、warn 期間が開始）。**`pm_api`（web、7/27 起動）は未再起動でフック無し** —
     Web UI 経由のジョブ（embed / xlsx / minutes publish）は依然として観測できていない
   - (b) 本番 pm.db への `canary_tokens` 作成（`--plant-hostname-canary` が自動で作る）
   - (c) **canary の植え付け** — 発行したホスト名を「モデルが読む場所」に記載する。
     人間向けレポートに出る場所（pm.db の action_items / decisions）に植えるには
     先に `is_canary` 列と全レポート経路での除外が必要（§4.3）。除外漏れは PM の
     レポートに架空項目を出すため、**box_docs 側から始める**のが安全
   - (d) アラートの届け先 — 現状は exit 1 + `logs/pm_selfcheck.log` のみで、人が見に
     行かないと気づけない。Slack への通知は Phase 3 のブローカー経由にする
     （ここで場当たりの egress を足すと allow-list の意味が薄れる）
   - (e) **canary の生存確認チェックが無い**（実装済みなのは発火検知だけ）。植えた行が
     同名PDFクリーンアップで消える / `relevance='noise'` を付けられて索引から落ちる /
     `pm_embed` の対象外になる — どれが起きても監視は「異常なし」を出し続ける。
     **植えるなら「box_docs に存在し qa_index にチャンクがある」ことの検査を同時に入れる**
   - **費用対効果の再評価（2026-08-01）**: 植え付けは (c) より先に `tool_calls` 記録
     （下記 5）を入れる方が増分が大きい。理由は §4.3 の検知点②（出力・ツール引数）が
     未実装で出力側を覆えていないこと、およびモデルが呼べる 13 ツールに URL 取得が無く
     「餌に噛める口が無い」こと。未知の宛先への到達自体は canary 無しで
     `netguard_deny` が拾う
4. **Box 既存共有リンクの正規化** — 事前に「読ませたい人が全員 collaborator か」を Box 側で
   揃える（順序を逆にすると一時的にリンク切れ）
5. ~~`tool_calls` 記録~~ → **2026-08-01 実装**（pm.db `tool_calls`、ハッシュ連鎖＋追記専用
   トリガ、`execute_tool` の唯一のチョークポイントで記録、`pm_selfcheck` に連鎖検証
   `tool_call_chain` を追加）。**拒否された呼び出しも記録する**（モデルが何を試みたかが残る）。
   **`reasoning_traces` も同日実装**（保持期間 既定90日、`tool_calls` は sha256 で参照、
   canary 発火時は `keep_sessions` で保全）。**これで §4.3 の検知点②③が埋まった。**
   残り: 外部アンカー（Phase 3 のブローカー待ち）。
   ~~`top_k` 200→100→50 の被害半径実測~~ → **2026-08-02 実施**（`recall_eval.py exposure`。
   `recall_eval.py exposure --k 50,100,200`）。**1 クエリの露出**は k=50 で 24 文書 /
   k=100 で 40 / k=200 で 62。**17 問の union 到達率**はコーパス 3,498 文書に対し
   7.1% / 10.1% / 14.6%。**取りこぼし**は k=50 で 19%、k=100 で 9.5%（k=200 比）。
   **運用値 50 がコード既定になっていなかった**（環境変数任せ）ため既定を 200→50 に変更。
   k=100 は「被害半径を半分に削りつつ取りこぼしを 1 割未満に抑える」候補だが、
   RIKYU の 600 秒制約との兼ね合いは未評価。
   **2026-08-02 に判明した重大な欠陥**: `_guard()` が `conn` 未指定時に canary 検査と
   `tool_calls` 記録を丸ごとスキップしており、**Slack 経路では一度も動いていなかった**
   （呼び出し 25 箇所すべてが未指定）。ファネル側が自分で pm.db を開く形に修正。
   あわせて `record_tool_call` の prev_hash 読み取り〜INSERT を `BEGIN IMMEDIATE` で包み、
   `ensure_*` の `executescript` が呼び出し側のトランザクションを暗黙 COMMIT する問題も直した。
   **本番台帳の既存 78 行はテスト由来なので証拠にならない**（LOG.md 参照）。
6. ~~供給網の固定（§4.6）~~ → **2026-08-01 実装**（`config/model_pin.yaml` + `scripts/utils/model_pin.py`）。
   本番3モデル（glm-5.2 / Kimi-K2-Thinking / bge-m3）＋評価2モデルの id 照合が全て OK。
   既定は warn、`ARGUS_MODEL_PIN=enforce` で拒否。**`declared_*`（revision / trust_remote_code /
   engine）は検証不能なので判定に使わない** — 申告の記録として git 差分で変更を検知する（R12）。
   **残り**: (a) 運用主体からのモデル更新通知の取り決め（Phase 0、技術ではなく合意）
   (b) K3 の `declared_engine` / `declared_trust_remote_code` の確認 — **これが取れるまで
   K3 は `production: false`**（§1.3 攻撃者2 に直接該当するため）(c) enforce への切り替え
7. ~~**R8: 第2系統をトリアージへ（Phase 4）**~~ → **2026-08-01 実装**
   （`config/sensitive_terms.yaml` + `ingest/slack.py` の `flag_sensitive_terms` /
   `apply_second_opinion` + pm.db `triage_second_opinion`）。**フラグ語が立った項目だけ**に
   非中国系モデル（Llama-4-Scout-17B）を当て、一致・不一致の両方を記録する。
   **第2系統の判定で主系統を上書きしない** — 小型モデルの能力差による誤りが混ざるため、
   自動で覆さずフラグを立てて人が見る。あわせて **`triage_items` の `missing_verdict` 既定を
   `DROP` → `KEEP` に統一**（判定不能時に欠落を作らない。Phase 4 の明示項目）。
   **2026-08-02 追記**: この配線は **production から一度も呼ばれていなかった**（呼び出し元は
   テストのみ）。トリアージが `integrated` へ移行して後段の `triage_items` が消えていたため。
   さらに integrated では DROP された項目が出力に現れないので、**生存項目の再審査では
   欠落が原理的に見えない**（LOG.md 参照）。
   **同日、欠落検出型へ作り直して 2 経路に配線した**: Pass 1 抽出（同じ生入力を第2系統に
   独立抽出させ、主系統に無い項目を `kind=*_extraction` / `primary_verdict=MISSING` で記録）と
   Box relevance（`noise` 判定のみ再審査。索引から落とす判定だけが欠落を作るため）。
   **残り**: (a) 実運用での不一致率の観測 — 能力差による雑音がどれくらいかを実測してから
   フラグ語の広さを調整する。**まだ 1 度も本番で走っていないので観測はこれから**
8. **能力分離 5b（Phase 5）** — **2026-08-01 に第1スライス実装**。
   `scripts/argus/pm_read_worker.py`（Read Plane プロセス）＋ `net_guard` の
   `ARGUS_NETGUARD_PLANES`（平面単位で許可集合を絞る）。**ゲートは実測で達成**:
   scrub した環境で起動した Read Plane プロセスから `slack.com` / `api.box.com` の
   名前解決が遮断され、LLM エンドポイントのみ到達可能。トークンは environ から除き、
   子プロセス側でも自己検査で二重に確認する（親が scrub を忘れても止まる）。
   **2026-08-01 に切替フラグを実装**（`ARGUS_READ_PLANE_SUBPROCESS=1`、**既定 OFF**）。
   倒す前に `tool_calls` で「調べながら中間結果を投稿する」使い方の実在を確認する。
   **残り**: (a) 上記フラグを ON にする判断
   (c) **OS レベルの強制**（iptables / network namespace）— 現状は同一プロセスの
   socket フックなので、subprocess とフック解除には効かない (d) ブローカーと Artifact
   への流れの再構成（下記 9 と一体）

   **(b) Patrol の Read Plane 分割は見送った（2026-08-02）**。理由を残す:
   Patrol の LLM は**ツールを持たず、宛先も選べない**（送信先は config 由来、判定は
   verdict 文字列を返すだけ）。つまり「読取能力と送信能力が同一プロセスに同居する」ことの
   危険が investigate とは違って**モデルからは行使できない**。一方、分割には**親を
   ブローカーにする IPC** が要る — 検出器は読取と送信が交互に走る構造で、送信の戻り値
   （メッセージ ts）を承認フローが使うため、片方向の受け渡しでは成立しない。
   **代わりに、実在した穴（LLM 判定が本番データを書き換えるのに記録が無い）を埋めた** —
   `patrol/audit.py` で 3 つの LLM 判定と自動クローズを `tool_calls` / `reasoning_traces` に
   記録する（本文は sha256 のみ、生応答は 90 日保持の `reasoning_traces` 側）。
   **Patrol は cron から意図的に外してある**（PM 判断。2026-08-02 確認、最終実行 2026-07-30）。
   **理由: Patrol はアクション保有者へ DM を送るが、その判定にまだ信頼が置けていない。
   cron を止めていることが DM を保留する手段そのものである。**
   したがって「Patrol は 30 分間隔なので放置で宛先が出る」という warn 期間の被覆前提は
   **誤りだった**（動いていないものを被覆に数えていた）。
   **Patrol への追加の手入れは、DM の信頼性の問題が片付いてから。** 本番実行での監査記録の
   確認（`tool_calls` に `patrol_judge_completion` が入ること）も、実行すると自動クローズが
   走るため**保留**。`--dry-run` は記録を残さない仕様なので代替にならない。
9. **輸送層ブローカー（Phase 3・検知点①）** — **2026-08-01 に第1スライス実装**。
   `scripts/argus/output_broker.py` + `config/egress_targets.yaml`。宛先は識別子で選ぶだけで
   モデルは構築できない。送信前に **canary 検出（検出したら遮断）・ゼロ幅文字・自由文可否・
   承認要否**を検査し、可否にかかわらず `tool_calls` に記録する（連鎖を1本に保つため
   新テーブルは作らない）。
   **実 ID は `egress_targets.yaml` に置かず `argus_config.yaml` への参照（`config_ref`）にした** —
   origin が public で `no-slack-id-literals` があるため。方針（外部可視性・自由文可否・承認要否）
   だけが git 差分に出る形。
   **被覆率は Canvas と Box のみ**（既存ファネルがあるため安い）。**Slack は SDK 直叩き
   25 箇所 / 7 モジュールの移送が未完了**で、`_dispatch` は slack を明示的に拒否する
   （黙って成功させない）。**「ブローカーがあるから守られている」と読んではいけない**段階。
   **Slack ファネルの新設と第1陣の移送は 2026-08-01 に完了**（`slack_post.py` に
   `post_message` / `post_ephemeral` / `update_message` / `upload_file`）。
   **設計文書が「輸送層ではない」と指摘した `slack_post.py` が輸送層になった。**
   移送済みは**パイプライン型（自動投稿）** — `output_tools` / `patrol/actions`（2）/
   `pm_argus`（録音進捗）/ `transcribe_pipeline`（2）/ `pm_minutes_import`。
   設計文書が「出力量の大半かつ人間の介在なし」と名指しした側を先に通した。
   **2026-08-01 に 25 箇所すべての移送が完了し、直叩きはゼロ**（`narrate.py` 10 /
   `pm_qa_server.py` 4 / `transcribe_pipeline` 3 / `pm_argus` 3 / `patrol` 3 /
   `output_tools` 1 / `pm_minutes_import` 1）。**pre-commit の `slack-egress-funnel` で
   直叩きを禁止した**（移送が全部終わってから入れた。途中で入れると恒常的に落ち、
   lint を無効化する方向に圧力がかかるため）。
   **`/argus-narrate` の順序制約も実装**: TTS へ渡す前に `scan_text_for_egress` で
   合成前テキストを検査する。mp3 になった後では canary もゼロ幅文字も検出できないため、
   **テキスト以外の成果物は生成元テキストの検査をもって代える**（§4.2 の原則）。
   **Canvas / Box も 2026-08-01 に対応済み** — ブローカー経由への全面移行ではなく、
   **既存の単一ファネル**（`canvas_utils.post_to_canvas` / `box_cli.box_upload_or_version`）に
   `guard_outbound_text` を入れる形にした。**2 箇所の編集で 8 / 9 モジュールを覆える**ため。
   Box はテキスト系拡張子のみ中身を検査し、**xlsx/pptx 等は「検査した」と記録しない**
   （検査できていないものを通過扱いにすると誤った根拠になる）。
   **残り**: (a) **承認フロー** — `EgressPendingApproval` を投げるところまでは実装済みだが、
   受け皿（誰がどこで承認するか）は**UX の決めが要るので未実装**。現状 `external_visible` の
   宛先は送信されず例外になる (b) **外部アンカー** — `db_utils.tool_call_anchor()` で
   連鎖の頭を取り出せるようにした。**日次投稿の cron 化は未実施**（投稿先チャンネルの
   決めが要る。ブローカー経由なので新しい egress は増えない）

**public リポジトリの機微情報**（origin: RIKEN-RCCS/ProjectManagement）:

HEAD からは除去済み（アプリ名 0 / Slack ID 0 / 機微ファイル 0）。再発は pre-commit の
4 lint（`no-box-open-company-access` / `net-guard-import-required` / `no-slack-id-literals` /
`no-mcp-server-registration`）が防ぐ。

- **人名 — 当面そのまま（2026-07-31 PM 判断）。** 敬称つきで 18 件 / 7 ファイル。
  **敬称なしの姓はパターンで検出できない**（例: `patrol/users.py` の docstring）。
  棚卸しには `docs/project.md` の名簿との突き合わせが必要（Claude は読めないため PM 実行）。
  **lint も無いので今後も混入しうる**
- **会議名 — 当面そのまま（2026-07-31 PM 判断）。** `docs/commands.md` 21 / `docs/argus_system.md` 15 /
  `pm_minutes_import.py` 27 ほか。既定の会議種別としてコード全体に埋まっており設定化は広範囲
- **公開履歴の除去 — 保留。** 2026-07-13 以降の履歴にアプリ名・Slack ID・`docs/decisions/` 等が残る。
  除去には `filter-repo`（パス指定＋`--replace-text`）と force-push が必要で、全 SHA が変わるため
  組織調整が前提。**上記 2 件を保留した結果、今実行しても部分的な除去にしかならない**（人名・
  会議名は残る）。**後から追加で force-push を繰り返すのは public リポジトリでは悪手**なので、
  除去範囲が確定してから 1 回で実施する

**その他の懸案**:
- ~~`data/qa_index.db` が平文~~ → **2026-08-01 に暗号化へ移行済み**（321MB / 29,313 チャンク）。
  検索経路 6 箇所を `open_maybe_encrypted`（暗号化優先・平文なら WARNING）に切替。
  **性能への影響は実測で FTS5 2.4→4.8ms、embedding 読み 23.5→33.9ms** で許容範囲。
  **残る平文の機微データ**: `data/processing/` の会議録音 mp4（§1.2）と
  `data/patrol_state.db`（設計上平文。機密を含まない前提）
- ~~MCP 経路（`pm_mcp_server`）は EGRESS を公開したまま~~ → **2026-07-31 に丸ごと廃止**
  （チョークポイント 2 箇所目が閉じた。再登録は pre-commit の `no-mcp-server-registration` が防ぐ）
- **`net_guard` の enforce が cron 経路に掛かっていない**（2026-08-02 確認）。`ARGUS_NETGUARD=enforce`
  を設定しているのは `pm_daemon.sh`（qa / web）だけで、`net_guard.py` の既定は `warn`。
  cron 5 本は**記録されるだけで遮断されない**。各ラッパーが `source` している `~/.secrets/` 側に
  置くのが早いが PM 作業。**Patrol を enforce で dry-run した結果は deny 0 件**（宛先は RIKYU と
  localhost:8001 のみ）なので、Patrol に限れば enforce にしても落ちない見込み
- **`silent_control`（`pm_selfcheck`）が本物の記録で意味を持つのは 2026-08-02 以降**。
  それ以前の `tool_calls` は全 78 行がテスト由来（LOG.md）。台帳が観測期間より新しい間は
  「判定不能」と出るので、`egress_other` / `read_tools` は当面その表示になる
- `~/.claude/settings.json` の GitHub PAT 失効（PM 作業）
- `argus_config.yaml` の `indices.pm-all.channels` は 55 件。旧ハードコード（57 件の和集合）との
  差分 2 件は PM 判断で現状維持

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

