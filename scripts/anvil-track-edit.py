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


def db_path() -> Path:
    raw = os.environ.get("ANVIL_DB_PATH", "~/.claude-anvil/anvil.db")
    return Path(os.path.expanduser(os.path.expandvars(raw)))


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
