"""Box CLI 呼び出しの共通ユーティリティ。

Box CLI (`box` コマンド) を subprocess 経由で実行するヘルパー群。
pm_xlsx_report / pm_xlsx_sync / pm_minutes_catalog / pm_minutes_publish 等で共有する。
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path


def box_json(cmd: list[str], timeout: int = 120):
    """Box CLI コマンドを JSON 出力モードで実行し、パース結果を返す。"""
    raw = subprocess.check_output(cmd, text=True, timeout=timeout)
    return json.loads(raw)


# 後方互換用のプライベートエイリアス
_box_json = box_json


def box_find_file(folder_id: str, filename: str) -> str | None:
    """folder_id 直下に filename と完全一致するファイルがあれば file_id を返す。"""
    items = box_json(
        ["box", "folders:items", folder_id, "--json", "--fields", "name,type"],
        timeout=60,
    )
    for item in items:
        if item.get("type") == "file" and item.get("name") == filename:
            return str(item.get("id"))
    return None


def box_download(file_id: str, dest: Path, log: Callable[[str], None]) -> None:
    """Box ファイルをローカルパスにダウンロードする。"""
    log(f"  [BOX] ダウンロード: file_id={file_id} → {dest}")
    subprocess.check_call(
        ["box", "files:download", file_id, "--destination", str(dest.parent),
         "--save-as", dest.name, "--overwrite"],
        timeout=300,
    )


def box_upload_or_version(
    local_path: Path, folder_id: str, filename: str,
    log: Callable[[str], None],
) -> str:
    """新規アップロード or 既存ファイルへのバージョン更新。file_id を返す。"""
    existing = box_find_file(folder_id, filename)
    if existing:
        log(f"  [BOX] 既存ファイル (id={existing}) のバージョン更新: {filename}")
        box_json(
            ["box", "files:versions:upload", existing, str(local_path), "--json"],
            timeout=300,
        )
        return existing

    log(f"  [BOX] 新規アップロード: {filename} → folder {folder_id}")
    info = box_json(
        ["box", "files:upload", str(local_path),
         "--parent-id", folder_id, "--name", filename, "--json"],
        timeout=300,
    )
    if isinstance(info, list):
        info = info[0] if info else {}
    file_id = str(info.get("id", ""))
    if not file_id:
        raise RuntimeError(f"box files:upload のレスポンスに id がありません: {info}")
    return file_id


def box_get_or_create_shared_link(file_id: str, log: Callable[[str], None]) -> str:
    """ファイルの共有リンクを取得（なければ作成）。URL を返す。"""
    info = box_json(
        ["box", "files:get", file_id, "--json", "--fields", "shared_link"],
        timeout=60,
    )
    if isinstance(info, list):
        info = info[0] if info else {}
    shared = info.get("shared_link")
    if shared and shared.get("url"):
        return shared["url"]

    log(f"  [BOX] 共有リンクを作成: file {file_id}")
    info = box_json(
        ["box", "files:share", file_id, "--access", "open", "--json"],
        timeout=60,
    )
    if isinstance(info, list):
        info = info[0] if info else {}
    shared = info.get("shared_link") or {}
    url = shared.get("url")
    if not url:
        raise RuntimeError(f"共有リンク作成のレスポンスに url がありません: {info}")
    return url
