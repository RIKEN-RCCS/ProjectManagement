"""slack_pipeline.py --dry-run のテスト（Slack API はフェイク、DB実接続は使わない）。

--dry-run は Slack API 取得は通常どおり行うが、slack.db への UPSERT/commit を
一切行わないことを検証する（過去に --dry-run が未配線でDBに書き込まれるバグがあった）。
"""
import slack_pipeline  # scripts/data-pipeline は tests/conftest.py で sys.path に追加済み


class _FakeClient:
    """slack_sdk.WebClient の最小フェイク（実ネットワークなし）。"""

    def __init__(self, history_pages, replies_by_ts):
        self._history_pages = history_pages
        self._replies_by_ts = replies_by_ts
        self.history_calls = 0

    def conversations_history(self, **kwargs):
        page = self._history_pages[self.history_calls]
        self.history_calls += 1
        return page

    def conversations_replies(self, channel, ts, limit=100):
        return {"messages": self._replies_by_ts.get(ts, [])}

    def users_info(self, user):
        return {"user": {"profile": {"display_name": f"user-{user}"}}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://example.slack.com/archives/{channel}/p{message_ts}"}


def _setup(monkeypatch, tmp_path):
    parent_msg = {
        "type": "message", "ts": "1700000000.000001", "user": "U1",
        "text": "テストメッセージ",
    }
    # conversations_replies は親メッセージ含めて返る（先頭がスキップされる仕様）
    reply_msg = {
        "type": "message", "ts": "1700000001.000002",
        "thread_ts": "1700000000.000001", "user": "U2",
        "text": "テスト返信",
    }
    history_page = {"messages": [parent_msg], "response_metadata": {"next_cursor": ""}}
    replies_by_ts = {"1700000000.000001": [parent_msg, reply_msg]}
    fake_client = _FakeClient([history_page], replies_by_ts)
    monkeypatch.setattr(slack_pipeline, "_make_client", lambda: fake_client)

    db_path = tmp_path / "slack_scratch.db"
    conn = slack_pipeline.init_db(str(db_path), no_encrypt=True)
    return conn


def test_dry_run_skips_all_db_writes(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)

    calls = {"upsert_message": 0, "upsert_reply": 0}
    monkeypatch.setattr(
        slack_pipeline, "db_upsert_message",
        lambda *a, **kw: calls.__setitem__("upsert_message", calls["upsert_message"] + 1),
    )
    monkeypatch.setattr(
        slack_pipeline, "db_upsert_reply",
        lambda *a, **kw: calls.__setitem__("upsert_reply", calls["upsert_reply"] + 1),
    )

    fetched = slack_pipeline.fetch_and_store(
        conn=conn, channel_id="C123", limit=100, since_date=None,
        fetch_permalink=True, dry_run=True,
    )

    assert calls["upsert_message"] == 0
    assert calls["upsert_reply"] == 0
    assert fetched == 1  # 新規スレッドとしてカウントはされる

    # DB に実際に書き込みが起きていないことも確認する
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0


def test_dry_run_logs_planned_counts(monkeypatch, tmp_path, capsys):
    conn = _setup(monkeypatch, tmp_path)

    slack_pipeline.fetch_and_store(
        conn=conn, channel_id="C123", limit=100, since_date=None,
        fetch_permalink=True, dry_run=True,
    )

    err = capsys.readouterr().err
    assert "[DRY-RUN] messages: 1件, replies: 1件（DB未書き込み）" in err


def test_normal_run_calls_db_writes(monkeypatch, tmp_path):
    """dry_run=False（既定）では従来どおり書き込み関数が呼ばれ、DBに反映される（回帰）。"""
    conn = _setup(monkeypatch, tmp_path)

    calls = {"upsert_message": 0, "upsert_reply": 0}
    orig_upsert_message = slack_pipeline.db_upsert_message
    orig_upsert_reply = slack_pipeline.db_upsert_reply

    def fake_upsert_message(*a, **kw):
        calls["upsert_message"] += 1
        return orig_upsert_message(*a, **kw)

    def fake_upsert_reply(*a, **kw):
        calls["upsert_reply"] += 1
        return orig_upsert_reply(*a, **kw)

    monkeypatch.setattr(slack_pipeline, "db_upsert_message", fake_upsert_message)
    monkeypatch.setattr(slack_pipeline, "db_upsert_reply", fake_upsert_reply)

    slack_pipeline.fetch_and_store(
        conn=conn, channel_id="C123", limit=100, since_date=None,
        fetch_permalink=True, dry_run=False,
    )

    assert calls["upsert_message"] == 1
    assert calls["upsert_reply"] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 1
