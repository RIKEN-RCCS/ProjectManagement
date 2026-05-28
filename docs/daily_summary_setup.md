# 日次サマリー機能のセットアップガイド

## 概要

`--today-only` フラグを使用して、毎日17時（JST）に本日のSlackメッセージと議事録をまとめるブリーフィングをCanvas に投稿します。

## 実装内容

### 1. `pm_argus.py` の修正

- **`--today-only` フラグ追加**: 今日のデータのみ収集する新しいオプション
- **期間表示ヘルパー関数**: `_format_period_description(days)` を追加
  - `days == 0` → "本日のデータ"
  - `days > 0` → "過去X日間のデータ"
- **プロンプト修正**: Brief/Risk両方のプロンプトで `{days}` → `{period_desc}` に変更（6箇所）
- **`build_brief_prompt()` / `build_risk_prompt()` 関数修正**: 期間表示ヘルパー関数を呼び出し

### 2. 新規スクリプト

`scripts/pm_argus_daily_summary.sh` - 日次サマリー用cronスクリプト

### 3. 日付計算ロジック

`--today-only` 指定時の優先順位：
```
--today-only > --since > --days
```

## 使用方法

### CLIモード（手動実行）

```bash
# ドライラン（Canvas投稿なし、出力を確認）
source ~/.secrets/slack_tokens.sh
source ~/.secrets/rivault_tokens.sh
~/.venv_aarch64/bin/python3 scripts/argus/pm_argus.py \
    --brief-to-canvas --today-only --dry-run

# 実際にCanvas投稿
~/.venv_aarch64/bin/python3 scripts/argus/pm_argus.py \
    --brief-to-canvas --today-only --canvas-id <CANVAS_ID>

# リスク分析も併せて実行
~/.venv_aarch64/bin/python3 scripts/argus/pm_argus.py \
    --risk --today-only --canvas-id <CANVAS_ID>
```

### Cronでの自動実行

`crontab -e` で以下を追加：

```cron
# 平日17時（UTC 8:00）に日次サマリーを生成・投稿
0 8 * * 1-5 cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement && bash scripts/pm_argus_daily_summary.sh >> logs/pm_argus_daily_summary.log 2>&1
```

**注**: UTC 8:00 = JST 17:00

### 既存の朝のブリーフィング（変更なし）

```cron
# 平日朝（UTC 7:47 = JST 16:47）に30日分のブリーフィングを生成
47 7 * * 1-5 /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/ProjectManagement/scripts/pm_argus_daily.sh
```

## テスト結果

### `--today-only` フラグの動作確認

```
✓ since: 2026-05-11 / today: 2026-05-11
✓ プロンプトに「本日のデータ」と表示
✓ 本日のSlackメッセージと議事録のみが含まれる
```

### 既存の `--days` オプションとの互換性

```
✓ --days 7: since: 2026-05-04 / today: 2026-05-11
✓ プロンプトに「過去7日間のデータ」と表示
✓ 7日分のデータが正常に収集される
```

### 既存機能への影響

```
✓ --days 未指定: since: 2026-04-11 / today: 2026-05-11（デフォルト30日）
✓ --since 2026-04-20: since: 2026-04-20 / today: 2026-05-11
✓ 既存のリスク分析機能も動作確認済み
```

## 設定ファイル

- **Canvas ID**: `<CANVAS_ID>`（リーダー会議Canvas、朝と夕方で同じCanvasに追記）
- **ログファイル**: `logs/pm_argus_daily_summary.log`（朝のブリーフィングとは別）

## 動作仕様

### データ収集範囲

- **Slack生メッセージ**: `WHERE date(timestamp) >= '2026-05-11'`
  - 本日00:00:00以降のメッセージをすべて収集
  
- **議事録**: `WHERE held_at >= '2026-05-11'`
  - 本日に開催された会議の議事録を収集
  
- **pm.db統計**: 
  - マイルストーン進捗（全期間）
  - 期限超過アイテム（全期間のopen）
  - 担当者別負荷（全期間のopen）
  - 未確認決定事項（全期間）
  - 週次トレンド（直近4週）

### タイムゾーン

- サーバーのローカル時刻（JST）で実行
- SlackメッセージはUTC→JST変換済みでDBに格納
- 議事録の `held_at` はYYYY-MM-DD形式（タイムゾーンなし）
- 17時（JST）に実行した場合、同日の00:00以降のデータが対象

## トラブルシューティング

### データが0件の場合

Slackメッセージも議事録も今日は何もない状況でも、LLMが「本日のデータはありませんでしたが...」と適切に対応します。

### キャンバスに投稿されない

1. `~/.secrets/slack_tokens.sh` に `SLACK_USER_TOKEN` が設定されているか確認
2. Canvas ID が正しいか確認（`<CANVAS_ID>`）
3. ログを確認: `tail -f logs/pm_argus_daily_summary.log`

### Cronが実行されない

1. `crontab -l` で設定が保存されているか確認
2. `crontab -e` で記述を再確認（相対パスではなく絶対パスを使用）
3. `~/.venv_aarch64/bin/python3` が実行可能か確認

## 将来の拡張案

1. **インデックスごとの個別サマリー**
   - `--index-name pm-hpc` などで特定インデックスのみ対象にする
   - 各インデックス専用のCanvasに投稿

2. **メール通知**
   - Canvas投稿に加えてメール通知も追加

3. **定時レポート**
   - 朝のブリーフィング（30日分、状況把握）
   - 夕方のサマリー（本日分、本日の行動確認）
   - 週末のレビュー（1週間分）

## チェックリスト

- [x] `--today-only` フラグを `pm_argus.py` に追加
- [x] 期間表示ヘルパー関数を実装
- [x] Brief/Risk プロンプトを修正
- [x] `build_brief_prompt()` / `build_risk_prompt()` を修正
- [x] `pm_argus_daily_summary.sh` を作成
- [x] 動作テスト（ドライラン、実投稿）
- [x] 既存機能への影響を確認
- [ ] Cronに登録（ユーザーが手動実行）
- [ ] 運用開始
