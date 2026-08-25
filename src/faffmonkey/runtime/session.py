import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from faffmonkey.types import Message, ToolCall, dict_to_message

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('main', 'isolated')),
    channel_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    daily_note_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_active
    ON sessions(channel_id, active, type);

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

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, timestamp);
"""


MAIN_SESSION_KEY = "main"


@dataclass
class Session:
    id: str
    type: str
    channel_id: str
    active: bool
    created_at: str
    updated_at: str


class SessionStore:
    # Invariant: SessionStore is created per-thread. Do not share across threads.
    # See scheduler.py:_run_main which creates its own instance.
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._in_transaction = False
        self._init_schema()

    def _init_schema(self) -> None:
        cursor = self._conn.cursor()

        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "schema_version" not in tables:
            cursor.executescript(_SCHEMA_SQL)
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()
            logger.info("initialised session database at schema v%d", SCHEMA_VERSION)
            return

        # Expand step for image support: additive, nullable, and invisible
        # to older readers, so the schema version does not move.
        columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "messages" in tables and "images" not in columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN images TEXT")
            self._conn.commit()
            logger.info("added messages.images column")
        session_columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "sessions" in tables and "daily_note_at" not in session_columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN daily_note_at TEXT")
            self._conn.commit()
            logger.info("added sessions.daily_note_at column")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _touch_session(self, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (self._now(), session_id),
        )

    def get_or_create_main_session(self, channel_id: str) -> Session:
        """The active main session for a session key.

        The column is called channel_id for historical reasons; the value
        is a session key. `faff run` keys every direct conversation on
        MAIN_SESSION_KEY, whichever channel it arrived by, so Telegram and
        Discord are two doors into one conversation. Group and guild rooms
        get their own key (`<channel>:<group_id>`), and `faff chat` keeps
        `cli`.
        """
        # The lookup and the insert are one transaction: two processes opening
        # the same channel concurrently would otherwise both see no row and
        # both create a main session, splitting the conversation across two.
        own_transaction = not self._in_transaction
        if own_transaction:
            self.begin()
        try:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE channel_id = ? AND type = 'main' AND active = 1 "
                "ORDER BY updated_at DESC LIMIT 1",
                (channel_id,),
            ).fetchone()

            if row is not None:
                logger.info("resuming main session %s for channel %s", row["id"], channel_id)
                self._touch_session(row["id"])
                session = Session(
                    id=row["id"],
                    type=row["type"],
                    channel_id=row["channel_id"],
                    active=bool(row["active"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            else:
                session = self._create_session("main", channel_id)

            if own_transaction:
                self.commit()
            return session
        except Exception:
            if own_transaction:
                self.rollback()
            raise

    def _create_session(self, session_type: str, channel_id: str) -> Session:
        now = self._now()
        session_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO sessions (id, type, channel_id, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (session_id, session_type, channel_id, now, now),
        )
        if not self._in_transaction:
            self._conn.commit()
        logger.info("created %s session %s for %s", session_type, session_id, channel_id)
        return Session(
            id=session_id,
            type=session_type,
            channel_id=channel_id,
            active=True,
            created_at=now,
            updated_at=now,
        )

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        tool_call_id: str | None = None,
        *,
        timestamp: str | None = None,
        images: list[str] | None = None,
    ) -> str:
        now = timestamp or self._now()
        msg_id = uuid.uuid4().hex
        images_json = json.dumps(images) if images else None
        tc_json = None
        if tool_calls is not None:
            tc_json = json.dumps([
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ])
        self._conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, timestamp, images) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                session_id,
                role,
                content,
                tc_json,
                tool_call_id,
                now,
                images_json,
            ),
        )
        self._touch_session(session_id)
        if not self._in_transaction:
            self._conn.commit()
        return msg_id

    def get_history(self, session_id: str) -> list[Message]:
        rows = self._conn.execute(
            "SELECT role, content, tool_calls, tool_call_id, timestamp, images FROM messages "
            "WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        ).fetchall()

        result: list[Message] = []
        for row in rows:
            d: dict = {"role": row["role"]}
            if row["content"] is not None:
                d["content"] = row["content"]
            if row["tool_calls"] is not None:
                d["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"] is not None:
                d["tool_call_id"] = row["tool_call_id"]
            if row["images"] is not None:
                try:
                    d["images"] = json.loads(row["images"])
                except json.JSONDecodeError:
                    logger.warning("skipping unreadable images column on a message")
            msg = dict_to_message(d)
            if msg is None:
                continue
            msg.timestamp = row["timestamp"]
            result.append(msg)
        return result

    def deactivate_session(self, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET active = 0, updated_at = ? WHERE id = ?",
            (self._now(), session_id),
        )
        # Every other mutating method defers to the caller's transaction.
        # This one committed regardless, which would end a caller's
        # transaction early while _in_transaction stayed True, so the writes
        # after it fell into autocommit and a rollback undid nothing.
        if not self._in_transaction:
            self._conn.commit()
        logger.info("deactivated session %s", session_id)

    def daily_note_at(self, session_id: str) -> str | None:
        """Timestamp of the last message covered by a daily note, or None
        when nothing in this session has been noted yet."""
        row = self._conn.execute(
            "SELECT daily_note_at FROM sessions WHERE id = ?", (session_id,),
        ).fetchone()
        return row[0] if row else None

    def set_daily_note_at(self, session_id: str, timestamp: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET daily_note_at = ? WHERE id = ?",
            (timestamp, session_id),
        )
        if not self._in_transaction:
            self._conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def message_count(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0]

    def delete_all_messages(self, session_id: str) -> None:
        own_transaction = not self._in_transaction
        if own_transaction:
            self.begin()
        try:
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            )
            if own_transaction:
                self.commit()
        except Exception:
            if own_transaction:
                self.rollback()
            raise

    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True

    def commit(self) -> None:
        try:
            self._conn.execute("COMMIT")
        finally:
            self._in_transaction = False

    def rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        finally:
            self._in_transaction = False

    def backup(self, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(dest_path))
        try:
            self._conn.backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        self._conn.close()
