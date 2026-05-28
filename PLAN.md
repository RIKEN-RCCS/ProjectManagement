# 進行中の実装計画 (PLAN.md)

In-flight な実装計画と保留中の構想だけを置く。運用ルールは `CLAUDE.md` を参照。

---

## 現在進行中の計画

なし（直近の主要実装はすべて完了。詳細は `LOG.md` 参照）。

---

## 保留中の構想

着手判断待ちの計画。動かすときは「現在進行中の計画」セクションに移動して詳細化する。

### 1. 日程調整 Agent (`/argus-schedule`)

**ステータス**: 保留中（2026-05-26〜）。Modal (views.open) 案が最有力。

**保留理由**: Slack 単独 UI では TONTON 並みのグリッド体験が得られない。
DM + checkboxes は要素上限制約、Box xlsx 共同編集は排他制御リスクで NG（ユーザー判断）。

**再開時の出発点**:
- UI は Modal (views.open) を第一候補に詳細化
- `pm_qa_server.py` に Socket Mode で trigger を受信するハンドラを追加
- 候補日生成は Argus が直近会話から推定 + 引数明示の両対応
- 確定後は `.ics` 添付で OAuth 不要のカレンダー連携
- DB は `data/schedule.db` を新設、確定したものだけ `pm.db.meetings` に転記
- 締切処理は `pm_argus_patrol.py` の cron サイクルに乗せる

UI モックの履歴は専用 Canvas 末尾と検討用 DM に蓄積（具体的な ID は memory `project_schedule_agent` を参照）。

### 2. Argus Phase 2: `/argus-do` 自動実行

**ステータス**: 保留中。LLM の JSON 構造化出力品質が安定したら着手。

**設計方針**:
- `/argus-brief` のアクション提案に `action_id` を付与
- 提案内容を `secretary_proposals` テーブルに保存
- `/argus-do a1` で対応する提案を pm.db に反映（assign_item, close_item 等）
- 実行前に対象アイテム ID をユーザーに確認表示する安全策を必須化

**未着手の理由**: 自動実行は誤りの影響が大きいため、まず Phase 1（提案・草案）の品質と
ユーザー受容を確認してから着手。

### 3. Web UI 認証追加・ログインノード移設

**ステータス**: 情報セキュリティ部門の確認待ち（実装保留中）。
詳細はメモリ `project_web_auth_todo` 参照。

### 4. ナレッジ蒸留 (knowledge.db) の品質改善

**ステータス**: Stage 2 二段ゲート導入後の運用観察中。重複・ノイズの実測値次第で次の改善を検討。

**観察ポイント**:
- `pm_knowledge_inspect.py` で重複・多重抽出の発生率
- `/argus-investigate` でナレッジ引用の有用性
- `pm_knowledge_edit.py --invalidate` の発動頻度（誤抽出の指標）
