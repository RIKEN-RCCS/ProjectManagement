# ナレッジ蒸留ポリシー

`data/knowledge.db` に格納する **蒸留ナレッジ** の採否基準・運用ルール・人手介入の手順を定める。
ナレッジ層全体の設計は `docs/architecture.md`、テーブル定義は `docs/schema.md` 参照。

## 基本思想

- **日々ナレッジを確認する運用は前提にしない**。人手はトラブル対応のときだけ降りてくる
- 「正しさ」より「**上書きしやすさ**」を優先する。間違ったレコードがあっても investigate で気付いた瞬間に 1 コマンドで無効化できれば実害は小さい
- 件数を増やしすぎない。**500 件を超えたら抽出粒度を見直す**

---

## 採否基準（蒸留時の足切り）

LLM 蒸留プロンプトに以下を明記して、些末な決定を最初から取り込まない。

### 採用すべき（knowledge.db に書き込む）

- アーキテクチャ選択（例: Scale-up ドメインサイズの確定）
- 外部関係者との合意事項（理研 / 富士通 / NVIDIA 三者の決定）
- 長期にわたって参照される制約・前提条件（例: FP8 ゼタスケール目標、温水冷却前提、機密区分）
- 撤回・上書きが起きにくい用語定義（例: 「ブロック2」「SubWG3」「Made with Japan」）
- 立場の表明（重要ステークホルダーの方針声明）

### 採用すべきでない（書き込まない / `distill_state.status='skipped'`）

- 1 回限りの会議運営事項（時刻変更、開催場所、Zoom URL）
- 当日中に消費されるアクションアイテム（pm.db の action_items の領域）
- 個人の暫定見解（チーム合意に至っていない発言）
- 既に上書きされた情報を再度抽出してしまったケース
- 形式的な承認（議事録レビューの承認、議事録掲載の許可）

### confidence ルール

- `confidence='high'` — 議事録に明示された決定事項、外部関係者との合意、複数ソースで一致
- `confidence='medium'` — 1 ソースのみだが内容が明確
- `confidence='low'` — **knowledge.db には書き込まない**。LLM が「自信がない＝些末かも」と判断したものは自動で落とす

これによりナレッジは常に「自信を持って参照できる」レコードだけで構成される。

---

## 自動運用フロー

### 蒸留トリガ

| 入力ソース | トリガ | 処理 |
|---|---|---|
| `box_docs.db` | `doc_content.content_hash` の変化 | `pm_box_distill.py --source box` で再蒸留 |
| `data/minutes/{kind}.db` | 新規 `meeting_id` の追加 | `pm_box_distill.py --source minutes` |
| `pm.db.decisions` | 新規 `id` の追加 | `pm_box_distill.py --source decisions` |

`distill_state(source_type, source_ref)` の `last_input_hash` と現在値が同じならスキップ。

### 矛盾検知

蒸留時、新規レコードの `topic` と類似する既存レコードを検索し、`current_state` が矛盾する場合:

1. `knowledge_relations` に `conflicts_with` の行を作る
2. Patrol Agent がリーダー会議チャンネルに **1 件だけ** 通知（cooldown_days=14）
3. 通知メッセージには「`/argus-knowledge KN-XXXX` で詳細確認 → `/argus-knowledge supersede` で上書き、または `invalidate` で無効化」を含める

### 鮮度チェック（cron 週1回）

`last_validated_at` が 180 日以上前のレコードを `/argus-risk` の出力末尾に「要再確認」セクションとして列挙する。能動的に通知はしない（`/argus-risk` を実行したときだけ目に入る）。

---

## 人手介入の手段

普段使わない前提で、以下 3 経路を用意する。

### (A) CSV 一括編集（`pm_knowledge_edit.py`）

`pm_relink.py` と同じ思想。LLM を使わず CSV で編集する。

```sh
# 全レコードをCSVにエクスポート
python3 scripts/pm_knowledge_edit.py --export

# 編集後にインポート
python3 scripts/pm_knowledge_edit.py --import knowledge_edit.csv

# 確認のみ
python3 scripts/pm_knowledge_edit.py --import knowledge_edit.csv --dry-run
```

編集可能なフィールド:

| フィールド | 空欄の扱い |
|---|---|
| `topic` / `current_state` / `rationale` / `alternatives_rejected` / `constraints_invariants` | スキップ（変更なし） |
| `tags` / `owners` | JSON 配列文字列で更新 |
| `confidence` | `high` / `medium` / `low` |
| `superseded_by` | 上書き連鎖を作る（`KN-XXXX` を指定） |
| `deleted` | `1` で論理削除、`0` で復活 |

全変更は `knowledge_audit` に `source='human_edit'` で記録される。物理削除はしない（`deleted=1` のみ）。

### (B) Slack コマンド `/argus-knowledge`

会議中に「これは古い」と気付いたとき即座に直せる。

```
/argus-knowledge KN-0042                  → 1件詳細表示（topic / current_state / rationale / sources）
/argus-knowledge supersede KN-0042 KN-0099 → KN-0042.superseded_by = KN-0099 を立てる
/argus-knowledge invalidate KN-0042       → KN-0042.deleted = 1
/argus-knowledge confidence KN-0042 low   → 確度を下げる
```

実装は `pm_qa_server.py` のスラッシュコマンドハンドラに追加する。応答は ephemeral。

### (C) `/argus-investigate` の出力に修正導線を埋め込む

回答が `knowledge` レコードを引用した場合、回答末尾に以下を自動付与する:

> **引用したナレッジ**: KN-0042 (Scale-up は NVL4 で合意, 2026-03-10)
> 修正が必要なら `/argus-knowledge invalidate KN-0042` または `/argus-knowledge supersede KN-0042 <新ID>`

普段ナレッジ管理画面を開かなくても、investigate を使ったついでに気付いたものを直せる導線。

---

## 運用上の不変条件

| 規則 | 理由 |
|---|---|
| `confidence='low'` を書き込まない | 雑音を最初から排除 |
| 物理削除しない（`deleted=1` のみ） | `knowledge_audit` が経緯を保ち続けられる |
| `superseded_by` が立っているレコードは brief / risk のプロンプトに入れない | 古い情報を出さない |
| LLM 自動更新は人手承認なしで反映 | 日々のレビュー工数をゼロにする |
| 件数 500 を超えたら抽出粒度を見直す | 細かすぎる抽出のサイン |
| 物理削除・スキーマ変更は `knowledge_audit` を別系統で保全してから行う | 監査証跡を維持 |

---

## メトリクス（運用判断のための観測項目）

| メトリクス | 取得方法 | 解釈 |
|---|---|---|
| 総レコード数 | `SELECT COUNT(*) FROM knowledge WHERE deleted=0` | 500 超で抽出粒度を再考 |
| `confidence` 分布 | `GROUP BY confidence` | `low` が混入していたら蒸留プロンプトを見直す（書き込まれているはずがない） |
| `superseded_by` 立ちの割合 | `WHERE superseded_by IS NOT NULL / 全件` | 高すぎる場合は意思決定の流動性が高い領域なので粒度を粗くする |
| 鮮度（`last_validated_at` 180日超）の件数 | 鮮度チェックの集計 | この値が増えるなら蒸留が止まっているか、対象 BOX フォルダが更新されていない |
| 矛盾通知の頻度 | `notifications.event_type='knowledge_conflict'` | 高頻度なら蒸留プロンプトに揺れがあるサイン |

---

## 関連ドキュメント

- `docs/architecture.md` 「Pass 3: ナレッジ蒸留」
- `docs/schema.md` 「data/knowledge.db」
- `docs/argus_system.md` `/argus-knowledge` コマンド（実装後に追記）
