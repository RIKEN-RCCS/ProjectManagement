## Slack Canvas API 調査結果

調査日: 2026-03 (`canvas_debug.py` 開発時)、2026-03 追記（バッチ削除・セクションID取得方法）

---

### Canvas の種類

| 種類 | 作成API | 用途 |
|---|---|---|
| スタンドアロンCanvas | `canvases.create` | チャンネルに紐づかない独立Canvas |
| チャンネルCanvasタブ | `conversations.canvases.create` | チャンネルのタブに表示されるCanvas |

**重要**: この2つは別物。間違えると Canvas が2つ作成される。

---

### チャンネルタブの格納場所

`conversations.info` → `channel.properties` 以下に格納。**`bookmarks.list` は Canvas タブを返さない**。

| フィールド | 内容 |
|---|---|
| `properties.tabs` | タブ一覧。各要素に `id`（`Ct0...` 形式）・`type`・`data.file_id`・`label` |
| `properties.tabz` | `tabs` の別形式（`id` なし項目あり）。実態は同じ |
| `properties.meeting_notes` | チャンネル固定Canvas（`file_id`）。`type=channel_canvas` タブに対応 |

デフォルトタブ（`type=files`, `type=workflows` 等）は `file_id` が空で操作不可。

確認コマンド:
```sh
python3 scripts/canvas_debug.py -c CHANNEL_ID --show-bookmarks
```

---

### Canvas セクション構造

- セクションIDは HTML の `id='...'` 属性（`h1`/`p`/`div`/`table` 等のタグ上）
- `canvases_sections_lookup` はキーワード検索のみ。全件列挙**不可**。キーワードによって漏れが出るため信頼性が低い
- **推奨**: `files_info` → `url_private` の HTML（filetype=quip）をダウンロードして正規表現で ID を全件抽出する
- 空の `<table>` タグには `id` 属性がなく、セクションAPIで削除**不可**
- バッチ削除は以下の3形式すべて**失敗** → 1件ずつ削除必須

| バッチ削除の試み | 結果 |
|---|---|
| `changes=[op1, op2]`（複数 operation） | ❌ `invalid_arguments` |
| `changes=[{"operation": "delete", "section_id": [sid1, sid2]}]` | ❌ 失敗 |
| `changes=[{"operation": "delete", "section_ids": [sid1, sid2]}]` | ❌ 失敗 |

検証コマンド:
```sh
python3 scripts/canvas_debug.py --canvas-id CANVAS_ID --test-batch-delete
```

#### セクションID取得の実装パターン

```python
import re, urllib.request

_PAT_TAG_WITH_ID = re.compile(
    r"<(h[1-6]|p|div|ul|ol|li|blockquote|pre|hr|table|tbody|thead|tr|td|th)\b[^>]*\sid=['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

def collect_section_ids(client, canvas_id, include_h1=False):
    resp = client.files_info(file=canvas_id)
    url = resp["file"].get("url_private", "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        html = r.read().decode("utf-8", errors="replace")

    seen, ids = set(), []
    for m in _PAT_TAG_WITH_ID.finditer(html):
        tag, sid = m.group(1).lower(), m.group(2)
        if sid not in seen and (include_h1 or tag != "h1"):
            seen.add(sid); ids.append(sid)
    return ids
```

---

### タブ操作の可否

| 操作 | API | 結果 |
|---|---|---|
| タブ付きCanvasを新規作成 | `conversations.canvases.create` | ✅ 成功。ただし既存タブは**置き換わらず追加**される |
| スタンドアロンCanvas削除 | `canvases.delete` | ✅ ファイルは消えるが `properties.tabs` のエントリは**残る（stale）** |
| 旧タブエントリ削除 | `conversations.canvases.delete` | ❌ `unknown_method`（非公開API） |
| 旧タブエントリ削除 | `bookmarks.remove(bookmark_id=Ct0...)` | ❌ 失敗（確認済み） |
| bookmarks.list でタブ取得 | `bookmarks.list` | ❌ Canvas タブは返らない（常に空） |
| conversations.info でタブ取得 | `conversations.info` | ✅ `properties.tabs` で全タブ取得可能 |

---

### `--recreate` の正しいフロー（チャンネルタブCanvasの場合）

```
1. conversations.info で properties.tabs を確認 → old_tab_id (Ct0...) を記録
2. canvases.delete(old_canvas_id)          → ファイル削除（タブエントリは残る）
3. conversations.canvases.create()         → 新Canvas作成 + 新タブ追加
4. bookmarks.remove(bookmark_id=old_tab_id) → 旧タブ削除を試みる
   └─ 失敗した場合: 手動削除（タブ上で右クリック → タブを閉じる）
5. canvas_map.json を新IDで更新
```

---

### 必要なスコープ（User Token xoxp-）

| スコープ | 用途 |
|---|---|
| `channels:read` または `groups:read` | `conversations.info` でタブ情報取得 |
| `bookmarks:read` | `bookmarks.list`（Canvas タブには効かないが取得は可） |
| `bookmarks:write` | `bookmarks.remove` でタブ削除を試みる |
| `canvases:write` | `canvases.delete`, `canvases.edit` 等 |

---

### canvas_map.json

チャンネルID → Canvas ID のマッピングを `data/canvas_map.json` で管理。`--recreate` 時に自動更新。

```json
{"C0A9KG036CS": {"canvas_id": "F0AN1RTL003", "title": "Summary", "updated_at": "..."}}
```

操作コマンド:
```sh
# 登録・確認・再作成
python3 scripts/canvas_debug.py -c CHANNEL_ID --register CANVAS_ID
python3 scripts/canvas_debug.py --list-map
python3 scripts/canvas_debug.py -c CHANNEL_ID --recreate
python3 scripts/canvas_debug.py -c CHANNEL_ID --recreate --add-tab   # タブがなかった場合に新規追加
```
