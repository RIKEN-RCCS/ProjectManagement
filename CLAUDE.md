# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## プロジェクト文脈

このリポジトリは**富岳NEXTプロジェクトのプロジェクトマネージメント支援システム**である。Slackの日常的なやり取りと会議議事録を統合し、決定事項・アクションアイテムの一元管理と定期レポート生成を目的とする。

### プロジェクト概要

1. 富岳NEXTに求められる役割
近年、シミュレーションやデータサイエンスの進展に加え、生成AIの急速な普及により計算資源の需要が急増しています。また、AIやシミュレーション、自動実験、リアルタイムデータを組み合わせた新しい科学研究の重要性が高まるなど、必要とされる計算資源は一層多様化しています。こうした背景を踏まえ、新たなフラッグシップスーパーコンピュータとして、「富岳NEXT」には、従来のスーパーコンピュータが追求してきたシミュレーション性能をさらに強化するとともに、AIにおいても世界最高水準の性能を達成し、両者が密に連携して処理を行うことができる「AI-HPCプラットフォーム」となることが求められています。

2. 富岳NEXT開発体制
AIおよびシミュレーションの両面で世界最高水準の性能を達成するため、「富岳NEXT」の開発は、理研を中核とし、高性能ARMベースCPUで世界をリードする富士通新しいタブで開きますのCPU・システム化技術、AI/HPC向けGPUで世界トップシェアを誇るNVIDIA新しいタブで開きますのGPU技術およびグローバルエコシステムを活用した三者連携のもとで進めます。
また、システムソフトウェアを含めたソフトウェア開発は、三者連携による取組に加え、国際連携によるオープンな体制で実施する計画です。これらの取組により、AI性能（FP8）で世界発のゼダ（Zetta）スケールを達成する競争力のあるシステムを構築し、グローバルマーケットへの展開を通じた世界的エコシステムの形成を目指します。

3. 富岳NEXT開発方針
「富岳NEXT」の開発においては、「次世代計算基盤に関する報告書 最終とりまとめ新しいタブで開きます」や「次世代計算基盤に係る調査研究新しいタブで開きます」の研究結果、さらに「富岳」の開発と運用による経験と教訓を踏まえた検討結果を基に、開発方針として、「Made with Japan」、「技術革新」、「持続性/継続性」を掲げ、推進していきます。
これらの開発方針を基盤に、次世代AI-HPCプラットフォームによる計算可能領域の拡張と「AI for Science新しいタブで開きます」による新しい科学の創出、先端AI技術・計算基盤における日本の主権確保、さらに半導体や計算資源のロードマップに基づく持続的研究開発を進めることで、世界的な「富岳NEXT」エコシステムを築き上げ、日本の半導体産業と情報基盤とさらなる強化を目指します。

4. 運用方針
「富岳NEXT」は、2030年頃の稼働開始を目標に理研神戸地区隣接地に整備し、「富岳」からのシステム移行時においても計算資源が利用不可となる期間を極力生じさせない利用環境を整え、世界最高水準の計算性能・資源を安定的に提供し続けることを目指しています。また、量子コンピュータとの連携により新たな計算領域を拡大しつつ、最新の冷却技術の導入や再生可能エネルギーの活用を促進するシステム運用技術等を組み合わせることで、省エネルギー化、低炭素化を追求します。これらの取組に加え、AIによる運用および利用者支援をさらに進化させることで、計算基盤の持続性と効率性を確保し、誰もが利用しやすい研究環境を提供する計画です。

5. 研究開発テーマ
5.1. アーキテクチャエリア
「富岳NEXT」の開発において、半導体製造技術やパッケージング技術、およびメモリ技術の動向を調査しながら、CPUや加速機構のマイクロアーキテクチャやメモリサブシステム、計算ノードアーキテクチャ、Scale-upやScale-outの相互接続網、全体システムなどの、主にハードウェア設計に関する研究開発を行う。特に、開発アーキテクチャに対するアプリケーション性能モデリングの研究を行いつつ、アプリケーションの特性を考慮して、ハードウェアシステムとアプリケーションの協調設計を実施する。

5.2. システムソフトウェアエリア
「富岳NEXT」の目標性能達成や高いユーザビリティ実現に向け、ハードウェアの潜在能力を最大限に引き出すシステムソフトウェアを開発する。開発成果はオープンソースとして公開し、国際的なOSSコミュニティとの連携を通じて継続的な開発とエコシステム形成を進める。また、開発環境、数値計算・通信基盤、AIソフトを有機的に統合し、次世代のHPC-AI融合基盤を創出する。

5.3. アプリケーション開発エリア
「富岳NEXT」を代表とする次世代計算基盤の協調設計を想定したHPCアプリケーション研究開発、開発支援、性能評価のためのベンチマークからなる、一連の研究開発を行う。また、シミュレーションとAI技術の高度な融合によるHPCアプリケーションの高速化・機能の拡張と強化を支える基盤技術の創出を目指す。これらの活動を効果的かつ効率的に行うための各種フレームワークの開発と公開を行う。

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
- 福本 尚人 Naoto Fukumoto fukumoto.naoto@fujitsu.com: アプリケーションエリア担当技術者

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
- Co-design_RF_Syncup_Meeting: 月1回、コデザイン検討会議 3者会議の富士通との準備会議
- Co-design_RN_Syncup_Meeting: 月1回、コデザイン検討会議 3者会議のNVIDIAとの準備会議
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
│   ├── pm_meeting_import.py            # 議事録 → pm.db（1ファイル単位）
│   ├── pm_meeting_bulk_import.py            # meetings/ の議事録を一括で pm.db に登録
│   ├── pm_extractor.py              # Slack DB → 決定事項・アクションアイテム抽出 → pm.db
│   ├── pm_report.py                 # pm.db → 進捗レポート生成・Canvas投稿
│   ├── pm_sync_canvas.py            # Canvas「対応状況」列 → pm.db 同期
│   ├── db_utils.py                  # DB接続の一元管理（SQLCipher暗号化対応）
│   ├── db_migrate.py                # 平文DBを暗号化DBに変換
│   ├── trans.sh                     # 会議録音をテキスト化するSlurmジョブスクリプト（whisper_vad.pyを呼び出す）
│   └── whisper_vad.py               # VAD+DeepFilterNet+Whisperによる話者分離・文字起こし
└── data/                            # DBと出力ファイル
    ├── {channel_id}.db              # Slackデータ（例: C0A9KG036CS.db）
    ├── pm.db                        # PM統合データ
    └── slack_summarize_*.md         # 全体要約（デバッグ・履歴用）
```

---

## 主なコマンド

### 1. Slack取得・要約・Canvas投稿（slack_pipeline.py）

```sh
# 通常運用: 差分のみ取得・要約してCanvas投稿
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db

# 初回・過去分の取り込み（oldest をAPIに渡してページネーション全件取得）
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db \
    --since 2025-04-01

# Canvas投稿せず取得・要約のみ（全体要約生成もスキップ）
python3 scripts/slack_pipeline.py -c C08SXA4M7JT --db data/C08SXA4M7JT.db \
    --skip-canvas
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID |
| `--db PATH` | `data/{channel_id}.db` | SQLite DBファイルパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のメッセージのみ取得（APIに oldest として渡す） |
| `-l N` | `100` | 1ページあたりの取得件数上限（最大999） |
| `--skip-fetch` | - | Slack API取得をスキップ（DBのみ使用） |
| `--force-resummary` | - | 全スレッドを強制再要約 |
| `--skip-canvas` | - | Canvas投稿・全体要約生成をスキップ |
| `--no-permalink` | - | パーマリンク取得を無効化 |
| `--canvas-id ID` | `F0AAD2494VB` | 投稿先CanvasID |

### 2. 会議録文字起こし（trans.sh + whisper_vad.py）

```sh
# Slurmジョブとして投入（ai-l40sパーティション、GPU1枚）
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4
sbatch scripts/trans.sh GMT20260302-032528_Recording.mp4 30   # 冒頭30秒スキップ
```

| 引数 | 説明 |
|---|---|
| arg1 | 入力動画/音声ファイル（拡張子付き） |
| arg2 | 冒頭スキップ秒数（省略可） |

処理フロー: ffmpeg → WAV変換（16kHz, mono） → DeepFilterNetノイズ除去 → SileroVAD → pyannote話者分離 → Whisper large-v3 文字起こし
出力: 入力と同名の `.md` ファイル（タイムスタンプ・話者ラベル付きMarkdown形式）

### 3. 会議議事録の一括登録（pm_meeting_bulk_import.py）

`meetings/` ディレクトリ内の `YYYY-MM-DD_{会議名}.md` ファイルを一括で pm.db に登録する。ファイル名から `--held-at` と `--meeting-name` を自動抽出して `pm_meeting_import.py` を順次呼び出す。`_parsed.md` で終わるファイルは対象外。

```sh
# 全ファイルを一括登録（初回）
python3 scripts/pm_meeting_bulk_import.py

# 確認のみ（DB保存なし）
python3 scripts/pm_meeting_bulk_import.py --dry-run

# 特定日付以降のみ対象
python3 scripts/pm_meeting_bulk_import.py --since 2026-01-01

# 既存レコードを上書き
python3 scripts/pm_meeting_bulk_import.py --force
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--meetings-dir DIR` | `meetings/` | 議事録ディレクトリ |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降のファイルのみ対象 |
| `--force` | - | 既存レコードを上書き |
| `--dry-run` | - | DB保存なし・対象ファイルを表示のみ |
| `--no-encrypt` | - | DBを暗号化しない（平文モード） |

### 3a. 会議議事録 → pm.db（pm_meeting_import.py、1ファイル単位）

```sh
python3 scripts/pm_meeting_import.py meetings/GMT20260302-032528_Recording.md \
    --meeting-name "アプリ-ベンチマークリーダー会議" --held-at 2026-03-02 \
    --output meetings/GMT20260302-032528_Recording_parsed.txt
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--meeting-name NAME` | `"不明"` | 会議種別名（「会議の種類と頻度」参照） |
| `--held-at YYYY-MM-DD` | ファイル名から推定 | 開催日 |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--force` | - | 既存レコードを上書き |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 標準出力の内容をファイルにも保存 |

### 4. Slack要約 → pm.db（pm_extractor.py）

```sh
# 通常運用: 未処理スレッドのみ抽出
python3 scripts/pm_extractor.py -c C08SXA4M7JT

# 確認用（DB保存なし）
python3 scripts/pm_extractor.py -c C08SXA4M7JT --dry-run --output result.txt
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `-c CHANNEL_ID` | `C0A9KG036CS` | 対象チャンネルID |
| `--db-slack PATH` | `data/{channel_id}.db` | Slack DBのパス |
| `--db-pm PATH` | `data/pm.db` | pm.db のパス |
| `--since YYYY-MM-DD` | なし（全件） | この日付以降の要約のみ対象 |
| `--force-reextract` | - | 抽出済みスレッドも再処理 |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |
| `--output PATH` | - | 標準出力の内容をファイルにも保存 |

### 5. PMレポート生成・Canvas投稿（pm_report.py）

レポート構成: **サマリー → 直近の決定事項 → 要注意事項 → 未完了アクションアイテム（表形式）**

未完了アクションアイテム表には ID・担当者・内容・期限・ソース・対応状況 の列があり、会議中にCanvas上で対応状況を直接記入できる。

```sh
# 週次進捗レポートを生成してCanvas投稿
python3 scripts/pm_report.py

# 直近1ヶ月のデータのみ対象にしてレポート生成
python3 scripts/pm_report.py --since 2026-02-01

# 確認用（Canvas投稿なし）
python3 scripts/pm_report.py --dry-run --output report.md
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--canvas-id ID` | `F0AAD2494VB` | 投稿先 Canvas ID |
| `--since YYYY-MM-DD` | なし（全期間） | この日付以降のデータのみ対象 |
| `--skip-canvas` | - | Canvas 投稿をスキップ |
| `--dry-run` | - | Canvas 投稿なし・結果を標準出力のみ |
| `--output PATH` | - | 出力をファイルにも保存 |

### 6. Canvas対応状況 → pm.db 同期（pm_sync_canvas.py）

会議中にCanvas上の「対応状況」列に記入された内容をpm.dbに反映する。

**運用フロー**:
1. `pm_report.py` でアクションアイテム表をCanvas投稿（対応状況列は空）
2. 会議中にメンバーがCanvas上の「対応状況」列に記入
3. 会議後に本スクリプトを実行してpm.dbを更新

```sh
# 通常運用
python3 scripts/pm_sync_canvas.py

# 確認用（DB更新なし）
python3 scripts/pm_sync_canvas.py --dry-run
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--canvas-id ID` | `F0AAD2494VB` | 対象 Canvas ID |
| `--db PATH` | `data/pm.db` | pm.db のパス |
| `--dry-run` | - | DB保存なし・結果を標準出力のみ |

**完了判定キーワード**（`status='closed'` に更新）: `完了` `done` `済` `対応済` `解決` `closed` `finish` `finished`

それ以外の記入内容は `note` 列に保存（`status` は `open` のまま）

---

## 環境変数

**トークンは `.bashrc` に直書きしないこと。** 全プロセスに漏洩する危険がある。

### 安全な運用方法（推奨）

```sh
# 1. トークンファイルを作成（初回のみ）
mkdir -p ~/.secrets
chmod 700 ~/.secrets
cat > ~/.secrets/slack_tokens.sh << 'EOF'
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_MCP_XOXB_TOKEN="xoxb-..."
EOF
chmod 600 ~/.secrets/slack_tokens.sh

# 2. 実行前に読み込む（毎回）
source ~/.secrets/slack_tokens.sh
python3 scripts/slack_pipeline.py ...
```

---

## DBの暗号化

`pm.db` は SQLCipher（AES-256）で暗号化している。ファイルが漏洩しても鍵なしでは内容を読めない。

### 暗号化の仕組み

- `scripts/db_utils.py` の `open_db()` が全スクリプトのDB接続を一元管理する
- 鍵の読み込み優先順位: 環境変数 `PM_DB_KEY` > `~/.secrets/pm_db_key.txt`
- `pm.db` および `{channel_id}.db`（Slack DB）の全DBを暗号化対象とする

### 初回セットアップ

```sh
# 1. 鍵を生成（初回のみ）
python3 scripts/db_utils.py --gen-key
# → ~/.secrets/pm_db_key.txt に 64文字のランダム鍵を生成（chmod 600）

# 2. 既存の平文DBを暗号化DBに変換（初回のみ）
python3 scripts/db_migrate.py data/pm.db data/C08SXA4M7JT.db data/C0A9KG036CS.db
# → 各 .bak にバックアップを作成してから変換・検証
```

**鍵ファイルを紛失すると暗号化済みDBは復元不可能。パスワードマネージャー等に必ずバックアップすること。**

### 各スクリプトの暗号化オプション

全スクリプトに `--no-encrypt` オプションがあり、平文モードで動作させることができる（暗号化を使わない場合や移行作業時に使用）。

| オプション | 説明 |
|---|---|
| `--no-encrypt` | 平文の sqlite3 で接続する（暗号化しない） |

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

## DBスキーマ

### {channel_id}.db（Slackデータ）

#### messages（親メッセージ）

| カラム | 型 | 説明 |
|---|---|---|
| `thread_ts` | TEXT | スレッドタイムスタンプ（PK）。スレッドなし投稿は `msg_id` と同値 |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `user_id` | TEXT | 投稿者のユーザーID |
| `user_name` | TEXT | 投稿者の表示名 |
| `text` | TEXT | メッセージ本文 |
| `timestamp` | TEXT | 投稿日時（JST、例: `2026-01-20 19:43:23`） |
| `permalink` | TEXT | Slack上の投稿URL |
| `fetched_at` | TEXT | DBへの保存日時（ISO8601） |

#### replies（返信メッセージ）

| カラム | 型 | 説明 |
|---|---|---|
| `msg_ts` | TEXT | 返信のタイムスタンプ（PK） |
| `thread_ts` | TEXT | 親スレッドの `thread_ts`（messages への FK） |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `user_id` | TEXT | 投稿者のユーザーID |
| `user_name` | TEXT | 投稿者の表示名 |
| `text` | TEXT | メッセージ本文 |
| `timestamp` | TEXT | 投稿日時（JST） |
| `permalink` | TEXT | Slack上の投稿URL |
| `fetched_at` | TEXT | DBへの保存日時（ISO8601） |

#### summaries（スレッド要約）

| カラム | 型 | 説明 |
|---|---|---|
| `thread_ts` | TEXT | スレッドタイムスタンプ（PK） |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `summary` | TEXT | Claude CLIが生成した要約テキスト |
| `summarized_at` | TEXT | 要約生成日時（ISO8601） |
| `last_reply_ts` | TEXT | 要約時点での最新返信の `msg_ts`（返信なしは NULL） |

#### 差分判定ロジック

```
Slack から取得した最新返信 msg_ts  vs  summaries.last_reply_ts
  新規（thread_ts が DB に存在しない） → 取得・要約
  更新（最新 msg_ts > last_reply_ts）  → 返信再取得・再要約
  変化なし                             → スキップ（API・LLM呼び出しなし）
```

### pm.db（PM統合データ、未実装）

#### meetings

| カラム | 型 | 説明 |
|---|---|---|
| `meeting_id` | TEXT | ファイル名ベースのID（PK） |
| `held_at` | TEXT | 開催日 |
| `kind` | TEXT | 会議種別（全体会議/技術WG等） |
| `file_path` | TEXT | 議事録ファイルパス |
| `summary` | TEXT | LLMによる要約 |
| `parsed_at` | TEXT | 解析日時 |

#### action_items

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `content` | TEXT | アクションアイテムの内容 |
| `assignee` | TEXT | 担当者 |
| `due_date` | TEXT | 期限（なければNULL） |
| `status` | TEXT | `open` / `closed` |
| `source` | TEXT | `meeting` または `slack` |
| `source_ref` | TEXT | 背景への参照（議事録パス or Slackパーマリンク） |
| `extracted_at` | TEXT | 抽出日時 |

#### decisions

| カラム | 型 | 説明 |
|---|---|---|
| `id` | INTEGER | PK |
| `content` | TEXT | 決定事項の内容 |
| `decided_at` | TEXT | 決定日 |
| `source` | TEXT | `meeting` または `slack` |
| `source_ref` | TEXT | 背景への参照（議事録パス or Slackパーマリンク） |
| `extracted_at` | TEXT | 抽出日時 |

#### slack_extractions（抽出済みスレッド管理）

| カラム | 型 | 説明 |
|---|---|---|
| `thread_ts` | TEXT | スレッドタイムスタンプ（PK） |
| `channel_id` | TEXT | SlackチャンネルID（PK） |
| `extracted_at` | TEXT | 抽出日時（ISO8601） |

差分判定: `slack_extractions` に存在するスレッドは `--force-reextract` なしでスキップ

---

## ロードマップ

### フェーズ1: Slack DB化と差分処理（実装済み）

- SlackメッセージをチャンネルごとのSQLiteに永続化
- 新規・更新スレッドのみ取得・要約（変化なしはAPI/LLM呼び出しゼロ）
- 全スレッド要約からCanvasに全体要約を投稿

### フェーズ2: 会議議事録との統合（実装済み）

- `meetings/*.md` をLLMで解析し `pm.db` に構造化保存（`pm_meeting_import.py`）
- Slack要約から決定事項・アクションアイテムを抽出し `pm.db` に保存（`pm_extractor.py`）
- `source_ref` により背景（会議議事録 or Slackスレッド）に常に遡れる設計
- 差分処理: `slack_extractions` テーブルで抽出済みスレッドを管理、変化なしはLLM呼び出しゼロ

### フェーズ3: PMレポートと次回会議アジェンダ自動生成（実装済み）

- 未完了アクションアイテム一覧（担当者・期限付き）の自動生成
- 次回会議アジェンダ草案（未解決課題 + Slackで浮上した検討事項）
- 週次/月次進捗レポート
- リスク検知（「問題」「障害」「遅延」等を含むアイテムへの自動フラグ）

### フェーズ4: インポート済み議事録の記録（TODO）

- `pm_meeting_import.py` / `pm_meeting_bulk_import.py` 実行後に、インポート済みファイルの一覧を残す仕組みを作る
  - 候補1: `data/imported_meetings.log`（1行1ファイルのテキストログ）
  - 候補2: `pm.db` の `meetings` テーブルに既に `file_path` / `parsed_at` が記録されているため、それをクエリして一覧表示するサブコマンドを追加する
  - 再インポート・抜け漏れ確認・監査証跡として活用できること

### フェーズ5: 資料登録とマイルストーン管理（TODO）

- 議事録・Slack以外の資料（計画書・スライド・報告書等）を `pm.db` に登録できるインポートスクリプト（`pm_document_import.py`）を作成する
  - 対象フォーマット: PDF / PowerPoint / Markdown / テキスト等
  - ファイル名・登録日・種別・本文テキストを `documents` テーブルに保存
- LLMで資料からマイルストーン（目標・期限・達成条件）を抽出し `milestones` テーブルに構造化保存する
  - 抽出項目: マイルストーン名・期限・担当エリア・達成条件・ソース参照
- `pm_report.py` のレポートにマイルストーン進捗セクションを追加する
  - 現在日付とマイルストーン期限を照合し、達成済み・進行中・未着手・遅延を自動判定
  - 「プロジェクトの現在地」として直近・今後のマイルストーン一覧を表示
