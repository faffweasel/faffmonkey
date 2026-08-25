"""Tests for faff export."""

import json
import sqlite3
from pathlib import Path


from faffmonkey.cli.export import run_export


def _init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL CHECK(type IN ('main', 'isolated')),
            channel_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- Must match SessionStore's real schema. This stand-in drifted:
        -- it lacked images, so the export reader could silently stop
        -- returning a column and every test here would still pass.
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp TEXT NOT NULL,
            images TEXT
        );
    """)
    return conn


def _seed_session(
    conn: sqlite3.Connection,
    session_id: str = "sess-1",
    session_type: str = "main",
    active: int = 1,
    channel_id: str = "cli",
    updated_at: str = "2026-01-01T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, type, channel_id, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, session_type, channel_id, active, updated_at, updated_at),
    )
    conn.commit()


def _seed_messages(conn: sqlite3.Connection, session_id: str, messages: list[dict]) -> None:
    for i, msg in enumerate(messages):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, timestamp, images) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"msg-{session_id}-{i}",
                session_id,
                msg["role"],
                msg.get("content"),
                json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else None,
                msg.get("tool_call_id"),
                f"2026-01-01T00:00:{i:02d}Z",
                json.dumps(msg["images"]) if msg.get("images") else None,
            ),
        )
    conn.commit()


class TestExportOpenaiFormat:
    def test_exports_role_and_content(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        conn.close()

        result = run_export(state_dir, "sess-1", "openai", None)
        assert result == 0

    def test_openai_format_structure(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        conn.close()

        run_export(state_dir, "sess-1", "openai", None)
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0] == {"role": "user", "content": "hello"}
        assert data[1] == {"role": "assistant", "content": "hi there"}

    def test_openai_format_includes_tool_calls(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        tc = [{"id": "tc-1", "name": "shell_exec", "arguments": {"cmd": "ls"}}]
        _seed_messages(conn, "sess-1", [
            {"role": "assistant", "content": "running", "tool_calls": tc},
            {"role": "tool", "content": "file.txt", "tool_call_id": "tc-1"},
        ])
        conn.close()

        run_export(state_dir, "sess-1", "openai", None)
        data = json.loads(capsys.readouterr().out)

        assert data[0]["tool_calls"] == tc
        assert data[1]["tool_call_id"] == "tc-1"

    def test_openai_format_null_content_becomes_empty_string(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "assistant"},
        ])
        conn.close()

        run_export(state_dir, "sess-1", "openai", None)
        data = json.loads(capsys.readouterr().out)
        assert data[0]["content"] == ""


class TestExportJsonFormat:
    def test_json_format_preserves_raw_fields(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        conn.close()

        run_export(state_dir, "sess-1", "json", None)
        data = json.loads(capsys.readouterr().out)

        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["role"] == "user"
        assert data[0]["content"] == "hello"
        assert data[0]["timestamp"] is not None

    def test_json_format_includes_null_fields(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
        ])
        conn.close()

        run_export(state_dir, "sess-1", "json", None)
        data = json.loads(capsys.readouterr().out)

        assert "tool_calls" in data[0]
        assert data[0]["tool_calls"] is None
        assert "tool_call_id" in data[0]
        assert data[0]["tool_call_id"] is None


class TestExportSessionSelection:
    def test_specific_session_by_id(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn, session_id="sess-1")
        _seed_session(conn, session_id="sess-2", updated_at="2026-01-02T00:00:00Z")
        _seed_messages(conn, "sess-1", [{"role": "user", "content": "from session 1"}])
        _seed_messages(conn, "sess-2", [{"role": "user", "content": "from session 2"}])
        conn.close()

        run_export(state_dir, "sess-1", "openai", None)
        data = json.loads(capsys.readouterr().out)

        assert len(data) == 1
        assert data[0]["content"] == "from session 1"

    def test_default_uses_active_main_session(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn, session_id="old", updated_at="2026-01-01T00:00:00Z")
        _seed_session(conn, session_id="new", updated_at="2026-01-02T00:00:00Z")
        _seed_messages(conn, "old", [{"role": "user", "content": "old msg"}])
        _seed_messages(conn, "new", [{"role": "user", "content": "new msg"}])
        conn.close()

        run_export(state_dir, None, "openai", None)
        data = json.loads(capsys.readouterr().out)

        assert len(data) == 1
        assert data[0]["content"] == "new msg"

    def test_default_ignores_inactive_sessions(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn, session_id="inactive", active=0, updated_at="2026-01-02T00:00:00Z")
        _seed_session(conn, session_id="active", active=1, updated_at="2026-01-01T00:00:00Z")
        _seed_messages(conn, "inactive", [{"role": "user", "content": "inactive"}])
        _seed_messages(conn, "active", [{"role": "user", "content": "active"}])
        conn.close()

        run_export(state_dir, None, "openai", None)
        data = json.loads(capsys.readouterr().out)

        assert data[0]["content"] == "active"

    def test_default_ignores_isolated_sessions(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn, session_id="iso", session_type="isolated", updated_at="2026-01-02T00:00:00Z")
        _seed_session(conn, session_id="main", session_type="main", updated_at="2026-01-01T00:00:00Z")
        _seed_messages(conn, "iso", [{"role": "user", "content": "isolated"}])
        _seed_messages(conn, "main", [{"role": "user", "content": "main"}])
        conn.close()

        run_export(state_dir, None, "openai", None)
        data = json.loads(capsys.readouterr().out)

        assert data[0]["content"] == "main"


class TestExportErrors:
    def test_no_active_session_exits_nonzero(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        conn.close()

        result = run_export(state_dir, None, "openai", None)
        assert result == 1
        assert "No active main session" in capsys.readouterr().err

    def test_missing_session_id_exits_nonzero(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        conn.close()

        result = run_export(state_dir, "nonexistent", "openai", None)
        assert result == 1
        assert "not found" in capsys.readouterr().err

    def test_missing_db_exits_nonzero(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = run_export(state_dir, None, "openai", None)
        assert result == 1
        assert "No sessions database" in capsys.readouterr().err


class TestExportOutput:
    def test_output_to_file(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
        ])
        conn.close()

        out_file = tmp_path / "out" / "export.json"
        result = run_export(state_dir, "sess-1", "openai", str(out_file))
        assert result == 0
        assert out_file.exists()

        data = json.loads(out_file.read_text())
        assert len(data) == 1
        assert data[0]["content"] == "hello"

        stderr = capsys.readouterr().err
        assert "Exported 1 messages" in stderr

    def test_stdout_when_no_output(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
        ])
        conn.close()

        result = run_export(state_dir, "sess-1", "openai", None)
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert captured.err == ""

    def test_output_is_valid_indented_json(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        conn = _init_db(state_dir / "sessions.db")
        _seed_session(conn)
        _seed_messages(conn, "sess-1", [
            {"role": "user", "content": "hello"},
        ])
        conn.close()

        run_export(state_dir, "sess-1", "openai", None)
        raw = capsys.readouterr().out
        assert "  " in raw
        assert raw.endswith("\n")
        json.loads(raw)
