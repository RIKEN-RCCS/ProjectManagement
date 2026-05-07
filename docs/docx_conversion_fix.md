# Docx変換失敗の修正レポート

## 問題の概要

**症状**: `F_20250909_Minute_Project_regular_meeting_en.docx`（3.18 MB）の変換が失敗
- ファイルのダウンロード: ✅ 成功
- LibreOffice変換: ❌ **失敗**（変換結果が空）

**報告日時**: 2026-05-07

## 根本原因の特定

### XML検証エラー

変換プロセスから以下のエラーメッセージが出ていました:

```
Entity: line 3: parser error : xmlSAX2Characters: huge text node
kvLEvgl0WqskNLv0/cxtWHTE+ygsMMVmELno/cQqfcJqRmEzfsRbfsVbTZ6GZ74BBWbk6KQ9xOaXUphV
Entity: line 3: parser error : Extra content at the end of the document
Error: Please verify input parameters..
```

### 原因分析

1. **ファイルの特性**: 
   - 埋め込み画像やBase64エンコードデータを大量に含む（3.18 MB）
   - XHTML形式での厳密なXML検証が失敗

2. **既存コードの問題**:
   ```python
   # 修正前
   subprocess.check_call(
       ["libreoffice", ..., "--convert-to", "html:XHTML Writer File:UTF8", ...],
       timeout=120,
       stdout=subprocess.DEVNULL,  # ← エラーメッセージが捨てられている
       stderr=subprocess.DEVNULL,
   )
   ```
   - LibreOfficeの出力（stdout/stderr）が完全に捨てられていた
   - 失敗の診断が不可能だった
   - タイムアウトが120秒に限定（複雑なファイルでは不足）

## 実装した修正

### 修正1: LibreOffice出力のキャプチャ（行343-362）

**修正前**:
```python
subprocess.check_call(..., stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
```

**修正後**:
```python
result = subprocess.run(
    [...],
    timeout=300,  # 120秒 → 300秒に延長
    capture_output=True,  # stdout/stderrをキャプチャ
    text=True,
)
if result.returncode != 0:
    logger.warning(
        f"LibreOffice変換失敗 (code={result.returncode}): {path.name}\n"
        f"STDOUT: {result.stdout[:500]}\n"
        f"STDERR: {result.stderr[:500]}"
    )
```

**効果**:
- 変換失敗時の詳細なエラーメッセージをログに記録
- root cause analysis が可能に

### 修正2: 変換結果の妥当性チェック（行357-360）

```python
# 変換結果の妥当性チェック
if len(md.strip()) < 50:
    logger.warning(f"LibreOffice変換結果が短すぎます ({len(md)}文字): {path.name}")
    return None
```

**効果**:
- 変換は成功してもコンテンツが空の場合を検出
- 無駄なDB書き込みを防止

### 修正3: HTML形式フォールバック（行274-285）

**修正前**:
```python
def _convert_docx(path: Path) -> tuple[str, str]:
    md = _libreoffice_to_html_to_md(path, "html:XHTML Writer File:UTF8")
    if md:
        return md, "libreoffice_html"
    return "", "failed"
```

**修正後**:
```python
def _convert_docx(path: Path) -> tuple[str, str]:
    # 最初にXHTML形式で試行（従来の形式）
    md = _libreoffice_to_html_to_md(path, "html:XHTML Writer File:UTF8")
    if md:
        return md, "libreoffice_html_xhtml"

    # XHTML失敗時は標準HTML形式で再試行（埋め込みデータが多いファイル対応）
    logger.info(f"    XHTML変換失敗、標準HTML形式で再試行: {path.name}")
    md = _libreoffice_to_html_to_md(path, "html:HTML")
    if md:
        return md, "libreoffice_html_standard"

    return "", "failed"
```

**効果**:
- XHTML形式（厳密な検証）で失敗 → HTML標準形式（緩い検証）へ自動リトライ
- 埋め込みデータが多いファイルでも対応可能
- 変換方式の記録により、後から変換品質を分析可能

### 修正4: タイムアウト延長（行349）

```python
timeout=300  # 120秒 → 300秒（5分に延長）
```

**効果**:
- 大容量・複雑なdocxファイルの変換成功率が向上
- 300秒以内で収まるファイルはほぼ対応可能

### 修正5: デバッグモード（新規機能）

`--debug-convert` オプションを追加:

```bash
python3 scripts/pm_document_content.py --debug-convert /path/to/file.docx
```

**機能**:
- 特定ファイルの変換をテスト実行
- 詳細なプレビュー表示（500文字）
- docx, xlsx, pptx, pdf に対応

## テスト結果

### 問題ファイルの変換結果

**F_20250909_Minute_Project_regular_meeting_en.docx**:

| 段階 | 変換形式 | 結果 | 詳細 |
|---|---|---|---|
| 1次 | XHTML Writer File:UTF8 | ❌ 失敗 | XMLパーサーエラー（huge text node） |
| 2次 | HTML 標準形式 | ✅ **成功** | **19,678 文字の議事録を正常に抽出** |

**メタデータ**:
```
- ファイルサイズ: 3.18 MB
- 変換方式: libreoffice_html_standard
- 抽出文字数: 19,678
- 処理時間: 約30秒（300秒タイムアウト内）
```

### 先行実行結果

修正版で全体変換を実行した結果（2026-05-07 08:39時点）:
- 処理済みファイル: 72件
- 成功: 72件（100%）
- 含む docx ファイル複数（F_20250909_Minute_Project_regular_meeting_en含む）

## 変更履歴

**コミット**: c8e1606
```
fix: docx変換失敗の診断・修正（HTML標準形式フォールバック対応）
```

**修正ファイル**: `scripts/pm_document_content.py`
- 行274-285: `_convert_docx()` 関数（HTML形式フォールバック追加）
- 行343-376: `_libreoffice_to_html_to_md()` 関数（出力キャプチャ・詳細ログ・タイムアウト延長）
- 行945-988: `debug_convert_file()` 関数（デバッグモード実装）
- argparse: `--debug-convert` オプション追加

## 今後の対応

### 運用レベル

1. **定期的なモニタリング**:
   - 変換失敗ファイルのログ監視
   - 変換方式の統計集計（XHTML vs HTML標準）

2. **手動テスト**:
   新しいdocxファイル形式が追加された場合:
   ```bash
   python3 scripts/pm_document_content.py --debug-convert /path/to/new.docx
   ```

### 改善案（将来検討）

- **python-docx ライブラリの追加導入**: HTMLパース不要で直接読込（ただし、既存環境への影響を最小化するため現時点では見送り）
- **セマンティック解析**: 抽出したMarkdownの構造化（テーブル・リスト・見出しの自動認識精度向上）

## 参考資料

### 関連するデバッグコマンド

```bash
# 修正版での全体変換
python3 scripts/pm_document_content.py --convert --workers 1 --force

# 特定ファイルのデバッグ
python3 scripts/pm_document_content.py --debug-convert /tmp/F_20250909_Minute_Project_regular_meeting_en.docx

# FTS5インデックスへの組み込み（変換完了後）
python3 scripts/pm_embed.py

# /argus-investigate での検索テスト（Slack）
/argus-investigate F_20250909_Minute に関する内容は？
```

### アーキテクチャ参考

- **変換パイプライン**: `pm_document_content.py` → `box_docs.db` (SQLCipher暗号化) → `pm_embed.py` → FTS5インデックス
- **情報源**: `/argus-investigate`, `/argus-brief` で検索可能
- **検索基盤**: FTS5（形態素解析+トライグラムハイブリッド検索）

---

**修正完了日**: 2026-05-07  
**作成者**: Claude Code Haiku 4.5
