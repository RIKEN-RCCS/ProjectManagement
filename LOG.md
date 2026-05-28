# 変更ログ (LOG.md)

判断の経緯・破棄された案・方針転換を残す journal。運用ルールは `CLAUDE.md` を参照。

**フォーマット**: `## YYYY-MM-DD <一行サマリ>` → 本文に **背景 / 決定 / 影響** を 1-2 行ずつ。
新しいエントリを上に追加する。1 エントリ 3-5 行を目安に、長文は `docs/` 側に逃がす。

---

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
