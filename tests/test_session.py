import sqlite3

import pytest

from faffmonkey.runtime.session import SCHEMA_VERSION, SessionStore
from faffmonkey.types import ToolCall


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


class TestWALMode:
    def test_wal_mode_enabled(self, db_path):
        store = SessionStore(db_path)
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        store.close()
        assert mode == "wal"

    def test_busy_timeout_set(self, db_path):
        store = SessionStore(db_path)
        timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        store.close()
        assert timeout == 5000


class TestSchemaVersioning:
    def test_schema_version_set(self, store, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        conn.close()
        assert row[0] == SCHEMA_VERSION

    def test_reopening_does_not_duplicate_version(self, db_path):
        s1 = SessionStore(db_path)
        s1.close()
        s2 = SessionStore(db_path)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        conn.close()
        s2.close()
        assert len(rows) == 1
        assert rows[0][0] == SCHEMA_VERSION


class TestSessionCRUD:
    def test_create_main_session(self, store):
        session = store.get_or_create_main_session("cli")
        assert session.type == "main"
        assert session.channel_id == "cli"
        assert session.active is True
        assert session.id

    def test_get_or_create_returns_existing(self, store):
        s1 = store.get_or_create_main_session("cli")
        s2 = store.get_or_create_main_session("cli")
        assert s1.id == s2.id

    def test_different_channels_get_different_sessions(self, store):
        s1 = store.get_or_create_main_session("cli")
        s2 = store.get_or_create_main_session("telegram")
        assert s1.id != s2.id


class TestMessagePersistence:
    def test_append_and_retrieve(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "hello")
        store.append_message(session.id, "assistant", "hi there")

        history = store.get_history(session.id)
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "hello"
        assert history[1].role == "assistant"
        assert history[1].content == "hi there"

    def test_append_with_tool_calls(self, store):
        session = store.get_or_create_main_session("cli")
        calls = [ToolCall(id="tc1", name="shell_exec", arguments={"cmd": "ls"})]
        store.append_message(session.id, "assistant", "running command", tool_calls=calls)

        history = store.get_history(session.id)
        assert len(history) == 1
        assert history[0].tool_calls is not None
        assert len(history[0].tool_calls) == 1
        assert history[0].tool_calls[0].id == "tc1"
        assert history[0].tool_calls[0].name == "shell_exec"
        assert history[0].tool_calls[0].arguments == {"cmd": "ls"}

    def test_append_with_tool_call_id(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "tool", "file listing", tool_call_id="tc1")

        history = store.get_history(session.id)
        assert len(history) == 1
        assert history[0].tool_call_id == "tc1"

    def test_history_ordered_by_timestamp(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "first")
        store.append_message(session.id, "assistant", "second")
        store.append_message(session.id, "user", "third")

        history = store.get_history(session.id)
        contents = [m.content for m in history]
        assert contents == ["first", "second", "third"]

    def test_messages_isolated_between_sessions(self, store):
        s1 = store.get_or_create_main_session("cli")
        s2 = store.get_or_create_main_session("telegram")
        store.append_message(s1.id, "user", "cli msg")
        store.append_message(s2.id, "user", "telegram msg")

        assert len(store.get_history(s1.id)) == 1
        assert store.get_history(s1.id)[0].content == "cli msg"
        assert len(store.get_history(s2.id)) == 1
        assert store.get_history(s2.id)[0].content == "telegram msg"


class TestSessionResume:
    def test_resume_after_reopen(self, db_path):
        s1 = SessionStore(db_path)
        session = s1.get_or_create_main_session("cli")
        s1.append_message(session.id, "user", "before restart")
        s1.append_message(session.id, "assistant", "noted")
        original_id = session.id
        s1.close()

        s2 = SessionStore(db_path)
        resumed = s2.get_or_create_main_session("cli")
        assert resumed.id == original_id
        history = s2.get_history(resumed.id)
        assert len(history) == 2
        assert history[0].content == "before restart"
        s2.close()

class TestDeactivation:
    def test_deactivate_session(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "hello")
        store.deactivate_session(session.id)

        new_session = store.get_or_create_main_session("cli")
        assert new_session.id != session.id
        assert len(store.get_history(new_session.id)) == 0

    def test_deactivated_session_history_preserved(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "preserved")
        store.deactivate_session(session.id)

        history = store.get_history(session.id)
        assert len(history) == 1
        assert history[0].content == "preserved"

    def test_new_command_deactivates_and_creates(self, store):
        s1 = store.get_or_create_main_session("cli")
        store.append_message(s1.id, "user", "old")

        store.deactivate_session(s1.id)
        s2 = store.get_or_create_main_session("cli")

        assert s2.id != s1.id
        assert len(store.get_history(s2.id)) == 0
        assert len(store.get_history(s1.id)) == 1


class TestDeleteAllMessagesTransaction:
    def test_delete_is_atomic(self, store):
        session = store.get_or_create_main_session("cli")
        for i in range(5):
            store.append_message(session.id, "user", f"msg {i}")
        assert store.message_count(session.id) == 5
        store.delete_all_messages(session.id)
        assert store.message_count(session.id) == 0

    def test_delete_maintains_in_transaction_flag(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "msg")
        assert store._in_transaction is False
        store.delete_all_messages(session.id)
        assert store._in_transaction is False

    def test_delete_within_caller_transaction_preserves_flag(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "msg")
        store.begin()
        assert store._in_transaction is True
        store.delete_all_messages(session.id)
        assert store._in_transaction is True
        store.commit()
        assert store._in_transaction is False


class TestCallerManagedTransaction:
    def test_begin_commit_groups_operations(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "before")
        store.begin()
        store.delete_all_messages(session.id)
        store.append_message(session.id, "system", "summary")
        store.append_message(session.id, "user", "recent")
        store.commit()
        history = store.get_history(session.id)
        assert len(history) == 2
        assert history[0].content == "summary"

    def test_rollback_reverts_all(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "keeper")
        store.begin()
        store.delete_all_messages(session.id)
        store.append_message(session.id, "system", "should vanish")
        store.rollback()
        history = store.get_history(session.id)
        assert len(history) == 1
        assert history[0].content == "keeper"


class _FailingConn:
    """Wraps a real sqlite3.Connection, raising on a target SQL statement."""

    def __init__(self, real_conn: sqlite3.Connection, fail_on: str, *, substring: bool = False) -> None:
        self._real = real_conn
        self._fail_on = fail_on
        self._substring = substring

    def execute(self, sql, *args, **kwargs):
        match = self._fail_on in sql if self._substring else sql == self._fail_on
        if match:
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestTransactionFlagClearedOnFailure:
    def test_commit_failure_clears_in_transaction(self, store):
        store.get_or_create_main_session("cli")
        store.begin()
        assert store._in_transaction is True

        real_conn = store._conn
        store._conn = _FailingConn(real_conn, "COMMIT")
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            store.commit()

        assert store._in_transaction is False

        store._conn = real_conn
        store._conn.execute("ROLLBACK")

    def test_rollback_failure_clears_in_transaction(self, store):
        store.get_or_create_main_session("cli")
        store.begin()
        assert store._in_transaction is True

        real_conn = store._conn
        store._conn = _FailingConn(real_conn, "ROLLBACK")
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            store.rollback()

        assert store._in_transaction is False
        store._conn = real_conn

    def test_autocommit_resumes_after_failed_commit(self, store):
        session = store.get_or_create_main_session("cli")
        store.append_message(session.id, "user", "before transaction")

        store.begin()
        store.append_message(session.id, "user", "in transaction")

        real_conn = store._conn
        store._conn = _FailingConn(real_conn, "COMMIT")
        with pytest.raises(sqlite3.OperationalError):
            store.commit()

        store._conn = real_conn
        store._conn.execute("ROLLBACK")

        store.append_message(session.id, "user", "after failed commit")

        history = store.get_history(session.id)
        contents = [m.content for m in history]
        assert "before transaction" in contents
        assert "after failed commit" in contents


class TestCrossThreadSafety:
    def test_two_threads_persist_messages(self, db_path):
        import threading

        # Pre-create the DB so threads don't contend on schema init.
        init_store = SessionStore(db_path)
        init_store.close()

        errors: list[Exception] = []

        def _writer(channel_id: str, count: int) -> None:
            try:
                s = SessionStore(db_path)
                s._conn.execute("PRAGMA busy_timeout = 2000")
                try:
                    session = s.get_or_create_main_session(channel_id)
                    for i in range(count):
                        s.append_message(session.id, "user", f"{channel_id} msg {i}")
                finally:
                    s.close()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_writer, args=("telegram", 20))
        t2 = threading.Thread(target=_writer, args=("discord", 20))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == [], f"thread errors: {errors}"

        verify = SessionStore(db_path)
        try:
            tg = verify.get_or_create_main_session("telegram")
            dc = verify.get_or_create_main_session("discord")
            assert len(verify.get_history(tg.id)) == 20
            assert len(verify.get_history(dc.id)) == 20
        finally:
            verify.close()

    def test_close_one_store_other_still_works(self, db_path):
        store_a = SessionStore(db_path)
        store_b = SessionStore(db_path)

        session_a = store_a.get_or_create_main_session("chan-a")
        session_b = store_b.get_or_create_main_session("chan-b")

        store_a.append_message(session_a.id, "user", "from a")
        store_a.close()

        store_b.append_message(session_b.id, "user", "from b")
        history = store_b.get_history(session_b.id)
        assert len(history) == 1
        assert history[0].content == "from b"
        store_b.close()


