# Argus AI — 使い方ガイド

プロジェクトマネージャーが Slack から呼び出せる 5 つのコマンドの実践的な使い方まとめ。

| コマンド | いつ使うか | 応答形式 | 典型的な所要時間 |
|---|---|---|---|
| `/argus-brief`      | 今週〜直近の状況を俯瞰して優先順位をつけたい | ephemeral（自分のみ） | 30〜60秒 |
| `/argus-today`      | 本日の動きと自分宛メンションを見落としなく確認したい | ephemeral | 20〜40秒 |
| `/argus-risk`       | 顕在化しているリスク・予兆を洗い出したい | ephemeral | 30〜60秒 |
| `/argus-investigate`| 「なぜ」「どこで決めた」の深掘り調査がしたい | ephemeral | 30〜180秒 |
| `/argus-transcribe` | 会議の録音・録画から議事録を生成したい | スレッドに投稿 | 10〜20分 |

すべてのコマンドは本人だけに見える **ephemeral 返信**。ただし `/argus-transcribe` の進捗通知はスレッド全員に可視（完了通知は ephemeral）。

---

## 1. `/argus-brief` — 優先順位づけブリーフィング

今日やるべきことを優先度順に最大5件提示する。pm.db 統計・Slack 生メッセージ・議事録を総合分析。

### 使い方

```
/argus-brief                     # 直近30日を全体俯瞰
/argus-brief 60                  # 期間を60日に拡張
/argus-brief @西澤               # 西澤さん担当事項にフォーカス
/argus-brief Benchpark           # Benchpark 話題にフォーカス
/argus-brief 60 @西澤 GPU性能    # 組み合わせも可
```

### 引数ルール

| トークン | 解釈 | 例 |
|---|---|---|
| 数字のみ   | 直近日数           | `60` → 過去60日分 |
| `@` 始まり | 担当者フォーカス   | `@西澤` |
| その他文字 | 話題フォーカス     | `Benchpark` |

### 想定シーン

- 毎朝の 5 分レビュー
- 週初めに「自分のチームが今抱えている山」を把握
- 特定マイルストーンが遅延気味で、関連AI・決定を一望したい

### 自動実行

平日朝 8:57 に cron で過去30日分のブリーフィングを Canvas（`F0AT4N36TFF`）へ自動投稿。朝礼前に全員が確認できる。

---

## 2. `/argus-today` — 今日の活動サマリー（個人向け）

本日の Slack・議事録を 4 観点（議論・決定・AI・進捗）でサマライズし、さらに **実行者宛のメンション** を別セクションで生データ表示。

### 使い方

```
/argus-today
```

引数なし。本日分のデータのみが対象。

### 想定シーン

- 朝イチで「昨日から今日にかけて何があったか」を 1 分で把握
- 自分宛の依頼（メンション）を見落とさない
- 17:00 の cron が Canvas（`F0ATCN7E2D9`）へ自動投稿する日次サマリーのオンデマンド版

### `/argus-brief` との違い

| 観点 | `/argus-brief` | `/argus-today` |
|---|---|---|
| 対象期間   | 直近30日（`--days` で変更可） | 本日のみ |
| 出力形式   | 優先アクション5件 | 4観点の活動サマリー |
| メンション | なし | あなた宛メンション別セクション |

---

## 3. `/argus-risk` — リスク分析

顕在化しているリスクと放置すると問題になる予兆を優先度付きで列挙。`/argus-brief` と同じ引数ルール。

### 使い方

```
/argus-risk                      # 全プロジェクトのリスク俯瞰
/argus-risk 60                   # 期間拡大
/argus-risk @小林                # 特定担当者のリスク
/argus-risk Benchpark            # 特定話題のリスク
```

### 想定シーン

- 週次の役員会前に「報告すべき火種」を洗い出す
- 新しい責任範囲を引き継いだ直後に全体のリスク状況を理解
- `/argus-brief` で気になった項目をリスク観点で掘り下げ

### Patrol Agent との棲み分け

| 機構 | いつ動く | 何を見る |
|---|---|---|
| Patrol Agent（自動） | 平日 30 分ごと | 決定論的ルール（期限超過・停滞・健全性・トレンド悪化）→ 担当者/リーダーへ自動通知 |
| `/argus-risk`（手動） | ユーザー実行時 | LLM による文脈解釈込みのリスク俯瞰 |

Patrol が「拾い漏れないための常時監視」、`/argus-risk` が「今からここを語れるように整理する」補完関係。

---

## 4. `/argus-investigate` — マルチステップ調査（Agent）

LLM が自律的にツール（DB検索・FTS全文検索・Slackメッセージ取得）を選択しながら最大 5 ステップで調査。旧 `/argus-ask` の単発 QA 機能もこれに統合済み。

### 使い方

```
/argus-investigate M3マイルストーンの遅延原因を調査して
/argus-investigate 先週の決定事項が実行されているか確認
/argus-investigate @西澤 の負荷が高い原因を分析して
/argus-investigate 設計方針に関する最近の議論は？
/argus-investigate GPU Outsourcing Agreement の最新版はどこか
```

### 得意なこと

| 質問タイプ | 例 |
|---|---|
| 因果分析            | 「遅延の原因」「負荷集中の理由」 |
| クロスソース相関    | pm.db + 議事録 + Slack を跨いだ整合確認 |
| 過去決定の検索      | 「前に何を決めたか」「どの会議の決定か」 |
| ドキュメント探索    | BOX資料・外部Web記事のタイトル・共有者・URL |
| 構造化QA            | 「〇〇さんの担当」「M2のAI件数」など SQL 相当 |

### 内部で使われるツール

| ツール | 情報源 |
|---|---|
| `get_milestone_progress`      | pm.db |
| `get_overdue_items`           | pm.db |
| `get_assignee_workload`       | pm.db |
| `get_weekly_trends`           | pm.db |
| `get_unacknowledged_decisions`| pm.db |
| `search_action_items`         | pm.db（条件検索） |
| `search_decisions`            | pm.db（キーワード） |
| `search_text`                 | FTS5（議事録/Slack/BOXドキュメント/Web記事） |
| `get_slack_messages`          | {channel_id}.db |

### 出力中のID参照

回答中に `a:670` / `d:42` のようなID参照が現れた場合、自動的に対象アイテムの冒頭60文字を併記するので、参照先をいちいち開かなくても意味が分かる。

### CLI モード

```bash
python3 scripts/argus/pm_argus_agent.py --investigate "M3の遅延原因を調査" --dry-run
python3 scripts/argus/pm_argus_agent.py --investigate "先週の決定事項の実行状況" --max-steps 5
```

---

## 5. `/argus-transcribe` — 会議録音の文字起こし・議事録生成

Slack チャンネルにアップロードされた音声・動画ファイルをダウンロードし、Whisper → LLM 議事録生成までを自動実行してスレッドに投稿する。

### 使い方

```
/argus-transcribe GMT20260302-032528_Recording.mp4
/argus-transcribe 2026-04-20_Leader_Meeting.m4a
```

ファイル名は太字 (`*foo.mp4*`) やコード記法 (`` `foo.mp4` ``) で囲まれていても自動で剥がす。

### 処理フロー

1. Slack からファイル検索・ダウンロード（同名 VTT も自動検出）
2. 動画（mp4 等）の場合は **スライドOCR** 実行（ffmpeg scene detect + マルチモーダルLLM）
3. Whisper large-v3 で文字起こし（スライドから抽出した固有名詞を initial_prompt に注入）
4. ローカルLLM（gemma4）で議事録生成（マルチステージ: 抽出 → 統合 → 決定事項・AI 抽出）
5. スレッドに議事録ファイル（Markdown）をアップロード

### 品質向上の3系統

| 系統 | 効果 | 自動判定 |
|---|---|---|
| **VTT話者情報** | Zoom自動文字起こしの話者名を統合 → 担当者推定精度向上 | 同名 `.vtt` / `.transcript.vtt` を自動検出 |
| **スライドOCR** | 固有名詞・技術用語・数値の誤変換を抑制 | mp4 で `OPENAI_API_BASE` 設定時に自動実行（`--no-slide-ocr` で無効化） |
| **Whisper + LLM** | 高品質日本語文字起こし + 構造化抽出 | 常時有効 |

3 系統は独立して ON/OFF でき、共存する。

### 進捗通知

- 処理開始・DL完了・ASR完了・Stage 1/2/3 進捗 → **スレッドに投稿**（全員に可視）
- 最終完了・エラー → **ephemeral**（実行者のみ）

### 排他制御

同時実行は 1 ジョブのみ。処理中の再実行は現在のジョブ情報を表示してエラーを返す。

### 想定シーン

- Zoom 会議終了直後、録画ファイルをチャンネルに投げて即議事録化
- 「口頭で決まったこと」を pm.db に載せるため、聞き直し不要で AI・決定が抽出される

---

## セットアップ（最小要件）

1. デーモン起動（これだけで全コマンドが有効化）
   ```bash
   bash scripts/pm_daemon.sh start qa
   ```

2. FTS5 インデックス構築（`/argus-investigate` の `search_text` 用）
   ```bash
   python3 scripts/pm_embed.py --full-rebuild
   ```

3. cron 設定
   ```
   57 8   * * 1-5  python3 scripts/argus/pm_argus.py --brief-to-canvas             # 朝ブリーフィング
   0  17  * * 1-5  bash   scripts/pm_argus_daily_summary.sh                         # 17時日次サマリー
   */30 * * * 1-5  python3 scripts/argus/pm_argus_patrol.py                         # Patrol（リスク自動検知）
   ```

---

## よくある質問

**Q: `/argus-brief` と `/argus-today` の使い分けは？**
A: 広く浅く（30日）が brief、今日だけを深く + 自分宛メンション強調が today。

**Q: `/argus-risk` と Patrol はどう違う？**
A: Patrol が決定論的ルールで自動通知、`/argus-risk` は LLM による文脈解釈付きの手動呼び出し。

**Q: `/argus-investigate` の回答に「a:670」と出るが参照先が分からない**
A: 現在は冒頭60文字が自動併記される。さらに詳細を見たい場合は Web UI (`pm_api.py`) か `python3 scripts/pm_relink.py --list` で確認。

**Q: `/argus-transcribe` が「現在処理中のジョブがあります」と返る**
A: 前のジョブ完了まで待機。進捗は `tail -f logs/pm_qa_server.log` または対象スレッドで確認可能。

**Q: LLMの品質が悪い／タイムアウトする**
A: `curl http://localhost:8000/v1/models` で gemma4 の起動確認。落ちていれば RiVault へ自動フォールバックするので `RIVAULT_URL` 設定を確認。

**Q: 検索結果が的外れ（`/argus-investigate` の search_text）**
A: `pm_embed.py --full-rebuild --index-name <name>` で FTS5 を再構築。対象チャンネル→インデックス対応は `data/argus_config.yaml` を参照。
