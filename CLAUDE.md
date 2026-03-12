# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## プロジェクト文脈

このリポジトリは**富岳NEXTプロジェクトのプロジェクトマネージメント支援システム**である。

### 設計思想：目指すプロマネの姿

このシステムが目指すのは「議事録係＋ToDoリスト管理」ではなく、**プロジェクトのゴールへの到達を管理するプロジェクトマネジメント**である。

LLMを使ったPMツールは、発言・議事録・Slackから決定事項やアクションアイテムを拾い上げることに終始しがちである。それは情報の整理には役立つが、「プロジェクトが今どこにいるのか」「ゴールに向けて前進しているのか」を答えることができない。本システムは以下の2層構造でこの問題に対処する。

```
【トップダウン層】 ゴール・マイルストーン
                  └─ goals.yaml に人間（意思決定者）が定義・承認、gitで変更履歴管理
                          ↓ 評価の軸を与える
【ボトムアップ層】 アクションアイテム・決定事項
                  └─ 会議議事録・Slackから LLM が自動抽出・マイルストーンに紐づけ
```

**LLMと人間の役割分担**:
- 「何を目指すか」「マイルストーンの定義・承認」→ 人間（意思決定者）
- 「情報の収集・整理・抽出」「マイルストーンへの紐づけ推定」→ LLM
- 「誤りの修正・最終判断」→ 人間（Slack Canvas上で編集）
- 「達成状況の計算・レポート生成」→ システム

Slackの日常的なやり取りと会議議事録を統合し、決定事項・アクションアイテムの一元管理と定期レポート生成を目的とする。

### プロジェクト概要

1. 富岳NEXTに求められる役割
近年、シミュレーションやデータサイエンスの進展に加え、生成AIの急速な普及により計算資源の需要が急増しています。こうした背景を踏まえ、「富岳NEXT」には、シミュレーション性能をさらに強化するとともに、AIにおいても世界最高水準の性能を達成し、両者が密に連携して処理を行うことができる「AI-HPCプラットフォーム」となることが求められています。

2. 富岳NEXT開発体制
理研を中核とし、富士通のCPU・システム化技術、NVIDIAのGPU技術を活用した三者連携のもとで進める。AI性能（FP8）で世界初のゼタ（Zetta）スケールを達成する競争力のあるシステムを構築し、グローバルエコシステムの形成を目指す。

3. 富岳NEXT開発方針
「Made with Japan」「技術革新」「持続性/継続性」を掲げ推進する。2030年頃の稼働開始を目標に理研神戸地区隣接地に整備する。

### ステークホルダー

* 理化学研究所
- 松岡 聡 Satoshi Matsuoka matsu@acm.org: 計算科学研究センター　センター長
- 近藤 正章 Masaaki Kondo	masaaki.kondo@riken.jp: 次世代計算基盤開発本部　部門長、最終意思決定者
- 佐野 健太郎	Kentaro Sano	kentaro.sano@riken.jp: 次世代計算基盤開部門 次世代計算基盤システム開発ユニット　ユニットリーダー、アーキテクチャエリア責任者、マイクロ・ノードアーキテクチャWGリーダ
- 佐藤　賢斗 Kento Sato	kento.sato@riken.jp: 次世代計算基盤開発部門 先進的計算基盤技術開発ユニット ユニットリーダー、システムソフトウェアエリア責任者
- 青木 保道	Yasumichi Aoki	yasumichi.aoki@riken.jp: 次世代計算基盤開発部門 次世代計算基盤アプリケーション開発ユニット　ユニットリーダー、アプリケーション開発エリア責任者、HPCアプリケーションWGリーダー、富岳NEXTのアプリケーションに関する意思決定者
- 山本 啓二	Keiji Yamamoto	keiji.yamamoto@riken.jp: 次世代計算基盤部門 次世代計算基盤運用技術ユニット　ユニットリーダー、運用技術エリア責任者
- 嶋田　庸嗣	Yoji Shimada	yshima@riken.jp: 次世代計算基盤部門 マネジメント室　室長

* 富士通株式会社
- 新庄 直樹 Naoki Shinjo shinjo@fujitsu.com: 富士通側責任者

* NVIDIA
- Dan Ernst Dan Ernst dane@nvidia.com: NVIDIA側のアーキテクチャ責任者
- Heidi Poxon Heidi Poxon hpoxon@nvidia.com: NVIDIA側のアプリケーション責任者

### 主なプロジェクト参加者

* 理化学研究所
- 庄司 文由	Fumiyoshi Shoji	shoji@riken.jp: 次世代計算基盤開発本部　副部門長
- 安里 彰	Asato Akira	akira.asato@riken.jp: アーキテクチャエリア エリアマネージャ
- 上野 知洋	Tomohiro Ueno	tomohiro.ueno@riken.jp: アーキテクチャエリア システム・ネットワークWGリーダ
- Jens Domke Jens Domke	jens.domke@riken.jp: アーキテクチャエリア コデザインWGリーダ
- 村井 均	Hitoshi Murai	h-murai@riken.jp: システムソフトウェアエリア プログラミング環境WGリーダー
- 今村 俊幸	Toshiyuki Imamura	imamura.toshiyuki@riken.jp: システムソフトウェアエリア 数値計算ライブラリ・ミドルウェアWGリーダー
- 中村 宜文	Yoshifumi Nakamura	nakamura@riken.jp: システムソフトウェアエリア 通信ライブラリWGリーダー、アプリケーションエリア　ベンチマークWGサブリーダー
- Wahib Mohamed	Wahib Mohamed	mohamed.attia@riken.jp: システムソフトウェアエリア AIソフトウェアWGリーダー
- William Dawson William Dawson	william.dawson@riken.jp: アプリケーション開発エリアサブリーダー、HPCアプリケーションWGサブリーダー、SubWG2オーガナイザー
- 井上 晃	Hikaru Inoue hikaru.inoue@riken.jp: アプリケーション開発エリア エリアマネージャ
- 西澤 誠也	Seiya Nishizawa	s-nishizawa@riken.jp: アプリケーション開発エリア HPCアプリケーションWGサブリーダー
- 小林 千草	Chigusa Kobayashi	ckobayashi@riken.jp: アプリケーション開発エリア ベンチマークWGリーダー
- 伊東 真吾	Shingo Ito	shingo.ito@riken.jp:アプリケーション開発エリア　HPCアプリケーションWG SubWG1オーガナイザー
- 藤田　航平	Kohei Fujita	fujita@eri.u-tokyo.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG4オーガナイザー
- 大西 順也	Junya Onishi	junya.onishi@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG5オーガナイザー
- 金森 逸作	Issaku Kanamori	kanamori-i@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG6オーガナイザー兼リエゾン
- 鈴木 厚	Atsushi Suzuki	atsushi.suzuki.aj@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG8オーガナイザー
- 幸城 秀彦	Hidehiko Kohshiro	hidehiko.kohshiro@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG2メンバー
- 足立 幸穂	Sachiho Adachi 	sachiho.adachi@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3メンバー
- 河合 佑太	Yuta Kawai	yuta.kawai@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3メンバー
- 田中 福治	Fukuharu Tanaka	fukuharu.tanaka@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG7メンバー
- James Taylor	James Taylor	james.taylor@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3メンバー
- 垂水 勇太	Yuta Tarumi	yuta.tarumi@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3メンバー
- Hascoet, Tristan Erwan Marie	Hascoet, Tristan Erwan Marie	tristan.hascoet@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3メンバー
- 滝脇　知也	Tomoya Takiwaki	takiwaki.tomoya@nao.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG6リエゾン
- 寺山　慧	Kei Terayama	terayama@yokohama-cu.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG1リエゾン
- 高橋　大介	Daisuke Takahashi	daisuke@cs.tsukuba.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG8リエゾン
- 山口 弘純	Hirozumi Yamaguchi	hirozumi.yamaguchi@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG7オーガナイザー兼リエゾン
- 下川辺　隆史	Takashi Shimokawabe	shimokawabe@cc.u-tokyo.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG7アドバイザー
- 岩下 武史	Takeshi Iwashita	iwashita@i.kyoto-u.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG ブロック2アドバイザー
- 深沢　圭一郎	Keiichiro Fukazawa	fukazawa@chikyu.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG ブロック1アドバイザー
- 山地　洋平	Youhei Yamaji	YAMAJI.Youhei@nims.go.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG2リエゾン
- 小玉　知央	Chihiro Kodama	kodamac@jamstec.go.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3リエゾン
- 高木　亮治	Ryoji Takaki	takaki.ryoji@jaxa.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG5リエゾン
- 加藤　千幸	Chisachi Kato	kato.chisachi24@nihon-u.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG5アドバイザー
- 中島 研吾	Kengo Nakajima	nakajima@cc.u-tokyo.ac.jp: アプリケーション開発エリア　HPCアプリケーションWG ブロック1アドバイザー
- 富田 浩文	Hirofumi Tomita	htomita@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG ブロック2アドバイザー
- 横田 理央	Rio Yokota	rio.yokota@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG8メンバー
- 似鳥 啓吾	Keigo Nitadori	keigo@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG6メンバー
- 曽田 繁利	Shigetoshi Sota	sotas@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG2メンバー
- 大塚 雄一	Yuichi Otsuka	otsukay@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG2メンバー
- 安藤 和人	Kazuto Ando	kazuto.ando@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG5メンバー
- 山浦 剛	Tsuyoshi Yamaura	tyamaura@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG3メンバー
- 黒田 明義	Akiyoshi Kuroda	kro@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG8メンバー
- 石附 茂	Shigeru Ishizuki	shigeru.ishizuki@riken.jp: アプリケーション開発エリア　HPCアプリケーションWG SubWG4,7メンバー
- 足立 朋美	Tomomi Adachi	tomomi.adachi@riken.jp: 次世代計算基盤部門 マネジメント室
- 西 直樹	Naoki Nishi	naoki.nishi@riken.jp: 次世代計算基盤部門 マネジメント室
- 西田 拓展	Takuhiro Nishida	takuhiro.nishida@riken.jp: 次世代計算基盤部門 マネジメント室

* 富士通株式会社
- 本藤 幹雄 Mikio Hondou hondou@fujitsu.com: マイクロ・ノードアーキテクチャリーダー
- 草野 義博 Yoshihiro Kusano kusano-y@fujitsu.com
- 加瀬 将 Masaru Kase kase.masaru@fujitsu.com
- 三木 淳司 Atsushi Miki miki-atsushi@fujitsu.com
- 稲垣 貴範 Takanori Inagaki i-takanori@fujitsu.com
- 福本 尚人 Naoto Fukumoto fukumoto.naoto@fujitsu.com: コデザインリーダー
- 五味 照義 Teruyoshi Gomi gomi.teruyoshi@fujitsu.com
- 中村 洸介 Kosuke Nakamura nakamura.kosuke@fujitsu.com
- 淋 靖英  Yasuhide Sosogi sosogi.yasuhide@fujitsu.com
- 佐藤 賢太 Sato Kenta sato.kenta@fujitsu.com
- 安島 雄一郎 Yuichiro Ajima aji@fujitsu.com: システム・ネットワークリーダー
- 山中 栄次 Eiji Yamanaka e-yamanaka@fujitsu.com
- 小村 幸浩 Yukihiro Komura y_komura@fujitsu.com
- 津金 佳祐 Tsugane, Keisuke tsugane.keisuke@fujitsu.com

* NVIDIA
- 成瀬 彰 Akira Naruse anaruse@nvidia.com: アプリケーションエリア担当技術者
- 永田 聡美 Satomi Nagata snagata@nvidia.com: アプリケーションエリア担当営業 シニアマネージャ
- 竹本 祐介 Yusuke Takemoto ytakemoto@nvidia.com: アプリケーションエリア担当技営業 カスタマープログラムマネージャ

### プロジェクト固有の用語

**システム・ハードウェア関連**
- 富岳NEXT: 次世代スーパーコンピュータ開発プロジェクト
- MONAKA-X（富士通製次世代CPU、1.4nmプロセス、256コア/ソケット）
- NVLink-C2C（CPU-GPU間の広帯域コヒーレント接続）
- Scale-upネットワーク / Scale-outネットワーク（ノード内GPU接続 vs ノード間接続の区別）
- NVL4 / NVL72（Scale-upドメインサイズの選択肢）
- 3Dチップレット（MONAKA-Xのアーキテクチャ）
- SVE2 / SME2（ARMv9命令セットの拡張）

**プロジェクト・組織関連**
- Made with Japan（国際連携開発コンセプト）
- Genesis Mission（DOEとの国際協力枠組み）
- lighthouse challenge（Genesis Missionの26の科学技術目標）
- 4者連携（ANL・NVIDIA・富士通・理研によるMOU）
- HPSF（国際HPCソフトウェア組織、ベンダー中立な開発体制）
- JAMセッション（最先端AIツールを科学技術に応用する合同セッション）
- RiVault（理研R-CCS製LLM）
- RIKEN TRIP-AGIS（理研のAI4S関連プロジェクト）
- JHPC-quantum（量子-HPC連携プロジェクト）
- DBO方式（Design Build Operate、新計算機棟の建設方式）

**アプリケーション・ソフトウェア関連**
- Benchpark（DOE/MEXT共同開発のCI/CD/CBベンチマークフレームワーク）
- Ozakiスキーム（高精度行列演算を低精度演算器で実現する手法）
- バーチャル富岳（ソフトウェア検証環境）
- Tadashi（コード生成・最適化AIツール、スライド中に図示）
- GENESIS（分子動力学シミュレーション）
- SALMON（Scalable Ab-initio Light-Matter simulator for Optics and Nanoscience）
- SCALE-LETKF（Coupled weather simulation and data assimilation application）
- E-Wave（地震シミュレーションアプリ）
- FrontFlow/Blue（CFD）
- LQCD-DWF-HMC（Hybrid Monte-Carlo algorithm of domain wall fermions in Lattice QCD）
- FFVHC-ACE（次世代流体解析ソルバー）
- UWABAMI+INAZUMA（一般相対論的輻射磁気流体コード）
- Spack (HPCソフトウェアのパッケージ管理ツール)
- Ramble (Spackと連携して動作するHPCワークロード管理・実験自動化ツール)

**性能・アーキテクチャ概念**
- ゼタ（Zetta）スケール（FLOPSスケールの表現、AI性能目標）
- AI4S / AI for Science（科学へのAI応用の総称）
- Big Simulation / Big Data / AI Scientist（HPC-AI融合の3類型）
- テストベッド Phase-1〜4（段階的GPU環境整備計画）
- 尾崎スキーム（Ozakiスキームの別表記）

**データセンター関連**
- 温水冷却（冷凍機不要化による省エネ手法）
- 冷却電力比率10%目標（「富岳」の35%から削減）

### 内部・外部の境界

- 機密性2_限定（アクセス者限定）: 富岳NEXTに参画している関係者で当該フォルダ／ファイルにアクセスが必要な者（アクセス者を限定）
- 機密性2_理研内（客員含む）: 富岳NEXTに参画している理研研究員（理研客員研究員含む）
- 機密性2_関係者（要NDA）: 富岳NEXTに参画している理研研究員（理研客員研究員含む）、Research Engineer、および、富岳NEXTに参画している外部関係者（理研に兼務かかっていない他組織の者）
- 機密性1_公開（公開情報）: 一般のすべての方

### 会議の種類と頻度

- Leader_Meeting: 週1回、アプリケーション開発エリアのユニットリーダー、WGのリーダーおよびサブリーダーとの情報共有会
- Block1_Meeting: 不定期、HPCアプリケーションWGのブロック1の打合せ
- Block2_Meeting: 不定期、HPCアプリケーションWGのブロック2の打合せ
- SubWG_Meeting: 不定期、HPCアプリケーションWGのSubWGの打合せ
- BenchmarkWG_Meeting: 不定期、ベンチマークWGの打合せ
- Co-design_Review_Meeting: 月1回、コデザイン検討会議本番(3者会議)

### チャンネルID・Canvas IDの調べ方

<!-- TODO: 調べ方を記述する -->

<!-- TODO: 議事録ファイル（meetings/*.md）が平文のままファイルシステムに残るのはセキュリティリスク。
     trans.sh + whisper_vad.py による文字起こし結果を .md ファイルに出力するのではなく、
     pm_meeting_import.py を直接呼び出して暗号化済み pm.db にのみ保存する方式に変更することを検討する。
     あわせて meetings/*.md の既存ファイルの扱い（削除・暗号化アーカイブ化等）も要検討。 -->

### Slackチャンネル一覧

| チャンネルID | 用途 | 対応Canvas |
|---|---|---|
| `C08SXA4M7JT` | 20_1_リーダ会議メンバ | `F0AAD2494VB` |
| `C0A9KG036CS` | personal | `F0AA24YH2F9` | for DEBUG purpose

---

## システム概要

情報の流れは以下の2系統を統合する。

```
[Slack] ─── slack_pipeline.py ───→ {channel_id}.db
                                          ↓
[会議議事録] ── pm_meeting_import.py ──→ pm.db ←─ pm_extractor.py
  meetings/*.md                    (決定事項・               ↑
                                 アクションアイテム)   {channel_id}.db
                                          ↓
                                    pm_report.py
                                          ↓
                                   Slack Canvas / レポート
```

**各DBの役割分担**:
- `{channel_id}.db` — Slackデータ専用。チャンネルごとに独立。
- `pm.db` — PM情報専用。複数チャンネル・複数会議を横断して統合。

---

## ファイル構成

```
slack/
├── meetings/                        # 議事録の一次着地点（Markdown形式）
│   └── YYYY-MM-DD_会議名.md
├── scripts/                         # スクリプト一式
│   ├── slack_pipeline.py            # Slack取得・要約・Canvas投稿（統合版）
│   ├── pm_meeting_import.py         # 議事録 → pm.db（単一ファイル / 一括処理・一覧・削除）
│   ├── pm_extractor.py              # Slack DB → 決定事項・アクションアイテム抽出 → pm.db
│   ├── pm_report.py                 # pm.db → 進捗レポート生成・Canvas投稿
│   ├── pm_sync_canvas.py            # Canvas「対応状況」「マイルストーン」列 → pm.db 同期
│   ├── pm_relink.py                 # アクションアイテムの各フィールド（担当者・期限・内容・マイルストーン等）をCSV経由で一括編集（LLM不使用）
│   ├── pm_goals_import.py           # goals.yaml → pm.db 完全同期
│   ├── db_utils.py                  # DB接続の一元管理・平文DB暗号化変換（SQLCipher対応）
│   ├── trans.sh                     # 会議録音をテキスト化するSlurmジョブスクリプト（whisper_vad.pyを呼び出す）
│   └── whisper_vad.py               # VAD+DeepFilterNet+Whisperによる話者分離・文字起こし
└── data/                            # DBと出力ファイル
    ├── {channel_id}.db              # Slackデータ（例: C0A9KG036CS.db）
    ├── pm.db                        # PM統合データ
    └── slack_summarize_*.md         # 全体要約（デバッグ・履歴用）
```

---

## 環境変数

**トークンは `.bashrc` に直書きしないこと。** 全プロセスに漏洩する危険がある。

```sh
# 1. トークンファイルを作成（初回のみ）
mkdir -p ~/.secrets && chmod 700 ~/.secrets
cat > ~/.secrets/slack_tokens.sh << 'EOF'
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_MCP_XOXB_TOKEN="xoxb-..."
EOF
chmod 600 ~/.secrets/slack_tokens.sh

# 2. 実行前に読み込む（毎回）
source ~/.secrets/slack_tokens.sh
python3 scripts/slack_pipeline.py ...
```

ローカルLLM（OpenAI互換API）を使う場合:
```sh
export OPENAI_API_BASE="http://..."
export OPENAI_API_KEY="..."
export OPENAI_MODEL="..."
```

---

## 注意事項

- `claude -p` はClaude Codeセッション内からは実行不可（ネストセッション制限）。各スクリプトはClaude Codeの外のターミナルから実行すること。
- `call_claude()` 内で `CLAUDECODE` 環境変数を子プロセスから除外する処理を実装済み。
- `slack-mcp-server` バイナリが必要。PATH、`~/bin/`、`~/.local/bin/` の順で探索する。
- Python仮想環境は `~/.venv_x86_64` を使用。`~/.venv_x86_64/bin/python3 scripts/xxx.py` で実行する。

---

@docs/commands.md

---

@docs/schema.md

---

@docs/roadmap.md
