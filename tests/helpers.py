"""Test helpers: DB seeding, hook payloads, a fake urlopen, and a CLI runner."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import SCHEMA_SQL, SCRIPTS


# --- sqlite -------------------------------------------------------------------

def apply_schema(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def seed_db(db_path, *, sessions=(), checks=(), session_files=(), search_index=()):
    """Create the schema at ``db_path`` and insert the supplied rows.

    ``sessions``       -> dicts with task_id, repo_path, branch?, created_at?, ended_at?
    ``checks``         -> dicts with task_id, phase, check_name?, tool?, passed?, ts?
    ``session_files``  -> dicts with session_id, file_path, tool_name
    ``search_index``   -> dicts with content, session_id, source_type
    """
    conn = apply_schema(db_path)
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (id, task_id, repo_path, branch, summary, created_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)",
            (s.get("id"), s["task_id"], s.get("repo_path", "/repo"), s.get("branch", "main"),
             s.get("summary"), s.get("created_at"), s.get("ended_at")),
        )
    for c in checks:
        conn.execute(
            "INSERT INTO anvil_checks (task_id, phase, check_name, tool, command, exit_code, "
            "output_snippet, passed, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
            (c["task_id"], c["phase"], c.get("check_name", "chk"), c.get("tool", "t"),
             c.get("command", ""), c.get("exit_code", 0), c.get("output_snippet", ""),
             c.get("passed", 1), c.get("ts")),
        )
    for f in session_files:
        conn.execute(
            "INSERT INTO session_files (session_id, file_path, tool_name) VALUES (?, ?, ?)",
            (f["session_id"], f["file_path"], f.get("tool_name", "edit")),
        )
    for x in search_index:
        conn.execute(
            "INSERT INTO search_index (content, session_id, source_type) VALUES (?, ?, ?)",
            (x["content"], x.get("session_id"), x.get("source_type", "review")),
        )
    conn.commit()
    conn.close()
    return Path(db_path)


def query(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- hook payloads ------------------------------------------------------------

def hook_payload(tool_name, **tool_input) -> str:
    """Serialize a Claude Code hook-input blob for the two hook scripts."""
    return json.dumps({"tool_name": tool_name, "tool_input": tool_input})


# --- fake HTTP ----------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(responses, recorder=None):
    """Build a drop-in ``urlopen`` that replays a queue of canned outcomes.

    Each entry is either an ``Exception`` (raised) or a ``(status, body)`` pair.
    Every call appends ``(request, timeout)`` to ``recorder`` when given.
    """
    queue = list(responses)

    def _urlopen(req, timeout=None, context=None, **kwargs):
        if recorder is not None:
            recorder.append((req, timeout))
        if not queue:
            raise AssertionError("fake_urlopen: more calls than queued responses")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, body = item
        return FakeResponse(body, status)

    return _urlopen


# --- subprocess CLI -----------------------------------------------------------

def run_cli(script_stem, args, stdin=None, env=None, cwd=None):
    """Run ``plugin/scripts/<stem>.py`` out of process. Returns CompletedProcess.

    The child inherits ``os.environ`` rather than a hand-built allowlist, so its
    isolation is exactly the isolation the autouse ``isolate_env`` fixture has
    already applied to the parent: every ``ANVIL_*`` var deleted, and HOME /
    USERPROFILE / TMPDIR / TEMP / TMP repointed at ``tmp_path``. Inheriting is
    deliberate -- the scripts read PATH, SystemRoot and friends, and a minimal env
    would make them fail for reasons unrelated to what is under test. Anything a
    script reads that ``isolate_env`` does not cover must be passed via ``env``.
    """
    child_env = dict(os.environ)
    if env:
        for k, v in env.items():
            if v is None:
                child_env.pop(k, None)
            else:
                child_env[k] = str(v)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / f"{script_stem}.py"), *[str(a) for a in args]],
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        timeout=60,
        env=child_env,
        cwd=str(cwd) if cwd else None,
    )
