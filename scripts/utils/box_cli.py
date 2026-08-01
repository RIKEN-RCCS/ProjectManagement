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


# テキストとして検査できる拡張子。これ以外（xlsx/pptx/mp4 等）は中身を見ない。
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}


def _guard_box_payload(local_path, folder_id: str, filename: str) -> None:
    """Box アップロードの送信前検査。テキストファイルのみ中身を見る。

    **バイナリを「検査した」と記録しない。** 検査できていないものを通過扱いにすると、
    後から「ブローカーを通った」という誤った根拠に使われる（P10）。
    """
    from pathlib import Path as _Path

    path = _Path(local_path)
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    try:
        from utils.slack_post import guard_outbound_text
    except Exception:
        return
    conn = None
    try:
        from db_utils import open_db
        conn = open_db(_Path(__file__).resolve().parents[2] / "data" / "pm.db", encrypt=True)
    except Exception:
        pass
    try:
        guard_outbound_text(content, transport="box", dest=f"{folder_id}/{filename}", conn=conn)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def box_upload_or_version(
    local_path: Path, folder_id: str, filename: str,
    log: Callable[[str], None],
) -> str:
    """新規アップロード or 既存ファイルへのバージョン更新。file_id を返す。

    **送信前にテキストファイルの中身を検査する**（docs/security-architecture.md §4.2）。
    Box は単一ファネルなので、ここ1箇所で 9 モジュールの呼び出しを覆える。
    xlsx / pptx のようなバイナリは中身を検査できない — その場合は**生成元テキストの
    検査をもって代える**（生成側の責務）。
    """
    _guard_box_payload(local_path, folder_id, filename)
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


def box_get_file_modified_at(file_id: str, log: Callable[[str], None]) -> str | None:
    """Box ファイルの modified_at（ISO8601）を取得する。取得失敗時は None（warning ログ付き）。"""
    try:
        info = box_json(
            ["box", "files:get", file_id, "--json", "--fields", "modified_at"],
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, json.JSONDecodeError) as e:
        log(f"  [BOX] modified_at 取得失敗 (file_id={file_id}): {e}")
        return None
    if isinstance(info, list):
        info = info[0] if info else {}
    return info.get("modified_at")


# 共有リンクの目標アクセス範囲（招待された collaborator のみ）。
# セキュリティ上の理由でこの値をマジックストリングとして散らさないこと（docs/security-architecture.md §1.2）。
BOX_SHARED_LINK_ACCESS = "collaborators"


def _box_share(file_id: str, log: Callable[[str], None]) -> str:
    """`box files:share` を BOX_SHARED_LINK_ACCESS で実行し、shared_link.url を返す。"""
    try:
        info = box_json(
            ["box", "files:share", file_id, "--access", BOX_SHARED_LINK_ACCESS, "--json"],
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"共有リンク作成 (box files:share) に失敗しました: file_id={file_id}: {e}") from e
    if isinstance(info, list):
        info = info[0] if info else {}
    shared = info.get("shared_link") or {}
    url = shared.get("url")
    if not url:
        raise RuntimeError(f"共有リンク作成のレスポンスに url がありません: {info}")
    access = shared.get("access")
    effective_access = shared.get("effective_access")
    if access != BOX_SHARED_LINK_ACCESS:
        # Box テナントのポリシーにより access が降格されることは正常系でありうるため例外にはしない。
        log(
            f"  [BOX][WARN] 共有リンクの access が {BOX_SHARED_LINK_ACCESS} になりませんでした"
            f" (file_id={file_id}, access={access}, effective_access={effective_access})"
        )
    else:
        log(f"  [BOX] 共有リンク access={access} effective_access={effective_access} (file_id={file_id})")
    return url


def box_get_or_create_shared_link(file_id: str, log: Callable[[str], None]) -> str:
    """ファイルの共有リンクを取得（なければ作成）。URL を返す。

    既存リンクが見つかった場合でも BOX_SHARED_LINK_ACCESS でなければ貼り直して正規化する。
    """
    info = box_json(
        ["box", "files:get", file_id, "--json", "--fields", "shared_link"],
        timeout=60,
    )
    if isinstance(info, list):
        info = info[0] if info else {}
    shared = info.get("shared_link")
    if shared and shared.get("url"):
        if shared.get("access") == BOX_SHARED_LINK_ACCESS:
            return shared["url"]
        log(f"  [BOX] 既存リンクを {BOX_SHARED_LINK_ACCESS} に正規化: file {file_id}")
        return _box_share(file_id, log)

    log(f"  [BOX] 共有リンクを作成: file {file_id}")
    return _box_share(file_id, log)
