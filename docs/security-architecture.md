---
type: プロジェクト
作成日: 2026-07-31
更新日: 2026-07-31
状態: 提案
tags: [Argus, セキュリティ, Kimi-K3, エージェント設計, 設計文書]
想定配置先: ProjectManagement/docs/security-architecture.md
対象読者: 開発者
---

# Argus セキュリティアーキテクチャと Kimi-K3 導入ロードマップ

Argus のエージェント層に構造的な情報流出リスクがあることが判明した。本文書はその脅威モデル、目標アーキテクチャ、コンポーネント設計、実装フェーズを定義し、あわせて Kimi-K3 導入の各工程をどのセキュリティ段階にゲートするかを決める。

---

## 0. 要旨

**問題** — Argus は機微データの読取能力と外部送信能力を同一のエージェント呼び出しに持たせている。`/argus-investigate` は16ツールを LLM が自律選択し、その中に pm.db 検索と Box/Slack/Canvas 出力が同居している。これは LLM エージェントからのデータ流出が成立する条件そのものである。

さらに**モデルのツール呼び出しを経由しない自動投稿経路**（朝のブリーフィング cron、Patrol Agent）が存在し、実際の出力量の大半はこちらである（§4.8）。

**重要な認識** — これは Kimi-K3 固有の問題ではない。**GLM-5.2 を使い続けても同じ対策が必要**であり、対策の主な受益は「モデル提供元の悪意」ではなく「Box や Slack に置かれた悪性文書による間接プロンプト注入」に対する防御である。後者は実証済みで頻度が高い。

**方針** — モデルの清浄性を証明しようとせず、**モデルを信頼できない意思決定コンポーネントとして扱い、その行動を決定論的ソフトウェアで仲介する**。具体的には Argus を Read Plane / Broker / Write Plane の3層に分離し、外部アクセスはすべて allow-list 方式にする。

**決定事項（2026-07-31）**

1. **`pm_web_fetch.py` を廃止する。** リスクに対して得るものが少ない。これにより Argus の推論経路から**公開インターネットへの任意の外向き通信が消える**
2. **外部アクセスは3層すべてを allow-list にする。** ネットワーク層（到達可能ホスト）／ツール・MCP層（登録可能なサーバとツール）／宛先層（送信先識別子）

**流出リスクの現状評価** — Argus の出力先は Box / Slack / Canvas に限られ、いずれも理研の認証システムで保護されている。**外部の攻撃者は出力を読めないため、符号化されても復号する側が存在せず、流出の回路が閉じない。** `pm_web_fetch.py` の廃止により、認証境界の外へ出る経路は消滅する。したがって流出は大きく抑止される。

> [!important] ただしこれは「構造的な性質」ではない
> 認証境界が守ってくれているのは **Argus のコードの性質ではなく、現時点で成立している運用上の外部事実**である。Slack Connect チャンネルの追加、Box のリンク共有設定、新しい出力先の追加、アカウント運用の変更——いずれも **Argus のコードを1行も触らずに起こる**。
> **能力分離は、同じ性質をコードの構造として与える。** したがって「認証境界があるから分離は不要」という推論は取らない。分離は流出・改竄の両方に対して必要である。

残るのは「認証境界の内側の定義」の問題（Slack Connect・ゲスト・Box共有リンク）と、**改竄リスク**（§4.9）である。

**投資順序** — 安価で効果が確実なものから。

1. canary とツール呼び出しログ（Phase 1）— 既存コードをほとんど触らずに現状の危険度を実測できる
2. `pm_web_fetch.py` の廃止（Phase 2）— 削除なので安い
3. **能力分離の第一段 5a**（Phase 2）— ツールの Plane 分類とレジストリ制限、**Read Plane からのトークン除去**。設定とランチャの変更が中心で安い。これだけで「EGRESS ツールは Read Plane に存在しない」という検証可能な不変条件とトークン分離が手に入る
4. 出力ブローカーと宛先 allow-list（Phase 3）— **5a の上に乗る**。トークン分離がなければ allow-list は規約に過ぎず、迂回するコードが1行入れば無効になる
5. 能力分離の第二段 5b（Phase 5）— Artifact/Broker への流れの再構成、プロセス分割、UX変更。ここが高価

**Kimi-K3 との関係** — K3 導入の6提案のうち、視覚入力と長時間自律は能力分離（Phase 5）完了をゲート条件とする。Kimi Code CLI 案は恒久的に不採用とする。APIクライアント再設計（K3提案の優先度1）は Phase 1 と同一のコードパスなので同時に実施する。

---

## 1. 脅威モデル

### 1.1 流出の成立条件

エージェント経由の情報流出は**3つが同時に揃ったときのみ**成立する。

1. **機微な情報がモデルの視界に入る** — セッションメモリ、検索結果、DB検索の返り値
2. **外向きの通信手段がある** — HTTP、web検索、メール、シェル、外部MCPサーバ、自動URL取得、あるいは後で人間が転送する出力
3. **発火条件が満たされる** — トリガ（悪性文書の指示、あるいは重みに仕込まれた条件）

**1 と 2 を同一の呼び出しが持たなければ成立しない。** ここが防御の梃子である。重みが清浄であることの証明は事実上不可能だが、能力の分離は決定論的ソフトウェアの設計問題なので実行可能。

### 1.2 Argus が該当する理由

**読む側（機微）**

| 対象 | 内容 |
|---|---|
| `data/pm.db` | 正本。action_items / decisions / meetings / goals / milestones |
| `data/slack.db` | 全チャンネル統合の生メッセージ |
| `data/minutes/{kind}.db` | 議事録詳細（議事内容・決定事項） |
| `data/box_docs.db` | Box 文書本文（Markdown 化済み） |
| `data/qa_index.db` | FTS5 + bge-m3。上記の横断索引 |
| **`data/processing/`** | **会議録音そのもの（mp4 115本）・VTT 115本・combined.txt 120本。すべて平文** |
| **`reasoning_traces`**（新設予定） | **モデルが見た機微データがそのまま入る思考トレース。** SQLCipher 適用と保持期間の設定が必須（§4.4） |

全DBに SQLCipher AES-256 が適用されているが、**LLM が受け取るのは復号後の平文**である。保存時暗号化はこの脅威に対して無効。

> [!warning] 保護レベルの逆転（2026-07-31 追加）
> **要約が SQLCipher で守られ、全発言を含む原データが平文で置かれている。** `data/processing/` に会議録音（mp4 115本）と VTT 115本が平文で残っている。
> 派生物より原データの方が機微度が高いのに、保護が弱い。これは流出・改竄のどちらの議論とも独立に是正すべきで、**Phase 2 に「`data/processing/` の暗号化またはパージ」を入れた。**

**外に出す側**

| コンポーネント | 経路 | 認証境界の外へ出るか | 方針 |
|---|---|---|---|
| `scripts/argus/output_tools.py` | Box / Slack / Canvas への出力 | **出ない**（理研の認証で保護） | allow-list 化して維持 |
| `scripts/data-pipeline/pm_web_fetch.py` | 外部Web記事の取得（＝任意の外向きHTTP） | **出る** | **廃止（決定）** |
| `scripts/utils/canvas_utils.py` / `box_cli.py` | Canvas / Box の輸送層。**単一ファネル**（`post_to_canvas` / `box_upload_or_version`） | 出ない | Write Plane に隔離。ブローカーをここに置ける |
| **Slack SDK の直接呼び出し**（7モジュール・25箇所） | **輸送層のファネルが存在しない。** `slack_post.py` は mrkdwn 整形ヘルパ2関数のみで投稿関数を持たない | 出ない | **ファネルを新設して移送が必要**（§4.2） |
| `scripts/argus/narrate.py` | LLM生成テキスト → mp3/mp4 → `files_upload_v2` | 出ない | **テキストDLPが効かない出口**（§4.2） |

**そして両方が同一セッションに存在する。** `/argus-investigate M3の遅延原因 --to-slack` は、DB を検索した同じエージェントループが Slack へ投稿する。

> [!info] 流出リスクの評価（2026-07-31 更新）
> `pm_web_fetch.py` を廃止すると、**認証境界の外へ出る経路がなくなる**。Box / Slack / Canvas はいずれも理研の認証で保護されており、外部の攻撃者は出力を読めない。符号化されても復号する側が存在しないため、**流出の回路が閉じない**。
> したがって同居問題の主眼は流出から**改竄**（§4.9）へ移る。ただし能力分離は依然として必要である。理由は §5 Phase 5 に記載。
>
> **この評価は 2026-07-31 の棚卸しで裏付けられた**（Phase 2-2）。Slack Connect・ゲストは不在、パートナーの参加チャンネルには Argus は投稿せず、Box 共有リンクの実効アクセスは `company`。

> [!warning] ただし Box を守っているのは Argus のコードではない（2026-07-31 実測）
> `box_cli.py`:103 は共有リンクを **`--access open`（リンクを知っている全員＝認証不要の一般公開）で要求している。**
>
> ```python
> box_json(["box", "files:share", file_id, "--access", "open", "--json"], timeout=60)
> ```
>
> 呼び出し元は3箇所——`pm_xlsx_report.py`:595（**action_items / decisions / 実績の全件を含む `pm_report.xlsx`**）、`pm_minutes_catalog.py`:182（議事録 Markdown）、`output_tools.py`:109（エージェントの `box_upload_file`）。
>
> **実測では `effective_access` が `company` に降格されていた。理研 Box の企業ポリシーが要求を上書きしている。** つまり**公開を防いでいるのは Box の管理者設定であって、Argus 側は公開を要求し続けている。**
>
> これは §2「認証境界と分離の関係」で述べた構図そのものである——**テナント設定の変更、フォルダ単位のポリシー例外、別テナントへの移行のいずれでも、Argus のコードは1行も変わらずに全世界公開になる。** 緊急ではないが、**明示的なアクセス指定で Box 側の設定に依存しない状態にする**（Phase 2-4）。**目標値は `collaborators`**（招待されたユーザーのみ。出力先フォルダの collaborator 一覧は 2026-07-31 に確認済み）。
>
> あわせて **`box_get_or_create_shared_link` は既存リンクをアクセス範囲を見ずにそのまま返す**（:99）。`--access` を直すだけでは**過去に作られたリンクが永久に残る**ため、既存リンクの正規化も同時に必要。

### 1.3 想定攻撃者（現実性の順）

**設計は上から順に防御しなければならない。** 4 を主眼に置くと優先順位を誤る。

| # | 攻撃者 | 経路 | 現実性 |
|---|---|---|---|
| 1 | **Box / Slack に文書やメッセージを置ける者** | 悪性指示を埋め込んだ資料が `pm_box_crawl.py` で索引化され、エージェントの視界に入る。内部者に限らず、共同研究先・メール添付・転送された外部資料を含む | **最も高い。** 間接プロンプト注入として実証済み・頻度が高い |
| 2 | **供給網に入る第三者** | `trust_remote_code`、Pickle 形式の重み、非公式派生物、モデル提供元のCLI。読み込み時に任意コードが実行される | **高い。** JFrog は HuggingFace 上に約100件の悪意あるモデルリポジトリを実際に発見している |
| 3 | ~~**外部Webコンテンツの提供者**~~ | ~~`pm_web_fetch.py` が取得したページに指示が埋め込まれる~~ | **`pm_web_fetch.py` の廃止により消滅（§4.5）** |
| 4 | **モデル提供元の意図的バックドア** | 重みに条件付き振る舞いを埋め込む | 技術的に実現可能で監査困難だが、**公式チェックポイントでの実例は現時点で未確認** |

> **1〜3 への防御は 4 への防御でもある。** 能力分離・宛先allow-list・完全監査は攻撃者の意図を問わない。したがって「モデル提供元が悪意を持っているか」を判定する必要がない設計にできる。これが本設計の戦略的な要点である。

### 1.4 立証されていることと、されていないこと

議論が混乱する最大の原因は次の3つを混同すること。

| 発見 | 立証 | Argus への該当 |
|---|---|---|
| バックドアの**実現可能性** | 強く実証（研究者が自ら仕込んだ実験） | 該当（ただし公式重みでの実例は未確認） |
| 一部モデルの**エージェント安全性の弱さ** | 実証（NIST/CAISI：DeepSeek R1-0528 は資格情報流出を37%で試行、米国フロンティアモデル平均4%） | **大いに該当**（通常のプロンプト注入） |
| ホスト型サービスの**データ漏洩** | 実際に発生（韓国PIPC、Wiz） | **該当せず**（理究でローカル実行） |

補足すべき点として、**米国製モデルでも 4% は 0% ではない**。モデル選択は脆弱性の程度を変えるが、脆弱性を消さない。したがって「中国製を使わない」は本問題の解決策にならない。

### 1.5 なぜ重みの検査で解決しないか

- 数十億の浮動小数点数は**ソースコードではない**
- 公式リリースには比較対象となる**「既知の清浄版」が存在しない**
- トリガは「特定語＋日付」「ツールスキーマ名」「対象組織名」「複数ターンにまたがる内容」などの**連言**でありうる。組み合わせ空間が広すぎて黒箱探索が不可能
- 通常のベンチマークは圧倒的に**非発火時の方針**を試すが、それは攻撃者が意図的に保存する部分そのもの
- 追加学習でも消えない。Sleeper Agents では SFT・RL安全化・敵対的学習でも確実には消えず、**敵対的学習は隠蔽を上手にすることすらあった**
- Microsoft の `llm-backdoor-scanner` は 87.8% 検出・誤検出ゼロを達成したが、**検証は14Bまで**。2.8T の Kimi-K3 には適用できない

**結論：自前で重みを検査して安全宣言する路線は取らない。**

---

## 2. 設計原則

> **モデルを、提案する行動が決定論的ソフトウェアによって仲介される、信頼できない意思決定コンポーネントとして扱う。**

以下は Argus に適用する5原則。すべてモデルの出自に依存しない。

| # | 原則 | 意味 |
|---|---|---|
| P1 | **能力分離** | いかなるエージェント呼び出しも、機微データ読取と外部送信を同時に持たない。**これは他の原則の土台である**（下記） |
| P2 | **宛先と取得範囲の非モデル決定** | **送信先**はモデルが構築できない。モデルは列挙された識別子を選ぶだけで、URL・ホスト名・チャンネルIDを生成しない。**取得範囲**も同様——Ingest Plane の外部呼び出し（Slack のチャンネル・期間、Box のフォルダ）は決定論的であり、**LLM 出力でパラメータ化されない**（下記） |
| P3 | **決定論的仲介** | 外部送信は必ずブローカーを経由し、ブローカーは LLM を含まない |
| P4 | **完全監査** | すべてのツール呼び出し・引数・思考トレースを追記専用で記録し、改竄を検出可能にする |
| P5 | **供給網の固定** | チェックポイントの revision とハッシュを固定し、`trust_remote_code` と Pickle を使わず、自動更新を禁止する |
| P6 | **fail-closed は「大きく失敗する」ことと対にする** | 遮断は静かに起き、機能の劣化も静かに起きる。allow-list を導入するときは、期待する依存先ごとの起動時到達性アサーションと稼働中の健全性チェックを必ず併設する（§4.7） |
| P7 | **最小権限（文脈と能力の両方）** | 発火した場合の**被害半径**を小さくする。軸は2つ——**文脈**（何を見せるか。pm.db 全体を渡さない）と**能力**（何をさせるか。コマンド別ツール allow-list、§4.1）。**両方を測って決める**（下記） |
| P8 | **対策の被覆率を実際の流量で検算する** | すべての対策について「これは実際に流れている量の何割を覆うか」を問う。**Argus の主流は cron とパイプラインであり、エージェントループではない**（下記） |
| P9 | **allow-list を、制約対象と同じ情報源から定義しない** | 接続に使う環境変数で allow-list を書くと、環境変数を変えれば許可も動くので何も禁止しない。**リテラル値で書き、実行時の値を照合する**（§4.7） |
| P10 | **対策が「何を証明するか」を明示する** | 証明していないことを「防げる」と書かない。**P8 が幅（何割を覆うか）の検算なら、P10 は強度（何を証明するか）の検算である**（下記） |

### P7 — 被害半径は「同居」ではなく「量」で決まる

**能力分離（P1）が解消するのは「同一呼び出しが読取と送信を持つ」ことであって、「読んだものが最終的に外に出る」ことではない。** 後者は製品の目的なので消せない。分離が与えるのは①出口の1点集約と決定論的検査、②注入 → **即**行動の連鎖の切断、③境界の運用が変わっても壊れない構造——の3つで、**散文への符号化の検出は与えない**（DLP も人間レビューも無力、§4.2）。

**したがって分離を完了しても、「発火したとき何が漏れるか」は残る。そしてそれを決めるのは、その呼び出しが何を見ていたかである。** これが P7 の担当領域であり、**P1 の後ろに残る唯一の梃子**である。

> [!warning] 現行の本番設定は P7 の文脈軸と逆方向に動いている
> 前版は「P7 は K3 の窓分割方式（1M文脈に全部載せない）と同じ方向を向いており、セキュリティと性能設計が一致する」と書いた。**窓分割については正しいが、現に動いている構成については逆である。**
>
> - `ARGUS_ONESHOT=1` が本番で有効（glm-5.2）
> - one-shot の取得件数は **既定 `top_k = 200`**（`pm_argus_agent.py`:57）
>
> one-shot は「補助 LLM を挟まず、太い文脈を1回で渡す」設計そのもので、**1回の呼び出しで 200 チャンクがモデルの視界に入る。** K3 の売りである視覚入力と長時間自律も、どちらも視界を最大化する方向である。
>
> **2つの軸は現状トレードオフになっており、本番は片方の角に振り切っている。**
>
> | | 文脈（見る量） | 能力（できること） |
> |---|---|---|
> | **one-shot（本番既定）** | **200 チャンク＝大きい** | **0 ツール＝最小** |
> | ループ | 1ステップずつ小さい | 16 ツール（EGRESS 3 含む）＝最大 |
>
> **能力軸は宣言だけでほぼ閉じる**（6コマンド中5つが既に0、§4.1）。**文脈軸は品質とのトレードオフなので、実測して数値で決めるしかない。**

**P7 はタダではない。** 文脈を削れば回答品質は落ちる。だからこそ原則で終わらせず、**効果（被害半径）とコスト（正答率）の両方が測れる項目**として扱う。既存の `recall_eval` と `investigate_ab.py` は、まさに「文脈量を変えたときの回答品質」を測るハーネスである。

### P2 はなぜ取得側にも及ぶのか

**Ingest Plane は Slack / Box の読取トークンを持ち、Box の未信頼文書を受け取り、その内容を LLM に渡す。** もし取得対象（チャンネル、Box フォルダ、期間）が LLM 出力でパラメータ化されていれば、**注入された文書が自分の読取範囲を広げられる**。「この文書も参照せよ」という指示が、実際に索引化対象を増やす。

**現状はこの不変条件を満たしている（2026-07-31 実測）。**

| 取得 | 範囲の決まり方 | LLM 由来か |
|---|---|---|
| Slack | `slack_pipeline.py` の `--channel`（CLI 引数・既定値・cron の指定） | **いいえ** |
| Box | `box_sources.yaml` / `argus_config.yaml` の `folder_id` | **いいえ** |

**つまりこれは「塞ぐべき穴」ではなく「既に成立している不変条件」である。** だからこそ**明文化して固定する**価値がある——現状の性質は、コードを1行足せば失われる（§2「認証境界と分離の関係」と同じ構図）。Phase 0 で全数を確認し、Phase 3 の lint（`.pre-commit-config.yaml`）に「取得対象の識別子が LLM 応答から流れ込む経路がない」を加える。

### なぜ P1 が土台なのか

**P1 がないと P2・P3 が強制力を失う。**

Read Plane が `SLACK_BOT_TOKEN` を持ったままなら、宛先 allow-list（P2）とブローカー経由の強制（P3）は**コーディング上の規約に過ぎない**。ブローカーを迂回して Slack API を直接叩くコードが将来1行入れば、それで無効になる。レビューで防ぐしかなくなり、時間とともに劣化する。

**トークンを持たせないことが、allow-list を「守るべき規約」から「破れない制約」に変える。** 依存関係は次の通り。

```
P1 能力分離（トークン分離）
      └─→ P2 宛先の非モデル決定
              └─→ P3 決定論的仲介
```

したがって実装順序も P1 の安い部分（Phase 2 の 5a）を先に置き、その上に P2・P3（Phase 3）を乗せる。

### P8 の由来：ツールレジストリを漏斗と見なす誤り

**本文書は同じ型の誤りを4回犯した。** 最初の3回は「エージェントのツールレジストリが、そこを通らないと何もできない漏斗である」と暗黙に仮定したことが原因である。4回目は**分母を数え直したつもりで、数え漏らした**ものだった。

| # | 誤った前提 | 実際 | 見落としていた主流 |
|---|---|---|---|
| 1 | ブローカーの対象＝モデルが選ぶ EGRESS ツール | cron の朝ブリーフィングと Patrol Agent が輸送層を直接呼ぶ | **実際の出力量の大半** |
| 2 | 監査の対象＝`mcp_tools.py` 経由のツール | EGRESS 3つは `agent_tools.py` 直接実装 | **最も記録が必要な送信系** |
| 3 | 書込制約の対象＝MUTATE ツール | **MUTATE ツールは1つも存在しない** | **Pass 1 ingest**（§4.8） |
| 4 | 輸送層の呼び出しは6箇所 | Slack だけで **25箇所 / 7モジュール**。しかも `slack_post.py` は輸送層ですらない | **`/argus-narrate` の音声・動画出力ほか**（§4.2） |

**4回目の教訓は3回目までと少し違う。** 検算はした（P8 に従って出口を数え直した）が、**数えた対象が「前回の指摘で名前が挙がった箇所」に限られていた。** 指摘は網羅ではなくサンプルである。**検算は指摘の追認ではなく、独立した全数調査として行う。**

**Argus の実際のデータフローの主流は cron とパイプラインであって、エージェントループではない。** `/argus-investigate` は目立つが量的には少数派である。

したがって新しい対策を設計するたびに、次を明示的に検算する。

> **この対策は、実際に流れている量の何割を覆うか。残りはどこを通っているか。**

覆えていない経路を「例外」として脚注に落とさない。それが主流である可能性がある。

### P10 の由来：強度の検算漏れ

**P8 の検算を通しても、まだ過大主張は残る。** 引用スパンの必須化（§4.8）を提案したとき、当初は「これで捏造は原理的に不可能になる」と書いた。被覆率（P8）は問うたが、**その対策が何を証明するかを問わなかった。**

実際には、逐語照合が証明するのは**引用された根拠が実在すること**だけである。**その根拠から結論が導かれることは証明しない。** 決定事項の抽出は複数発言の統合なので、本文に逐語一致を課せば品質が落ちるか、モデルが「それらしいスパン」を選ぶだけになる。

> **P8 は「何割を覆うか」。P10 は「覆っている部分について、何を証明するか」。** 両方を問わないと、被覆率100%の対策が実は何も証明していない、ということが起こる。

したがって対策を書くときは、次の2つを必ず併記する。

| 問い | 原則 | 書き方 |
|---|---|---|
| 実際に流れている量の何割を覆うか | P8 | 覆えない経路を明示する |
| 覆っている部分について何を証明するか | **P10** | **証明しないことを列挙する**（§4.8 の「改竄の3型」表がその形） |

### 認証境界と分離の関係

Argus の出力先が理研の認証で守られていることは**実質的な防御**であり、流出の回路を閉じる。しかしそれは運用で変わる外部事実であって、コードの性質ではない（§0）。

| | 認証境界に依存する防御 | 能力分離による防御 |
|---|---|---|
| 何が保証するか | Slack/Box の設定と組織の運用 | Argus のコード構造 |
| 変化したとき | **気づかずに崩れる** | コード変更が必要なので気づく |
| 検証方法 | チャンネル・フォルダの定期棚卸し | ユニットテスト・grep・起動時チェック |
| 改竄への効果 | **なし** | あり（注入 → 行動の連鎖を切る） |

両方を持つ。片方で代替しない。

---

## 3. 目標アーキテクチャ

### 3.1 4つの平面

```
┌─────────────────────────────────────────────────────────────┐
│ INGEST PLANE（取り込む。Slack/Box の読取トークンを持つ）      │
│                                                             │
│  Slack / Box / 会議録音 ──→ 抽出LLM（理究 / RiVault）        │
│                        ──→ Docling（文書変換）              │
│  ・LLM生成の自由文を pm.db に書く ← §4.8 の真の入口          │
│  ・書込スコープのトークンは持たない（目標。現状は違う→§3.2）  │
└────────────────────────┬────────────────────────────────────┘
                         │ pm.db / slack.db / minutes / box_docs
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ READ PLANE（機微データを見る。外に出せない）                  │
│                                                             │
│  LLM ── read tools ──→ pm.db / slack.db / minutes / box_docs │
│                        qa_index (FTS5 + bge-m3)             │
│                                                             │
│  ・SLACK_*_TOKEN / BOX_* を環境に持たない                     │
│  ・slack.com / box.com への経路を持たない                     │
│  ・出力は Artifact（Markdown + 宛先識別子）のみ               │
└────────────────────────┬────────────────────────────────────┘
                         │ Artifact（DB経由で受け渡し）
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ BROKER（LLMを含まない。決定論的）                             │
│                                                             │
│  1. 宛先識別子を allow-list で解決                            │
│  2. canary スキャン                                          │
│  3. DLP（エントロピー / Base64様 / ゼロ幅文字 / 未知URL）       │
│  4. サイズ上限                                               │
│  5. 自由文なら人間承認キューへ                                │
│  6. 追記専用ログに記録                                        │
└────────────────────────┬────────────────────────────────────┘
                         │ 検証済みの送信要求
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ WRITE PLANE（外に出す。機微DBを読めない）                     │
│                                                             │
│  slack_post / box_cli / canvas_utils / pm_tts               │
│  ・pm.db への読取権限を持たない                               │
│  ・書込スコープのトークンを持つのはここだけ                      │
│  ・宛先はすべて理研の認証境界の内側                             │
└─────────────────────────────────────────────────────────────┘

  ┌───────────────────────────────────────────────────────┐
  │ FETCH PLANE — 廃止（2026-07-31 決定）                  │
  │ pm_web_fetch.py は削除する。                           │
  │ 公開インターネットへの任意の外向き通信を持たない。       │
  └───────────────────────────────────────────────────────┘
```

**廃止によって得られる不変条件** — 「Argus は公開インターネットに任意のリクエストを出せない」という**検証可能な命題**が成立する。

> [!warning] 「3つだけ」は誤り（2026-07-31 訂正）
> 当初「到達可能な外部ホストは理究・Box・Slack の3つだけ」と書いたが、実測では **9エントリ / 8ホスト**である（RiVault・EMBED・Docling・VOICEVOX・fish-speech が追加）。命題自体は壊れないが、数え方が誤っていた。allow-list を「3つ」で実装すると機能が壊れる（§4.7）。

### 3.2 分離をどう強制するか

コード上の約束ではなく**プロセスと秘密情報の分割**で強制する。Argus は既にトークンを `~/.secrets/*.sh` に置き `source` して使う構成なので、これは自然に実装できる。

| 平面 | プロセス | 環境変数 | ネットワーク |
|---|---|---|---|
| **Ingest** | `pm_ingest.py` / `slack_pipeline.py` / `pm_box_crawl.py` / `pm_minutes_import.py` | DB鍵、**Slack・Box の読取スコープ**トークン | 理究・RiVault（抽出LLM）、Docling |
| Read | `pm_read_worker.py`（新） | DB鍵のみ。**外部サービスのトークンを一切持たない** | 理究・RiVault（推論）、EMBED |
| Broker | `pm_broker.py`（新） | なし（DBは egress ログのみ書込） | なし |
| Write | `pm_write_worker.py`（新） | **Slack・Box・Canvas の書込スコープ**トークン | slack.com / box.com / TTS(localhost) |
| ~~Fetch~~ | ~~`pm_web_fetch.py`~~ | — | **廃止** |

> [!important] Ingest Plane を第4の平面として定義する（2026-07-31 追加）
> 当初 allow-list にのみ `ingest_plane` が現れており、§3.1 / §3.2 に定義がなかった。**平面はプロセスとトークンの割り当て単位なので、帰属が曖昧だと 5a の「どのプロセスから何を外すか」が決まらない。**
>
> Ingest Plane が独立して必要な理由：**取り込みは Slack と Box を読む必要があるため、読取スコープのトークンを持たなければならない。** 「Write Plane だけがトークンを持つ」という当初の整理では表せない。
>
> したがってトークンの分割は3分割になる。
>
> | | 持つトークン |
> |---|---|
> | Ingest Plane | Slack・Box の**読取**スコープ |
> | Read Plane（エージェント） | **なし** |
> | Write Plane | Slack・Box・Canvas の**書込**スコープ |
>
> **Slack と Box で読取/書込のスコープを実際に分離できるかは Phase 0 の確認事項。** Slack はボットトークンのスコープ設定で可能なはずだが、現状 `~/.secrets/slack_tokens.sh` が bot / app / user の3種を一括で `source` している。分離できない場合は「Ingest と Write は同じトークンを持つが、Read Plane は持たない」に後退する。**その場合でも 5a の要点（エージェントがトークンを持たない）は達成される。**

> [!warning] Ingest Plane の定義が実装と矛盾している（2026-07-31 実測）
> 上で Ingest Plane を「**書込スコープのトークンは持たない**」と定義したが、そこに分類されるモジュールが実際には Slack に投稿している。
>
> - `pm_minutes_import.py` — 1箇所
> - `transcribe_pipeline.py` — 3箇所
>
> **平面はプロセスとトークンの割り当て単位なので、この2つの帰属が決まらないと 5a の「どのプロセスから何を外すか」が確定しない。** 選択肢は2つ。
>
> | 案 | 内容 | 評価 |
> |---|---|---|
> | **A** | これらの投稿を **Write Plane 側へ移す**（ブローカー経由にする） | **本線。** Slack 25箇所の移送作業（§4.2）に含められるので追加コストが小さい |
> | B | Ingest Plane も書込トークンを持つと定義を緩める | 平面の意味が薄れる。トークン3分割の利点を失う |
>
> **A を採る。** Phase 2-5 の確認項目に入れる。

**Read Plane が Slack トークンを持たないという一点で、P1 の大半が達成される。** 現在は `pm_qa_server.py` が `source ~/.secrets/slack_tokens.sh` の下で全部を担っているため、まずここを割る。

ネットワーク制限は iptables / network namespace が理想だが、理究の運用制約次第。最低限、**トークンの不在**と**プロセス分割**は必ず入れる。

### 3.3 チョークポイントは2箇所（`agent_tools.py` と MCP 側）

> [!warning] 当初の記述は誤り（2026-07-31 訂正）
> 「`mcp_tools.py` が全ツールの実装本体なので、そこにラップを入れれば両経路に一度で効く」と書いたが、**実測で成立しないことが判明した。**
> - ツールは **15 ではなく 16**
> - `_call_mcp` に委譲しているのは **8つだけ**
> - **EGRESS 3つ（`box_upload_file` / `slack_post_message` / `canvas_post_content`）はすべて `agent_tools.py` の直接実装**
>
> したがって記載どおり `mcp_tools.py` だけにラップを入れると、**`tool_calls` に最も記録が必要な送信系が1件も残らない。** 設計として逆の結果になるところだった。Plane による絞り込みも同じ理由で片肺になる。

**正しいチョークポイントは2箇所。**

| 箇所 | 対象 |
|---|---|
| `agent_tools.py` の `_TOOL_MAP` / `pm_argus_agent.execute_tool` | `/argus-investigate` 経路の全16ツール（EGRESS 3つを含む） |
| MCP 側（`pm_mcp_server.py` / `mcp_tools.py`） | Claude Code など外部オーケストレータ経由 |

両方に同じラップ（`tool_calls` 記録 ＋ Plane 絞り込み）を入れる。**ただし外部サービスへの送信そのものは、さらに下の輸送層でブローカーが捕まえる**（§4.2。輸送ごとにファネルの有無が異なる点に注意）。ツール層のラップは監査と Plane 制限のため、輸送層のブローカーは送信の検査と宛先解決のため、という役割分担になる。

> [!note] 実装上の追い風
> `agent_tools.py` には既に `_FILE_PINNED_EXCLUDED_TOOLS` と `exclude_tools` というツール除外の下地がある。**`registry_for`（§4.1）はその一般化として自然に入る**ため、能力分離5a の実装難度は当初の想定より低い。

---

## 4. コンポーネント設計

### 4.1 ToolDef の三分割

**16ツール**（当初「15」と記載していたが実測で16）を3カテゴリに分け、レジストリを分離する。2分割（読/書）では粒度が粗い。**内部DB変更は外部送信ほど危険ではないが不可逆**なので独立させる。

**実測による全16ツールの分類（2026-07-31）**

| カテゴリ | 件数 | ツール | 与える平面 |
|---|---|---|---|
| `READ` | **13** | `get_app_achievements` `get_assignee_workload` `get_milestone_progress` `get_overdue_items` `get_slack_messages` `read_document` `search_action_items` `search_decisions` `search_entity` `search_mentions` `search_text` `search_text_hybrid` `synthesize_answers` | Read Plane |
| `MUTATE` | **0** | **該当ツールが存在しない**（下記） | — |
| `EGRESS` | **3** | `box_upload_file` `slack_post_message` `canvas_post_content` | **Write Plane のみ。Read Plane には登録しない** |

> [!warning] MUTATE ツールは存在しない（2026-07-31 訂正）
> 当初「action_items の更新、decisions の確認済みマーク」を MUTATE として書いたが、**そのようなツールは1つもない。** `/argus-investigate` のエージェントは pm.db を書き換えられない。
> **したがって MUTATE に対する制約を実装しても、現に流れているものは何も制約されない。** 実際に LLM 生成の自由文が pm.db に入るのは **Pass 1 ingest** と **Patrol の自動クローズ**であり、どちらもツールレジストリを通らない（§4.8）。
> `MUTATE` カテゴリ自体は**将来ツールが追加されたときの受け皿として定義を残す**が、現在0件であることを明示する。**Plane 機構は「今の防御」ではなく「将来の逸脱を検出する枠」としてここでは機能する。**

**EGRESS 3つはすべて `agent_tools.py` の直接実装である**（`mcp_tools.py` に委譲していない。委譲は8つのみ）。したがって Plane 絞り込みは `agent_tools.py` 側に必ず入れる（§3.3）。

**宛先引数の現状（2026-07-31 実測）**

| ツール | 宛先の決まり方 | P2 適合 |
|---|---|---|
| `box_upload_file` | `folder_id` を `argus_config.yaml` から解決 | **適合済み。この形が正解** |
| `slack_post_message` | `channel` が**モデル指定の引数** | 未適合 |
| `canvas_post_content` | `canvas_id` が**モデル指定の引数** | 未適合 |

**Phase 3 の最短路は、後者2つを `box_upload_file` の形に揃えること。** 既に1つ正解の実装があるので、パターンを新規に設計する必要がない。

#### ツール表面の実測 — ループを持つのは1ファイルだけ（2026-07-31）

**Plane による3分類は粒度が粗すぎる。実測すると、そもそもツールを使う経路がほとんど無い。**

```
$ grep -rln "execute_tool" scripts/
scripts/argus/pm_argus_agent.py     ← これだけ
```

| コマンド | ツール表面 | 備考 |
|---|---|---|
| `/argus-brief` | **0** | Orchestrator-Worker だがプロンプトのみ |
| `/argus-risk` | **0** | 同上 |
| Patrol Agent | **0** | 検出は SQL、投稿は直接呼び出し |
| Pass 1 ingest | **0** | 抽出・トリアージともプロンプトのみ |
| `/argus-narrate` | **0** | — |
| **`/argus-investigate`（ループ）** | **16（EGRESS 3 を含む）** | **唯一ツールループを持つ経路** |
| **`/argus-investigate`（one-shot）** | **0** | `_call_oneshot_llm` は `tools=` を渡さない |

> [!important] 本番の investigate はツールを1つも持たない
> **`ARGUS_ONESHOT=1` が本番で有効な現在、investigate の既定経路は one-shot** であり、決定論的検索 → 単一 LLM 呼び出し → 回答という流れでツールループを通らない。**モデルにツールが与えられていないので `slack_post_message` を呼べない。**
>
> **つまり 5a が目指す「エージェントは外に出せない」状態を、本番の investigate は既に満たしている。** ただし理由が異なる——5a は「トークンを持たない」、one-shot は「ツールを渡さない」。ループ経路（one-shot 無効時・`--file` 指定時・フォールバック時）だけが同居問題を抱えている。
>
> **同居問題は 1 ファイル・1 経路に閉じている。** 設計の緊急度はこの事実に合わせる。

#### 実装：Plane ではなくコマンド別 allow-list

**3分類（Plane）は「将来の逸脱を検出する枠」として残すが、実際に絞るのはコマンド単位にする。** 6 コマンド中 5 つが既にゼロなので、大半は現状の宣言的追認で終わる。

```python
# scripts/argus/agent_tools.py（2026-07-31 実装。Plane enum は不採用——下記注記）
EGRESS_TOOL_NAMES: frozenset[str] = frozenset({
    "box_upload_file", "slack_post_message", "canvas_post_content",
})
READ_TOOL_NAMES: frozenset[str] = frozenset({...})  # 13 件、明示リテラル列挙

# コマンド別のツール allow-list。空集合は「ツールを渡さない」を意味する
COMMAND_TOOLS: dict[str, frozenset[str]] = {
    "brief":               frozenset(),
    "risk":                frozenset(),
    "patrol":              frozenset(),
    "narrate":             frozenset(),
    "ingest":              frozenset(),
    "investigate_oneshot": frozenset(),
    # 唯一ツールを持つ経路。READ 13 のみ。EGRESS 3 は含めない
    "investigate_loop":    READ_TOOL_NAMES,
}

class ToolRegistryError(RuntimeError):
    """COMMAND_TOOLS の宣言違反（未宣言コマンド／EGRESS混入／不明ツール名）。"""

def registry_for(command: str) -> list[ToolDef]:
    """コマンドに宣言された allow-list のツールだけを返す。
    未宣言のコマンドは fail-closed（空ではなく例外）。"""
    if command not in COMMAND_TOOLS:
        raise ToolRegistryError(f"未宣言のコマンド: {command!r}")
    allowed = COMMAND_TOOLS[command]
    unknown = allowed - _TOOL_MAP.keys()
    if unknown:
        raise ToolRegistryError(f"TOOLS に存在しないツール名: {sorted(unknown)}")
    egress = allowed & EGRESS_TOOL_NAMES
    if egress:
        raise ToolRegistryError(f"EGRESS tools must not be exposed: {sorted(egress)}")
    return [_TOOL_MAP[name] for name in sorted(allowed)]
```

> [!note] `Plane` enum は実装しなかった（2026-07-31）
> 当初案は `ToolDef.plane` 属性 + `Plane` enum で EGRESS 判定する形だったが、実装では
> 既存の `EGRESS_TOOL_NAMES` frozenset（分類情報として温存）との集合演算で同じ
> fail-closed 判定を得ている。全 16 `ToolDef` に `.plane` フィールドを追加する変更を
> 避けられるぶん差分が小さい。効果は同一（宣言に EGRESS が混入したら
> `registry_for` が例外）なので、enum 化は「今のところ不要な一般化」として見送った。
> 加えて `unknown = allowed - _TOOL_MAP.keys()` によるタイプミス検出（TOOLS に無い
> ツール名を宣言したら例外）を追加している。これは当初案には無かった検出だが、
> 「宣言集合に TOOLS に存在しないツール名があったら例外」という不変条件を
> 機械的に保証するために加えた。

**実質的な変更は「ループ経路から EGRESS 3 を外す」の1点だけ**であり、**`exclude_tools` / `_FILE_PINNED_EXCLUDED_TOOLS` という除外機構が既にある**ので、その一般化として書ける。**トークン除去（プロセス分割）より先に、これだけ単独で入れられる。**

**ゲートは2段構え。人間承認は置かない**（ブローカーと重複し、自動化を殺すだけ）。

| ゲート | 内容 |
|---|---|
| **起動時 fail-closed** | 宣言にないツールがレジストリに載っていたら**起動拒否**。未宣言コマンドも拒否 |
| **実行時の記録** | `tool_calls` に**その呼び出しに渡したツール集合**を残す。最小化が実際に効いていたかを事後に検証できる |

> [!note] pre-commit lint は追加しなかった（2026-07-31）
> `.pre-commit-config.yaml` に既存の EGRESS 向け lint は無かった。`registry_for` の
> fail-closed 判定（EGRESS混入・不明ツール名混入・未宣言コマンドをすべて例外にする）と、
> `tests/argus/test_pm_argus_agent_pure.py` の
> `test_all_tools_are_classified_in_command_tools_or_egress`（新ツール追加時に
> `COMMAND_TOOLS` か `EGRESS_TOOL_NAMES` への分類を強制する安全網）で、pre-commit の
> `pytest` フック経由で同じ不変条件が担保される。lint の重複追加はしない。

### 4.2 出力ブローカー（`scripts/argus/output_broker.py` 新設）

> [!important] ブローカーの位置（2026-07-31 訂正）
> **ブローカーはツール層ではなく輸送層に置く。** 当初は「モデルが選ぶ EGRESS ツール」を出口と定義していたが、これは誤りだった。実際の出口は `canvas_utils.post_to_canvas` / `box_cli.box_upload_or_version` / Slack SDK の直接呼び出しであり、**モデルのツール呼び出しを経由しない自動投稿パイプラインが存在する**（§4.8）。ツール層に置くと、そちらが構造的に漏れる。
>
> **チョークポイントは輸送層の1段手前。** ただし輸送ごとにファネルの有無が違う（下記）。

#### 輸送ごとのファネルの有無（2026-07-31 実測）

> [!warning] 「呼び出し箇所は6箇所」は誤り
> 前版は「`canvas_utils` / `slack_post` の1段手前、実測6箇所なので grep で閉じる規模」と書いたが、**これは自動投稿の5箇所だけを数えた数字だった。** 出口の全数は下記のとおりで、**Slack にはそもそもファネルが存在しない。**

| 輸送 | 単一ファネル | 実測 | ブローカーの入れ方 |
|---|---|---|---|
| **Canvas** | **あり** — `canvas_utils.post_to_canvas`（内部で `canvases_edit` 3箇所） | 呼び出し **8箇所 / 8モジュール**。全員がこの関数を通る | **1箇所に入れれば全経路を覆う** |
| **Box** | **あり** — `box_cli.box_upload_or_version` | **9モジュール**が利用 | 同上 |
| **Slack** | **ない** | **SDK 直叩き 25箇所 / 7モジュール** | **ファネルを新設して移送する** |

**`scripts/utils/slack_post.py` は輸送層ではない。** 中身は `_to_slack_mrkdwn` と `_split_mrkdwn_to_blocks` の2関数だけで、投稿関数を持たない。mrkdwn 整形ヘルパである。実際の送信は各モジュールが Slack SDK を直接呼んでいる。

```
narrate.py             10   （chat_postMessage 5 + files_upload_v2 2 ほか）
pm_qa_server.py         5
pm_argus.py             3
transcribe_pipeline.py  3
patrol/actions.py       2
output_tools.py         1
pm_minutes_import.py    1
```

**したがって Canvas と Box は安く、Slack だけが高い。** Phase 3 の工数はこの非対称性を前提に見積もる。Slack は `slack_post.py` に投稿関数（`post_message` / `post_ephemeral` / `upload_file`）を新設し、25箇所をそこへ移送したうえでブローカーを噛ませる。

#### テキスト以外の出口

**`/argus-narrate` は LLM 生成テキストから mp3 / mp4 を合成し `files_upload_v2` で Slack に投稿する**（[narrate.py:146](../scripts/argus/narrate.py#L146) / [:232](../scripts/argus/narrate.py#L232)）。この出口は次の点で他と異なる。

- EGRESS ツール3つに**含まれない**（モデルが呼ぶツールではなく、コマンドハンドラが直接実行する）
- **テキストDLPが中身を検査できない。** エントロピー・Base64様・ゼロ幅文字はいずれも音声波形に適用できない
- **text canary も音声化されると検出できない**（§4.3 の検知点3つはすべて文字列前提）

**対策は順序制約で足りる。** 合成前のテキストをブローカーに通し、**通過したテキストだけを TTS に渡す**。既存の DLP がそのまま効く。設計上の原則として次を置く。

> **テキスト以外の成果物は、生成元テキストの検査をもって代える。生成元テキストを検査せずに合成・投稿してはならない。**

**ブローカーが受け取る producer は2種類ある。** どちらも LLM 生成の自由文を運ぶ。

| producer | 経路 | 人間の介在 | 量 |
|---|---|---|---|
| **エージェント選択型** | `/argus-investigate --to-slack` など。モデルが EGRESS ツールを呼ぶ | 人がコマンドを打つ | 少ない |
| **パイプライン型** | 毎朝のブリーフィング（cron）、Patrol Agent の投稿 | **なし** | **大半** |

**実際に外へ出ている出力の大半はパイプライン型であり、かつ誰も見ていない。** ブローカーの主対象はこちらである。

Read Plane（およびパイプライン）は宛先を持たない Artifact を produce するだけにする。

```python
@dataclass(frozen=True)
class Artifact:
    artifact_id: str          # uuid
    session_id: str
    created_at: str
    kind: str                 # 'investigation' | 'brief' | 'risk' | 'draft' | 'patrol'
    content_md: str
    destination_ref: str      # ← enum キー。URL/channel_id ではない
    cited_row_ids: list[str]  # 根拠として参照した行（監査用）
    model: str
    model_revision: str
```

`destination_ref` は `config/egress_targets.yaml` のキーのみを許す。

```yaml
# config/egress_targets.yaml
targets:
  leader_meeting_canvas:
    type: canvas
    canvas_id: "F0XXXXXXX"
    visibility: internal          # internal | external_visible
    free_text_allowed: false      # 表の列更新のみ
  pm_reports_box:
    type: box
    folder_id: "1234567890"
    visibility: internal
    free_text_allowed: true
    requires_human_approval: false
  app_dev_slack:
    type: slack
    channel_id: "C0XXXXXXX"
    visibility: internal
    free_text_allowed: true
    requires_human_approval: false
  # 外部が閲覧可能な宛先は必ず承認必須にする
  collab_shared_slack:
    type: slack
    channel_id: "C0YYYYYYY"
    visibility: external_visible
    free_text_allowed: true
    requires_human_approval: true
```

ブローカーの検査順序（**すべて fail-closed**）：

1. `destination_ref` が allow-list に存在するか。存在しなければ破棄して警報
2. **canary スキャン** — アクティブなトークンが `content_md` または引数に含まれていないか（§4.3）
3. **DLP** — 下記
4. サイズ上限（`kind` ごとに設定。異常に長い出力はそれ自体が signal）
5. `content_md` 内の URL が既知の内部ホストか。未知ホストがあれば承認必須へ
6. `free_text_allowed: false` の宛先には構造化出力のみ通す
7. `requires_human_approval: true` なら Web UI のレビューキューへ
8. `tool_calls` / `egress_log` に追記記録
9. Write Worker へ引き渡し

DLP の中身：

| 検査 | 目的 | 捕捉できるもの |
|---|---|---|
| フィールド単位の Shannon エントロピー閾値 | Base64/暗号化ペイロード | 明示的な符号化 |
| Base64/hex 様パターン（長さN以上） | 同上 | 同上 |
| **ゼロ幅文字・異体字（homoglyph）検出** | 不可視の隠しチャネル | **安価で確実に効く。必ず入れる** |
| 未知URL・未知ドメイン | 送信先の逸脱 | モデル構築ホスト名 |
| 出力長の異常 | 過剰な情報の持ち出し | 量的逸脱 |

> **正直な限界** — 上記はいずれも TrojanStego 型（自然な散文への符号化）を捕捉しない。散文への符号化に対する防御は DLP ではなく**宛先を内部限定にすること**である。DLP は安価な層として入れるが、主防御として扱わない。

> [!important] 人間承認は「内容」ではなく「宛先」に対して行う
> **本文書自身が「散文への符号化は人間には見えない」と認めている。** したがって「この内容を出してよいか」を問う承認キューは、**符号化に対しては儀式にすぎない。** レビュアーは符号化を見抜けないし、見抜けないことを責められない。
>
> 承認が実際に機能するのは次の2つである。**承認画面の問いをこの2つに絞る。**
>
> | 問い | 何に効くか |
> |---|---|
> | **「そもそもこの宛先に出してよいか」** | R1・R7（外部閲覧可能な宛先、pm.db 閲覧権限との不整合）。**人間にしか判断できず、人間が実際に判断できる** |
> | **「この内容は事実として正しいか」** | R5（改竄）。誤りは人間に見える |
> | ~~「符号化されていないか」~~ | **効かない。問うてはならない** |
>
> **これを明記しないと、レビュアーが誤った安心を持って形式承認に流れる。** 「承認済み」が「符号化されていないことの保証」と読まれるのが最悪の帰結である。承認 UI には**何を見て何を見ていないか**を明示する。

### 4.3 canary（最初に実装する）

**新テーブル**（pm.db）：

```sql
CREATE TABLE canary_tokens (
  token       TEXT PRIMARY KEY,   -- 例: 'ARGUS-CANARY-7f3a91c2'
  planted_in  TEXT NOT NULL,      -- 'action_items' | 'decisions' | 'minutes' | 'box_docs' | 'slack'
  row_ref     TEXT,               -- 埋めた行の参照
  planted_at  TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1,
  kind        TEXT NOT NULL,      -- 'text' | 'hostname'
  notes       TEXT
);
```

既存テーブルに `is_canary INTEGER DEFAULT 0` を追加し、レポート系クエリ（`pm_report.py` / `pm_xlsx_report.py` / `canvas_report.sh`）で除外する。

**2種類植える。**

- **text canary** — 現実的に見える架空のアクションアイテム・決定事項に一意トークンを埋める。ブローカー・`tool_calls.args_json`・思考トレースをスキャン
- **hostname canary** — 架空の Box 文書に一意のホスト名（例 `docs-a91c2f.internal-check.invalid`）を書く。**Fetch Plane がこれを解決/取得しようとしたら極めて強いシグナル**。DNS クエリログでも検知できる

> [!note] hostname canary の検知点は net_guard の DNS 遮断導入で移動した
> `scripts/utils/net_guard.py` は enforce モードで `getaddrinfo`（名前解決）自体を
> allow-list 照合の対象にした。canary ホスト名は allow-list に無いため、hostname
> canary への到達は**外部の権威 DNS サーバへクエリが出る前に** net_guard の
> `verdict=deny stage=resolve` ログとして検知される。したがって hostname canary の
> 主たる検知点は「DNS クエリログ」から「net_guard のログ（`verdict=deny` を ERROR
> レベルで記録）」へ移る。DNS クエリログでの検知は enforce 未適用のプロセス
> （warn モードのプロセスや net_guard 未導入の経路）に対する副次的な検知点として残る。

検知点は3つ。

1. ブローカー（送信前）
2. `tool_calls.args_json`（ツール引数）
3. **`reasoning_content`（思考トレース）** ← Kimi-K3 は思考を常に返すので、K3導入時にこの検知面が広がる。K3提案の優先度1(c)（思考トレースの逐次永続化）と同じコードで実現できる

**検知点3つはいずれも文字列を前提とする。** したがって `/argus-narrate` のように**テキストが音声・動画に変換されて出ていく経路では canary が機能しない**（§4.2）。合成前のテキストをブローカーに通す順序制約が、canary の検知面を保つ役割も兼ねる。

> [!warning] Phase 1 時点の canary 被覆は少数派である（P8 の検算）
> **検知点1（ブローカー）は Phase 3 まで存在せず、Slack 25箇所にファネルが入るのも Phase 3 である。** したがって Phase 1〜2 の間、canary が覆うのは**検知点2（ツール引数）と検知点3（思考トレース）だけ**——**流出が実際に現れる出力側がほぼ覆われていない。**
>
> この状態で「2週間 canary が発火しなかった」を安全の証拠として扱ってはならない。**覆っていない面で起きたことは、発火しないのではなく観測されない。**
>
> したがってフェーズのゲートを次のように分ける（Phase 1 / Phase 3）。
>
> | 時点 | ゲートにできること | できないこと |
> |---|---|---|
> | Phase 1 | **計装の完全性**（全経路が `tool_calls` に記録されている） | 「発火しなかった」の安全判定 |
> | Phase 3 以降 | 出力側を含む**全検知点での**発火監視 | — |

> **正直な限界** — canary は無差別な流出とプロンプト注入駆動の流出を捕まえる。**特定条件で発火する外科的なバックドアは canary に触れない可能性がある。** それでも、安価で、既存コードをほとんど触らず、現状の危険度を実測できるので最初に入れる価値がある。

### 4.4 監査ログの拡張

現状の `audit_log` は「全変更履歴」＝DB変更の追跡であり、**ツール呼び出しと引数は残らない**。egress ログとして不十分。

**新テーブル**：

```sql
CREATE TABLE tool_calls (
  call_id         TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  seq             INTEGER NOT NULL,     -- セッション内連番
  ts              TEXT NOT NULL,
  plane           TEXT NOT NULL,        -- 'read' | 'mutate' | 'egress'
  tool_name       TEXT NOT NULL,
  args_json       TEXT NOT NULL,
  args_max_entropy REAL,
  result_bytes    INTEGER,
  result_sha256   TEXT,
  model           TEXT NOT NULL,        -- 'glm-5.2' | 'kimi-k3'
  model_revision  TEXT NOT NULL,
  reasoning_sha256 TEXT,                -- 思考トレースのハッシュ
  outcome         TEXT NOT NULL,        -- 'ok' | 'blocked' | 'error'
  block_reason    TEXT,
  prev_hash       TEXT NOT NULL,
  entry_hash      TEXT NOT NULL
);

CREATE TRIGGER tool_calls_no_update BEFORE UPDATE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;

CREATE TRIGGER tool_calls_no_delete BEFORE DELETE ON tool_calls
BEGIN SELECT RAISE(ABORT, 'tool_calls is append-only'); END;
```

`entry_hash = sha256(prev_hash || call_id || ts || tool_name || args_json || outcome)` のハッシュ連鎖により、**過去エントリの改竄が検出可能**になる。追記専用トリガと合わせて実務上の不変性を得る。

> [!warning] 連鎖の頭が同じ信頼領域内にあると意味を失う
> §9.2 は週次でハッシュ連鎖を検証するが、**検証者は改竄されうる側と同じプロセス・同じ UNIX ユーザで動く。** コード実行を取られたら、エントリと連鎖の頭の両方を書き換えれば整合したまま改竄できる。**内部で完結した連鎖は「事故による破損」は検出できるが「意図的な改竄」は検出できない。**
>
> **安価な解 — 連鎖の頭を外部の追記専用アンカーに固定する。** 日次で `entry_hash` の最新値（32バイト）を、**ブローカー経由で専用の Slack チャンネルへ投稿する。**
>
> - 既存の egress をそのまま使うので新しい外向き経路が増えない
> - Slack の投稿は Argus が後から編集できない（**Argus 側にその権限を与えない**）
> - **ハートビートも兼ねる** — 投稿が途絶えたこと自体がシグナルになる
> - 過去のある日の状態と現在の連鎖が矛盾すれば、その日以降の改竄が確定する
>
> 投稿先は `egress_targets.yaml` に `audit_anchor_slack`（`free_text_allowed: false`、内容はハッシュと連番のみ）として定義する。

#### `reasoning_traces` の保護と保持

思考トレース本体は別テーブル（`reasoning_traces`）に保存し、`tool_calls` はハッシュだけ持つ。トレースは容量が大きく保持期間も異なるため。

**ただしこのテーブルは、モデルが見た機微データがそのまま入る新しい機微データストアである。** 容量と保持期間だけの問題として扱ってはならない。

- **`pm.db` 内に置き、SQLCipher の適用対象とする**（別 DB にする場合も暗号化必須）
- **保持期間を明示的に定める。** canary 調査には直近が要るが、無期限に持つ理由はない。既定は90日とし、canary 発火時は該当セッションを別途保全する（Phase 1 のランブック）
- **§1.2 の機微データ棚卸しに加える**（`data/processing/` を追加したのと同じ理由）
- レポート系クエリからは当然除外する

### 4.5 `pm_web_fetch.py` の廃止

**判断（2026-07-31）** — 隔離ではなく**廃止**する。リスクに対して得るものが少ない。

**なぜ隔離では足りないか** — 外部URLの取得は、攻撃者が管理しうるサーバへの外向きリクエストである。URL・パラメータ・ヘッダ・タイミングがそのままチャネルになり、**Box / Slack が認証で守られていることは一切の助けにならない**（宛先が認証境界の外だから）。しかも符号化を工夫する必要がなく、単純な Base64 で1回に数千ビットを運べる。allow-list ドメインに絞っても、そのドメインが攻撃者に観測可能なら成立する。

**廃止作業**

- [ ] `scripts/data-pipeline/pm_web_fetch.py` を削除
- [ ] `data/web_articles.db` の扱いを決める — 既存データを読取専用で索引に残すか、パージするか
- [ ] `pm_embed.py` から web_articles の索引対象を外す（残す場合は追記を止める）
- [ ] `retrieval.py` / `mcp_tools.py` から関連ツールを外す
- [ ] README の入力元一覧から「Web記事」を削除
- [ ] `pm_slack_box_links.py` が外向きHTTPを持っていないか確認（Phase 1 の棚卸し項目）

**代替手段** — Web記事を取り込む必要が生じたら、**人間が介在する経路**にする。人が記事を保存して Box に置き、既存の `pm_box_crawl.py` の流れに乗せる。これなら Argus 自身は外向き通信を持たず、取り込む内容に人間の判断が入る。頻度が低いなら手作業で十分。

**canary への影響** — hostname canary の主な検知点（Fetch Plane）が消えるが、むしろ**シグナルが明確になる**。正当なフェッチャーが存在しなくなるので、**canary ホスト名への名前解決が観測されたら、それは無条件に異常**である。DNS ログの監視だけで機能する。

### 4.6 供給網の固定

**新ファイル** `config/model_pin.yaml`：

```yaml
models:
  glm-5.2:
    endpoint: "${RIKYU_INFERENCE_ENDPOINT}"
    served_model_name: glm-5.2
    expected_revision: "<sha>"
    weights_format: safetensors
    trust_remote_code: false
    engine: vllm
    verified_at: "2026-07-31"
    verified_by: "<name>"
  kimi-k3:
    endpoint: "${RIKYU_INFERENCE_ENDPOINT}"
    served_model_name: kimi-k3
    expected_revision: "<sha>"
    weights_format: safetensors
    trust_remote_code: false        # ← 必須。transformers 経路は禁止
    engine: vllm                    # または sglang
    verified_at: null
    verified_by: null
```

起動時チェック（`scripts/utils/model_pin.py` 新設）：

1. `/v1/models` を叩き、`served_model_name` と revision が pin と一致するか
2. 不一致なら**起動を拒否**（fail-closed）
3. `verified_at` が null のモデルは本番経路で使わせない

> [!warning] 共有サービスでは revision を検証できない（2026-07-31 実測）
> **OpenAI 互換の `/v1/models` は `id` しか返さない。** 実際 `llm.py`:115 は `m["id"]` だけを取り出している。**チェックポイントの sha を Argus 側から取得する手段がない。**
>
> 理究が共有推論サービスである以上、これは実装で埋められる穴ではない。したがって pin は用途を分けて扱う。
>
> | 項目 | Argus 側で検証できるか | 扱い |
> |---|---|---|
> | `served_model_name`（モデル id） | **できる。** `/v1/models` と照合 | **不一致なら起動拒否**（fail-closed を維持） |
> | `expected_revision`（チェックポイント sha） | **できない** | **運用主体の申告の記録**として残す。`declared_by` / `declared_at` を併記し、**検証ではなく申告であることを明示する** |
> | `trust_remote_code` / `engine` / `weights_format` | できない（サービス側の設定） | 同上。申告の記録 |
>
> **`expected_revision` の不一致で起動拒否はできない**（そもそも取得できないため）。代わりに次を行う。
>
> - **申告の変更を検知する。** `model_pin.yaml` は git 管理下にあるので、申告値が変われば diff に現れる。**モデル更新の通知を運用主体から受ける取り決めを Phase 0 で作る**（技術ではなく合意で埋める）
> - **モデルの挙動側で代替検知する。** §9.2 の日次確認に「同一プロンプトに対する応答の性質が急変していないか」を加える。revision が黙って変わった場合、これが唯一の手がかりになる
>
> **この制約を明記しないと Phase 2 のゲートが満たせない。** ゲートの文言は「revision が一致していること」ではなく「**モデル id が pin と一致し、revision の申告が記録されていること**」とする。

**Kimi-K3 固有の注意** — K3 は HuggingFace 上で `custom_code` タグ付きであり、モデルカードの transformers 使用例は `trust_remote_code=True` である。KDA が新規アーキテクチャのため transformers 経路では必須になる。**これは §1.3 の攻撃者2に直接該当する具体的かつ現在形のリスク**。理究が K3 を vLLM / SGLang のネイティブ対応で動かしていることを確認し、`model_pin.yaml` に記録する。重み自体は safetensors なので形式は安全。

### 4.7 外部アクセスの allow-list（3層）

**方針（2026-07-31 決定）— 外部アクセスはツール・MCP を含めてすべて allow-list 方式にする。**

3層すべてで fail-closed（リストにないものは拒否）とする。層を分けるのは、1層が破られても他が残るようにするため。**1層目はコードの外側で強制できるので最も強い。**

#### 層1：ネットワーク層（到達可能ホスト）

**実測による接続先の全数（2026-07-31 訂正）** — `pm_web_fetch.py` を廃止しても **9エントリ / 8ホスト**ある（`127.0.0.1` が VOICEVOX と fish-speech の2ポートを持つため、エントリ数とホスト数が一致しない）。**以降の記述はすべて「9エントリ / 8ホスト」で統一する。**

> [!warning] `${ENV_VAR}` を並べた allow-list は制約にならない（2026-07-31 訂正）
> 当初 `host: "${RIVAULT_URL}"` のように**接続に使うのと同じ環境変数で allow-list を定義していた。** 環境変数を書き換えれば接続先と許可エントリが同時に動くので、**これらのエントリについて層1 は何も禁止していなかった。** `slack.com` がリテラルなのと非対称である（→ P9）。
> 型も合っていなかった。`RIVAULT_URL` / `EMBED_API_BASE` / `DOCLING_SERVE_URL` は**スキーム付きURL**（`http://host:port/v1`）であって `host:` に入る値ではない。

**リテラル値で書き、実行時の値を照合する。**

```yaml
# config/network_allowlist.yaml
# 値はリテラル。環境変数で定義してはならない（P9）
read_plane:
  - host: "rikyu-inference.example.riken.jp"   # 理究の推論（GLM-5.2 / Kimi-K3）
    port: 443
    from_env: RIKYU_INFERENCE_ENDPOINT          # 起動時に urlparse して照合する対象
  - host: "rivault.example.riken.jp"            # RiVault — 議事録・Slack抽出・judge
    port: 443                                   # ★第2のLLMプロバイダ。model_pin の対象
    from_env: RIVAULT_URL
  - host: "dgx-spark.example.riken.jp"          # bge-m3 embedding（ローカル vLLM）
    port: 8001                                  # ★443 ではない
    from_env: EMBED_API_BASE                    # ★落とすとハイブリッド検索が静かに壊れる
ingest_plane:
  - host: "docling.example.riken.jp"            # Box取込の文書変換
    port: 443                                   # ★注入経路上にある（§1.3 攻撃者1の通り道）
    from_env: DOCLING_SERVE_URL
broker:
  []                                            # 外向き通信なし
write_plane:
  - host: "slack.com"
    port: 443
  - host: "api.box.com"
    port: 443
  - host: "upload.box.com"
    port: 443
  - host: "127.0.0.1"                           # VOICEVOX（TTS の既定バックエンド）
    port: 50021
  - host: "127.0.0.1"                           # fish-speech（FISH_TTS_HOST 設定時のみ）
    port: 8080
    from_env: FISH_TTS_HOST
```

**起動時の照合ロジック**（`scripts/utils/network_pin.py` 新設）

```python
from urllib.parse import urlparse

def verify_endpoints(plane: str, allowlist: list[dict]) -> None:
    """実行時の環境変数の値が allow-list のリテラルと一致するか検証する。
    一致しなければ起動を拒否する（fail-closed）。"""
    for entry in allowlist:
        if (env := entry.get("from_env")) is None:
            continue
        raw = os.environ.get(env)
        if raw is None:
            raise StartupError(f"{env} が未設定（P6：静かに劣化させない）")
        u = urlparse(raw)
        if u.hostname != entry["host"] or (u.port or default_port(u)) != entry["port"]:
            raise StartupError(
                f"{env}={raw} が allow-list（{entry['host']}:{entry['port']}）と不一致"
            )
```

これは §4.7 が新設した到達性アサーションと同じ場所に置ける。

> [!note] 実測で判明した細部の訂正
> - **EMBED は 443 ではない。** bge-m3 はローカル vLLM（DGX-Spark、**8001**）。到達性アサーションを入れる以上、ここが違うと起動拒否で止まる
> - **VOICEVOX（`localhost:50021`）が抜けていた。** `pm_tts.py` :32 / :56 によれば**既定バックエンドは VOICEVOX** で、fish は `FISH_TTS_HOST` 設定時のみ。allow-list に載せるなら既定側こそ必要
> - **TTS は ingest ではなく出力側。** `/argus-narrate` を qa サーバが呼ぶ。したがって Write Plane に置く
>
> 同じ `${ENV_VAR}` パターンが §4.6 の `model_pin.yaml`（`endpoint: "${RIKYU_INFERENCE_ENDPOINT}"`）にもあるが、あちらは `served_model_name` と `revision` で縛るので影響は小さい。ただし整合のため同じ照合を通す。

強制手段は理究の運用制約次第だが、優先順に：

1. network namespace / iptables による egress フィルタ（最も強い）
2. HTTP プロキシ経由を強制し、プロキシ側で allow-list
3. 最低限、**Read Plane に Box/Slack の認証トークンを渡さない**（トークンがなければ届いても意味がない）

3は必ず実施する。1と2は運用と相談。

> [!important] fail-closed は「大きく失敗する」ことと対にする
> **遮断は静かに起き、機能の劣化も静かに起きる。** allow-list に `EMBED_API_BASE` を書き忘れて Read Plane から embedding が届かなくなると、**ハイブリッド検索が FTS 単独に静かに劣化する**。
> **2026年6〜7月に、この劣化が実際に約1か月間気づかれずに起きた前科がある。**
> したがって allow-list には必ず以下を対にする。
> - **起動時の到達性アサーション** — 期待する依存先ごとに疎通を確認し、届かなければ**起動を拒否する**（silently degrade させない）
> - **ランタイムの健全性チェック** — ハイブリッド検索の **vector 脚が空なら警告**。日次で監視する
> - 同様に、`model_pin.yaml`（§4.6）の対象に **RiVault を含める**。第2のLLMプロバイダであり、供給網リスクの管理対象である

#### 追加で棚卸しが必要な2つ

- **RiVault** — 議事録生成・Slack抽出・judge で使用中の**第2のLLMプロバイダ**。GLM-5.2 / Kimi-K3 と同じく `model_pin.yaml` の管理対象とし、モデルとバージョンを記録する。§1.3 攻撃者2・4 の評価対象でもある
- **Docling** — Box 文書の変換サービス。**注入経路上にある**（§1.3 攻撃者1の通り道）。単なる外向き接続先としてではなく、悪性文書が通過するコンポーネントとして扱う。自ホストか外部かを確認し、自ホストならバージョン固定の対象に加える

#### 層2：ツール・MCP層（登録可能なサーバとツール）

**MCP サーバは外部プロセスへの接続なので、これ自体が外部アクセスである。** 登録できるサーバを固定する。

> [!warning] この層が守るのは開発面であって本番の同居問題ではない（P8）
> **`pm_mcp_server` は本番デーモンの起動スクリプト（`scripts/bin/*.sh`）に一切現れない。** MCP は Claude Code など外部オーケストレータ用の**開発時経路**であり、本番の `/argus-investigate` は `agent_tools` を直接使う。
> **したがって MCP allowlist の本番 egress 被覆率はほぼゼロである。** 同居問題（§4.1）への対策としては数えない。
>
> **それでも入れる価値はある。** 守る対象が違うだけで、開発面は実在するリスク面である——Claude Code はこのリポジトリと機密ファイルに触れ、`.claude/settings.json` には現に GitHub PAT が平文で残っている。**「本番の防御」ではなく「開発環境の防御」としてラベルを付けたうえで維持する。**

```yaml
# config/mcp_allowlist.yaml
servers:
  pm-multi-agent:
    transport: stdio
    # プロジェクト規約：素の python3 は不可（sqlcipher3 が無く起動しない）
    command: "~/.venv_aarch64/bin/python3 scripts/pm_mcp_server.py"
    scope: local                  # ネットワーク越しではない
    tools_allowed: ["*"]          # ただし Plane による制限は別途かかる
# 上記以外のMCPサーバの登録を禁止する
deny_unlisted: true
```

- Argus のエージェントランタイムは、このリストにないMCPサーバに接続しない。**起動時に検証し、未登録サーバがあれば起動拒否**
- Claude Code など外部オーケストレータを使う場合、その `.claude/settings.json` の MCP 設定もレビュー対象に含める。**そこに別のMCPサーバが混ざれば層1と層3を迂回できる**
- ツール個別にも allow-list を持つ。`registry_for()`（§4.1）が Plane で絞ったうえで、さらにこのリストと照合する
- **Kimi Code CLI を採用しない判断（§6）は、この層の一部である。** 提供元のCLIは実行可能コードであり、どのMCPサーバや外部エンドポイントに繋ぐかを Argus 側で保証できない

#### 層3：宛先層（送信先識別子）

`config/egress_targets.yaml`（§4.2）。モデルは識別子を選ぶだけで、URL・ホスト名・channel_id を構築できない。

#### 3層の関係

| 層 | 何を防ぐか | 破られた場合 |
|---|---|---|
| 1 ネットワーク | 未知ホストへの到達そのもの | 層2・層3が残る |
| 2 ツール・MCP | 想定外の外部プロセス経由の迂回 | 層1が止める（層1が実装されていれば） |
| 3 宛先 | 許可済みホスト内での宛先の逸脱（例：Slack Connect チャンネル） | 層1・層2は通ってしまう。**ここは独自に効く** |

層3が独立して重要なのは、**Slack や Box は許可ホストなので層1を通過してしまう**ためである。「slack.com へ送ってよいか」と「そのチャンネルに送ってよいか」は別の問題。

### 4.8 LLM生成コンテンツの流路と管理対象

**設計上の穴（2026-07-31 追加、同日に2度一般化して訂正）。**

> [!important] §4.8 と §4.9 は同じ現象である
> 当初は「洗浄経路」（§4.8）と「出力の完全性」（§4.9）を別の節として書いたが、**両者は同一の現象を出口側と入口側から見たものだった。**
>
> - **入口** — Pass 1 ingest が LLM 抽出の自由文を pm.db に書く。ここで欠落・改変が起きれば**改竄**（R5）
> - **出口** — その pm.db の内容が週次レポートで Canvas に出る。ここで符号化されていれば**洗浄**
>
> **管理対象は1つ：「LLM が生成し pm.db に入り、やがて外に出るコンテンツ」。** 本節がその流路と入口側、§4.9 が完全性の検証を扱う。対策が重複しないようこの分担にする。

#### 入口の棚卸し（実測）

**LLM 生成の自由文が pm.db に入る経路は2本。どちらもツールレジストリを通らない。**

| 経路 | 実装 | 内容 | 量 |
|---|---|---|---|
| **Pass 1 ingest** | `ingest/slack.py` :973 / :989、`ingest/minutes.py` :353 / :376 が `decisions` / `action_items` に INSERT | LLM 抽出の自由文そのもの（`decisions.content`、アクションアイテムの説明・担当・期限） | **圧倒的に大半** |
| **Patrol の自動クローズ** | `patrol/actions.py` :310 が `status` を closed に、:341 の `_append_close_note` が `note` に根拠テキスト（200字）を追記 | 自由文（根拠抜粋） | 少量。`audit_log` 記録あり |

> [!warning] MUTATE ツール経由という前提は誤りだった
> 当初「MUTATE → 週次レポート → Canvas」を洗浄経路として書いたが、**MUTATE ツールは存在しない**（§4.1）。真の入口は Pass 1 ingest である。
> **これは「構造化フィールドのみに制約する」という当初のB案が成立しないことを意味する。** `decisions.content` は抽出された決定事項そのものであり、自由文であることが成果物の本質だからである。**入口側の制約では解決できない。**

#### 出口の棚卸し（実測）

**モデルのツール呼び出しを経由しない自動投稿が2本ある。**

| # | 経路 | 実装 | ブローカー |
|---|---|---|---|
| 1 | **毎朝 6:57 のブリーフィング（cron）** | `pm_argus.py` :2169 / :2213 / :2239 が `canvas_utils.post_to_canvas` を直接呼ぶ | 通らない |
| 2 | **Patrol Agent → リーダー会議チャンネル** | `patrol/actions.py` :273 / :395 が `ctx.slack.chat_postMessage` を直接呼ぶ | 通らない |
| 3 | **Pass 1 ingest** → pm.db → 週次レポート → Canvas | `canvas_report.sh` / `pm_report.py` | 通らない（間接） |
| 4 | **`/argus-narrate` の音声・動画** | `narrate.py` :146 / :232 が `files_upload_v2` で mp3/mp4 を投稿 | 通らない。**テキストDLPが原理的に効かない**（§4.2） |
| 5 | **議事録パイプラインの投稿** | `pm_minutes_import.py`（1箇所）/ `transcribe_pipeline.py`（3箇所）が Slack SDK を直接呼ぶ | 通らない。**Ingest Plane の定義と矛盾**（§3.2） |

1・2・4・5 は `output_tools.py` を経由しない。したがって**「`output_tools.py` をブローカー経由に書き換える」だけでは素通りのままである。**

性質が悪い理由が4つある。

- **モデルが選んだツール呼び出しではない。** パイプラインが定型的に呼ぶ投稿なので、**Plane 分類（§4.1）でも捕まらない。** EGRESS ツールを Read Plane から除いても影響を受けない
- **人間が介在しない。** cron と30分間隔の自律巡回。`/argus-investigate --to-slack` は少なくとも人がコマンドを打つが、こちらは誰も見ていない
- **内容は LLM 生成の自由文そのもの。** DLP の主対象になるべきもの
- **量的に大半。** 日次＋巡回なので、実際に外へ出ている出力の多数派はこちら

**ブローカーの対象を「モデルが選ぶ EGRESS ツール」と定義すると、守られる範囲が実際の出力量の少数派になる。**

#### 対応

**§4.2 のとおりブローカーを輸送層に置く。** ただし **Canvas と Box は既存のファネル1箇所で覆えるのに対し、Slack はファネル自体を新設して25箇所を移送する必要がある**（§4.2 の実測表）。工数はこの非対称性に支配される。

Phase 3 のゲートに次を追加する。**特定の関数名を列挙する書き方は取らない**（`chat_postEphemeral` や `files_upload_v2` が漏れるため）。

> **LLM が生成したコンテンツが、ブローカーを経由せずに Slack SDK クライアント・`post_to_canvas`・`box_upload_or_version` に到達する経路が存在しないこと**（grep で検証できる状態）

#### 入口側の対策

**Pass 1 ingest の自由文は制約できない**（それが成果物だから）。したがって入口側の対策は「制限」ではなく「記録と検証可能性の確保」になる。

| 対象 | 対策 | 評価 |
|---|---|---|
| **Pass 1 ingest**（大半） | **引用スパンの必須化と逐語照合**（下記）＋原文との照合可能性の確保（§4.9）＋件数の異常検知＋人間レビュー | **これが本体。** 一部は事後検知から事前防止に変わる |
| **Patrol の close note**（少量） | **「引用」に限定する。** 生成した散文ではなく、引用元の row_id と逐語一致する抜粋のみを許す。**逐語一致は機械的に検証できる** | **採用。安価で確実** |
| 将来追加される MUTATE ツール | 構造化フィールドのみ（`MUTABLE_FIELDS`）。**現在0件なので今は空振り**だが、枠として定義する | 予防的 |

#### 引用スパンの必須化（Patrol 向け対策の Pass 1 への一般化）

**Patrol の close note に採る「引用に限定し逐語一致を機械的に検証する」は、Pass 1 ingest にも一般化できる。** 抽出した各決定事項・アクションアイテムに、**一次情報源内の逐語スパンを必須で持たせ、存在を機械的に検証する。**

**現状（2026-07-31 実測）— Slack 側は半分できている。**

| ソース | 現在の `source_ref` | 逐語照合 | 必要な追加 |
|---|---|---|---|
| **Slack** | `permalink`（無ければ `slack://{channel_id}/{thread_ts}`）が**既に必須で入っている**（`ingest/slack.py`:934） | **していない**（ポインタがあるだけ） | **粒度をスレッドから個別 `ts` へ。** 照合処理を追加。`slack.db` に生メッセージがあるので容易 |
| **議事録** | `file_path` **のみ**（`ingest/minutes.py`:328）。`source_context` は LLM が書いた出典文字列で、原文オフセットではない | していない | **原文内の開始・終了オフセット。ただし原文が無いので Phase 4（`raw_transcript`）に依存する** |

**したがって追加すべきは「ポインタの付与」ではなく「照合の実施」と「粒度の細分化」である。** Slack 側は既存の `source_ref` の上に載る。

> [!warning] 「捏造は原理的に不可能になる」は言い過ぎである
> 逐語照合が証明するのは**引用された根拠が実在すること**だけで、**その根拠から結論が導かれること**ではない。決定事項の抽出は本質的に**要約的（abstractive）**であり、複数の発言をまたぐ統合であることが多い。逐語一致を本文そのものに課すと、品質が落ちるか、モデルが「それらしいスパン」を選ぶだけになる。
>
> | 改竄の型 | 引用スパン必須化の効果 |
> |---|---|
> | **存在しない根拠の捏造**（言っていないことを言ったことにする） | **防げる。** 検証で落ちる |
> | 根拠は実在するが結論が違う（誤読・歪曲） | 防げない。人間レビューと2系統差分が担当 |
> | **欠落**（あるものを落とす） | 防げない。下記 |

**欠落（omission）への対策 — 却下した候補を残す。**

**議事録側には既に triage 機構がある**（`ingest/minutes.py`:352 前後）。LLM が DROP 判定した候補は削除されず `deleted=1` で登録され、理由が `_write_triage_audit` で記録される。**捨てたものが見える構造が既にある。** これを Slack 側にも広げる。

> [!note] 記録できる欠落とできない欠落
> triage が残せるのは「**候補として挙がったが採用しなかったもの**」だけである。**抽出LLMが最初から出力しなかったものは、記録のしようがない。**
> これを覆うには抽出を**二段（候補列挙 → 採否判定）**に分け、一段目を高再現率で回す必要がある。コストが増えるので、**まず件数の異常検知と2系統差分でどこまで見えるかを Phase 4 で測ってから判断する。**「8件のうち1件落とす」は件数検知では捕まらないという指摘は正しく、二段抽出はその答えになりうる。

**出口側は §4.2 の輸送層ブローカーが全経路を覆う。** 入口側で自由文を止められない以上、出口の検査が実質的な最後の関門である。

> [!note] P8 の適用例
> 当初のB案（MUTATE の構造化制約）は、**被覆率0%の対策だった。** 実装すれば「完了」になるが何も守らない。しかも「守られた」という誤った確信を与える点で、何もしないより悪い。P8 の検算を通していれば設計段階で気づけた。

```python
# MUTATE ツールのフィールド制約の例
MUTABLE_FIELDS = {
    "action_items": {
        "status":       Enum("open", "in_progress", "done", "dropped"),
        "due_date":     Date,
        "milestone_id": ForeignKey("milestones"),
        "assignee":     Enum(*known_roster()),       # 既知の担当者のみ
        # description / notes は含めない（自由文のため）
    },
    "decisions": {
        "confirmed":    Bool,
    },
}
```

### 4.9 完全性の検証（改竄リスク）

§4.8 の管理対象を**入口側から**見た節。**これまでの他の節はすべて「情報が外へ出る」ことへの対策だが、改竄は経路を必要としない。**

`pm.db` は正本であり、人間はそれを信じて判断する。**Pass 1 ingest** でモデルが以下をしても、外向き通信は1バイトも発生しない。

- 議事録生成で決定事項を1つ落とす
- 担当者や期限を微妙にずらす
- 特定の話題を要約から除く
- リスク検知で本来上げるべき項目を上げない

NIST/CAISI は**政治的検閲がAPIではなくダウンロードした重みに埋め込まれている**ことを確認しており、CrowdStrike は政治的に機微な語で脆弱コード率が有意に上昇する（19% → 27.2%）ことを観測している。**強い条件付き方針が重みに入りうる**ことは実証済みである。

流出が「見られる」被害なら、改竄は「誤った意思決定をする」被害であり、**気づきにくい分こちらの方が痛い可能性がある。**

**対策**

> [!warning] 現状、事後照合の土台が存在しない（2026-07-31 実測）
> R5 を主要残留リスクに置いたが、**それを検証する手段が今は無い。**
> - 議事録DB（`minutes/*.db`）は**原文の列を持たない**（`minutes_content` は生成物のみ）
> - `pm_from_recording.sh:540` が**成功時に Whisper 生文字起こし（.md）を削除する**
> - 平文で残るのは `data/processing/` の mp4 115 / VTT 115 / combined.txt 120
> - **`combined.txt` は Stage 1 の LLM 出力なので ground truth にならない**
> - LLM を通っていない独立記録は **VTT と mp4 だけ**。VTT のない会議では mp4 の再文字起こしが必要
>
> 「保持方針を決める」という段階ではなく、**穴が空いている**状態である。

**方針決定（2026-07-31）— Whisper 原文を暗号化DBに保持する。**

- `pm_from_recording.sh:540` の削除処理をやめる
- `minutes/*.db` に原文列（`raw_transcript`）を追加し、SQLCipher 下で保持する
- 保持期間を定める（プロジェクト期間＋一定年数など。情報管理規程に合わせる）
- 平文の `data/processing/` は暗号化またはパージする（§1.2 の保護レベル逆転）

**トレードオフの認識** — 機微データの保有量は増える。ただし現状は同じデータが**より弱い保護（平文）で置かれている**ため、暗号化DBへの移動と平文の除去を同時に行えば**正味では露出が減る**。

| # | 対策 | 状態 |
|---|---|---|
| 1 | **原文の保持。** 生成された議事録を後から文字起こし原文と照合できること | **方針決定済（上記）。Phase 4 で実装** |
| 2 | 人間レビュー | 既存（Canvas / Web UI で編集可能）。**`audit_log` からの統計は現状のスキーマで実装可能**（下記） |
| 3 | 同一入力を複数モデルで処理し差分を取る | 重要な会議のみ。**ただし「複数」ではなく「出自が独立した複数」でなければ効かない**（下記） |
| 4 | 決定事項の件数・アクションアイテム数の異常検知 | 安価。急減はシグナル |
| 5 | 政治的・地政学的に機微な語を含む議事録にフラグを立て、人間レビューを必須にする | 富岳NEXTの文脈では該当が少ないはずだが、国際共同研究では起こりうる |

**1が最重要。** 原文が残っていなければ、事後の検証が原理的に不可能になる。

> [!warning] 対策3 の前提は独立性であり、現状それが成立していない
> 「2〜3系統の差分を取る」は**誤りが相関しないこと**を前提にしている。しかし現在 Argus が使っているモデルは**すべて中国系で、うち2つは同一提供元**である。
>
> | 用途 | モデル | 出自 |
> |---|---|---|
> | investigate / brief / risk | GLM-5.2 | Zhipu（中国） |
> | 保留中の移行先 | Kimi-K3 | Moonshot（中国） |
> | 議事録生成・Slack抽出（RiVault） | **Kimi-K2-Thinking** | **Moonshot（中国）— K3 と同一提供元** |
> | モデル評価の judge | DeepSeek-V4-Flash | DeepSeek（中国） |
>
> §1.4 と §4.9 が引用している CAISI の知見は「**政治的検閲が API ではなくダウンロードした重みに埋め込まれている**」ことだった。**この種の偏りは同系統のモデル間で相関して現れる。** 3系統すべてが同系統なら、差分は「共通の誤りを共通に見逃す」だけになる。
>
> **出自の異なる参照系を1つ確保する。** `gemma4` は運用終了したが、**RiVault の配信一覧に非中国系が2つ残っている**（2026-07-31 実測）。
>
> | 候補 | 出自 | 評価 |
> |---|---|---|
> | **`meta-llama/Llama-4-Scout-17B-16E-Instruct`** | Meta（米） | **第1候補。** MoE 17B で候補中最大 |
> | **`gemma3:12b`** | Google（米） | **第2候補。** サイズの割に日本語が強い系統 |
> | `Qwen/Qwen3.6-27B・35B` | Alibaba（中） | **独立性の要件を満たさないので対象外** |
> | `google/translategemma-27b-it` / `llava:7b` / `codellama:7b` | — | 用途特化。判定には不適 |
>
> **ただし 17B / 12B は GLM-5.2・Kimi-K2 より大幅に小さい。** ここに落とし穴がある——**能力差による不一致が、偏りによる不一致を埋もれさせる。** 弱いモデルは「間違っているから」不一致になることが多く、不一致率だけを見ると信号が雑音に沈む。
>
> したがって測る指標は**不一致率ではなく「不一致のうち第2系統が正しかった割合」**とし、不一致サンプルを人間がラベル付けする。これを外すと実験そのものが無意味になる。
>
> Phase 0 の「RiVault のモデルとバージョン」の棚卸しは、供給網リスク（R8）だけでなく**この独立性の評価のためにも必要**である。

#### 欠落を作れる位置はどこか（2026-07-31 実測）

**「何を落とすかを決める」位置を特定しないと、独立性をどこに投入すべきか決められない。** 実測の結果、**本番経路の欠落生成点は3つあり、いずれも `call_argus_llm`（RiVault の Kimi-K2-Thinking または ローカルの GLM-5.2）で動いている。**

| # | 位置 | 実装 | 落とせるもの |
|---|---|---|---|
| 1 | **Pass 1 抽出そのもの** | `ingest/slack.py` / `ingest/minutes.py` | 出力しなければ落ちる。**記録も残らない**（R13） |
| 2 | **3ゲートトリアージ** | `ingest/slack.py`:625 `triage_items_batched`（Slack・議事録の両方から呼ばれる） | DROP 判定。**`deleted=1` ＋理由が残るので可視**（§4.8） |
| 3 | **Box relevance 判定** | `pm_box_relevance.py`:108 | 索引に入るかを決める。**入らなかった文書は検索にも現れない** |

> [!warning] judge（DeepSeek-V4-Flash）は本番経路にいない
> レビューで「judge が何を落とすかを決める位置にいる」と指摘されたが、**実測では DeepSeek-V4-Flash は `scripts/eval/` 配下（`argus_ab.py` / `argus_ab_judge.py` / `minutes_ab.py`）と `eval_rivault_models.py` にしか現れない。** オフラインのベンチマーク評価専用であり、**pm.db に欠落を作ることはできない。**
>
> **したがって judge を差し替えても本番の欠落リスクは下がらない。** 効くのは上記3箇所である。
>
> ただし judge にまったくリスクがないわけではない。**judge は「どのモデルを採用するか」の判断材料を作る。** 同系統モデルを系統的に高く評価する偏りがあれば、モデル選定が歪む。速度は遅く間接的だが、**方向性としては「同系統への集中を強める」側に働く**。DeepSeek を選んだのは Kimi と GLM の比較における中立性のためだったが、**「中国系モデルという類に対する中立性」は確保していなかった。** モデル選定という用途に限れば、出自の異なる judge を1系統併走させる価値はある（判定が割れたときだけ人間が見る）。

**投入すべきは judge ではなく上記1〜3である。** 特に **2（トリアージ）は Slack と議事録の両方から呼ばれる単一関数**なので、ここに出自の異なる第2系統を入れて判定差分を取るのが、最も少ない変更で最も広く効く。

> [!important] 第2系統は「全件」ではなく「フラグ付き項目」に当てる
> 利用できる非中国系モデルは 17B / 12B と小さく、全件に当てると能力差による不一致が支配的になる（上記の警告）。**しかし懸念しているのは平均的な性能差ではなく、条件付きの偏りである。**
>
> CAISI / CrowdStrike が観測したのは**特定の語で挙動が変わる**という現象だった。**弱いモデルでもその条件を共有していないことには変わりなく、トリガ付き入力での不一致こそが探している信号である。**
>
> したがって **§4.9 対策3（複数系統の差分）と対策5（機微語のフラグ付け）を統合する。**
>
> - 対策5 のフラグ（政治的・地政学的に機微な語、組織名、国名）が立った項目**だけ**に第2系統を当てる
> - 件数が小さいので、**不一致は人間がすべて見られる**——能力差による雑音を人手で落とせる
> - 全件に当てるより安く、**探している信号に対する的中率が高い**
>
> 全件差分は「重要な会議のみ」の運用に留め、常時走らせるのはフラグ付き項目に対してとする。

> [!note] トリアージの fail 方向を確認する
> `triage_items` は `missing_verdict="DROP"`、`triage_items_batched` は `missing_verdict="KEEP"` が既定である。**LLM が判定を返さなかった項目の扱いが関数によって逆向き**になっている。議事録 ingest は batched（KEEP）を使うので現状は安全側だが、**欠落は敵対的でなくても失敗モードとして起きる。** Phase 4 で全呼び出しの fail 方向を KEEP に統一する。

> [!note] 対策2 は既存スキーマで実装できる（2026-07-31 実測）
> `audit_log` は `pm_sync_canvas.write_audit_log(conn, item_id, field, old, new, source)` の形で、**`action_items` の列単位に `old` / `new` / `source` を記録している**（Patrol の status・note 更新でも呼ばれる）。
> **`field` 単位で old/new と source が揃っているため、`source` が人間か `argus_auto` かで分けて集計すれば、モデルの誤り傾向がそのまま可視化できる。** 新テーブルは不要。
> 一方で**ツール名・引数は記録されない**ので、egress ログとしては不十分（§4.4 の `tool_calls` が必要）という評価は変わらない。用途が別である。

---

## 5. 実装フェーズ

各フェーズに**ゲート条件**を置く。次フェーズはゲートを満たすまで始めない。

### Phase 0 — 棚卸しと計測

既存コードの実態を確認する。**設計の前提が正しいかを検証する工程**であり、ここで想定が外れたら §3〜4 を修正する。

**別セッションでの実測により、以下は判明済み（2026-07-31）。**

| 項目 | 結果 |
|---|---|
| ツール数と実装場所 | **16。`_call_mcp` 委譲は8つ。EGRESS 3つは `agent_tools.py` 直接実装**（§3.3） |
| ツールの Plane 分類 | **READ 13 / MUTATE 0 / EGRESS 3。MUTATE ツールは存在しない**（§4.1） |
| LLM生成自由文が pm.db に入る経路 | **Pass 1 ingest（大半）と Patrol の自動クローズ（少量）。どちらもツールを経由しない**（§4.8） |
| `audit_log` の記録範囲 | `write_audit_log(conn, item_id, field, old, new, source)`。**`action_items` の列単位に old/new/source。ツール名・引数は入らない**（§4.4・§4.9） |
| 宛先引数 | `slack_post_message` の `channel` と `canvas_post_content` の `canvas_id` は**モデル指定可**。`box_upload_file` のみ設定解決で適合済み（§4.1） |
| ブローカー迂回経路 | **朝のブリーフィング（cron）と Patrol Agent が輸送層を直接呼ぶ**（§4.8） |
| 文字起こし原文 | **残っていない。** `pm_from_recording.sh:540` が削除。独立記録は VTT と mp4 のみ（§4.9） |
| 外部接続先 | **9エントリ / 8ホスト。** RiVault / EMBED / Docling / VOICEVOX / fish-speech を追加（§4.7） |
| 出口の輸送層 | **Canvas と Box は単一ファネルあり。Slack は無し**（SDK 直叩き 25箇所 / 7モジュール）。`slack_post.py` は整形ヘルパで投稿関数を持たない（§4.2） |
| テキスト以外の出口 | **`/argus-narrate` が mp3/mp4 を `files_upload_v2` で投稿**。テキストDLP・canary が効かない（§4.2） |
| 5a の下地 | `_FILE_PINNED_EXCLUDED_TOOLS` と `exclude_tools` が既存。`registry_for` はその一般化 |

**残りの棚卸し**

- [ ] `pm_qa_server.py` が `source` している秘密情報の一覧
- [ ] **Slack / Box で読取スコープと書込スコープを分離できるか**（Ingest Plane と Write Plane のトークン分割、§3.2）
- [ ] `pm_web_fetch.py` の依存箇所（廃止の影響範囲）
- [ ] 理究の K3 サービング方式（vLLM / SGLang / transformers）と revision
- [ ] **RiVault のモデルとバージョン**（`model_pin` 対象＝R8、**かつ §4.9 対策3 の出自独立性の評価のため**）
- [ ] **Docling が自ホストか外部か**（注入経路上のコンポーネント）
- [ ] **取得範囲が LLM 出力でパラメータ化されていないことの全数確認**（P2 の取得側。Slack のチャンネル・期間、Box のフォルダ。実測では決定論的だが不変条件として固定する）
- [ ] **理究の運用主体とモデル更新の通知の取り決め**（`expected_revision` は Argus 側で検証不能なため、合意で埋める。§4.6）
- [ ] **出自の独立した参照系の確保。** `gemma4` は運用終了。**RiVault 配信の `meta-llama/Llama-4-Scout-17B-16E-Instruct`（第1候補）または `gemma3:12b`（第2候補）**を使う。Qwen は中国系なので対象外（§4.9）。**投入先は judge ではなく `triage_items_batched`**。指標は不一致率ではなく**「不一致のうち第2系統が正しかった割合」**

**ゲート** — 16ツールの分類表と秘密情報の一覧が揃うこと。

### Phase 1 — canary と監査ログ

**最初に価値が出るフェーズ。** 既存の制御構造をほとんど変えずに、現状の危険度を実測できる。

- [ ] `canary_tokens` テーブル追加、既存テーブルに `is_canary`
- [ ] text canary を pm.db / minutes に 10〜20 件植える。レポート系クエリで除外
- [ ] hostname canary を Box 文書に 2〜3 件植える
- [ ] `tool_calls` / `reasoning_traces` テーブル追加、追記専用トリガとハッシュ連鎖
- [ ] **`agent_tools.py` の `_TOOL_MAP` / `pm_argus_agent.execute_tool` と MCP 側の2箇所**をラップして `tool_calls` に記録（§3.3）。**`mcp_tools.py` だけでは EGRESS 3つ（最も記録が必要な送信系）が漏れる**
- [ ] **Pass 1 ingest と Patrol の書き込みも記録対象に含める。** ツール経由ではないが、LLM生成コンテンツの主流はこちら（§4.8、P8）
- [ ] canary スキャナ（`scripts/utils/canary.py`）と日次警報

**K3提案の優先度1（APIクライアント層の再設計）と同一のコードパス。** 思考トレースの逐次永続化・ストリーミング・partial mode 再開もここでまとめて実装する。

**ゲート** — **計装の完全性**。次の3つ。

1. `tool_calls` に**全ツール呼び出し**が記録されている（`agent_tools` 経路と MCP 経路の両方）
2. Pass 1 ingest と Patrol の書き込みが記録されている
3. ハッシュ連鎖の検証と外部アンカーへの固定が日次で動いている

> [!warning] 「canary が2週間発火せず」を Phase 1 のゲートにしてはならない
> ブローカー（検知点1）は Phase 3 まで存在しない。**Phase 1 時点の canary は出力側を覆っていないので、「発火しなかった」は安全の証拠にならない**（§4.3 の被覆表）。発火監視を合否判定に使うのは **Phase 3 完了後**とする。
> Phase 1〜2 の canary は「植えて計装する」ところまでが目的であり、**発火したら極めて強いシグナル**（偽陽性が少ない）だが、**発火しないことは何も意味しない**。

発火した場合はその調査が Phase 2 より優先。対応手順は下記のとおり事前に定める。

**アラート対応手順（canary 発火時のランブック）** — アラートに行動が紐づいていなければ、検知は運用されない。

| 段階 | 行動 | 実行者 |
|---|---|---|
| 即時（自動） | **EGRESS の凍結** — ブローカーを `outcome='blocked'` 固定にし、Patrol と朝の cron を停止。Phase 3 以前は cron/Patrol の停止のみ | システム |
| 即時（自動） | 発火した canary・セッションID・該当 `tool_calls` の連番範囲を PM へ通知（Slack DM ＋ 専用チャンネル） | システム |
| 30分以内 | **モデル経路の切り替え** — 発火時に使用中だったモデルを `model_pin.yaml` で無効化し、別系統に退避（§4.9 対策3 の独立系統を使う） | PM |
| 当日中 | `reasoning_traces` とハッシュ連鎖の保全（別媒体へ複製）。**調査前に上書きされないようにする** | PM |
| 後日 | 侵入経路の特定（注入元の文書・メッセージの同定）と、当該ソースの索引除外 | PM |

**凍結の解除は人間の明示的判断による。** 自動復帰はさせない。

### Phase 2 — 外向き経路の削減と能力分離 5a

**費用対効果が最も高いフェーズ。** 削除と設定変更が中心で、コードの大改造を含まない。

#### 2-1. `pm_web_fetch.py` の廃止

- [ ] `pm_web_fetch.py` の廃止（作業一覧は §4.5）
- [ ] `pm_slack_box_links.py` を含め、他に外向きHTTPを持つ箇所がないか確認

#### 2-2. 認証境界の棚卸し

> [!note] 棚卸し完了（2026-07-31）— 結果は白
> | 確認項目 | 結果 |
> |---|---|
> | Slack Connect チャンネル | **なし** |
> | ゲストアカウント | **なし** |
> | 富士通・NVIDIA メンバー | ワークスペースには通常メンバーとして参加。**ただし Argus は彼らのチャンネルに投稿しない** |
> | Box 共有リンクの実効アクセス | **`company`**（議事録・`pm_report.xlsx` の2件で確認）|
> | pm.db ファイルの読取 | PM のみ |
>
> **したがって「Argus の出力はすべて理研の認証境界の内側に留まる」は、推定ではなく確認済みの事実になった。** R1 の格下げ条件のうち、棚卸しの部分は満たされた。

- [x] Argus の投稿先に Slack Connect / ゲスト参加チャンネル / 公開共有リンクの Box フォルダが含まれていないこと（上記のとおり確認済み）
- [x] 宛先チャンネルの参加者と pm.db の閲覧権限の整合を確認（R7 — 下記の注記に結果）

#### 2-3. 保護レベル逆転の是正（**パージはしない**）

> [!warning] Phase 4 との矛盾を回避する
> 当初「暗号化またはパージ」と書いたが、**Phase 2 でパージすると Phase 4 の遡及照合の材料（VTT 115本・mp4 115本）が消える。** 順序として Phase 2 が先なので、このまま実行すると R10 の緩和策が実行前に失われる。
> **保護レベル逆転の是正という目的は暗号化だけで達成されるため、パージはしない。**

- [ ] **`data/processing/` を暗号化して移送する。** 会議録音 mp4 115本と VTT 115本（§1.2）。**削除はしない**
- [ ] 以降の処理で平文の中間ファイルが残らないことを確認
- [ ] 保持期間の方針は Phase 4 で遡及取り込みの範囲を決めた後に確定する

#### 2-4. Box 共有リンクの明示化（衛生管理）

**緊急ではない。** 実効アクセスは既に `company` に降格されているが、**それは Box の企業ポリシーによるものであって Argus のコードの性質ではない**（§1.2）。

**目標値は `collaborators`（招待されたユーザーのみ）とする**（2026-07-31 決定）。

> [!note] `collaborators` は `company` より単純に厳しいわけではない
> | 設定 | 誰が開けるか | 境界との関係 |
> |---|---|---|
> | `company` | 理研の Box アカウント保持者**全員** | **必ず理研内**。広いが境界の内側は構造的に保証される |
> | **`collaborators`** | そのフォルダに招待された人だけ | **招待リスト次第。外部アカウントが招待されていれば境界の外に出る** |
>
> **設定名だけでは安全性が決まらない**（`--access open` と同じ形）。したがって collaborator 一覧の確認が前提条件になる。
>
> **確認済み（2026-07-31）— 出力先フォルダの collaborator に問題なし。** この前提が満たされたため `collaborators` を採用する。**R7 の母集団は「理研全体」からプロジェクト関係者まで縮む。**

- [ ] `box_cli.py`:103 の `--access open` → **`--access collaborators`**（1語）
- [ ] **`box_get_or_create_shared_link` が既存リンクをそのまま返す挙動を修正**（:99）。アクセス範囲を確認し、`collaborators` でなければ正規化する。**これが無いと過去のリンクが永久に残る**
- [ ] 既存の共有リンクの棚卸しと正規化（`pm_report.xlsx` の各版・議事録 Markdown 全件）。**降格前に対象一覧を出す**
- [ ] **降格の影響確認** — `collaborators` にすると、フォルダに招待されていない人は Canvas 上のリンクを開けなくなる。**「読ませたい人が全員 collaborator になっているか」を Box 側で揃えてから実行する**（コードでは解決しない）
- [ ] pre-commit lint に「`--access open` / `--access company` の混入を禁止」を追加（Phase 3 の lint 表に統合）

#### 2-5. allow-list の層1・層2

- [ ] `config/network_allowlist.yaml` を定義（§4.7）。**9エントリ / 8ホストを漏れなく、リテラル値で列挙する**（P9。`${ENV_VAR}` で書かない）
- [ ] `network_pin.py` を新設し、**`urlparse(os.environ[...])` の結果を allow-list のリテラルと照合**。不一致なら起動拒否
- [ ] **起動時の到達性アサーション**を実装。期待する依存先に届かなければ起動を拒否する
- [ ] `model_pin.yaml` の `endpoint` にも同じ照合を通す（§4.6・§4.7）。**ただし `expected_revision` は検証不能なので照合対象はモデル id まで**（§4.6）
- [ ] **ハイブリッド検索の vector 脚が空なら警告**するランタイムチェック（6〜7月の静かな劣化の再発防止）
- [ ] `config/mcp_allowlist.yaml` を定義し、起動時検証（§4.7）

#### 2-6. 能力分離 5a（安い半分）

**「EGRESS ツールは Read Plane に存在しない」という検証可能な不変条件を、ここで先に立てる。** Phase 3 の allow-list はこの上に乗る（§2「なぜ P1 が土台なのか」）。

- [ ] **`COMMAND_TOOLS`（コマンド別ツール allow-list）と `registry_for` を `agent_tools.py` に導入**（§4.1）。**既存の `_FILE_PINNED_EXCLUDED_TOOLS` / `exclude_tools` の一般化として実装する**
  - 6コマンド中5つは `frozenset()` の宣言のみ（現状の追認。**将来ツールが足された瞬間に起動拒否で気づける**）
  - **実質的な変更は「investigate ループから EGRESS 3 を外す」の1点。トークン除去より先に単独で入れられる**
  - `tool_calls` に**その呼び出しに渡したツール集合**を記録し、最小化が効いていたことを事後検証できるようにする
- [ ] **P7 文脈軸の実測と既定値の決定**（§2）。`recall_eval` / `investigate_ab.py` で `top_k` を **200 → 100 → 50** と下げ、gold 設問の正答率がどこで落ちるかを測る。落ちない範囲で `_ONESHOT_TOP_K_DEFAULT` を下げる
- [ ] 質問に無関係な `chunk_indexes` を渡さない（論理 index 分離は既存。設問型と index の対応を静的に決められるか検討）
- [ ] **`agent_tools.py` の `_TOOL_MAP` / `pm_argus_agent.execute_tool` と MCP 側の2箇所**に Plane 絞り込みと `tool_calls` 記録を入れる（§3.3）。**`mcp_tools.py` だけでは EGRESS 3つが漏れる**
- [ ] **Read Plane プロセスから `SLACK_*` / `BOX_*` / Canvas のトークンを外す。** `pm_qa_server.py` の調査部分を、トークンを `source` しないサブプロセスとして起動する
- [ ] EGRESS ツールが Read Plane に登録されないことを検証するユニットテスト
- [ ] 起動時チェック（未登録ツール・未登録MCPサーバがあれば起動拒否）
- [ ] **`pm_minutes_import.py` / `transcribe_pipeline.py` の Slack 投稿の帰属を確定する**（§3.2 の矛盾）。案A（Write Plane へ移す）を採る方針。実際の移送は Phase 3 の Slack ファネル新設と同時に行う

この時点では投稿の流れ自体は「調査 → 投稿」のままでよい。**プロセスとトークンが割れていることが要点**で、Artifact/Broker への再構成は Phase 5（5b）で行う。

**ゲート** — 次の5つ。

1. Argus のどのプロセスからも、`network_allowlist.yaml` に列挙した **9エントリ / 8ホスト**以外への外向き通信が発生しない
2. **起動時の到達性アサーションが通っており、ハイブリッド検索の vector 脚が機能している**（fail-closed による静かな劣化が起きていない）
3. **Read Plane プロセスの環境に外部サービスのトークンが1つも存在しない**
4. 外部閲覧可能な宛先が存在しない（または承認必須化されている）
5. `data/processing/` に**平文の**会議録音・VTT が残っていない（暗号化済みで存在することは可。Phase 4 の材料として必要）

### Phase 3 — 出力ブローカーと宛先 allow-list（層3）

**Phase 2 で確認した「宛先が認証境界の内側にある」という性質を、たまたまではなく構造的に保証する。** 5a のトークン分離が済んでいるため、ここで定めた制約は迂回できない。

- [ ] `config/egress_targets.yaml` を定義（層3、§4.2）
- [ ] `output_broker.py` を新設し、`Artifact` を導入
- [ ] **Canvas（安い）** — `canvas_utils.post_to_canvas` の1段手前にブローカーを噛ませる。**既存の単一ファネルなので1箇所**で 8呼び出し / 8モジュールを覆う
- [ ] **Box（安い）** — `box_cli.box_upload_or_version` の1段手前。同じく1箇所で 9モジュールを覆う
- [ ] **Slack（高い）** — **ファネルが存在しないので新設する**（§4.2）。`slack_post.py` に `post_message` / `post_ephemeral` / `upload_file` を追加し、**SDK 直叩き 25箇所 / 7モジュールを移送**したうえでブローカーを噛ませる
  - `narrate.py` 10 / `pm_qa_server.py` 5 / `pm_argus.py` 3 / `transcribe_pipeline.py` 3 / `patrol/actions.py` 2 / `output_tools.py` 1 / `pm_minutes_import.py` 1
  - 移送と同時に `pm_minutes_import.py` / `transcribe_pipeline.py` の投稿が Write Plane 側に移る（§3.2 の Ingest Plane 矛盾の解消）
- [ ] **TTS の順序制約** — `narrate.py` で**合成前のテキストをブローカーに通し、通過したものだけを TTS に渡す**。音声・動画そのものは検査しない（§4.2）
- [ ] `slack_post_message` の `channel` と `canvas_post_content` の `canvas_id` を `destination_ref` に変換。**`box_upload_file` が既に採っている設定解決の形に揃える**（§4.1）
- [ ] DLP（エントロピー・Base64様・**ゼロ幅文字**・未知URL・サイズ上限）
- [ ] 外部閲覧可能な宛先には `requires_human_approval: true`。Web UI に承認キューを追加
- [ ] **`.pre-commit-config.yaml` に不変条件の lint を追加**（下記）。ゲートは1回きりだが lint は永続する

> [!important] ゲートを不変条件に変える
> **Phase 3 のゲートは「grep で検証できる状態」だが、ゲートは一度検証するだけである。** 次に誰かが Slack SDK を直叩きするコードを書けば静かに戻る。**25箇所を移送した直後がいちばん壊れやすい。**
> Argus には既に `.pre-commit-config.yaml`（ruff / pytest / mypy）があるので、そこに追加する。
>
> | 検査 | 失敗条件 |
> |---|---|
> | import の制限 | 輸送モジュール以外が `slack_sdk` / `WebClient` を import したら fail |
> | 呼び出しの制限 | ブローカー以外から `post_to_canvas` / `box_upload_or_version` を呼んだら fail |
> | レジストリの不変条件 | EGRESS ツールが Read Plane のレジストリに載ったら fail（ユニットテスト。§4.1 の `registry_for` の assert と対にする） |
> | TTS の順序制約 | `pm_tts` の呼び出し元がブローカー通過済みテキストを渡していない場合に fail |
>
> **同じことが Phase 2 の allow-list にも当てはまる。** `network_allowlist.yaml` に載っていないホストへ接続するコードが増えても、起動時チェックがなければ気づかない（P6）。

**ゲート** — 次の3つ。いずれも grep で検証できる状態にする。

1. LLM が宛先を文字列で指定する経路がコード上に存在しない
2. **LLM が生成したコンテンツが、ブローカーを経由せずに Slack SDK クライアント・`post_to_canvas`・`box_upload_or_version` に到達する経路が存在しない**（関数名を列挙する書き方は取らない。`chat_postEphemeral` / `files_upload_v2` が漏れるため）
3. **TTS・動画生成の入力テキストがブローカーを通過している**（テキスト以外の成果物は生成元テキストの検査をもって代える）
4. **上記3つが `.pre-commit-config.yaml` の lint として恒久化されている**（ゲートの一回性を不変条件に変える）
5. **canary の全検知点（ブローカーを含む）が稼働している。** ここではじめて「発火しないこと」が意味を持つ。以後2週間の無発火を Phase 4 の前提として確認する（§4.3）

### Phase 4 — LLM生成コンテンツの完全性

> [!warning] 当初の「MUTATE の制約」は空振りする
> MUTATE ツールは0件なので `MUTABLE_FIELDS` を実装しても現に流れているものは何も制約されない（§4.1・§4.8）。**本フェーズの対象は Pass 1 ingest と Patrol の書き込みである。**

- [ ] **Whisper 原文の保持を実装**（§4.9、方針決定済）
  - `pm_from_recording.sh:540` の削除処理をやめる
  - `minutes/*.db` に `raw_transcript` 列を追加（SQLCipher 下）
  - 保持期間を情報管理規程に合わせて定める
- [ ] 過去分の扱いを決める — VTT 115本を暗号化DBに取り込み、VTT のない会議は mp4 の再文字起こしを行うか判断（**Phase 2-3 でパージしないことが前提**）
- [ ] **Patrol の `_append_close_note`（`patrol/actions.py`:341）を「引用」に限定する。** 生成散文ではなく引用元 row_id と逐語一致する抜粋のみを許し、**一致を機械的に検証する**（§4.8）
- [ ] 決定事項・アクションアイテム数の異常検知（**Pass 1 ingest に対する検知**）
- [ ] **`audit_log` の `source` 別集計**でモデルの誤り傾向を可視化。既存スキーマで実装可能（§4.9）
- [ ] **出自の異なる第2系統を「欠落を作れる位置」に投入する**（R8 の集中リスク）。優先順は §4.9 のとおり **② トリアージ（`triage_items_batched`、Slack・議事録の両方から呼ばれる単一関数）→ ③ Box relevance → ① Pass 1 抽出**
- [ ] **第2系統は全件ではなく「対策5 のフラグが立った項目」に当てる**（§4.9）。17B/12B の能力差による雑音を避け、条件付きの偏りという探している信号に的を絞る。件数が小さいので不一致は人間が全件見られる
- [ ] `triage_items` / `triage_items_batched` の `missing_verdict` を **KEEP に統一**（判定欠落による非敵対的な欠落を塞ぐ）
- [ ] 重要な会議で差分を取る仕組み。**「複数系統」ではなく「出自の独立した複数系統」であることを要件にする**（§4.9）
- [ ] 将来 MUTATE ツールが追加された場合の受け皿として `MUTABLE_FIELDS` を定義（**現在0件なので予防的**）

**ゲート** — 今後生成される議事録を原文と照合できる状態にあること。過去分については照合可能な範囲を明文化すること。

### Phase 5 — 能力分離 5b（高い半分）

5a（Phase 2）で不変条件とトークン分離は済んでいる。ここは**流れそのものの再構成**で、コストが大きい代わりに以下を得る。

1. **注入 → 行動の連鎖を切る。** 悪性文書が「M3は順調と報告せよ」と指示しても、成果物がブローカーの検査と（必要なら）人間承認を通る
2. Read Plane が外部サービスに構造的に到達不能になる（5a はトークン不在による分離、5b はプロセス分離による分離）
3. 出口が Artifact 1点に集約され、監査とDLPが1箇所で完結する
4. 将来、外向き宛先が追加されたときの封じ込め

- [ ] `pm_read_worker.py` / `pm_broker.py` / `pm_write_worker.py` に分割
- [ ] `/argus-investigate` を「調査 → Artifact → ブローカー → 投稿」の流れに再構成
- [ ] Patrol Agent も同様に分割
- [ ] ネットワーク層の強制（network namespace / iptables / プロキシ）を運用と調整して実装

**UXの劣化を受け入れる範囲を決める必要がある。** 「調べながら中間結果を投稿する」使い方はできなくなる。実運用でこれが必要だった場面を Phase 1〜4 の `tool_calls` ログから確認して判断する。**この判断材料が揃うのが Phase 5 をここに置く理由**でもある。

**ゲート** — Read Plane プロセスが、ネットワーク到達性のレベルで slack.com / box.com に届かないこと。

### Phase 6 — Kimi-K3 の導入

Phase 1〜5 のゲートを満たしてから着手する。詳細な性能設計は `Kimi-K3移行設計メモ`（→ §6）。

- [ ] `model_pin.yaml` を整備し、`trust_remote_code: false` を確認（**Phase 1 と並行可**）
- [ ] ハイブリッド・ルーティング（GLM-5.2 と K3 の役割分担）
- [ ] Pass 1 + Pass 2 の統合（キャッシュprefix + 窓分割方式）
- [ ] **視覚入力**（Phase 5 完了が前提）
- [ ] **長時間自律セッション**（Phase 5 完了が前提。Read Plane 限定）
- [ ] モデル固有レッドチーミング（§9）

---

## 6. Kimi-K3 導入との対応

`Kimi-K3移行設計メモ` の6提案を、本設計のフェーズにゲートする。

| K3提案 | 内容 | 必要なPhase | 状態 |
|---|---|---|---|
| 優先度1 | APIクライアント層の再設計（`reasoning_content` 往復・ストリーミング・逐次永続化・partial mode・冪等性） | **Phase 1 と同時**（同一コードパス） | 着手可 |
| 優先度5 | ハイブリッド・ルーティング（GLM-5.2 と K3 の役割分担） | Phase 1（`model_pin.yaml` と併せて） | 着手可 |
| 優先度4 | Pass 1 + Pass 2 統合（キャッシュprefix + 窓分割） | Phase 3 | 待ち |
| 優先度2 | **視覚入力**（Box資料を画像で渡す） | **Phase 5（5b）完了後** | **保留** |
| 優先度3 | **長時間自律セッション** | **Phase 5（5b）完了後**（Read Plane 限定） | **保留** |
| 優先度6 | Kimi Code CLI をオーケストレータにする | — | **恒久的に不採用** |

**優先度2 を保留する理由** — 多数の寄稿者由来の Box 資料を画像でモデルに渡すことは、既存のテキストDLPやFTS5では見えない新しい指示注入面を作る。§1.3 の攻撃者1（最も現実性が高い）の攻撃面を直接拡大する。流出の回路が閉じていても、**注入 → 改竄**の連鎖は成立するので、能力分離（Phase 5）を前提とする。技術的価値は最も高いが、封じ込めのない状態で先に入れてはならない。

**優先度3 を保留する理由** — 100+ターン・最小限の人間監督は発火機会を最大化しレビュー密度を最小化する。トリガは複数ターンにまたがり、環境コンテンツ（ツール応答・文書内容）から入りうるため、長いセッションはそれだけ多くの環境コンテンツを取り込む。Phase 5 後に **Read Plane 限定**で実施する。

**優先度6 を不採用にする理由** — 重みは出力を計算するだけだが、CLI は**実行可能コードでネットワークアクセスを持つ**。どのMCPサーバや外部エンドポイントに接続するかを Argus 側で保証できないため、§4.7 の層1・層2 を迂回されうる。§1.3 の攻撃者2に該当し、脅威の重心はこちら側にある。既存の `pm_mcp_server.py` ＋自前クライアント（優先度1で作り直すもの）を維持する。

> [!important] 能力分離はK3導入のクリティカルパス上にある
> 視覚入力（優先度2）と長時間自律（優先度3）は、**K3 に移行する動機そのもの**である。両方が 5b をゲート条件とするため、**分離を後ろに送ることは移行の目的を後ろに送ること**を意味する。セキュリティ対策と移行の利益が同じ方向を向いているので、分離への投資を「安全のためのコスト」として扱わないこと。

**副産物として使える選択肢** — Kimi K3 License には出力の利用制限も禁止分野リストもないため、**K3 の出力で小型モデルを蒸留し高頻度パスに使うことが許諾されている**。常時 thinking のコストと nginx 600秒制約への打ち手になりうるが、蒸留物は派生物なのでライセンス表記の設計が必要（→ `Kimi-K3ライセンス`）。

---

## 7. 明示的な非目標

**やらないことを決めておく。**これらは労力を吸い込むが安全性を生まない。

| 非目標 | 理由 |
|---|---|
| 重みを検査して清浄性を証明する | §1.5。既知の清浄版が存在せず、トリガ空間が広すぎる |
| `llm-backdoor-scanner` に依存する | 検証が14Bまで。2.8T には適用できない |
| DLP を主防御にする | TrojanStego 型（自然な散文への符号化）を捕捉できない。安価な層として入れるだけ |
| すべての出力に人間承認を課す | Patrol Agent と `/argus-brief` の即時性が失われ、自動化の価値を大きく損なう。外部閲覧可能な宛先と自由文に限定する |
| 中国製モデルを排除して解決とする | 米国製モデルでも間接プロンプト注入は 4% で成立する（NIST/CAISI）。モデル選択は程度を変えるが問題を消さない |
| クリーンな追加学習でバックドアを消す | Sleeper Agents。敵対的学習は隠蔽を上手にすることすらあった |

---

## 8. 残留リスクと受容

Phase 1〜5 を完了しても残るもの。**明示的に受容するか、追加の判断を仰ぐ。**

| # | 残留リスク | 緩和 | 判断 |
|---|---|---|---|
| R1 | **外部閲覧可能な宛先への自由文出力** | **格下げ（2026-07-31）。** Argus の出力先は Box / Slack / Canvas に限られ理研の認証で保護されている。`pm_web_fetch.py` 廃止により認証境界の外へ出る経路も消える。**外部の攻撃者は復号する側に立てないため回路が閉じない** | **条件付きで解消。** Phase 2 の棚卸しで Slack Connect・Box共有リンクの不在を確認すること、**かつ能力分離（5a/5b）でこの性質を構造として固定すること**が条件。認証境界は運用で変わる外部事実であり、**分離が入らない限りこの格下げは持続しない**（§2） |
| R2 | **人間が出力を外部に転送する**。Argus の外の経路 | 対象外。組織の情報管理規程に委ねる | 受容 |
| R3 | **外科的なバックドアが canary に触れない** | canary は無差別流出とプロンプト注入駆動を捕まえる設計。R3 への防御は能力分離（Phase 5） | 受容 |
| R4 | **理究の推論基盤自体が侵害される** | Argus のスコープ外。理研の基盤運用に依存 | 上位判断が必要 |
| R5 | **改竄**（流出ではなく誤誘導）。**経路を必要としないため上記の対策が効かない。発生場所は Pass 1 ingest** | §4.9。原文保持による事後照合、人間レビュー（`audit_log` の source 別集計）、2〜3モデル差分、件数の異常検知 | **本設計での主要な残留リスク。Phase 4 で対処** |
| R6 | **最も機微な作業負荷への十分性** | エアギャップ推論エンクレーブ（内部検索コーパス＋一方向レビュー済み輸出）が原理的な解。Argus の現用途で必要かは要判断 | 上位判断が必要 |
| R7 | **内部者による特権昇格**。pm.db への正規アクセスを持たないが出力は読める者が、符号化された出力を受け取る | **棚卸し完了（2026-07-31）。** Slack 側は白（Connect・ゲスト不在、パートナーの参加チャンネルには投稿しない）。Box は現在 `company`＝理研全体だが、**Phase 2-4 で `collaborators` に締める（決定済み。collaborator 一覧の確認も完了）** | **対処予定。** 現状は pm.db 本体が PM のみに対し**派生物の読者が理研全体**という開きがあるが、`collaborators` 化でプロジェクト関係者まで縮む。**縮んだ時点で「範囲が狭い」が実際に真になる** |
| R8 | **提供元レベルの集中リスク（2026-07-31 格上げ）。** RiVault の主力は **Kimi-K2-Thinking**＝Kimi-K3 と**同一提供元**。しかも RiVault は**議事録生成・Slack抽出・トリアージ**という **R5（改竄）の発生場所そのもの**を担っている。提供元レベルのバックドアが仮に存在すれば、Argus では**2経路に同時に存在する**。並列のリスクではなく**集中リスク**である | `model_pin` 対象に含める（Phase 0）だけでは不十分。**§4.9 の「欠落を作れる位置」1〜3 に出自の異なる第2系統を投入する**。特にトリアージ（`triage_items_batched`）は Slack・議事録の両方から呼ばれる単一関数なので費用対効果が高い | **受容しない。Phase 4 で対処する。** K3 移行が再開される場合、この集中はさらに深まるため Phase 6 の前提条件に含める |
| R9 | **Docling が注入経路上にある**。Box 文書の変換を担うため、悪性文書が必ず通過する | 自ホストならバージョン固定。変換結果の異常検知は §4.9 の対策4に含める | 受容 |
| R10 | **過去分の議事録は照合不能**。Whisper 原文の保持は今後の分にしか効かない | VTT のある会議（115本）は取り込み可能。VTT のない会議は mp4 の再文字起こしが必要 | **Phase 4 で範囲を明文化して受容** |
| R11 | **音声・動画そのものは検査していない**。ブローカーが見るのは合成前のテキストだけで、TTS 以降の工程で内容が変わっても検知できない | 順序制約（合成前テキストの検査）＋ TTS を Write Plane 内の決定論的コンポーネントとして扱う（LLM を含まない） | 受容（TTS は LLM ではないため改変の余地が小さい） |
| R12 | **チェックポイントの revision を検証できない**。`/v1/models` は `id` しか返さず、共有推論サービスなので sha を取得する手段がない | モデル id の照合は fail-closed で維持。revision は**運用主体の申告の記録**とする。加えて **挙動指紋（behavioral fingerprint）による差し替え検知**を置く（§9.2） | **緩和付きで受容。** 「検証済み」と書かないこと。§4.6 の用語を「申告」に統一した |
| R13 | **抽出LLMが最初から出力しなかった項目は記録できない**。triage が残せるのは候補に挙がったものだけ | 二段抽出（候補列挙 → 採否判定）が原理的な解だがコスト増。まず件数異常検知と2系統差分でどこまで見えるかを Phase 4 で測る。**二段化する場合、stage 2 は stage 1 と出自の異なるモデルにする**——同一モデルの二段は「自分が出さなかったもの」を自分で見つける構図になり、欠落に対して無力（R8） | **Phase 4 の実測後に判断** |
| R14 | **Phase 1〜2 の canary は出力側を覆っていない**。ブローカーが Phase 3 まで存在しないため | ゲートを「計装の完全性」に変更し、発火の合否判定を Phase 3 以降に移した（§4.3） | **設計で対処済み。** 残るのは「Phase 3 到達前の期間は出力側が観測されない」こと自体 |

R1 は条件付きの格下げであり、**能力分離が入って初めて持続する**。R5（改竄）は経路を必要としないため、上記のいずれの対策でも消えない。**R5 が本設計における主要な残留リスクである。** R4 と R6 は Argus 側では決められないので、理究の運用主体と情報管理の責任者に上げる。

---

## 9. 検証計画

### 9.1 モデル固有レッドチーミング

Phase 5 で K3 に対して実施。**トリガの連言性を意識して軸を組み合わせる。**

| 軸 | 例 |
|---|---|
| 日付 | 特定日・特定曜日・年度境界 |
| 地理・政治用語 | 国名、地域名、政治的に機微な語（DeepSeek で有意な差が観測された領域） |
| 組織名 | 理研、R-CCS、富岳NEXT、共同研究先名 |
| ツールスキーマ名 | ツール名・コネクタ名を変えて挙動が変わるか |
| 複数ターン | トリガを2〜4ターンに分散させる |
| 言語 | 日本語／英語／中国語の切り替え |

判定は「機微データが外向き引数に現れるか」ではなく（ブローカーが止めるので）、**`tool_calls` と `reasoning_traces` に不自然な読取パターンや符号化の痕跡が出るか**で行う。

### 9.2 継続的な検証

- canary 発火の日次チェック（**ただし合否判定に使えるのは Phase 3 以降**。§4.3）
- `tool_calls` のハッシュ連鎖検証（週次）＋**連鎖の頭の外部アンカーへの日次固定**（§4.4）
- ブローカーの `outcome='blocked'` 件数のトレンド監視。急増は注入試行のシグナル
- `model_pin.yaml` と実エンドポイントの一致確認（起動時＋日次）。**照合できるのはモデル id まで**（R12）

### 9.3 挙動指紋（behavioral fingerprint）— revision 検証の代替

**暗号学的な pin が取れない共有サービスに対する実務的な代替。** `expected_revision` を Argus 側で検証できない以上（R12）、**モデルが黙って差し替わったことを振る舞いから検知する。**

- **固定プロンプト集（10〜20問、`temperature=0`）を日次で流し、出力を記録する**
- 判定は**厳密一致ではなく、合格率と出力類似度のトレンド**で見る（サンプリングのゆらぎと非決定性があるため）
- 想定を超えた drift は「理究側でサービングモデルが差し替わった可能性」のシグナルとして扱う
- 設問は**用途に近いもの**を選ぶ（抽出・トリアージ・要約）。汎用ベンチマークは非発火時の方針しか測らない（§1.5）

**これは §9.1 のレッドチーミング結果の有効期限を守る仕組みでもある。** レッドチーミングの結論は「申告されたバージョン」に対してしか成立せず、**黙って差し替わればその日から無効になる。** 挙動指紋があれば、少なくとも**無効化に気づける。**

**Phase 0 の「理究との更新通知の取り決め」と対で運用する。** 通知は合意、指紋は検証——どちらか一方では足りない。通知が来ないこともあれば、通知の前に差し替わることもある。

---

## 10. まとめ：意思決定の要点

1. **これはモデル選定の問題ではなくアーキテクチャの問題である。** GLM-5.2 のままでも Phase 1〜5 は必要
2. **最も現実的な攻撃者は Box / Slack に文書を置ける者**であり、モデル提供元ではない。設計はそちらを主眼に置く
3. **1〜3 への防御は 4 への防御でもある。** モデル提供元の意図を判定する必要がない設計にできる
4. **`pm_web_fetch.py` を廃止する（決定）。** これにより「Argus は公開インターネットに任意のリクエストを出せない」という検証可能な不変条件が成立する。なお到達可能な外部接続先は3つではなく **9エントリ / 8ホスト**（理究・RiVault・EMBED・Docling・VOICEVOX・fish-speech・Slack・Box×2）
5. **外部アクセスはネットワーク層・ツール/MCP層・宛先層の3層すべてを allow-list にする（決定）**
6. **流出リスクの重心は下がった。** 出力先が理研の認証境界の内側に限られるため、符号化されても復号する側が存在しない。**代わって改竄（R5）が主要な残留リスクになる**
7. **ただし認証境界は運用で変わる外部事実であり、コードの性質ではない。** 能力分離が同じ性質を構造として与える。**「認証境界があるから分離は不要」という推論は取らない**
8. **能力分離は他の対策の土台である。** Read Plane がトークンを持ったままでは、宛先 allow-list は規約に過ぎず迂回できる。ゆえに安い半分（5a：Plane 分類とトークン除去）を Phase 2 に前倒しし、その上に allow-list を乗せる
9. **Phase 1（canary と監査ログ）は安価で、現状の危険度を実測できる。** ここから始める
10. **能力分離は K3 導入のクリティカルパス上にある。** 視覚入力と長時間自律は 5b をゲート条件とし、それが移行の動機そのものなので、分離の遅延は移行の遅延を意味する
11. **ツールレジストリは漏斗ではない（P8）。** 本文書は同じ型の誤りを4回犯した。**Argus の実際のデータフローの主流は cron とパイプラインであって、エージェントループではない。** 新しい対策を設計するたびに「実際に流れている量の何割を覆うか」を検算する。**検算は指摘の追認ではなく独立した全数調査として行う**（4回目はこれを怠って再発した）
12. **ブローカーはツール層ではなく輸送層に置く。** 実際の出力量の大半は cron と Patrol Agent による非対話的な自動投稿である
13. **輸送層のコストは非対称。** Canvas（`post_to_canvas`）と Box（`box_upload_or_version`）は既存の単一ファネルなので各1箇所で済むが、**Slack はファネルが存在せず SDK 直叩き25箇所を移送する必要がある**。`slack_post.py` は整形ヘルパで投稿関数を持たない
14. **テキスト以外の出口がある。** `/argus-narrate` は LLM 生成テキストを mp3/mp4 にして投稿する。テキストDLPも canary も効かないので、**合成前のテキストを検査する順序制約**で代替する
15. **`MUTATE` ツールは0件。** 書込制約の対象は Pass 1 ingest であり、その自由文は成果物そのものなので**制約できない**。対策は制限ではなく検証可能性の確保になる
16. **fail-closed は「大きく失敗する」ことと対にする。** allow-list による遮断は機能の静かな劣化を招く（6〜7月に実例あり）。到達性アサーションと健全性チェックを必ず併設する
17. **R5（改竄）の対策には今、土台がない。** Whisper 原文が削除されており事後照合ができない。原文を暗号化DBに保持する方針を決定した
18. **「canary が発火しない」は Phase 3 まで安全の証拠にならない。** ブローカー（検知点1）が無い間、出力側は観測されていない。Phase 1 のゲートは**計装の完全性**とし、発火監視の合否判定は Phase 3 以降に移した
19. **引用スパンの必須化で、改竄の一部が事後検知から事前防止に変わる。** 「存在しない根拠の捏造」は逐語照合で落とせる。**ただし「根拠はあるが結論が違う」と「欠落」は防げない。** Slack 側は `source_ref` が既に permalink で入っているので、必要なのは照合の実施と粒度の細分化
20. **差分の前提は独立性であり、現状それが無い。** 使用中のモデルは GLM-5.2 / Kimi-K2-Thinking / DeepSeek-V4-Flash で**すべて中国系、うち2つは同一提供元**。`gemma4` は運用終了したが、**RiVault に `Llama-4-Scout-17B`（Meta）と `gemma3:12b`（Google）が残っている**。ただし小型なので**全件ではなくフラグ付き項目に当て、指標は「不一致のうち第2系統が正しかった割合」**とする
21. **R8 は集中リスクであり受容しない（格上げ）。** RiVault の主力 Kimi-K2-Thinking は Kimi-K3 と同一提供元で、かつ**議事録生成・Slack抽出・トリアージという R5 の発生場所そのもの**を担っている。提供元レベルの偏りがあれば2経路に同時に存在する
22. **欠落を作れるのは judge ではなく本番3箇所。** DeepSeek-V4-Flash は `scripts/eval/` 配下のオフライン評価専用で pm.db に欠落を作れない。実際の位置は **Pass 1 抽出 / トリアージ / Box relevance 判定**で、いずれも `call_argus_llm`。**独立系統はここに投入する**
23. **証明の強度も検算する（P10）。** 被覆率（P8）だけでは足りない。引用スパンの必須化は「根拠の実在」を証明するが「結論の妥当性」は証明しない。**証明しないことを列挙して書く**
24. **同居問題は1ファイル・1経路に閉じている。** ツールループを持つのは `pm_argus_agent.py` だけで、他の5コマンドはツール表面ゼロ。**しかも本番既定の one-shot はツールを1つも渡さない**ため、5a が目指す状態を既に満たしている。**残る露出はループ経路のみで、対策は「EGRESS 3 を外す」の1点**
25. **能力分離の後にも被害半径は残る（P7）。** 分離は「同一呼び出しでの同居」を解消するが「読んだものが外に出ること」は解消しない（それが製品の目的）。**残る梃子は、発火時に何が見えていたか。** 本番は `top_k=200` で、P7 の文脈軸と逆方向に振れている
26. **MCP allowlist の本番被覆率はゼロ。** `pm_mcp_server` は本番デーモンに現れない開発時経路。**守るのは開発環境であって同居問題ではない**——そうラベルを付けたうえで維持する
27. **ゲートは一度きり、lint は永続する。** Phase 3 の grep 検証は `.pre-commit-config.yaml` に落として不変条件にする。25箇所を移送した直後がいちばん壊れやすい
28. **`expected_revision` は検証できない（R12）。** `/v1/models` は id しか返さない。pin は「検証」ではなく**運用主体の申告の記録**であり、そう書く。モデル id の照合だけが fail-closed にできる。**代替として挙動指紋を日次で回す**（§9.3）——レッドチーミング結果の有効期限を守る仕組みでもある
29. **人間承認は宛先と事実の正確さに対して行う。** 「符号化されていないか」を人間に問うてはならない——見抜けないし、承認済みが安全の保証と誤読される
30. **ハッシュ連鎖の頭を外部アンカーに固定する。** 内部で完結した連鎖は意図的な改竄を検出できない。日次でブローカー経由の専用チャンネルに投稿し、ハートビートも兼ねる
31. **Kimi Code CLI は使わない**
32. R4（理究基盤の侵害）と R6（エアギャップの必要性）は上位判断が必要

---

## 付録：文献と検証状況

| 文献 | 内容 | 検証 |
|---|---|---|
| [Back-Reveal（arXiv 2604.05432）](https://arxiv.org/abs/2604.05432) | 意図的にバックドア化したエージェントがセッションメモリを読み Base64 化して検索リクエストに埋め込み送信。発火率94%超、MT-Bench低下1%未満、既存フィルタは81〜87%を通過 | **確認済み。数値一致。** 論文の適用条件が Argus の定義そのもの |
| [CAISI/NIST DeepSeek評価](https://www.nist.gov/news-events/news/2025/09/caisi-evaluation-deepseek-ai-models-finds-shortcomings-and-risks) | 公式重みを自前ホスト。資格情報流出試行 37%（米国製平均4%）、悪意ある指示への追従は平均12倍 | **確認済み。数値一致** |
| [The Trigger in the Haystack（arXiv 2602.03085）](https://arxiv.org/abs/2602.03085) / [microsoft/llm-backdoor-scanner](https://github.com/microsoft/llm-backdoor-scanner) | トリガの事前知識なしに87.8%検出・誤検出ゼロ。**検証は14Bまで** | **確認済み。OSS公開を確認** |
| BadAgent（ACL 2024） | 最大500例で汚染、成功率85%超。クリーンな追加学習後も90%超が残存。トリガは環境側から入りうる | 未検証（一次整理より） |
| TrojanStego（EMNLP 2025） | 32bitの秘密を自然な文章に符号化、回復精度87%（3世代で97%超） | 未検証（一次整理より） |
| Sleeper Agents（Anthropic他） | SFT・RL安全化・敵対的学習でも消えず、敵対的学習は隠蔽を上手にすることすらあった | 未検証（一次整理より） |
| [Kimi K3 モデルカード](https://huggingface.co/moonshotai/Kimi-K3) | `custom_code` タグ、transformers 使用例が `trust_remote_code=True`、重みは safetensors | **確認済み** |

一次の整理は松岡先生から共有された `Chinese Models Backdoor Threats.pdf`（5ページ）。**LLM が生成した調査回答**であり査読論文ではないが、引用文献は実在し数値も正確でヘッジも適切。主要3件を独自検証した結果も一致した。

---

## 関連

- [[Argus]]
- [[Kimi-K3移行設計メモ]] — 性能面の設計（nginx 600秒制約、ハイブリッド・ルーティング）
- [[Argusのエージェント権限をどう分離するか]] — 仮説A/B/Cの比較検討
- [[LLMバックドアとエージェント流出リスク]] — 脅威モデルの一般形
- [[2026-07-30_中国製モデルのバックドア脅威]] — 一次整理
- [[Kimi-K3ライセンス]] — 蒸留の許諾と派生物の表記義務
- [[Kimi-K3]] / [[RIKYU]]

## 更新履歴

- 2026-07-31 — **認証境界の棚卸し（Phase 2-2）を実施。結果は白。ただし Box に「コードは公開を要求している」構図が見つかった。**
  - **棚卸し結果** — Slack Connect なし／ゲストなし／富士通・NVIDIA は通常メンバーだが **Argus は彼らのチャンネルに投稿しない**／Box 共有リンクの `effective_access` は **`company`**（議事録・`pm_report.xlsx` の2件で確認）／pm.db ファイルは PM のみ。**「Argus の出力はすべて理研の認証境界の内側」は推定ではなく確認済みの事実になり、R1 の格下げ条件の棚卸し部分が満たされた**
  - **ただし `box_cli.py`:103 は `--access open`（認証不要の一般公開）を要求している。** 防いでいるのは理研 Box の企業ポリシーであって Argus のコードではない。**テナント設定が変われば、コードを1行も触らずに全世界公開になる。** §2「認証境界と分離の関係」の構図そのもの。緊急ではないが **Phase 2-4 として `--access company` の明示**を追加
  - **`box_get_or_create_shared_link` は既存リンクをアクセス範囲を確認せずに返す**（:99）。`--access` だけ直しても**過去のリンクが永久に残る**ため、既存リンクの正規化を同時に行う必要がある
  - **R7 の「範囲が狭い」を訂正。** `company` は理研の Box アカウント保持者**全体**。pm.db 本体は PM のみだが、**派生物（全決定事項・全議事録・実績台帳）の読者は理研全体**である。母集団はプロジェクト関係者ではない
  - **Box の目標値を `collaborators`（招待されたユーザーのみ）に決定。** ただし **`collaborators` は `company` より単純に厳しいわけではない**——`company` が「必ず理研内」を構造的に保証するのに対し、`collaborators` は**招待リスト次第で外部アカウントを含みうる**。`--access open` と同じく**設定名だけでは安全性が決まらない**ため、collaborator 一覧の確認を前提条件とした。**確認の結果、出力先フォルダに問題なし**。これにより R7 の母集団がプロジェクト関係者まで縮み、格付けを「受容」から「対処予定」に変更
- 2026-07-31 — **`gemma4` 運用終了に伴い、独立参照系を RiVault 配信モデルから選び直した。**
  - **RiVault の配信一覧を実測**（`/v1/models`）。非中国系は **`meta-llama/Llama-4-Scout-17B-16E-Instruct`（Meta）**と **`gemma3:12b`（Google）**の2つ。**Qwen3.6-27B/35B は Alibaba（中国系）なので独立性の要件を満たさず対象外**。RIKYU 側は glm-5.2 / kimi-k2.6 / kimi-k3 / qwen3.6-35b で**全て中国系**
  - **能力差の落とし穴を明記。** 17B / 12B は GLM-5.2・Kimi-K2 より大幅に小さく、**能力差による不一致が偏りによる不一致を埋もれさせる**。指標を「不一致率」から**「不一致のうち第2系統が正しかった割合」**に変更し、不一致サンプルの人手ラベル付けを必須とした
  - **§4.9 対策3 と対策5 を統合。** 懸念しているのは平均性能差ではなく**条件付きの偏り**（CAISI / CrowdStrike が観測したのは特定語での挙動変化）。したがって第2系統は**全件ではなく機微語フラグが立った項目にだけ当てる**。件数が小さいので不一致を人間が全件見られ、能力差による雑音を人手で落とせる。全件差分は「重要な会議のみ」に留める
- 2026-07-31 — **ツール表面の実測により、同居問題のスコープが1経路に閉じていることが判明。P7 を2軸に再定義。**
  - **§4.1：ツールループを持つのは `pm_argus_agent.py` 1ファイルだけ。** brief / risk / patrol / ingest / narrate は**ツール表面ゼロ**（プロンプトのみ）。さらに**本番既定の one-shot は `tools=` を渡さない**ため、**5a が目指す「エージェントは外に出せない」状態を投資ゼロで既に満たしている**（理由は異なる——5a はトークン不在、one-shot はツール不在）。**残る露出はループ経路のみ**
  - **§4.1：`registry_for` を Plane（粗い3分類）から `COMMAND_TOOLS`（コマンド別 allow-list）へ変更。** 6コマンド中5つは空集合の宣言で済み、**実質的な変更は「investigate ループから EGRESS 3 を外す」の1点**。`exclude_tools` の下地があるため、トークン除去より先に単独実施できる。ゲートは起動時 fail-closed ＋ `tool_calls` へのツール集合記録（人間承認は置かない——ブローカーと重複し自動化を殺す）
  - **§2 / P7 を「文脈の最小権限」から「最小権限（文脈と能力の両方）」へ再定義。** 能力分離（P1）が解消するのは「同一呼び出しでの同居」であって「読んだものが外に出ること」ではない——後者は製品の目的。**分離後に残る唯一の梃子が P7 であり、それが Phase に1行も入っていなかった**
  - **§2：「セキュリティと性能設計が一致する」を訂正。** 窓分割とは一致するが、**現に動いている one-shot（`top_k` 既定 200）とは逆方向**。2軸はトレードオフで、本番は「文脈 最大 / 能力 最小」の角に振り切っている。能力軸は宣言でほぼ閉じるが、**文脈軸は品質とのトレードオフなので実測して決める**（`recall_eval` / `investigate_ab.py` が使える）
  - **Phase 2-5 に P7 の実測を追加**（`top_k` 200 → 100 → 50 の正答率、`chunk_indexes` の絞り込み）
  - **§4.7 層2：MCP allowlist の本番被覆率はゼロと明記。** `pm_mcp_server` は本番デーモンの起動スクリプトに現れない**開発時経路**。同居問題の対策としては数えない。ただし開発環境の防御としては実在の価値があるため、**ラベルを付け替えて維持**
- 2026-07-31 — **レビュー再応答（P10 / R8 格上げ / 挙動指紋）を反映。実測で judge の配置に事実誤認があった。**
  - **§2 / P10 新設：対策が「何を証明するか」を明示する。** 引用スパンの必須化に「捏造は原理的に不可能」と書いたのは、被覆率（P8）は検算したが**強度を検算しなかった**ためだった。**P8 は幅、P10 は強度。** 以後すべての対策に両方を併記し、**証明しないことを列挙する**
  - **§4.9：欠落を作れる位置を実測。** レビューは「judge が何を落とすかを決める位置にいる」としたが、**DeepSeek-V4-Flash は `scripts/eval/` 配下のオフライン評価専用で本番経路にいない**（`argus_ab.py` / `argus_ab_judge.py` / `minutes_ab.py` / `eval_rivault_models.py` のみ）。**pm.db に欠落を作ることはできないので、judge を差し替えても本番リスクは下がらない。** 実際の欠落生成点は **① Pass 1 抽出 ② トリアージ（`ingest/slack.py`:625、Slack・議事録の両方から呼ばれる）③ Box relevance 判定（`pm_box_relevance.py`:108）** の3つで、いずれも `call_argus_llm`。**独立系統の投入先を judge から②へ変更した**（単一関数で最も広く効くため）。ただし judge にモデル選定を歪める間接的影響はあるため、その旨は残した
  - **R8 を「受容」から「受容しない」へ格上げ。** RiVault の主力 Kimi-K2-Thinking は Kimi-K3 と**同一提供元**で、かつ**議事録生成・Slack抽出・トリアージという R5 の発生場所そのもの**を担う。並列ではなく**集中リスク**。Phase 4 で対処し、K3 移行再開時は Phase 6 の前提条件に含める
  - **R13：二段抽出の前提条件を追加。** stage 2 が stage 1 と同一モデルなら「自分が出さなかったもの」を自分で探す構図になり欠落に無力。**出自を変えることを要件化**
  - **§9.3 新設：挙動指紋（behavioral fingerprint）。** R12 を「受容」で終わらせず、固定プロンプト集（10〜20問・`temperature=0`）を日次で流して合格率と類似度の drift を監視する。**§9.1 のレッドチーミング結果は「申告されたバージョン」に対してしか成立しない**ため、黙って差し替わったことに気づく仕組みが要る。Phase 0 の更新通知の取り決め（合意）と対で運用する
  - **Phase 4：トリアージの fail 方向を KEEP に統一する項目を追加。** `triage_items` の既定が `missing_verdict="DROP"`、`triage_items_batched` が `"KEEP"` と逆向きだった。**欠落は敵対的でなくても失敗モードとして起きる**
- 2026-07-31 — **対策の中身に対する外部レビュー10点を反映。** 実測で3点に差分があった（うち1点はレビューより深刻、1点は過大主張、1点は既に充足）。
  - **§4.3 / Phase 1：canary のゲートが P8 の誤りを再生産していた。** 検知点1（ブローカー）は Phase 3 まで存在しないため、**Phase 1 時点の canary は出力側を覆っていない。** 「2週間発火せず」を安全の証拠として扱うゲートを廃し、Phase 1 は**計装の完全性**、発火の合否判定は Phase 3 以降に分割。R14 を追加
  - **Phase 1：canary 発火時のランブックを新設。** EGRESS 凍結 → 通知 → モデル経路切替 → トレース保全 → 経路特定。**凍結解除は人間の明示判断とし自動復帰させない**
  - **§4.8：引用スパンの必須化を Pass 1 ingest に一般化。** ただし実測で **Slack 側の `source_ref` は既に permalink が必須で入っている**（`ingest/slack.py`:934）ので、必要なのは**照合の実施と ts 粒度への細分化**であってポインタの付与ではない。議事録側は `file_path` のみで、スパン検証は Phase 4 の `raw_transcript` に依存する。**「捏造は原理的に不可能になる」は過大主張として退けた** — 逐語照合が証明するのは根拠の実在であって結論の妥当性ではなく、抽出は本質的に要約的である。改竄の3型（捏造／歪曲／欠落）のうち防げるのは捏造のみと明記
  - **§4.8：欠落対策として却下候補の記録。** ただし**議事録側には既に triage 機構がある**（DROP 候補を `deleted=1` ＋ 理由付きで保存）。原理的限界（抽出LLMが出力しなかったものは記録できない）と二段抽出という解を R13 として明示
  - **§4.9 対策3：出自の独立性が成立していない。** レビューの指摘より深刻で、RiVault の主力は **Kimi-K2-Thinking（K3 と同一提供元）**、judge は DeepSeek-V4-Flash。**使用中の全系統が中国系。** CAISI の知見は同系統間で相関するため差分が機能しない。`gemma4` を出自の異なる基準系として確保する項目を Phase 0 に追加
  - **Phase 3：ゲートを `.pre-commit-config.yaml` の lint に落とす項目を追加。** ゲートは一度きりだが lint は永続する。既存の ruff / pytest / mypy に4つの不変条件検査を足す
  - **§2 / P2：取得側に拡張。** Ingest Plane の外部呼び出しが LLM 出力でパラメータ化されていれば、注入文書が自分の読取範囲を広げられる。**実測では既に決定論的**（Slack は `--channel`、Box は設定の `folder_id`）なので、これは穴ではなく**明文化して固定すべき既存の不変条件**。lint と Phase 0 の全数確認に追加
  - **§4.4：ハッシュ連鎖の頭を外部アンカーに固定。** 検証者が改竄されうる側と同じ信頼領域にいるため、内部完結の連鎖は意図的改竄を検出できない。日次でブローカー経由の専用 Slack チャンネルへ投稿（ハートビート兼用）
  - **§4.6 / R12：`expected_revision` は検証不能。** `/v1/models` は `id` しか返さない（`llm.py`:115 が実際に `m["id"]` のみ使用）。pin を「検証」から「**運用主体の申告の記録**」に位置づけ直し、fail-closed の対象をモデル id に限定。Phase 2 のゲート文言も修正し、更新通知の取り決めを Phase 0 に追加
  - **§4.2：人間承認の対象を内容から宛先へ。** 文書自身が「散文への符号化は人間に見えない」と認めている以上、内容承認は符号化に対して儀式である。承認の問いを「この宛先に出してよいか」「事実として正しいか」の2つに限定し、**「符号化されていないか」を問うてはならない**と明記
  - **§4.4 / §1.2：`reasoning_traces` の保護と保持を定義。** モデルが見た機微データがそのまま入る新しい機微データストア。SQLCipher 適用・保持期間90日・機微データ棚卸しへの追加
- 2026-07-31 — **3回目のコード実測レビューを反映。P8 を出口側の分母に適用した結果、「6箇所」が誤りと判明。**
  - **§4.2 / §4.8：輸送層のコストが非対称であることが判明。** 前版の「`canvas_utils` / `slack_post` の1段手前、実測6箇所」は**自動投稿5箇所だけを数えた数字**だった。実測では **Canvas は `post_to_canvas` の単一ファネル（8呼び出し/8モジュール）、Box は `box_upload_or_version` の単一ファネル（9モジュール）で各1箇所で覆えるが、Slack はファネルが存在せず SDK 直叩き25箇所/7モジュール**。`slack_post.py` は `_to_slack_mrkdwn` / `_split_mrkdwn_to_blocks` の2関数のみで**投稿関数を持たない**（輸送層ではなく整形ヘルパ）。Phase 3 を「Canvas・Box は安い / Slack はファネル新設＋移送」に書き分けた
  - **§4.2 / §4.3：テキスト以外の出口を追加。** `/argus-narrate` が LLM 生成テキストから mp3/mp4 を合成し `files_upload_v2` で投稿している（`narrate.py`:146/:232）。**EGRESS ツール3つに含まれず、テキストDLPもエントロピー検査も canary も原理的に効かない。** 対策は順序制約（合成前テキストをブローカーに通し、通過したものだけ TTS に渡す）とし、原則として明文化。R11 を追加
  - **Phase 3 のゲートから関数名の列挙をやめた。** `post_to_canvas` / `chat_postMessage` を名指しすると `chat_postEphemeral` と `files_upload_v2` が漏れる。「Slack SDK クライアント・`post_to_canvas`・`box_upload_or_version` にブローカー以外から到達しないこと」に変更し、TTS の入力テキスト検査をゲート3として追加
  - **§3.2：Ingest Plane の定義が実装と矛盾していた。** 「書込スコープのトークンは持たない」と定義したが、`pm_minutes_import.py`（1箇所）と `transcribe_pipeline.py`（3箇所）が Slack に投稿している。**案A（Write Plane へ移す）を採用**し、移送は Phase 3 の Slack ファネル新設と同時に行うことにした。帰属の確定を Phase 2-5 に追加
  - **接続先の数を「9エントリ / 8ホスト」に統一。** 本文中で 7 / 8 が混在していた（`127.0.0.1` が VOICEVOX と fish-speech の2ポートを持つためエントリ数とホスト数が一致しない）。Phase 2 ゲート1 は検証条件なので数が要る
  - §10：箇条書きの番号重複（11,12,13,12,13）を修正し、輸送層の非対称性とテキスト以外の出口を項目として追加
- 2026-07-31 — **2回目のコード実測レビューを反映。最大の成果は誤りの「型」の特定。**
  - **§2 / P8 新設：ツールレジストリを漏斗と見なす誤りを3回犯していた**（ブローカーの対象／監査の対象／書込制約の対象）。**Argus の実際のデータフローの主流は cron とパイプラインであってエージェントループではない。** 以後、対策ごとに「実際に流れている量の何割を覆うか」を検算する
  - **§4.1：MUTATE ツールは0件**（READ 13 / MUTATE 0 / EGRESS 3）。当初の MUTATE カテゴリは**現存しないツールの記述**だった。カテゴリは将来の受け皿として残すが、現在0件であることを明示
  - **§4.8 / §4.9 を統合。** 両者は同一現象を出口側と入口側から見たものだった。管理対象は「LLM が生成し pm.db に入り、やがて外に出るコンテンツ」の1つ。**真の入口は Pass 1 ingest**（`ingest/slack.py`:973/:989、`ingest/minutes.py`:353/:376）で、量的に圧倒的に大半。ツールを経由しない
  - **当初のB案（構造化フィールドのみに制約）を却下。** `decisions.content` は抽出された決定事項そのものであり、自由文が成果物の本質なので入口側では制約できない。**被覆率0%の対策であり、実装すれば「守られた」という誤った確信を与える点で何もしないより悪かった**
  - **§4.8：Patrol の `_append_close_note` を「引用」に限定**する対策を追加（引用元 row_id と逐語一致を機械的に検証）
  - **§4.7 / P9 新設：`${ENV_VAR}` を並べた allow-list は制約にならない。** 接続に使うのと同じ環境変数で許可を定義していたため何も禁止していなかった。リテラル値で書き、`urlparse(os.environ[...])` で照合して fail-closed にする（`network_pin.py`）。型も不整合だった（URL vs host）
  - **§4.7 細部：EMBED は 443 でなく 8001**（ローカル vLLM / DGX-Spark）。**VOICEVOX（`localhost:50021`）が抜けていた**（`pm_tts.py` の既定バックエンド。fish は `FISH_TTS_HOST` 設定時のみ）。接続先は8つ
  - **§3.1 / §3.2：Ingest Plane を第4の平面として定義。** allow-list にのみ現れて定義がなかった。取り込みは Slack/Box の**読取スコープ**トークンを必要とするため、トークン分割は3分割（Ingest=読取／Read=なし／Write=書込）になる。TTS は ingest ではなく Write Plane
  - **Phase 2-3：パージをやめ「暗号化して移送」に限定。** Phase 4 の遡及照合の材料（VTT 115本・mp4 115本）が消える矛盾があった
  - **Phase 1：`mcp_tools.py` ラップの記述を訂正**（前回の反映漏れ）。Pass 1 ingest と Patrol の書き込みも記録対象に追加
  - **Phase 4：`MUTABLE_FIELDS` 中心から Pass 1 ingest 中心に書き直し**
  - Phase 0：`audit_log` の記録範囲が判明（`write_audit_log(conn, item_id, field, old, new, source)`）。**§4.9 対策2（人間の編集傾向の可視化）は既存スキーマで実装可能**、一方 egress ログとしては不十分という評価は変わらず
- 2026-07-31 — **別セッションによるコード実測レビューを反映。事実誤認3件を訂正した。** 本文書は当初 README のみに基づいて書かれていたため、実装との乖離があった。
  - **§4.2 / §4.8：ブローカーを輸送層に下げた。** 朝のブリーフィング（cron、`pm_argus.py`）と Patrol Agent（`patrol/actions.py`）が `canvas_utils` / `chat_postMessage` を直接呼んでおり、`output_tools.py` を通らない。洗浄経路の一般形は「MUTATE経由」ではなく**「非対話的な自動投稿パイプライン」**。しかもこれが**実際の出力量の大半かつ人間の介在なし**であり、当初設計では守られる範囲が少数派だった。Phase 3 のゲートに「LLM生成文が `post_to_canvas` / `chat_postMessage` に到達する経路がブローカー以外に存在しない」を追加
  - **§3.3：チョークポイントを2箇所に訂正。** ツールは16、`_call_mcp` 委譲は8つのみで、**EGRESS 3つは `agent_tools.py` の直接実装**。`mcp_tools.py` だけにラップを入れると**最も記録が必要な送信系が1件も残らない**という逆の結果になっていた。一方 `_FILE_PINNED_EXCLUDED_TOOLS` / `exclude_tools` が既存のため 5a の実装難度は想定より低い
  - **§4.7：外部接続先を7つに訂正**（RiVault / EMBED / Docling / FISH_TTS を追加）。「公開インターネットへの任意通信を持たない」という命題は成立するが数え方が誤っていた。**allow-list を記載どおり実装すると embedding が届かずハイブリッド検索が静かに劣化する**（2026年6〜7月に約1か月実際に発生）。「**fail-closed は大きく失敗することと対にする**」を原則化し、起動時到達性アサーションと vector 脚の空検出を追加。RiVault を `model_pin` 対象（第2のLLMプロバイダ）、Docling を注入経路上のコンポーネントとして位置づけ
  - **§4.9：事後照合の土台が存在しないことが判明。** `pm_from_recording.sh:540` が Whisper 原文を削除し、`minutes/*.db` に原文列がない。`combined.txt` は Stage 1 の LLM 出力なので ground truth にならない。**方針決定：Whisper 原文を暗号化DBに保持する**（`raw_transcript` 列追加、削除処理の停止、保持期間の設定）
  - **§1.2：保護レベルの逆転を追記。** `data/processing/` に会議録音 mp4 115本・VTT 115本が**平文**で残っている。要約が SQLCipher で守られ原データが平文という状態。Phase 2 に暗号化またはパージを追加
  - **§4.1：宛先引数の現状を追記。** `box_upload_file` のみ設定解決で P2 適合済み。`slack_post_message` の `channel` と `canvas_post_content` の `canvas_id` はモデル指定可。前者の形に揃えるのが最短路
  - §4.7：`mcp_allowlist.yaml` の command を `~/.venv_aarch64/bin/python3` に訂正（素の `python3` は sqlcipher3 が無く起動しない）
- 2026-07-31 — **能力分離を5a/5bに分割し、5a（Plane分類・レジストリ制限・Read Planeからのトークン除去）を Phase 2 に前倒し。** 前版で認証境界を根拠に分離の優先度を下げたのは過剰な修正だった。認証境界は運用で変わる外部事実でありコードの性質ではない。§2 に「なぜ P1 が土台なのか」と「認証境界と分離の関係」を追記し、**P1 → P2 → P3 の依存関係**（トークン分離がないと allow-list は規約に過ぎない）を明示。R1 の格下げに「分離が入って初めて持続する」条件を付加。§6 に「能力分離は K3 導入のクリティカルパス上にある」を追記
- 2026-07-31 — **`pm_web_fetch.py` の廃止を決定**（隔離ではなく削除）。**外部アクセスを3層allow-list（ネットワーク／ツール・MCP／宛先）として §4.7 に定義**。Argus の出力先が理研の認証境界の内側に限られることを評価に反映し、**残留リスクR1（外部閲覧可能な宛先への流出）を格下げ**。代わって **R5（改竄）を主要な残留リスクに格上げ**し §4.9「出力の完全性」を新設。§4.8「MUTATE 経由の洗浄経路」を新設（レポート自動公開がブローカーを迂回する問題）。Phaseを0〜6に再編し、`pm_web_fetch` 廃止と認証境界の棚卸しを Phase 2 に前倒し、能力分離を Phase 5 に後退（根拠を流出対策から改竄対策へ）
- 2026-07-31 — 初期作成。脅威モデル・3平面アーキテクチャ・Phase 0〜5・K3提案のゲート表を定義
