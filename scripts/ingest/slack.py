#!/usr/bin/env python3
"""
ingest_slack.py

Slack {channel_id}.db → pm.db へ決定事項・アクションアイテムを抽出するプラグイン。
pm_ingest.py slack 経由で呼び出される。
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli_utils import (
    call_argus_llm,
    load_claude_md,
    retrieve_knowledge_for_extraction,
)
from db_utils import normalize_assignee, open_db

from ingest.ingest_plugin import IngestContext

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
# 既定チャンネルは環境変数 PM_DEFAULT_SLACK_CHANNEL から取得する。
# （実値はチャンネル機密のためソース内に持たない）。
DEFAULT_CHANNEL = os.environ.get("PM_DEFAULT_SLACK_CHANNEL", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_extractions (
    thread_ts    TEXT,
    channel_id   TEXT,
    extracted_at TEXT,
    PRIMARY KEY (thread_ts, channel_id)
);
"""


# --------------------------------------------------------------------------- #
# Slack DB 接続
# --------------------------------------------------------------------------- #
def open_slack_db(db_path: Path, no_encrypt: bool = False):
    if not db_path.exists():
        print(f"ERROR: Slack DBが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    return open_db(db_path, encrypt=not no_encrypt)


# --------------------------------------------------------------------------- #
# pm.db 初期化（slack_extractions テーブル追加）
# --------------------------------------------------------------------------- #
def ensure_slack_extractions(pm_conn) -> None:
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                pm_conn.execute(stmt)
            except Exception:
                pass
    pm_conn.commit()


# --------------------------------------------------------------------------- #
# コンテキスト読み込み
# --------------------------------------------------------------------------- #
def load_context_from_claude_md() -> str:
    text = load_claude_md(CLAUDE_MD)
    sections = []
    capture = False
    for line in text.splitlines():
        if re.match(r"^###\s+(ステークホルダー|主なプロジェクト参加者|プロジェクト固有の用語|会議の種類)", line):
            capture = True
        elif re.match(r"^---", line) and capture:
            capture = False
        if capture:
            sections.append(line)
    return "\n".join(sections) if sections else text[:3000]


# --------------------------------------------------------------------------- #
# マイルストーン取得
# --------------------------------------------------------------------------- #
def fetch_milestones(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT milestone_id, name, due_date, area FROM milestones WHERE status='active' ORDER BY due_date"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def format_milestones_for_prompt(milestones: list[dict]) -> str:
    if not milestones:
        return "（マイルストーン未登録）"
    lines = ["| ID | マイルストーン名 | 期限 | エリア |",
             "|----|----------------|------|--------|"]
    for m in milestones:
        lines.append(f"| {m['milestone_id']} | {m['name']} | {m.get('due_date') or '未定'} | {m.get('area') or ''} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# スレッド取得
# --------------------------------------------------------------------------- #
def fetch_threads(slack_conn, channel_id: str, since: str | None) -> list[dict]:
    query = """
        SELECT m.thread_ts, m.timestamp, m.permalink, m.user_name, m.text
        FROM messages m
        WHERE m.channel_id = ?
    """
    params: list = [channel_id]
    if since:
        query += " AND m.timestamp >= ?"
        params.append(since)
    query += " ORDER BY m.timestamp ASC"
    parents = slack_conn.execute(query, params).fetchall()

    results = []
    for p in parents:
        thread_ts = p["thread_ts"]
        lines = [f"[{(p['timestamp'] or '')[:16]}] {p['user_name'] or '不明'}: {p['text'] or ''}"]
        replies = slack_conn.execute(
            "SELECT timestamp, user_name, text FROM replies"
            " WHERE thread_ts=? AND channel_id=? ORDER BY msg_ts ASC",
            (thread_ts, channel_id),
        ).fetchall()
        for r in replies:
            lines.append(f"  [{(r['timestamp'] or '')[:16]}] {r['user_name'] or '不明'}: {r['text'] or ''}")
        results.append({
            "thread_ts": thread_ts,
            "thread_text": "\n".join(lines),
            "timestamp": p["timestamp"],
            "permalink": p["permalink"],
            "user_name": p["user_name"],
        })
    return results


# --------------------------------------------------------------------------- #
# 重複管理
# --------------------------------------------------------------------------- #
def is_already_extracted(pm_conn, thread_ts: str, channel_id: str) -> bool:
    row = pm_conn.execute(
        "SELECT 1 FROM slack_extractions WHERE thread_ts=? AND channel_id=?",
        (thread_ts, channel_id),
    ).fetchone()
    return row is not None


def mark_extracted(pm_conn, thread_ts: str, channel_id: str) -> None:
    pm_conn.execute(
        "INSERT OR REPLACE INTO slack_extractions (thread_ts, channel_id, extracted_at) VALUES (?,?,?)",
        (thread_ts, channel_id, datetime.now().isoformat()),
    )


# --------------------------------------------------------------------------- #
# LLM 抽出
# --------------------------------------------------------------------------- #
# トリアージ方式: integrated=抽出プロンプトに3ゲートを統合し1回のLLM呼び出しで完結（既定）、
# two_stage=Extractor→Triage の2段LLM呼び出し（旧方式。2次審査のレスポンス欠落で
# 実在アイテムを取りこぼすパスがあり、A/B で integrated が全勝+引き分けだったため退役）。
DEFAULT_TRIAGE_MODE = "integrated"

EXTRACT_PROMPT = """
あなたは富岳NEXTプロジェクトのプロジェクトマネージャーです。
以下のSlackスレッドのメッセージを読み、決定事項とアクションアイテムを抽出してください。

## アクションアイテムの定義（厳守）

アクションアイテムとは **プロジェクトを推進するうえで欠かせない作業で、明確なアウトプットがあるもの** に限る。
以下の基準を **すべて** 満たすものだけを抽出すること:

1. **第三者からの依頼・合意された作業である**: 投稿者が他者に依頼している、または会議・スレッドで担当を決めて合意したもの。
   投稿者自身による自発的な意志表明（「〜します」「〜する予定」「〜を目指す」「〜したい」）や、
   既に完了した作業の事後報告（「〜しました」「〜をリリースしました」「〜を実施した」）は **抽出しない**。
2. **未来に向けた未完了の作業である**: 過去形・完了形（「〜した」「〜済」「〜完了」「〜しました」）で書かれた進捗報告は抽出しない。
3. **具体的な成果物・アウトプットがある**: 報告書、資料、設計書、コード、見積もり、提案書など、形のある成果物が生まれる作業であること。
4. **プロジェクト推進に不可欠**: その作業が完了しないと後続の意思決定やマイルストーン達成に支障が出ること。
5. **担当者が特定可能**: 誰がやるかがスレッド中に明示されていること。担当が「?」「未定」「正メンバー」のような不明確な記載しか得られない場合は抽出しない。

以下は **抽出しない**（誤抽出が頻発するため特に注意）:
- **進捗報告・宣言**: 「Gromacsベータ版をリリースしました」「ベンチマークの完成を目指します」「対応中です」など
- **会議運営事項**: アジェンダ作成・Zoom URL投稿・カレンダー招待・ミーティング日程調整・会議への招集
  - アジェンダの中身が技術的な議題（性能ギャップ調査・統合状況等）であっても、本体が「ミーティングを開催する／設定する／セットアップする」であれば会議運営事項として扱い抽出しない
  - 議題そのものを実行する具体的な作業（例:「性能ギャップ調査結果をレポートにまとめて期限内に提出する」）が明示されている場合のみ、その作業を抽出する
- **日常的な確認・周知作業**: 「確認する」「チェックする」「共有する」「展開する」「周知する」「連絡する」だけのもの
- **定期的な繰り返し作業**: 「スケジュールの更新」「議事録の確認」「TWIの更新」など毎週/毎月発生するルーチン
- **単なる会議開催・日程調整**: 「〜について議論する」「ミーティングを設定する」「打合せを実施する」
- **Slack上の連絡・伝達行為**: 「〜をSlackで共有する」「〜に連絡する」「〜に声掛けする」
- **一過性の事務手続き**: 「出席登録」「欠席連絡」「チャンネルへの追加」「アカウント削除」「カレンダー招待送付」
- **資料アップロード・投稿の指示**: 「Boxフォルダへアップロード」「スライドを投稿」など格納先を指示するだけのもの

## Few-shot 判定例

**抽出する例 (✓)**:
- 「富岳太郎さん、Gromacsの性能評価に関する契約状況（MoU等）を確認してください。フレームワーク本体への導入が止まっているため、別契約の有無を明確化する必要があります。」
  → 担当者明確 / 第三者依頼 / 後続作業のブロッカー解消
- 「富岳次郎、デベロッパーサーベイを作成して各SubWGへ送付してください。今後の開発支援方針を決定するための基礎資料です。期限: 5/31」
  → 担当者・期限・成果物・背景がすべて明示
- 「富岳太郎さんからのコメントで、Gromacsに関しては別の契約（MoU?）が必要との指摘があったため、契約状況を整理して関係者に共有する必要がある。」
  → 文中で名前が示された担当者（富岳太郎さん）に対する確認依頼。間接的な依頼表現でも、担当・必要性・成果物（整理結果の共有）が読み取れれば抽出する

**抽出しない例 (✗)**:
- 「OpenFOAMベータ版をリリースしました。GitHub上で公開されました。」
  → 完了済の進捗報告
- 「現行のmainブランチを用いて、各環境でのベンチマーク完成を目指します。」
  → 投稿者自身の意志表明・抽象的な目標
- 「ISCでのミーティングのセットアップを行う。」
  → 会議運営事項
- 「ISCでHeCBenchに関するミーティングを開催したい。アジェンダ: NVIDIAによる性能ギャップ調査、Kokkos版の進捗、F2Kokkosの活用促進。」
  → 議題（アジェンダ）が技術的でも、本体は「ミーティングの開催・設定」なので会議運営事項として除外。
    アジェンダ内の各議題を実行する具体的作業（成果物・期限・担当が明示されたもの）が別途あればそちらは抽出する
- 「アーキテクチャ会議用のスライド資料を指定のBoxフォルダへアップロードする。」
  → 資料格納先の指示・会議運営の付随作業
- 「次回のミーティング（5/18）に向けてアプリ進捗を更新してください。」
  → 定期更新・会議運営の付随作業
- 「2026年度のスケジュールを更新してください。」
  → ルーチン更新作業（担当も「各エリアリーダー」と曖昧）

## 決定事項の定義（厳守）

決定事項とは **意思決定者による判断・方針決定** に限る。
以下の基準を **すべて** 満たすものだけを抽出すること:

1. **意思決定者による合意・判断である**: プロジェクト・組織として方針を決めた・選んだ・承認したという内容。
2. **未来の行動・状態を規定する**: 今後どう進めるかを示すもの。過去形の進捗報告・状況報告は決定事項ではない。
3. 種別はいずれか:
   - **方針・戦略の決定**: プロジェクトの進め方、技術選定、開発方針に関する決定
   - **リソース配分の決定**: 予算、人員、計算資源の割り当てに関する決定
   - **スケジュール・スコープの変更**: マイルストーン期限の変更、機能の追加・削除
   - **対外的な合意・承認**: 他組織との取り決め、承認事項

以下は **抽出しない**（誤抽出が頻発するため特に注意）:
- **会議運営に関する取り決め**: 「ミーティングを開催する」「次回は〇月〇日に開催」「アジェンダに追加する」「Zoom URLを発行する」
  - アジェンダの中身が技術的議題（性能評価・統合状況等）であっても、本体が「会議の開催・設定」であれば抽出しない
- **進捗報告・状況報告**: 「〜しました」「〜が完了した」「〜をリリースした」「ブランチを変更しました」「〜を更新しました」
  - 過去形・完了形で書かれた事実の報告は、たとえ運用変更を含んでいても決定事項ではない
- 情報の共有・報告（「〜が判明した」「〜の状況を報告した」）
- 既知事実の確認（「〜であることを確認した」）
- アクションアイテムの言い換え（担当者への作業依頼を決定事項として重複記載しない）

## 決定事項の Few-shot 判定例

**抽出する例 (✓)**:
- 「Co-Designレビューでの議論の結果、Scale-upネットワークはNVL4方式を採用する方針に決定した。」
  → 意思決定者による合意・技術選定
- 「2026年度予算のうち、ベンチマークWGに XX 万円を割り当てることが承認された。」
  → リソース配分の決定

**抽出しない例 (✗)**:
- 「Gromacsベータ版をリリースしました。GitHub上で公開されました。」
  → 完了済の進捗報告（決定事項ではない）
- 「ベンチマークリポジトリのdevelopブランチをFN_appsブランチへ名称変更しました。」
  → 過去形の運用変更通知（事実の報告であり、意思決定者による方針決定ではない）
- 「ISCでHeCBenchおよびF2Kokkosに関するミーティングを開催する。」
  → 会議運営事項（アジェンダが技術的でも会議開催そのものは決定事項ではない）
- 「次回ミーティングを5/18に開催する。」
  → 会議運営事項

## 決定事項の分類ゲート（判断に迷う場合の最終確認）

上記の基準を満たすか迷う場合は、以下の3問のいずれかに該当するかで最終判定する。
いずれにも該当しなければ決定事項ではなく作業（アクションアイテム）または対象外として扱う。

1. この記録を覆すと、他の作業のやり直しが生じるか
2. 選択肢を排除するか（他の案を採らないと確定したか）
3. 資源（予算・人員・計算資源）や方向（技術選定・スケジュール）を確定させるか

## 決定事項の付帯情報（理由が失われる前に固定する）

決定事項は、時間の経過とともに失われる「なぜ選んだか」を可能な限り併せて抽出する。
**スレッドに明示されている場合のみ**記入し、推測で補完しない（不明なら null）。

- `rationale`: なぜこの選択をしたか（他の理由より優先した根拠）
- `trade_off`: 検討したが採用しなかった代替案・捨てた選択肢
- `reversal_condition`: 何が起きたらこの決定を見直すか（覆す条件）

## その他の指示

1. **明示されたものだけ抽出**: メッセージに明示されていない内容を推測・補完しないこと
2. **出力形式**: 必ず以下のJSON形式のみ出力すること（前後の説明テキスト不要）
3. 決定事項・アクションアイテムがない場合は空配列 `[]` を返すこと。**大半のスレッドは空配列が正しい。**
4. **マイルストーン紐づけ**: 各アクションアイテムについて、下記「マイルストーン一覧」の
   いずれかに明らかに関連する場合は milestone_id を記入すること。判断できない場合は null。
5. **content は2〜3文で記述**: (1) 何をするか (2) なぜ必要か・背景 (3) 期待される成果物。
   1文だけの曖昧な記述（例:「予算の確認」「資料の作成」）は不可。

## マイルストーン一覧

{milestones}

## 過去の関連議論・決定事項（参考情報）

{knowledge_context}

## プロジェクト文脈

{context}

## Slackスレッド

投稿日時: {timestamp}
投稿者: {user_name}
{thread_text}

## 出力JSON形式

```json
{{
  "decisions": [
    {{
      "content": "決定事項の内容（意思決定の結論とその理由・影響を1〜2文で）",
      "decided_at": "YYYY-MM-DD または null",
      "rationale": "なぜこの選択をしたか（スレッドに明示されている場合のみ、無ければ null）",
      "trade_off": "採用しなかった代替案（スレッドに明示されている場合のみ、無ければ null）",
      "reversal_condition": "何が来たら見直すか（スレッドに明示されている場合のみ、無ければ null）"
    }}
  ],
  "action_items": [
    {{
      "content": "何をするか・なぜ必要か・期待される成果物を2〜3文で記述",
      "assignee": "担当者名（不明な場合は null）",
      "due_date": "YYYY-MM-DD または null",
      "milestone_id": "マイルストーンID（M1等）または null"
    }}
  ]
}}
```
"""


# --------------------------------------------------------------------------- #
# 統合トリアージ（1パス版）: 抽出プロンプトに3ゲートの自己審査を織り込む
# --------------------------------------------------------------------------- #
_TRIAGE_GATES_SECTION = """## 出力前の最終審査（3ゲート）

上記の定義に該当する候補を抽出したら、出力する前に各候補を以下の3つのゲートで順番に自己審査し、
**すべてのゲートを通過した項目だけ**を出力JSONに含めること。
シニアプロジェクトマネージャーとして「マイルストーン達成に実質的に必要な項目だけ」を残す。

### ゲート1: マイルストーン関連性
- この項目が完了しない場合、いずれかのマイルストーンの達成に実質的な支障が出るか？
- どのマイルストーンにも関連づけられない → 除外

### ゲート2: 代替可能性
- この項目は、他の既存アクションアイテムや決定事項の付随作業に過ぎないか？
- 「〜を更新する」「〜を確認する」「〜を共有する」「〜を準備する」などの
  他項目の実行に伴う副次的作業 → 除外

### ゲート3: 影響範囲
- この項目が完了しない場合、後続の意思決定・他のタスク・スケジュールに影響が出るか？
- 影響が出ない → 除外

### 審査基準
- **保守的に判定**: 判定に迷う場合は残すのではなく除外する
- ただし、プロジェクトの戦略的転換点・リスク顕在化のシグナルとなる項目は迷った場合でも残す
- 実質的に同じ内容の候補が複数ある場合、より詳細な方のみ残す
- すべての候補が審査で除外されることもあり得る — その場合は空配列を返す

"""

if EXTRACT_PROMPT.count("## その他の指示") != 1:
    raise RuntimeError(
        "EXTRACT_PROMPT のアンカー '## その他の指示' が想定外の出現回数です"
        f"（{EXTRACT_PROMPT.count('## その他の指示')} 回）。"
        "EXTRACT_PROMPT_INTEGRATED の合成に失敗する可能性があるため中断します。"
    )

EXTRACT_PROMPT_INTEGRATED = EXTRACT_PROMPT.replace(
    "## その他の指示", _TRIAGE_GATES_SECTION + "\n## その他の指示", 1
)


# --------------------------------------------------------------------------- #
# トリアージプロンプト（Extractor → Triage の2段階分離）
# --------------------------------------------------------------------------- #
TRIAGE_PROMPT = """
あなたは富岳NEXTプロジェクトのシニアプロジェクトマネージャーです。
以下の抽出候補リストを審査し、**マイルストーン達成に実質的に必要な項目だけ**を残してください。

## 審査の3ゲート

各候補について、以下の3つのゲートを **順番に** 評価してください。
いずれかのゲートで DROP と判定された候補は結果から除外します。

### ゲート1: マイルストーン関連性
- この項目が完了しない場合、いずれかのマイルストーン（M1〜Mn）の達成に実質的な支障が出るか？
- milestone_id が指定されていない場合、どのマイルストーンにも関連づけられないか？
- 関連づけられない → **DROP**（理由: "マイルストーン非関連"）

### ゲート2: 代替可能性
- この項目は、他の既存アクションアイテムや決定事項の付随作業に過ぎないか？
- 「〜を更新する」「〜を確認する」「〜を共有する」「〜を準備する」などの
  他項目の実行に伴う副次的作業 → **DROP**（理由: "代替可能な付随作業"）

### ゲート3: 影響範囲
- この項目が完了しない場合、後続の意思決定・他のタスク・スケジュールに影響が出るか？
- 影響が出ない → **DROP**（理由: "影響範囲が局所的"）

### 決定事項へのゲート適用（重要な例外）
決定事項はアクションアイテムと異なり「なぜそう決めたか」を将来参照するための台帳である。
特定のマイルストーンに直接紐づかなくても、以下のいずれかに該当すれば **ゲート1〜3を通過（KEEP）** とみなす:
1. 覆すと他の作業のやり直しが生じる
2. 選択肢を排除する（他の案を採らないと確定した）
3. 資源（予算・人員・計算資源）や方向（技術選定・測定方針・体制・対外方針）を確定させる

ただし以下は上記に該当しても **DROP** する:
- 会議運営の取り決め（開催日時・時間変更・参加者調整・チャンネル/Zoom作成・アジェンダ）
- 単なる連絡・共有・周知の取り決め（「〜を共有することになった」「〜に展開する」）

## 審査基準

- **保守的に判定**: 判定に迷う場合は KEEP ではなく DROP を選ぶ
- ただし、プロジェクトの戦略的転換点・リスク顕在化のシグナルとなる項目は迷った場合でも KEEP
- **同一項目の重複**: 既に他の候補と実質的に同じ内容がある場合、より詳細な方のみ KEEP

## 入力

{context_note}
### マイルストーン一覧
{milestones}

### 抽出候補（アクションアイテム）
{action_items_json}

### 抽出候補（決定事項）
{decisions_json}

## 出力JSON形式

```json
{{
  "action_items": [
    {{
      "content": "元のcontentをそのまま",
      "assignee": "元のassigneeをそのまま",
      "due_date": "元のdue_dateをそのまま",
      "milestone_id": "元のmilestone_idをそのまま",
      "verdict": "KEEP" または "DROP",
      "reason": "DROPの場合は理由。KEEPの場合は空文字"
    }}
  ],
  "decisions": [
    {{
      "content": "元のcontentをそのまま",
      "decided_at": "元のdecided_atをそのまま",
      "verdict": "KEEP" または "DROP",
      "reason": "DROPの場合は理由。KEEPの場合は空文字"
    }}
  ]
}}
```

**重要**:
- 元の候補リストの全項目について必ず判定すること（漏れがないように）
- content, assignee, due_date, milestone_id, decided_at は元の値をそのままコピーすること（変更しない）
- KEEP と判定された項目のみが有効なレコードとして扱われる
- **すべての候補が DROP になることもあり得る** — その場合は全項目空配列を返す
"""


def extract_json(text: str) -> dict:
    m = re.search(r"```json\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON not found:\n{text[:300]}")


# --------------------------------------------------------------------------- #
# トリアージ（抽出候補の2次審査）
# --------------------------------------------------------------------------- #
def _index_triaged_items(triaged, kind: str) -> dict:
    """トリアージ応答（LLM）を content → 判定dict の索引にする。

    素の文字列や content 欠落・型違いで落ちないようにする。**判定が読み取れない
    項目は verdict を持たない dict として索引に入れる** — 呼び出し側の
    「verdict が不明 → KEEP」という保守的フェイルセーフに乗せるのが狙いで、
    ここで例外を投げてチャンクごと捨てるより取りこぼしが少ない
    （応答形式の逸脱は実測済み。`extraction_item_contents` の docstring 参照）。
    """
    if not isinstance(triaged, dict):
        print(f"[WARN] トリアージ応答が dict ではありません（判定なしとして扱います）: "
              f"{type(triaged).__name__}", file=sys.stderr)
        return {}
    idx: dict = {}
    n_bare = 0
    for item in triaged.get(kind) or []:
        if isinstance(item, str):
            n_bare += 1
            idx[item] = {"content": item}
        elif isinstance(item, dict):
            idx[item.get("content")] = item
        else:
            n_bare += 1
    if n_bare:
        print(f"[WARN] トリアージ応答の {kind} に判定を読み取れない項目が {n_bare} 件"
              "ありました（該当候補は KEEP 扱いになります）", file=sys.stderr)
    return idx


def triage_items(
    extracted: dict,
    milestones: list[dict],
    *,
    context_note: str = "",
    return_verdicts: bool = False,
    missing_verdict: str = "KEEP",
) -> dict:
    """Extractor が抽出した候補を 3 ゲートで審査し、マイルストーン達成に
    実質的に必要な項目だけを残す。

    ゲート1: マイルストーン関連性
    ゲート2: 代替可能性（他項目の付随作業でないか）
    ゲート3: 影響範囲（完了しなくても後続に影響しないなら DROP）

    return_verdicts=True の場合、返り値に "verdicts" キーを追加し、
    元の候補全件（KEEP/DROP 双方）について {"content", "verdict", "reason"} を含む
    一覧を返す（呼び出し元が個別の DROP 理由を audit_log 等に記録したい場合用）。

    missing_verdict: LLM応答に候補が欠落していた場合の扱い（"DROP" または "KEEP"）。
    Slack 抽出経路（既定 "DROP"）は誤抽出が多いため保守的に除外するが、
    minutes 転記時トリアージ・pm_screen --triage の既存データ審査では
    判定不能を DROP にすると出力打ち切り等で実在項目を失いかねないため
    呼び出し元は "KEEP" を渡す。
    """
    a_items = extracted.get("action_items", []) or []
    d_items = extracted.get("decisions", []) or []
    if not a_items and not d_items:
        if return_verdicts:
            return {**extracted, "verdicts": {"decisions": [], "action_items": []}}
        return extracted

    prompt = TRIAGE_PROMPT.format(
        context_note=context_note,
        milestones=format_milestones_for_prompt(milestones),
        action_items_json=json.dumps(a_items, ensure_ascii=False, indent=2),
        decisions_json=json.dumps(d_items, ensure_ascii=False, indent=2),
    )

    max_tokens = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "8192"))
    raw = call_argus_llm(prompt, timeout=600, think=True, max_tokens=max_tokens)

    try:
        triaged = extract_json(raw)
    except Exception as e:
        print(f"[WARN] Slack triage JSON パース失敗、トリアージをスキップ: {e}", file=sys.stderr)
        if return_verdicts:
            return {
                **extracted,
                "verdicts": {
                    "decisions": [{"content": d.get("content"), "verdict": "KEEP", "reason": ""} for d in d_items],
                    "action_items": [{"content": a.get("content"), "verdict": "KEEP", "reason": ""} for a in a_items],
                },
            }
        return extracted

    # --- action_items: KEEP のみ残す ---
    kept_a = []
    verdicts_a = []
    triaged_a = _index_triaged_items(triaged, "action_items")
    for item in a_items:
        content = item.get("content")
        t = triaged_a.get(content)
        if t and t.get("verdict") == "DROP":
            reason = t.get("reason", "不明")
            print(f"[TRIAGE] DROP action_item: {(content or '')[:80]}… — 理由: {reason}", file=sys.stderr)
            verdicts_a.append({"content": content, "verdict": "DROP", "reason": reason})
        elif t and t.get("verdict") == "KEEP":
            kept_a.append(item)
            verdicts_a.append({"content": content, "verdict": "KEEP", "reason": ""})
        elif t is None:
            # レスポンスに欠落 → missing_verdict に従う（Slack既定は保守的にDROP、
            # minutes/pm_screen 経路は判定不能を落とさないよう KEEP を渡す）
            reason = "候補がレスポンスに欠落"
            print(f"[TRIAGE] {missing_verdict} action_item: {(content or '')[:80]}… — 理由: {reason}", file=sys.stderr)
            verdicts_a.append({"content": content, "verdict": missing_verdict, "reason": reason})
            if missing_verdict == "KEEP":
                kept_a.append(item)
        else:
            # verdict が不明 → KEEP（保守的フェイルセーフ）
            kept_a.append(item)
            verdicts_a.append({"content": content, "verdict": "KEEP", "reason": ""})

    # --- decisions: KEEP のみ残す ---
    kept_d = []
    verdicts_d = []
    triaged_d = _index_triaged_items(triaged, "decisions")
    for item in d_items:
        content = item.get("content")
        t = triaged_d.get(content)
        if t and t.get("verdict") == "DROP":
            reason = t.get("reason", "不明")
            print(f"[TRIAGE] DROP decision: {(content or '')[:80]}… — 理由: {reason}", file=sys.stderr)
            verdicts_d.append({"content": content, "verdict": "DROP", "reason": reason})
        elif t and t.get("verdict") == "KEEP":
            kept_d.append(item)
            verdicts_d.append({"content": content, "verdict": "KEEP", "reason": ""})
        elif t is None:
            reason = "候補がレスポンスに欠落"
            print(f"[TRIAGE] {missing_verdict} decision: {(content or '')[:80]}… — 理由: {reason}", file=sys.stderr)
            verdicts_d.append({"content": content, "verdict": missing_verdict, "reason": reason})
            if missing_verdict == "KEEP":
                kept_d.append(item)
        else:
            kept_d.append(item)
            verdicts_d.append({"content": content, "verdict": "KEEP", "reason": ""})

    print(
        f"[INFO] Slack triage: action_items {len(a_items)}→{len(kept_a)}, "
        f"decisions {len(d_items)}→{len(kept_d)}",
        file=sys.stderr,
    )

    result: dict[str, Any] = {"decisions": kept_d, "action_items": kept_a}
    if return_verdicts:
        result["verdicts"] = {"decisions": verdicts_d, "action_items": verdicts_a}
    return result


# --------------------------------------------------------------------------- #
# バッチ分割トリアージ（minutes 転記時トリアージ・pm_screen --triage が共用）
# --------------------------------------------------------------------------- #
_TRIAGE_BATCH_SIZE = 20


def _chunk_or_placeholder(items: list, size: int = _TRIAGE_BATCH_SIZE) -> list[list]:
    """items を size 件ごとに分割する。空リストの場合は [[]] を返す
    （呼び出し側で action_items/decisions のチャンク数を揃えて zip しやすくするため）。"""
    if not items:
        return [[]]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _default_batch_log(msg: str) -> None:
    print(msg, file=sys.stderr)


def triage_items_batched(
    action_items: list[dict],
    decisions: list[dict],
    milestones: list[dict],
    *,
    context_note: str = "",
    missing_verdict: str = "KEEP",
    batch_size: int = _TRIAGE_BATCH_SIZE,
    log=None,
    group_label: str = "",
) -> dict:
    """action_items / decisions を batch_size 件ずつに分割して triage_items を呼び、
    結果を結合して返す（1回のLLM呼び出しに大量の候補を投げると出力打ち切りで
    後半候補が「レスポンス欠落」判定になる問題への対策。minutes 転記時トリアージ・
    pm_screen --triage が共用する）。

    1チャンクの呼び出しが例外を投げた場合はそのチャンクのみスキップして
    log に [WARN] を出力し、他チャンクの結果は保持したまま処理を継続する
    （1件の障害でグループ全体の結果を捨てない）。

    戻り値:
      {
        "action_items": [(item, verdict, reason), ...],  # 元の入力順
        "decisions": [(item, verdict, reason), ...],
        "n_chunks": int,          # 実際に呼び出したチャンク数
        "n_skipped_chunks": int,  # 例外でスキップしたチャンク数
      }
    """
    log = log or _default_batch_log

    ai_chunks = _chunk_or_placeholder(action_items, batch_size)
    dec_chunks = _chunk_or_placeholder(decisions, batch_size)
    n_calls = max(len(ai_chunks), len(dec_chunks))

    ai_results: list[tuple[dict, str, str]] = []
    dec_results: list[tuple[dict, str, str]] = []
    n_chunks = 0
    n_skipped = 0

    for i in range(n_calls):
        ai_part = ai_chunks[i] if i < len(ai_chunks) else []
        dec_part = dec_chunks[i] if i < len(dec_chunks) else []
        if not ai_part and not dec_part:
            continue
        n_chunks += 1
        try:
            result = triage_items(
                {"decisions": dec_part, "action_items": ai_part},
                milestones,
                context_note=context_note,
                return_verdicts=True,
                missing_verdict=missing_verdict,
            )
        except Exception as e:
            n_skipped += 1
            prefix = f"{group_label}: " if group_label else ""
            log(f"[WARN] {prefix}チャンク{i + 1}/{n_calls} のトリアージ呼び出しに失敗、"
                f"このチャンクのみスキップします: {e}")
            continue

        for orig, v in zip(ai_part, result["verdicts"]["action_items"], strict=True):
            ai_results.append((orig, v["verdict"], v.get("reason") or ""))
        for orig, v in zip(dec_part, result["verdicts"]["decisions"], strict=True):
            dec_results.append((orig, v["verdict"], v.get("reason") or ""))

    return {
        "action_items": ai_results,
        "decisions": dec_results,
        "n_chunks": n_chunks,
        "n_skipped_chunks": n_skipped,
    }


# --------------------------------------------------------------------------- #
# 第2系統（独立系統）による差分検査 — docs/security-architecture.md §4.9 対策3+5
# --------------------------------------------------------------------------- #

_SENSITIVE_TERMS_PATH = Path(__file__).resolve().parents[2] / "config" / "sensitive_terms.yaml"
_second_opinion_cache: dict | None = None


def _load_second_opinion_config() -> dict:
    """config/sensitive_terms.yaml を読む（読めなければ空 dict）。"""
    global _second_opinion_cache
    if _second_opinion_cache is not None:
        return _second_opinion_cache
    cfg: dict = {}
    try:
        import yaml
        if _SENSITIVE_TERMS_PATH.is_file():
            cfg = yaml.safe_load(_SENSITIVE_TERMS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    _second_opinion_cache = cfg
    return cfg


def flag_sensitive_terms(text: str) -> list[str]:
    """テキストに含まれるフラグ語を返す（§4.9 対策5）。

    **「機微そうな語」ではなく「トリガになりうる語」を拾う。** 目的は情報の秘匿ではなく、
    *特定の語で挙動が変わる* という条件付きの偏りを探すための絞り込みである。
    """
    if not text:
        return []
    terms = _load_second_opinion_config().get("terms") or {}
    found = []
    for group in terms.values():
        for t in group or []:
            if t and t in text:
                found.append(t)
    return sorted(set(found))


def second_opinion_verdict(content: str, milestones: list[dict], *,
                           context_note: str = "", model: str | None = None) -> tuple[str, str]:
    """独立系統（非中国系モデル）に KEEP / DROP を単独で問う。

    主系統と同じ 3 ゲートの基準を渡すが、**モデルの出自が異なる**ことが唯一の狙い。
    返り値は (verdict, raw)。判定不能なら ("UNKNOWN", raw)。
    """
    cfg = (_load_second_opinion_config().get("second_opinion") or {})
    model = model or cfg.get("model")
    ms = "\n".join(f"- {m.get('name', '')}" for m in (milestones or [])[:20])
    prompt = (
        "次の項目が、下記マイルストーンの達成に実質的に必要かを判定してください。\n"
        "判定は KEEP（必要）か DROP（不要）のどちらか1語だけを1行目に出力し、\n"
        "2行目に理由を1文で書いてください。\n\n"
        f"## マイルストーン\n{ms or '(なし)'}\n\n"
        f"## 文脈\n{context_note or '(なし)'}\n\n"
        f"## 判定対象\n{content}\n"
    )
    from utils.llm import call_rivault

    raw = call_rivault(prompt, model=model, max_tokens=512, timeout=120)
    head = (raw or "").strip().splitlines()[0].upper() if raw else ""
    verdict = "KEEP" if "KEEP" in head else ("DROP" if "DROP" in head else "UNKNOWN")
    return verdict, (raw or "")[:500]


def apply_second_opinion(results: dict, milestones: list[dict], *,
                         context_note: str = "", conn=None, log=None) -> list[dict]:
    """トリアージ結果のうち**フラグ語が立った項目だけ**に第2系統を当てる（§4.9）。

    全件に当てない理由は config/sensitive_terms.yaml の冒頭に書いてある
    （小型モデルの能力差による不一致が支配的になり、探している信号が埋もれる）。

    不一致は握りつぶさず、`triage_second_opinion` テーブルに記録して**人が見る**。
    第2系統の判定で主系統の結果を上書きはしない — 能力差による誤りが混ざるため、
    **自動で覆すのではなくフラグを立てるに留める**。

    戻り値: 不一致だった項目の一覧。

    **注意（2026-08）**: この関数は `triage_items(return_verdicts=True)` が返す
    `(item, verdict, reason)` タプル形式を前提としている。既定の `DEFAULT_TRIAGE_MODE`
    は "integrated" であり、この形式の後段 `triage_items` 呼び出し自体が発生しないため、
    **integrated モードの production 経路からは呼ばれない**（呼び出しは
    `tests/ingest/test_second_opinion.py` のみ）。削除はせず、two_stage 復帰時のために残す。
    production の第2系統は `apply_second_opinion_extraction`（Pass 1 抽出）と
    `pm_box_relevance.py` 側の配線を使う。
    """
    log = log or _default_batch_log
    if os.environ.get("ARGUS_SECOND_OPINION", "1").strip() not in ("1", "true", "yes"):
        return []
    cfg = (_load_second_opinion_config().get("second_opinion") or {})
    cap = int(cfg.get("max_flagged_per_run") or 30)

    flagged: list[tuple[str, dict, str, str, list[str]]] = []
    for kind in ("action_items", "decisions"):
        for item, verdict, _reason in results.get(kind, []):
            content = (item or {}).get("content", "") or ""
            terms = flag_sensitive_terms(content)
            if terms:
                flagged.append((kind, item, verdict, content, terms))

    if not flagged:
        return []
    if len(flagged) > cap:
        log(f"[WARN] フラグ付き項目が {len(flagged)} 件で上限 {cap} を超えました。"
            "フラグ語が広すぎる可能性があります（全件差分に近づくと信号が埋もれます）。"
            f"上位 {cap} 件のみ第2系統に当てます")
        flagged = flagged[:cap]

    disagreements = []
    for kind, _item, primary, content, terms in flagged:
        try:
            second, raw = second_opinion_verdict(
                content, milestones, context_note=context_note)
        except Exception as e:
            log(f"[WARN] 第2系統の呼び出しに失敗（この項目はスキップ）: {e}")
            continue
        agree = (second == primary)
        if conn is not None:
            try:
                from db_utils import record_second_opinion
                record_second_opinion(
                    conn, kind=kind, content=content, primary_verdict=primary,
                    second_verdict=second, flagged_terms=terms,
                    model=(cfg.get("model") or ""), raw=raw,
                )
            except Exception as e:
                log(f"[WARN] 第2系統の結果を記録できませんでした（判定は継続）: {e}")
        if not agree:
            disagreements.append({
                "kind": kind, "content": content[:120],
                "primary": primary, "second": second, "terms": terms,
            })

    if disagreements:
        log(f"[SECOND-OPINION] フラグ付き {len(flagged)} 件中 {len(disagreements)} 件で"
            "主系統と不一致。**自動では覆さない** — pm.db の triage_second_opinion を確認してください")
    return disagreements


# --------------------------------------------------------------------------- #
# 第2系統（独立系統）による Pass 1 抽出の差分検査 — R8 / Phase 4
# --------------------------------------------------------------------------- #
#
# apply_second_opinion（上）は主系統が KEEP/DROP を判定した *生き残った* 項目にだけ
# 第2系統を当てる。だが既定の DEFAULT_TRIAGE_MODE = "integrated" では DROP された項目は
# 出力に現れないため、生き残った項目をいくら再審査しても「誤って残ったもの」しか
# 見えず、**探している欠落（omission）は原理的に見えない**。
#
# そのため以下は、同じ生入力（スレッド本文）を第2系統に独立抽出させ、
# 主系統の抽出結果に **無い** 項目を探す。第2系統の判定で主系統の抽出結果を
# 上書きすることはしない — 記録してフラグを立てるに留める（apply_second_opinion と同じ方針）。

# SequenceMatcher.ratio() の一致判定閾値。0.6 は表記ゆれ（「〜する」/「〜します」、
# 助詞の省略等）を同一項目とみなしつつ、内容の異なる別項目まで同一視しない値として選定。
# 変更する場合は config/sensitive_terms.yaml 同様、件数への影響を見ること。
_EXTRACTION_MATCH_RATIO_THRESHOLD = 0.6

# 突合の包含判定パラメータ。ratio() は対称で長さの差に強く罰点を与えるため、
# 第2系統（特に K3）が主系統より詳しく長く書いた同一項目を「不一致」と誤判定する
# （実測: 保存済み29件中7件がこれで偽陽性になっていた。ratio<0.6 だが同一項目）。
#
# 当初「正規化した短い方から12文字窓を4文字ステップで切り出し、その70%以上が
# 長い方に出現する」で実装したが、実データ（保存済み29件 + 対応する議事録DB）で
# 再検証したところ **0/7件しか救えなかった**。原因はステップ4の固定グリッドの
# 位置ずれ: 実際に一致する連続部分文字列が12〜13文字ちょうどのことが多く、
# その一致区間が4の倍数位置から始まっていないと、どの窓もその区間を
# 丸ごとは切り出せずヒットしない（例: 一致区間が長さ13で開始位置が
# グリッド位置と1〜3文字ずれているだけで hits=0 になる）。ステップ幅を保つ限り
# 「70%以上」はほぼ満たせない値になってしまうため、判定方法そのものを
# 「正規化した2文字列間に長さ12文字以上の連続一致部分文字列が存在するか」
# （全オフセットを見る＝グリッドのずれに影響されない）に変更した。
# 12文字という長さの根拠は変わらない: 短すぎる一致では「〜を実施する」等の
# 共通する短い定型句だけで内容の異なる別項目まで同一視してしまう。
# 変更後の値で実データを再検証し、③相当7件・②相当1件を検出、残り21件が
# 真の欠落候補という結果を得た（元の見立て: ③7件・②2件・欠落20件前後に近い）。
_CONTAINMENT_WINDOW = 12


def normalize_for_extraction_match(text: str) -> str:
    """突合用の正規化: 空白（半角・全角）・句読点を除去し、ASCIIは小文字化する。"""
    if not text:
        return ""
    text = re.sub(r"[ \t　]+", "", text)
    text = re.sub(r"[、。（）()「」\[\],.:：・|\-]", "", text)
    return text.lower()


def _is_contained_match(a: str, b: str) -> bool:
    """正規化した a, b のうち短い方が、長い方と12文字以上の連続一致部分文字列を
    持てば一致とみなす（_CONTAINMENT_WINDOW 上のコメント参照。ステップ付き固定
    グリッドでの走査は位置ずれで実データの一致を検出できなかったため、
    全オフセットを見る最長共通部分文字列で判定する）。"""
    na = normalize_for_extraction_match(a)
    nb = normalize_for_extraction_match(b)
    if not na or not nb:
        return False
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) <= _CONTAINMENT_WINDOW:
        return short in long_
    match = difflib.SequenceMatcher(None, short, long_).find_longest_match(
        0, len(short), 0, len(long_)
    )
    return match.size >= _CONTAINMENT_WINDOW


def _extraction_texts_match(a: str, b: str) -> bool:
    """ratio または包含のいずれかを満たせば一致とみなす。

    ratio は「表記ゆれの短い項目同士」に効き、包含は「長さが大きく違う同一項目」に効く。
    どちらか一方が成立すれば十分なので、従来の ratio 判定は残したまま or 条件で足す。
    """
    if not a or not b:
        return False
    if difflib.SequenceMatcher(None, a, b).ratio() >= _EXTRACTION_MATCH_RATIO_THRESHOLD:
        return True
    return _is_contained_match(a, b)


_EXTRACTION_CONTENT_KEYS = (
    "content", "text", "item", "title", "summary", "description",
    "decision", "action_item", "action", "task",
)


def extraction_item_contents(value) -> list[str]:
    """抽出結果の1種別（decisions / action_items）を content 文字列のリストへ正規化する。

    **LLM は指示した形式に従わないことがある**。実測した逸脱:

    - `[{"content": "..."}]` … 指示どおり
    - `["..."]`              … 素の文字列の配列（2026-08-04 実測、Llama-4-Scout）
    - `"..."`                … 配列にせず1本の文字列
    - `[{"decision": "..."}]`… キー名違い

    以前は `item.get("content")` を直接呼んでいたため、素の文字列が返ると
    `AttributeError: 'str' object has no attribute 'get'` で**第2系統検査そのものが
    落ちていた**（会議1件の検査が丸ごと失われ、後続の読み手も走らなかった）。
    形式の逸脱で検出機会を失うのは本末転倒なので、拾えるものは拾う。

    ただし**黙って拾わない** — 逸脱があった場合は stderr に1行warnを出す。
    「モデルが指示に従っていない」ことはプロンプト側で直すべき情報であり、
    正規化で見えなくしてはいけない。
    """
    if value is None:
        return []
    if isinstance(value, str):
        # 配列にせず1本の文字列を返してきたケース。改行区切りの箇条書きの
        # 可能性があるため行に割る（先頭の "- " 等は突合時の正規化で落ちる）。
        lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
        if lines:
            print("[WARN] 抽出結果が配列ではなく文字列で返りました"
                  f"（{len(lines)} 行として扱います）", file=sys.stderr)
        return lines
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        print(f"[WARN] 抽出結果が想定外の型です（無視します）: {type(value).__name__}",
              file=sys.stderr)
        return []

    contents: list[str] = []
    n_bare_str = 0
    n_alt_key = 0
    n_dropped = 0
    for item in value:
        if isinstance(item, str):
            n_bare_str += 1
            contents.append(item)
            continue
        if isinstance(item, dict):
            c = item.get("content")
            if isinstance(c, str) and c.strip():
                contents.append(c)
                continue
            for k in _EXTRACTION_CONTENT_KEYS:
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    n_alt_key += 1
                    contents.append(v)
                    break
            else:
                # content 相当のキーが1つも無い dict。空の content は
                # compare_extractions 側で除外されるため空文字を入れておく
                # （件数の整合を保つ）。
                contents.append("")
            continue
        if item is None:
            contents.append("")
            continue
        n_dropped += 1
    if n_bare_str:
        print(f"[WARN] 抽出結果に素の文字列が {n_bare_str} 件ありました"
              "（{\"content\": ...} 形式を指示しているが従っていない。そのまま突合します）",
              file=sys.stderr)
    if n_alt_key:
        print(f"[WARN] 抽出結果に content 以外のキーで本文を返した項目が {n_alt_key} 件"
              "ありました（代替キーから読み取りました）", file=sys.stderr)
    if n_dropped:
        print(f"[WARN] 抽出結果の {n_dropped} 件は文字列でも dict でもないため無視しました",
              file=sys.stderr)
    return contents


def compare_extractions(primary: dict, second: dict, *, extra_haystack: str = "") -> dict:
    """主系統・第2系統の抽出結果を突き合わせ、**第2系統にあって主系統に無い**項目を返す。

    突合は「ratio または包含」のいずれかを満たせば「一致」とみなす
    （_extraction_texts_match 参照。根拠は各定数のコメント参照）。

    extra_haystack: 抽出表には無いが本文に書かれているテキスト（例: 議事録本文）を
    渡す省略可能引数。抽出表と一致しなかった項目についてのみ、この文字列との一致も
    確認する（一致すれば「本文にはあるので欠落ではない」として除外する）。
    既定は空文字列で、その場合は従来どおり抽出表のみで判定する（Slack Pass 1 抽出・
    Box の既存呼び出しの挙動は変わらない）。

    primary / second の各項目は `extraction_item_contents` で正規化する
    （LLM が `{"content": ...}` 形式に従わない場合があるため。同関数の docstring 参照）。

    戻り値: {"decisions": [未一致content...], "action_items": [...],
             "primary_counts": {...}, "second_counts": {...},
             "matched_in_table_counts": {...}, "matched_in_haystack_counts": {...}}
    """
    # dict 以外（LLM が top-level を配列で返した等）は空扱いにする。ここで
    # AttributeError を投げると呼び出し元の検査全体が落ちるため。
    if not isinstance(primary, dict):
        primary = {}
    if not isinstance(second, dict):
        print(f"[WARN] 第2系統の抽出結果が dict ではありません（空として扱います）: "
              f"{type(second).__name__}", file=sys.stderr)
        second = {}

    result: dict = {}
    primary_counts: dict = {}
    second_counts: dict = {}
    matched_in_table_counts: dict = {}
    matched_in_haystack_counts: dict = {}
    for kind in ("decisions", "action_items"):
        primary_contents = extraction_item_contents(primary.get(kind))
        second_contents = extraction_item_contents(second.get(kind))
        primary_counts[kind] = len(primary_contents)
        second_counts[kind] = len(second_contents)

        missing = []
        n_matched_table = 0
        n_matched_haystack = 0
        for s in second_contents:
            # 空・空白のみの content は突合対象から除外する。第2系統が
            # {"content": ""} を返すことがあり、これをそのまま扱うと空文字が
            # missing に入り、triage_second_opinion に空の所見が記録されてしまう。
            if not s.strip():
                continue
            if any(_extraction_texts_match(s, p) for p in primary_contents):
                n_matched_table += 1
                continue
            if extra_haystack and _extraction_texts_match(s, extra_haystack):
                n_matched_haystack += 1
                continue
            missing.append(s)
        result[kind] = missing
        matched_in_table_counts[kind] = n_matched_table
        matched_in_haystack_counts[kind] = n_matched_haystack

    result["primary_counts"] = primary_counts
    result["second_counts"] = second_counts
    result["matched_in_table_counts"] = matched_in_table_counts
    result["matched_in_haystack_counts"] = matched_in_haystack_counts
    return result


def _call_second_opinion_extraction(thread_text: str, *, context: str = "",
                                    model: str | None = None,
                                    route: str = "rivault") -> tuple[dict, str]:
    """第2系統モデルに同じ生入力から独立抽出させる（LLM 呼び出し本体）。

    `second_opinion_extraction()` の公開シグネチャは `dict` のみを返すが、
    `record_second_opinion` の `raw` には生応答が必要なため、raw も返す
    内部ヘルパーを分離してある（呼び出し元での二重 LLM 呼び出しを避ける）。

    route: "rivault"（既定）は `config/sensitive_terms.yaml` の `second_opinion.model`
        （Llama-4-Scout、RiVault配信）を `call_rivault` で呼ぶ。R8（提供元レベルの
        集中リスク）対策の第2系統であり、**出自が主系統と独立していること**が本質。

        "k3" は `config/sensitive_terms.yaml` の `quality_reader.model`（kimi-k3）を
        `call_local_llm` で呼ぶ（`LOCAL_LLM_URL` = RIKYU 経由）。
        **2026-08-04 に RiVault 側にも `moonshotai/Kimi-K3` が入った**ため「RiVault には
        いないから call_rivault では呼べない」は既に成り立たない。それでも RIKYU 経由を
        使い続けているのは、読み手として実測した（`max_tokens=16384` / 所要時間 / 成功率）
        のがこの経路であり、**経路を変えれば実測をやり直す必要がある**ため。
        同じモデルが2経路にあることは `config/model_pin.yaml` の
        `observed_not_used: moonshotai/Kimi-K3` に記録してある。
        **これは R8 の独立系統ではない** — K3（Moonshot）は主系統の
        glm/DeepSeek/Qwen と同じ「本番経路の主要モデルが一系統に寄っている」構図の外には
        出られない。狙いは出自の分散ではなく、K3 の読解・網羅（recall）の高さを議事録の
        欠落検出に使うことだけ（`scripts/quality/pm_screen.py` の
        `--second-opinion-minutes --reader k3` から呼ばれる）。呼び出し元は record_second_opinion
        の `kind` を `minutes_extraction_recall` にして、R8 対策の `minutes_extraction` とは
        別集計にすること。
    """
    cfg_root = _load_second_opinion_config()
    if route == "k3":
        model = model or (cfg_root.get("quality_reader") or {}).get("model")
    else:
        model = model or (cfg_root.get("second_opinion") or {}).get("model")
    # 2026-08 粒度調整: 主系統（EXTRACT_PROMPT_INTEGRATED）と同じ3ゲート基準
    # （_TRIAGE_GATES_SECTION、複製せず参照）を入れ、日程調整・事務連絡のような
    # 「意図した除外」まで欠落として拾ってしまう問題（問題1）を抑える。
    # あわせて話者名・組織名を含めない旨の出力形式制約を入れる（問題2）。
    # 突合（compare_extractions の SequenceMatcher 閾値）を緩める案もあったが、
    # 閾値を下げると別項目の同一視が増えるため確実性が低い。出力側を主系統の
    # 記述スタイル（簡潔・話者名なし）に寄せる方が確実、という判断。
    prompt = (
        "次のやり取りから、(1) 決定事項 (2) アクションアイテム を抜き出し、JSON で返してください。\n"
        '`{"decisions": [{"content": "..."}], "action_items": [{"content": "..."}]}` の形式で、'
        "本文は1行の日本語にしてください。該当が無ければ空配列にしてください。\n"
        "\n"
        + _TRIAGE_GATES_SECTION
        + "\n## 出力形式の制約\n"
        "- content には話者名・組織名を含めないこと"
        "（「誰が言ったか」ではなく「何が決まったか・何をするか」を書く）\n"
        "- 1項目1行、簡潔に記述すること\n"
        "- 敬称・肩書（さん、氏、部長 等）を付けないこと\n"
    )
    if context:
        prompt += f"\n## 文脈\n{context}\n"
    prompt += f"\n## やり取り\n{thread_text}\n"

    try:
        if route == "k3":
            from utils.llm import _token_for_base, call_local_llm

            base_url = os.environ.get("LOCAL_LLM_URL")
            if not base_url:
                raise RuntimeError("LOCAL_LLM_URL 未設定（~/.secrets/localLLM.sh を確認）")
            # **K3 は thinking を無効化できない**（`think=False` を渡しても効かない。
            # 2026-07-29 実測: enable_thinking / thinking.type / reasoning_effort の
            # 3 手段すべて無効）。thinking だけで 2〜3k トークン消費するため、
            # RiVault 側と同じ max_tokens=1024 では**本文に到達する前に打ち切られ 0 文字が返る**
            # （2026-08-03 実測。JSON パース失敗として現れる）。
            # timeout も 120 秒では足りない（1 チャンクで数分かかる）。
            # **あるモデル向けに調整した値を別のモデルへそのまま適用してはいけない** —
            # 共用してよいのはプロンプトと突合ロジックで、thinking の有無のような
            # モデルの性質はパラメータに現れる。
            raw = call_local_llm(
                prompt, model=model, base_url=base_url,
                api_key=_token_for_base(base_url),
                # 8192 でも足りなかった（2026-08-03 実測）。3 ゲート節をプロンプトへ
                # 足して入力が長くなった結果、7 チャンク中 2 件が失敗した
                # （空応答 1 件＝thinking で使い切り、JSON 途中切れ 1 件＝出力中に上限到達）。
                # generate_minutes_local が K3 相当の処理で使っている 16384 に合わせる。
                max_tokens=16384, timeout=600, think=False,
            )
        else:
            from utils.llm import call_rivault

            raw = call_rivault(prompt, model=model, max_tokens=1024, timeout=120)
    except Exception as e:
        print(f"[WARN] 第2系統抽出（route={route}）の呼び出しに失敗: {e}", file=sys.stderr)
        return {"decisions": [], "action_items": []}, ""

    raw = raw or ""
    try:
        parsed = extract_json(raw)
    except Exception as e:
        print(f"[WARN] 第2系統抽出のJSONパースに失敗: {e}", file=sys.stderr)
        return {"decisions": [], "action_items": []}, raw[:500]
    return parsed, raw[:500]


def second_opinion_extraction(thread_text: str, *, context: str = "",
                              milestones: list[dict] | None = None,
                              model: str | None = None) -> dict:
    """独立系統（非中国系モデル）に、同じ生入力（スレッド本文）から決定事項・
    アクションアイテムを**独立に**抽出させる。

    主系統の EXTRACT_PROMPT は流用しない — 巨大かつ主モデル向けに調整済みのプロンプトを
    そのまま別モデルに当てると、差がプロンプト適合度の差になってしまうため、
    短く独立した指示にする。

    失敗（LLM呼び出し失敗・JSONパース失敗）しても例外は投げない —
    呼び出し元がこの項目をスキップできるよう、空の抽出結果を返す（ログには出す）。

    milestones は現時点ではプロンプトに使用しない（呼び出し側の signature を
    extract_from_thread 相当に揃えるために受け取るのみ）。
    """
    parsed, _raw = _call_second_opinion_extraction(thread_text, context=context, model=model)
    return parsed


def apply_second_opinion_extraction(
    thread_text: str,
    extracted: dict,
    milestones: list[dict],
    *,
    context_note: str = "",
    conn=None,
    log=None,
    state: dict | None = None,
) -> list[dict]:
    """Slack Pass 1 抽出（integrated モード）に第2系統の差分検査をかける（R8 / Phase 4）。

    主系統が抽出した `extracted` と、第2系統が同じ生入力から独立抽出した結果を
    `compare_extractions` で突き合わせ、**第2系統にあって主系統に無い**項目を
    `triage_second_opinion` に記録する（`kind="decisions_extraction"` /
    `"action_items_extraction"`, `primary_verdict="MISSING"`, `second_verdict="PRESENT"`）。

    pm.db への保存内容（save_slack_items 経由）は一切変えない — ここでは記録のみ行う。

    state: 呼び出し元（SlackIngestPlugin.run）が run 全体で使い回す可変 dict。
    `max_flagged_per_run` の上限判定に使う（スレッドをまたいだカウンタ）。
    省略時は呼び出しごとに新規カウンタになる。

    戻り値: 未一致だった項目一覧（テスト・ログ確認用。呼び出し元は無視してよい）。
    """
    log = log or _default_batch_log
    if os.environ.get("ARGUS_SECOND_OPINION", "1").strip() not in ("1", "true", "yes"):
        return []

    terms = flag_sensitive_terms(thread_text)
    if not terms:
        return []

    cfg = (_load_second_opinion_config().get("second_opinion") or {})
    cap = int(cfg.get("max_flagged_per_run") or 30)
    state = state if state is not None else {}
    count = state.get("count", 0)
    if count >= cap:
        if not state.get("cap_warned"):
            log(f"[WARN] 第2系統抽出の実行回数が上限 {cap} 件に達しました。"
                "これ以降のスレッドには第2系統を当てません"
                "（黙って打ち切ると「全部見た」と誤読されるため明示します）")
            state["cap_warned"] = True
        return []
    state["count"] = count + 1

    second, raw = _call_second_opinion_extraction(thread_text, context=context_note)
    diff = compare_extractions(extracted, second)
    model = cfg.get("model") or ""

    disagreements: list[dict] = []
    for kind, so_kind in (
        ("decisions", "decisions_extraction"),
        ("action_items", "action_items_extraction"),
    ):
        for content in diff.get(kind, []):
            disagreements.append({"kind": so_kind, "content": content})
            if conn is not None:
                try:
                    from db_utils import record_second_opinion
                    record_second_opinion(
                        conn, kind=so_kind, content=content,
                        primary_verdict="MISSING", second_verdict="PRESENT",
                        flagged_terms=terms, model=model, raw=raw,
                    )
                except Exception as e:
                    log(f"[WARN] 第2系統抽出の結果を記録できませんでした（判定は継続）: {e}")

    if disagreements:
        log(f"[SECOND-OPINION] Slack抽出: 第2系統が主系統の出力に無い項目を "
            f"{len(disagreements)} 件検出。**自動では追加しない** — "
            "pm.db の triage_second_opinion を確認してください")
    return disagreements


def _sample_extractions(prompt: str, n: int) -> list[dict]:
    """同一プロンプトを N 回サンプリングし、JSON 抽出に成功したドラフトのリストを返す。

    call_argus_llm で temperature を僅かに振って多様性を確保する。
    N=1 の場合は temperature=None（モデルデフォルト）で 1 回のみ呼ぶ。
    """
    if n <= 1:
        try:
            return [extract_json(call_argus_llm(prompt, timeout=600, think=True,
                                                max_tokens=int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "8192"))))]
        except Exception:
            return []

    max_tokens = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "8192"))

    # n=3 → -0.1, 0, +0.1
    if n == 2:
        deltas = [-0.05, 0.05]
    else:
        step = 0.2 / (n - 1)
        deltas = [-0.1 + step * i for i in range(n)]
    base_t = 0.4  # V4-Flash オーバーフィット防止

    drafts: list[dict] = []
    for i, d in enumerate(deltas, 1):
        t = max(0.05, min(1.5, base_t + d))
        try:
            text = call_argus_llm(
                prompt, timeout=600, think=True, max_tokens=max_tokens,
                temperature=t,
            )
        except Exception as e:
            print(f"[WARN] Slack 抽出サンプル {i}/{n} 失敗: {e}", file=sys.stderr)
            continue
        if not text or not text.strip():
            continue
        try:
            drafts.append(extract_json(text))
        except Exception as e:
            print(f"[WARN] Slack 抽出サンプル {i}/{n} JSON パース失敗: {e}", file=sys.stderr)
    return drafts


def _consensus_decisions(drafts: list[dict], min_vote: int, threshold: float) -> list[dict]:
    """各ドラフトの decisions をクラスタ化し、min_vote 以上の独立票を得たクラスタから代表を採用する。"""
    flat: list[tuple[int, dict]] = []  # (draft_idx, decision)
    for di, d in enumerate(drafts):
        for item in d.get("decisions", []) or []:
            content = (item or {}).get("content")
            if content and content.strip():
                flat.append((di, item))
    if not flat:
        return []
    keys = [item["content"] for _, item in flat]
    try:
        import numpy as np
        from embed_utils import cosine_similarity_matrix, embed_batch
        vecs = embed_batch(keys)
        clusters: list[list[int]] = []
        centers = []
        for i, v in enumerate(vecs):
            if not clusters:
                clusters.append([i])
                centers.append(v.copy())
                continue
            sims = cosine_similarity_matrix(v, np.stack(centers))
            best = int(np.argmax(sims))
            if float(sims[best]) >= threshold:
                clusters[best].append(i)
                n_old = len(clusters[best]) - 1
                centers[best] = (centers[best] * n_old + v) / (n_old + 1)
            else:
                clusters.append([i])
                centers.append(v.copy())
    except Exception as e:
        print(f"[ERROR] Slack 決定事項 embedding 失敗、最初のドラフトを採用: {e}", file=sys.stderr)
        return list(drafts[0].get("decisions") or []) if drafts else []

    accepted: list[dict] = []
    for cl in clusters:
        if len({flat[i][0] for i in cl}) < min_vote:
            continue
        # 代表選定: content が最長で decided_at が埋まっているものを優先
        cl_items = [flat[i][1] for i in cl]
        rep = max(cl_items, key=lambda d: (bool(d.get("decided_at")), len(d.get("content") or "")))
        accepted.append(rep)
    return accepted


def _consensus_action_items(drafts: list[dict], min_vote: int, threshold: float) -> list[dict]:
    """各ドラフトの action_items をクラスタ化し、min_vote 以上の独立票を得たクラスタから代表を採用する。

    クラスタリングキー: `[担当者] content` — 担当者違いは別クラスタ扱い。
    """
    flat: list[tuple[int, dict]] = []
    for di, d in enumerate(drafts):
        for item in d.get("action_items", []) or []:
            content = (item or {}).get("content")
            if content and content.strip():
                flat.append((di, item))
    if not flat:
        return []
    keys = [
        f"[{(item.get('assignee') or '未定')}] {item.get('content') or ''}"
        for _, item in flat
    ]
    try:
        import numpy as np
        from embed_utils import cosine_similarity_matrix, embed_batch
        vecs = embed_batch(keys)
        clusters: list[list[int]] = []
        centers = []
        for i, v in enumerate(vecs):
            if not clusters:
                clusters.append([i])
                centers.append(v.copy())
                continue
            sims = cosine_similarity_matrix(v, np.stack(centers))
            best = int(np.argmax(sims))
            if float(sims[best]) >= threshold:
                clusters[best].append(i)
                n_old = len(clusters[best]) - 1
                centers[best] = (centers[best] * n_old + v) / (n_old + 1)
            else:
                clusters.append([i])
                centers.append(v.copy())
    except Exception as e:
        print(f"[ERROR] Slack AI embedding 失敗、最初のドラフトを採用: {e}", file=sys.stderr)
        return list(drafts[0].get("action_items") or []) if drafts else []

    accepted: list[dict] = []
    for cl in clusters:
        if len({flat[i][0] for i in cl}) < min_vote:
            continue
        cl_items = [flat[i][1] for i in cl]
        # 代表選定: due_date / milestone_id が埋まっており content が最長のものを優先
        rep = max(
            cl_items,
            key=lambda a: (
                bool(a.get("due_date")),
                bool(a.get("milestone_id")),
                bool(a.get("assignee")),
                len(a.get("content") or ""),
            ),
        )
        accepted.append(rep)
    return accepted


def extract_from_thread(
    row: dict,
    context: str,
    milestones: list[dict],
    repo_root: Path = None,
    *,
    consensus_n: int = 1,
    consensus_threshold: float = 0.78,
    consensus_min_vote: int | None = None,
    enable_triage: bool = True,
    triage_mode: str = DEFAULT_TRIAGE_MODE,
    knowledge_context: str | None = None,
) -> dict:
    if triage_mode not in ("two_stage", "integrated"):
        raise ValueError(f"未知の triage_mode: {triage_mode!r}（'two_stage' または 'integrated' のみ）")

    # two_stage の場合のみ triage_items() を別途呼ぶ。integrated はプロンプト自体に
    # 3ゲートの自己審査を織り込み済みのため後段の LLM 呼び出しは不要。
    run_two_stage_triage = enable_triage and triage_mode == "two_stage"
    use_integrated_prompt = enable_triage and triage_mode == "integrated"

    # ナレッジ検索（Phase 2追加）— 統合 qa_index.db の pm-all で全件横断。
    # knowledge_context が呼び出し側から渡された場合（A/B 比較で両アームに同一の
    # ナレッジ文脈を注入したい場合など）は再検索しない。
    if knowledge_context is None:
        knowledge_context = retrieve_knowledge_for_extraction(
            row["thread_text"],
            qa_db_path=(repo_root or REPO_ROOT) / "data" / "qa_index.db",
            top_k=3,
            index_name="pm-all",
        )

    prompt_template = EXTRACT_PROMPT_INTEGRATED if use_integrated_prompt else EXTRACT_PROMPT
    prompt = prompt_template.format(
        context=context,
        knowledge_context=knowledge_context,
        timestamp=row.get("timestamp", "不明"),
        user_name=row.get("user_name", "不明"),
        thread_text=row["thread_text"],
        milestones=format_milestones_for_prompt(milestones),
    )

    if consensus_n <= 1:
        max_tokens = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "8192"))
        raw = call_argus_llm(prompt, timeout=600, think=True, max_tokens=max_tokens)
        result = extract_json(raw)
        if run_two_stage_triage:
            result = triage_items(result, milestones)
        return result

    drafts = _sample_extractions(prompt, consensus_n)
    if not drafts:
        return {"decisions": [], "action_items": []}
    if len(drafts) == 1:
        # サンプルが 1 件しか得られなかった場合は集約しない（投票不可）
        print(f"[WARN] Slack consensus: ドラフトが {len(drafts)}/{consensus_n} 件のみ。集約せず採用", file=sys.stderr)
        result = drafts[0]
        if run_two_stage_triage:
            result = triage_items(result, milestones)
        return result

    min_vote = consensus_min_vote if consensus_min_vote is not None else math.ceil(len(drafts) / 2)
    decisions = _consensus_decisions(drafts, min_vote, consensus_threshold)
    action_items = _consensus_action_items(drafts, min_vote, consensus_threshold)
    print(
        f"[INFO] Slack consensus: {len(drafts)}/{consensus_n} ドラフト, min_vote={min_vote} → "
        f"decisions={len(decisions)}, action_items={len(action_items)}",
        file=sys.stderr,
    )
    result = {"decisions": decisions, "action_items": action_items}
    if run_two_stage_triage:
        result = triage_items(result, milestones)
    return result


# --------------------------------------------------------------------------- #
# pm.db 書き込み
# --------------------------------------------------------------------------- #
def save_slack_items(
    pm_conn,
    thread_ts: str,
    channel_id: str,
    permalink: str | None,
    timestamp: str,
    extracted: dict,
) -> tuple[int, int]:
    post_date = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
    source_ref = permalink or f"slack://{channel_id}/{thread_ts}"

    # 再抽出時、手動削除(deleted=1)されたレコードは残し、
    # それ以外の既存 slack レコードを削除してから INSERT する。
    # これにより、ユーザーが Detect Duplicates 等で削除したレコードが
    # 再抽出で復活する問題を防ぐ。
    # 削除済みレコードの内容を収集しておき、同一内容の再INSERTを防ぐ。
    deleted_decisions = set()
    for row in pm_conn.execute(
        "SELECT content FROM decisions"
        " WHERE source='slack' AND source_ref=? AND COALESCE(deleted,0)=1",
        (source_ref,),
    ).fetchall():
        deleted_decisions.add(row["content"])

    deleted_actions = set()
    for row in pm_conn.execute(
        "SELECT content FROM action_items"
        " WHERE source='slack' AND source_ref=? AND COALESCE(deleted,0)=1",
        (source_ref,),
    ).fetchall():
        deleted_actions.add(row["content"])

    for table in ("action_items", "decisions"):
        pm_conn.execute(
            f"DELETE FROM {table}"
            " WHERE source='slack' AND source_ref=? AND COALESCE(deleted,0)=0",
            (source_ref,),
        )

    d_count = 0
    for d in extracted.get("decisions", []):
        if not d.get("content"):
            continue
        if d["content"] in deleted_decisions:
            print(f"    [SKIP] 削除済みの決定事項をスキップ: {d['content'][:60]}", file=sys.stderr)
            continue
        decided_at = d.get("decided_at") or post_date
        pm_conn.execute(
            "INSERT INTO decisions (meeting_id, content, decided_at, source, source_ref,"
            " extracted_at, channel_id, rationale, trade_off, reversal_condition)"
            " VALUES (?, ?, ?, 'slack', ?, ?, ?, ?, ?, ?)",
            (None, d["content"], decided_at, source_ref, post_date, channel_id,
             d.get("rationale"), d.get("trade_off"), d.get("reversal_condition")),
        )
        d_count += 1

    a_count = 0
    for a in extracted.get("action_items", []):
        if not a.get("content"):
            continue
        if a["content"] in deleted_actions:
            print(f"    [SKIP] 削除済みのアクションアイテムをスキップ: {a['content'][:60]}", file=sys.stderr)
            continue
        pm_conn.execute(
            "INSERT INTO action_items"
            " (meeting_id, content, assignee, due_date, status, source, source_ref,"
            " extracted_at, milestone_id, channel_id)"
            " VALUES (?, ?, ?, ?, 'open', 'slack', ?, ?, ?, ?)",
            (None, a["content"], normalize_assignee(a.get("assignee")), a.get("due_date"),
             source_ref, post_date, a.get("milestone_id"), channel_id),
        )
        a_count += 1

    return d_count, a_count


# --------------------------------------------------------------------------- #
# 抽出済み一覧表示
# --------------------------------------------------------------------------- #
def cmd_list_extractions(slack_conn, pm_conn, channel_id: str, since: str | None, log=print) -> None:
    se_query = "SELECT thread_ts, extracted_at FROM slack_extractions WHERE channel_id = ?"
    se_params: list = [channel_id]
    if since:
        se_query += " AND extracted_at >= ?"
        se_params.append(since)

    se_rows = pm_conn.execute(se_query, se_params).fetchall()

    ts_map: dict[str, str] = {}
    if se_rows:
        placeholders = ",".join("?" * len(se_rows))
        ts_rows = slack_conn.execute(
            f"SELECT thread_ts, timestamp FROM messages WHERE channel_id = ? AND thread_ts IN ({placeholders})",
            [channel_id] + [r["thread_ts"] for r in se_rows],
        ).fetchall()
        ts_map = {r["thread_ts"]: r["timestamp"] for r in ts_rows}

    sorted_rows = sorted(se_rows, key=lambda r: ts_map.get(r["thread_ts"], r["extracted_at"]))

    log(f"抽出済みスレッド一覧（チャンネル: {channel_id}）")
    log("─" * 50)
    for i, row in enumerate(sorted_rows, 1):
        ts = (ts_map.get(row["thread_ts"]) or "")[:19]
        extracted = (row["extracted_at"] or "")[:19]
        log(f"[{i:3d}] {ts}  抽出: {extracted}")
    log(f"合計: {len(sorted_rows)} 件")


# --------------------------------------------------------------------------- #
# プラグインクラス
# --------------------------------------------------------------------------- #
class SlackIngestPlugin:
    source_name = "slack"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--slack-channel", default=DEFAULT_CHANNEL,
            metavar="CHANNEL_ID",
            help="対象チャンネルID（slack ソース用、未指定時は環境変数 PM_DEFAULT_SLACK_CHANNEL）",
        )
        parser.add_argument(
            "--slack-db", default=None,
            metavar="PATH",
            help="Slack DB のパス（slack ソース用、省略時は data/slack.db）",
        )
        parser.add_argument(
            "--slack-force-reextract", action="store_true",
            help="抽出済みスレッドも再処理（slack ソース用）",
        )
        parser.add_argument(
            "--slack-list", action="store_true",
            help="抽出済みスレッドの一覧を表示して終了（slack ソース用）",
        )
        parser.add_argument(
            # 既定 1。2026-07-26 A/B（ランダム15件全一致・狙い撃ち10件で N=1 が
            # 100% 勝ち+引き分け、N=3 は実在アイテムを多数決で落とす事例あり）により変更。
            # 旧構成は --slack-consensus 3
            "--slack-consensus", type=int, default=1, metavar="N",
            help="Self-Consistency サンプリング数（既定 1。2026-07-26 A/B（ランダム15件"
                 "全一致・狙い撃ち10件で N=1 が100%%勝ち+引き分け、N=3 は実在アイテムを"
                 "多数決で落とす事例あり）により変更。旧構成は --slack-consensus 3。"
                 "N>=2 で Self-Consistency 有効）",
        )
        parser.add_argument(
            "--slack-consensus-threshold", type=float, default=0.78, metavar="FLOAT",
            help="Self-Consistency クラスタリングの cosine 閾値（デフォルト: 0.78）",
        )
        parser.add_argument(
            "--slack-consensus-min-vote", type=int, default=None, metavar="INT",
            help="Self-Consistency クラスタ採用に必要な独立票数（デフォルト: ⌈N/2⌉）",
        )
        parser.add_argument(
            "--slack-no-triage", action="store_true",
            help="トリアージ（抽出候補の2次審査）を無効化（デフォルト: 有効）",
        )
        parser.add_argument(
            "--slack-triage-mode", choices=["two_stage", "integrated"],
            default=DEFAULT_TRIAGE_MODE,
            help="トリアージ方式: integrated=抽出プロンプトに3ゲートを統合し1回で実行（既定）/ "
                 "two_stage=抽出後に別LLM呼び出しで審査（旧方式）",
        )

    def run(self, args: argparse.Namespace, ctx: IngestContext) -> None:
        channel_id = args.slack_channel
        slack_db_path = (
            Path(args.slack_db) if args.slack_db
            else ctx.repo_root / "data" / "slack.db"
        )

        slack_conn = open_slack_db(slack_db_path, no_encrypt=ctx.no_encrypt)
        ensure_slack_extractions(ctx.pm_conn)

        if getattr(args, "slack_list", False):
            cmd_list_extractions(slack_conn, ctx.pm_conn, channel_id, ctx.since, log=ctx.log)
            slack_conn.close()
            return

        ctx.log(f"[INFO] チャンネル  : {channel_id}")
        ctx.log(f"[INFO] Slack DB    : {slack_db_path}")
        if ctx.since:
            ctx.log(f"[INFO] since       : {ctx.since}")

        context = load_context_from_claude_md()
        milestones = fetch_milestones(ctx.pm_conn)
        ctx.log(f"[INFO] マイルストーン: {len(milestones)} 件")

        threads = fetch_threads(slack_conn, channel_id, ctx.since)
        ctx.log(f"[INFO] 対象スレッド: {len(threads)} 件")

        total_d = total_a = skipped = 0
        force_reextract = ctx.force or getattr(args, "slack_force_reextract", False)
        # 既定 1（2026-07-26 A/B により 3→1。旧構成は --slack-consensus 3）
        consensus_n = getattr(args, "slack_consensus", 1)
        consensus_threshold = getattr(args, "slack_consensus_threshold", 0.78)
        consensus_min_vote = getattr(args, "slack_consensus_min_vote", None)
        no_triage = getattr(args, "slack_no_triage", False)
        triage_mode = getattr(args, "slack_triage_mode", DEFAULT_TRIAGE_MODE)
        if not no_triage:
            if triage_mode == "integrated":
                ctx.log("[INFO] トリアージ有効: 2次審査を integrated（抽出プロンプトに統合）で実行")
            else:
                ctx.log("[INFO] トリアージ有効: 2次審査を two_stage で実行")
        else:
            ctx.log("[INFO] トリアージ無効: --slack-no-triage")
            if triage_mode == "integrated":
                ctx.log("[WARN] --slack-no-triage 指定のため --slack-triage-mode は無視されます")
        if consensus_n >= 2:
            ctx.log(
                f"[INFO] Self-Consistency 有効: N={consensus_n}, "
                f"threshold={consensus_threshold}, min_vote={consensus_min_vote or '⌈N/2⌉'}"
            )

        # 第2系統（独立系統）の run 全体カウンタ（max_flagged_per_run の上限判定に使う）。
        # R8 / Phase 4: docs/security-architecture.md 参照。
        second_opinion_state: dict = {}

        for i, row in enumerate(threads, 1):
            ts = row["thread_ts"]
            if not force_reextract and is_already_extracted(ctx.pm_conn, ts, channel_id):
                skipped += 1
                continue

            ctx.log(f"\n[{i}/{len(threads)}] {row.get('user_name')} ({row.get('timestamp', '')[:16]})")

            if ctx.dry_run:
                ctx.log("  [INFO] --dry-run のため LLM呼び出し・DB保存をスキップしました")
                skipped += 1
                continue

            try:
                extracted = extract_from_thread(
                    row, context, milestones, ctx.repo_root,
                    consensus_n=consensus_n,
                    consensus_threshold=consensus_threshold,
                    consensus_min_vote=consensus_min_vote,
                    enable_triage=not no_triage,
                    triage_mode=triage_mode,
                )
            except Exception as e:
                ctx.log(f"  [WARN] 抽出失敗: {e}")
                continue

            try:
                apply_second_opinion_extraction(
                    row["thread_text"], extracted, milestones,
                    context_note=context, conn=ctx.pm_conn, log=ctx.log,
                    state=second_opinion_state,
                )
            except Exception as e:
                ctx.log(f"  [WARN] 第2系統抽出の適用に失敗（主系統の抽出結果は影響を受けません）: {e}")

            d_count = len(extracted.get("decisions", []))
            a_count = len(extracted.get("action_items", []))

            if d_count == 0 and a_count == 0:
                ctx.log("  → 決定事項・アクションアイテムなし")
            else:
                for d in extracted.get("decisions", []):
                    ctx.log(f"  [決定] {d['content']}")
                for a in extracted.get("action_items", []):
                    assignee = a.get("assignee") or "未定"
                    due = f" (期限: {a['due_date']})" if a.get("due_date") else ""
                    ctx.log(f"  [AI  ] [{assignee}] {a['content']}{due}")

            nd, na = save_slack_items(
                ctx.pm_conn, ts, channel_id,
                row.get("permalink"), row.get("timestamp", ""), extracted,
            )
            mark_extracted(ctx.pm_conn, ts, channel_id)
            ctx.pm_conn.commit()
            total_d += nd
            total_a += na

        slack_conn.close()

        ctx.log("\n" + "=" * 60)
        ctx.log(f"完了: decisions={total_d}件, action_items={total_a}件, スキップ={skipped}件")
