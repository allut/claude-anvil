#!/usr/bin/env python3
"""anvil-track-edit.py -- PostToolUse hook handler for Edit|Write|MultiEdit.

Reads a Claude Code hook-input JSON blob on stdin and, if there is an
active /anvil session (row in `sessions` with ended_at IS NULL), appends a
row to session_files so the Recall step in future anvil runs can see that
this file has been touched before.

Never blocks the tool call. Any internal error -> print to stderr, exit 0.
The hook must be fast: it fires after every Edit/Write/MultiEdit.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from anvil_shared import db_path  # noqa: E402


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except Exception as e:
        print(f"anvil-track-edit: bad hook input: {e}", file=sys.stderr)
        return 0

    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    tool_name = (data.get("tool_name") or "").lower()
    if not file_path or not tool_name:
        return 0

    path = db_path()
    if not path.exists():
        # No anvil session has ever run -- nothing to track against.
        return 0

    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        session_id_env = os.environ.get("ANVIL_SESSION_ID", "").strip()
        try:
            sid = int(session_id_env) if session_id_env else None
        except ValueError:
            sid = None
        if sid is not None:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return 0
        session_id = row[0]
        conn.execute(
            "INSERT INTO session_files (session_id, file_path, tool_name) VALUES (?, ?, ?)",
            (session_id, file_path, tool_name),
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"anvil-track-edit: sqlite error: {e}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
