"""faff export — export conversation history as portable JSON."""

import json
import sqlite3
import sys
from pathlib import Path


def _find_active_main_session(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT id FROM sessions WHERE type = 'main' AND active = 1 "
        "ORDER BY updated_at DESC LIMIT 1",
    ).fetchone()
    return row[0] if row else None


def _fetch_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        # images is part of a message. Omitting it made `faff export --format
        # json` a lossy backup of a conversation containing photos, under a
        # test named test_json_format_preserves_raw_fields.
        "SELECT role, content, tool_calls, tool_call_id, timestamp, images "
        "FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
        (session_id,),
    ).fetchall()
    messages = []
    for row in rows:
        msg: dict = {
            "role": row[0],
            "content": row[1],
            "tool_calls": row[2],
            "tool_call_id": row[3],
            "timestamp": row[4],
            "images": row[5],
        }
        if msg["tool_calls"] is not None:
            msg["tool_calls"] = json.loads(msg["tool_calls"])
        if msg["images"] is not None:
            msg["images"] = json.loads(msg["images"])
        messages.append(msg)
    return messages


def _to_openai_format(messages: list[dict]) -> list[dict]:
    result = []
    for msg in messages:
        entry: dict = {"role": msg["role"], "content": msg["content"] or ""}
        if msg["tool_calls"] is not None:
            entry["tool_calls"] = msg["tool_calls"]
        if msg["tool_call_id"] is not None:
            entry["tool_call_id"] = msg["tool_call_id"]
        result.append(entry)
    return result


def run_export(
    state_dir: Path,
    session_id: str | None,
    fmt: str,
    output: str | None,
) -> int:
    db_path = state_dir / "sessions.db"
    if not db_path.exists():
        print("No sessions database found.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA query_only=ON")
    try:
        if session_id is None:
            session_id = _find_active_main_session(conn)
            if session_id is None:
                print("No active main session found.", file=sys.stderr)
                return 1

        count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()[0]
        if count == 0:
            print(f"Session {session_id!r} not found.", file=sys.stderr)
            return 1

        messages = _fetch_messages(conn, session_id)
    finally:
        conn.close()

    if fmt == "openai":
        data = _to_openai_format(messages)
    else:
        data = messages

    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    if output is not None:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Exported {len(messages)} messages to {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(text)

    return 0
