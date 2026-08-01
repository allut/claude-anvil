"""anvil-track-edit.py -- the PostToolUse file tracker.

Contract: always exit 0, never write stdout, never block the tool call.
"""
from __future__ import annotations

import pytest

from helpers import hook_payload, query, run_cli, seed_db

T0 = "2024-01-01 00:00:00"
T1 = "2024-01-01 00:00:01"


def track(db, stdin, session_id=None):
    return run_cli(
        "anvil-track-edit",
        [],
        stdin=stdin,
        env={"ANVIL_DB_PATH": str(db), "ANVIL_SESSION_ID": session_id},
    )


def files(db):
    return query(db, "SELECT session_id, file_path, tool_name FROM session_files ORDER BY id")


def test_happy_path_inserts_one_row_with_lowercased_tool(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t", "created_at": T0}])
    r = track(db, hook_payload("MultiEdit", file_path="C:/repo/x.py"))
    assert r.returncode == 0
    assert r.stdout == ""
    assert files(db) == [(1, "C:/repo/x.py", "multiedit")]


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_all_three_matched_tools_are_tracked(tmp_path, tool):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t", "created_at": T0}])
    assert track(db, hook_payload(tool, file_path="x.py")).returncode == 0
    assert files(db) == [(1, "x.py", tool.lower())]


def test_blank_stdin_is_a_noop(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t"}])
    r = track(db, "")
    assert r.returncode == 0
    assert files(db) == []


def test_malformed_json_is_a_noop_and_warns(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t"}])
    r = track(db, "{nope")
    assert r.returncode == 0
    assert "bad hook input" in r.stderr
    assert files(db) == []


def test_missing_file_path_is_a_noop(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t"}])
    r = track(db, hook_payload("Edit"))
    assert r.returncode == 0
    assert files(db) == []


def test_missing_tool_name_is_a_noop(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t"}])
    r = track(db, hook_payload("", file_path="x.py"))
    assert r.returncode == 0
    assert files(db) == []


def test_missing_db_is_a_noop(tmp_path):
    r = track(tmp_path / "nope.db", hook_payload("Edit", file_path="x.py"))
    assert r.returncode == 0
    assert r.stderr == ""


def test_corrupt_db_exits_zero(tmp_path):
    bad = tmp_path / "corrupt.db"
    bad.write_text("not a database", encoding="utf-8")
    r = track(bad, hook_payload("Edit", file_path="x.py"))
    assert r.returncode == 0
    assert "sqlite error" in r.stderr


def test_no_open_session_is_a_noop(tmp_path):
    db = seed_db(tmp_path / "a.db",
                 sessions=[{"id": 1, "task_id": "t", "created_at": T0, "ended_at": T1}])
    r = track(db, hook_payload("Edit", file_path="x.py"))
    assert r.returncode == 0
    assert files(db) == []


def test_autodetect_needs_no_baseline_evidence(tmp_path):
    """Divergence from the gate: track-edit's auto-detect is just the newest
    `ended_at IS NULL` session -- no EXISTS(baseline check) requirement
    (anvil-track-edit.py:59 vs anvil-gate-commit.py:92-102)."""
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 42, "task_id": "bare", "created_at": T0}])
    assert track(db, hook_payload("Edit", file_path="x.py")).returncode == 0
    assert files(db) == [(42, "x.py", "edit")]


def test_autodetect_picks_newest_open_session(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[
        {"id": 1, "task_id": "old", "created_at": T0},
        {"id": 2, "task_id": "new", "created_at": T0},
    ])
    track(db, hook_payload("Edit", file_path="x.py"))
    assert files(db) == [(2, "x.py", "edit")]


def test_explicit_session_id_pointing_at_ended_session_still_inserts(tmp_path):
    """The FK is satisfied, and the lookup at :55 does not filter on ended_at."""
    db = seed_db(tmp_path / "a.db", sessions=[
        {"id": 1, "task_id": "done", "created_at": T0, "ended_at": T1},
        {"id": 2, "task_id": "open", "created_at": T0},
    ])
    r = track(db, hook_payload("Edit", file_path="x.py"), session_id="1")
    assert r.returncode == 0
    assert files(db) == [(1, "x.py", "edit")]


def test_nonexistent_session_id_is_a_noop(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 1, "task_id": "t", "created_at": T0}])
    r = track(db, hook_payload("Edit", file_path="x.py"), session_id="999")
    assert r.returncode == 0
    assert files(db) == []


def test_non_numeric_session_id_falls_back_to_autodetect(tmp_path):
    db = seed_db(tmp_path / "a.db", sessions=[{"id": 8, "task_id": "t", "created_at": T0}])
    r = track(db, hook_payload("Edit", file_path="x.py"), session_id="garbage")
    assert r.returncode == 0
    assert files(db) == [(8, "x.py", "edit")]
