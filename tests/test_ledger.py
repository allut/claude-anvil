"""anvil-ledger.py -- real sqlite on tmp_path, mostly through the out-of-process CLI."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

import pytest

from helpers import git, make_repo, query, run_cli, seed_db


def _fts5_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


HAS_FTS5 = _fts5_available()
requires_fts5 = pytest.mark.skipif(not HAS_FTS5, reason="sqlite build lacks FTS5")


def ledger(db, *args, cwd=None):
    return run_cli("anvil-ledger", list(args), env={"ANVIL_DB_PATH": str(db)}, cwd=cwd)


def ok(r):
    assert r.returncode == 0, f"exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return r


# --- init ---------------------------------------------------------------------

def test_init_creates_the_db_and_parent_directories(tmp_path):
    db = tmp_path / "deep" / "nested" / "anvil.db"
    r = ok(ledger(db, "init"))
    assert db.exists()
    assert str(db) in r.stdout


def test_init_is_idempotent(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "init"))
    tables = {row[0] for row in query(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"anvil_checks", "sessions", "session_files", "memory"} <= tables


def test_foreign_keys_are_enforced(tmp_path):
    """PRAGMA foreign_keys=ON (connect()) makes an orphan session_files row fail.

    main() reports it as a clean exit 1 with a one-line message; the traceback
    that used to leak out of argparse dispatch is gone."""
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    r = ledger(db, "track-edit", "999", "x.py", "Edit")
    assert r.returncode == 1
    assert "anvil-ledger: sqlite error" in r.stderr
    assert "FOREIGN KEY constraint failed" in r.stderr
    assert "Traceback" not in r.stderr
    assert r.stdout == ""


def test_an_unreadable_db_path_is_reported_without_a_traceback(tmp_path):
    """A directory where the DB file should be is an OSError/sqlite3 failure.
    Either way the CLI exits 1 with a message rather than a traceback."""
    db = tmp_path / "a.db"
    db.mkdir()
    r = ledger(db, "init")
    assert r.returncode == 1
    assert "anvil-ledger:" in r.stderr
    assert "Traceback" not in r.stderr


# --- insert-check -------------------------------------------------------------

def test_insert_check_records_a_row(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after",
              "--check", "build", "--tool", "npm", "--command", "npm run build",
              "--exit-code", "0", "--passed", "1", "--output", "all good"))
    assert query(db, "SELECT task_id, phase, check_name, tool, command, exit_code, "
                     "output_snippet, passed FROM anvil_checks") == [
        ("t", "after", "build", "npm", "npm run build", 0, "all good", 1)]


def test_insert_check_reads_the_output_file(tmp_path):
    db = tmp_path / "a.db"
    log = tmp_path / "out.log"
    log.write_text("line one\nline two\n", encoding="utf-8")
    ok(ledger(db, "init"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
              "--tool", "x", "--passed", "1", "--output-file", str(log)))
    assert query(db, "SELECT output_snippet FROM anvil_checks")[0][0] == "line one\nline two\n"


def test_insert_check_clips_long_output(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
              "--tool", "x", "--passed", "1", "--output", "z" * 5000))
    assert len(query(db, "SELECT output_snippet FROM anvil_checks")[0][0]) == 4000


def test_missing_output_file_forces_a_reported_pass_to_fail(tmp_path):
    """Commit 7251309's invariant: a missing log means the command likely never
    ran, so passed=1 is downgraded to 0 with exit_code=-1 and a diagnostic."""
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
                  "--tool", "x", "--exit-code", "0", "--passed", "1",
                  "--output-file", str(tmp_path / "never-written.log")))
    assert "forcing passed=0" in r.stderr
    assert query(db, "SELECT passed, exit_code, output_snippet FROM anvil_checks") == [
        (0, -1, "output file missing — command likely did not run")]


def test_missing_output_file_with_passed_zero_changes_nothing(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
                  "--tool", "x", "--exit-code", "3", "--passed", "0",
                  "--output-file", str(tmp_path / "never.log")))
    assert "recording empty output" in r.stderr
    assert query(db, "SELECT passed, exit_code, output_snippet FROM anvil_checks") == [(0, 3, "")]


def test_output_file_wins_over_output(tmp_path):
    db = tmp_path / "a.db"
    log = tmp_path / "o.log"
    log.write_text("from file", encoding="utf-8")
    ok(ledger(db, "init"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
              "--tool", "x", "--passed", "1", "--output", "from arg",
              "--output-file", str(log)))
    assert query(db, "SELECT output_snippet FROM anvil_checks")[0][0] == "from file"


@pytest.mark.parametrize("phase", ["baseline", "after", "review"])
def test_insert_check_accepts_the_three_phases(tmp_path, phase):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", phase, "--check", "c",
              "--tool", "x", "--passed", "1"))


def test_insert_check_rejects_an_unknown_phase(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    r = ledger(db, "insert-check", "--task-id", "t", "--phase", "bogus", "--check", "c",
               "--tool", "x", "--passed", "1")
    assert r.returncode == 2


# --- search_index mirroring ---------------------------------------------------

@requires_fts5
def test_review_output_is_mirrored_into_the_search_index(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t", str(tmp_path), "main"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "review", "--check",
              "review-gemini", "--tool", "anvil-review", "--passed", "0",
              "--output", "z" * 900))
    rows = query(db, "SELECT content, session_id, source_type FROM search_index")
    assert len(rows) == 1
    assert len(rows[0][0]) == 500          # clipped to 500 chars
    assert rows[0][1] == 1
    assert rows[0][2] == "review"


@requires_fts5
def test_non_review_phases_are_not_mirrored(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t"))
    for phase in ("baseline", "after"):
        ok(ledger(db, "insert-check", "--task-id", "t", "--phase", phase, "--check", "c",
                  "--tool", "x", "--passed", "1", "--output", "some text"))
    assert query(db, "SELECT COUNT(*) FROM search_index") == [(0,)]


@requires_fts5
def test_review_with_empty_output_is_not_mirrored(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t"))
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "review", "--check", "c",
              "--tool", "x", "--passed", "1", "--output", ""))
    assert query(db, "SELECT COUNT(*) FROM search_index") == [(0,)]


@requires_fts5
def test_mirroring_targets_the_newest_session_for_that_task(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t"))
    ok(ledger(db, "start-session", "other"))
    ok(ledger(db, "start-session", "t"))          # id 3
    ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "review", "--check", "c",
              "--tool", "x", "--passed", "0", "--output", "finding"))
    assert query(db, "SELECT session_id FROM search_index") == [(3,)]


@requires_fts5
def test_review_output_without_a_session_is_dropped(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "insert-check", "--task-id", "orphan", "--phase", "review", "--check", "c",
              "--tool", "x", "--passed", "0", "--output", "finding"))
    assert query(db, "SELECT COUNT(*) FROM search_index") == [(0,)]


# --- select-bundle / count-phase ---------------------------------------------

def test_select_bundle_orders_baseline_then_after_then_review(tmp_path):
    db = seed_db(tmp_path / "a.db", checks=[
        {"task_id": "t", "phase": "review", "check_name": "r1"},
        {"task_id": "t", "phase": "baseline", "check_name": "b1"},
        {"task_id": "t", "phase": "after", "check_name": "a1"},
        {"task_id": "t", "phase": "baseline", "check_name": "b2"},
        {"task_id": "other", "phase": "after", "check_name": "SHOULD-NOT-APPEAR"},
    ])
    out = ok(ledger(db, "select-bundle", "t")).stdout
    assert "SHOULD-NOT-APPEAR" not in out
    order = [name for name in ("b1", "b2", "a1", "r1") if name in out]
    assert order == ["b1", "b2", "a1", "r1"]
    assert out.index("b1") < out.index("b2") < out.index("a1") < out.index("r1")


def test_select_bundle_renders_pass_and_fail_labels(tmp_path):
    db = seed_db(tmp_path / "a.db", checks=[
        {"task_id": "t", "phase": "after", "check_name": "good", "passed": 1},
        {"task_id": "t", "phase": "after", "check_name": "bad", "passed": 0},
    ])
    out = ok(ledger(db, "select-bundle", "t")).stdout
    assert "pass" in out and "FAIL" in out


def test_select_bundle_on_an_unknown_task_says_no_rows(tmp_path):
    db = seed_db(tmp_path / "a.db")
    assert ok(ledger(db, "select-bundle", "nope")).stdout.strip() == "(no rows)"


def test_count_phase_prints_a_bare_integer(tmp_path):
    db = seed_db(tmp_path / "a.db", checks=[
        {"task_id": "t", "phase": "after"},
        {"task_id": "t", "phase": "after"},
        {"task_id": "t", "phase": "review"},
    ])
    assert ok(ledger(db, "count-phase", "t", "after")).stdout.strip() == "2"
    assert ok(ledger(db, "count-phase", "t", "review")).stdout.strip() == "1"
    assert ok(ledger(db, "count-phase", "t", "baseline")).stdout.strip() == "0"


# --- sessions -----------------------------------------------------------------

def test_start_session_prints_the_new_row_id(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    assert ok(ledger(db, "start-session", "t", "/repo", "main")).stdout.strip() == "1"
    assert ok(ledger(db, "start-session", "t2", "/repo", "main")).stdout.strip() == "2"


def test_start_session_defaults_repo_path_to_cwd(tmp_path):
    db = tmp_path / "a.db"
    workdir = tmp_path / "work"
    workdir.mkdir()
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t", cwd=workdir))
    stored = query(db, "SELECT repo_path, branch FROM sessions")[0]
    assert stored[0] == str(workdir.resolve())
    assert stored[1] == ""


@requires_fts5
def test_end_session_sets_summary_and_indexes_it(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t"))
    assert ok(ledger(db, "end-session", "1", "regression fixed")).stdout.strip() == "ok"
    row = query(db, "SELECT summary, ended_at FROM sessions WHERE id=1")[0]
    assert row[0] == "regression fixed"
    assert row[1] is not None
    assert query(db, "SELECT content, source_type FROM search_index") == [
        ("regression fixed", "summary")]


def test_end_session_without_a_summary_indexes_nothing(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t"))
    ok(ledger(db, "end-session", "1"))
    assert query(db, "SELECT COUNT(*) FROM search_index") == [(0,)]
    assert query(db, "SELECT ended_at FROM sessions")[0][0] is not None


def test_track_edit_lowercases_the_tool_name(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "t"))
    assert ok(ledger(db, "track-edit", "1", "C:/x.py", "MultiEdit")).stdout.strip() == "ok"
    assert query(db, "SELECT file_path, tool_name FROM session_files") == [("C:/x.py", "multiedit")]


# --- recall -------------------------------------------------------------------

def test_escape_like_escapes_backslash_then_wildcards(anvil_ledger):
    assert anvil_ledger._escape_like(r"a\b%c_d") == r"a\\b\%c\_d"
    assert anvil_ledger._escape_like("plain") == "plain"
    assert anvil_ledger._escape_like("") == ""


def test_recall_finds_sessions_that_touched_a_file(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 1, "task_id": "t", "branch": "main", "summary": "did a thing"}],
        session_files=[{"session_id": 1, "file_path": "/repo/src/auth.py"},
                       {"session_id": 1, "file_path": "/repo/src/other.py"}],
    )
    out = ok(ledger(db, "recall", "auth.py")).stdout
    assert "auth.py" in out and "other.py" not in out
    assert "did a thing" in out


def test_recall_treats_percent_as_a_literal(tmp_path):
    """_escape_like + ESCAPE '\\' means a literal % cannot act as a wildcard."""
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 1, "task_id": "t"}],
        session_files=[{"session_id": 1, "file_path": "/repo/a%b.py"},
                       {"session_id": 1, "file_path": "/repo/aZZZb.py"}],
    )
    out = ok(ledger(db, "recall", "a%b.py")).stdout
    assert "a%b.py" in out
    assert "aZZZb.py" not in out


def test_recall_treats_underscore_as_a_literal(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 1, "task_id": "t"}],
        session_files=[{"session_id": 1, "file_path": "/repo/a_b.py"},
                       {"session_id": 1, "file_path": "/repo/aXb.py"}],
    )
    out = ok(ledger(db, "recall", "a_b.py")).stdout
    assert "a_b.py" in out and "aXb.py" not in out


def test_recall_on_no_matches_says_no_rows(tmp_path):
    db = seed_db(tmp_path / "a.db")
    assert ok(ledger(db, "recall", "nothing")).stdout.strip() == "(no rows)"


@requires_fts5
def test_recall_issues_surfaces_indexed_findings_for_that_file(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 1, "task_id": "t"}, {"id": 2, "task_id": "u"}],
        session_files=[{"session_id": 1, "file_path": "/repo/auth.py"},
                       {"session_id": 2, "file_path": "/repo/elsewhere.py"}],
        search_index=[
            {"content": "this change broke the login flow", "session_id": 1},
            {"content": "a totally unremarkable note", "session_id": 1},
            {"content": "regression in an unrelated file", "session_id": 2},
        ],
    )
    out = ok(ledger(db, "recall-issues", "auth.py")).stdout
    assert "broke the login flow" in out
    assert "unremarkable" not in out          # does not MATCH the issue vocabulary
    assert "unrelated file" not in out        # wrong session


# --- memory -------------------------------------------------------------------

def test_memory_roundtrip_and_overwrite(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    assert ok(ledger(db, "memory-set", "build-cmd", "pytest -q")).stdout.strip() == "ok"
    assert ok(ledger(db, "memory-get", "build-cmd")).stdout.strip() == "pytest -q"
    ok(ledger(db, "memory-set", "build-cmd", "pytest -x"))
    assert ok(ledger(db, "memory-get", "build-cmd")).stdout.strip() == "pytest -x"
    assert query(db, "SELECT COUNT(*) FROM memory") == [(1,)]


def test_memory_get_on_a_missing_key_prints_an_empty_line(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    assert ok(ledger(db, "memory-get", "nope")).stdout.strip() == ""


def test_memory_list(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    assert ok(ledger(db, "memory-list")).stdout.strip() == "(no rows)"
    ok(ledger(db, "memory-set", "k1", "v1"))
    out = ok(ledger(db, "memory-list")).stdout
    assert "k1" in out and "v1" in out


# --- print_table --------------------------------------------------------------

def test_print_table_on_empty_rows(anvil_ledger, capsys):
    anvil_ledger.print_table([], ["a", "b"])
    assert capsys.readouterr().out == "(no rows)\n"


def test_print_table_pads_columns_and_draws_a_rule(anvil_ledger, capsys):
    rows = [{"name": "x", "value": "longer-value"}, {"name": "yy", "value": None}]
    anvil_ledger.print_table(rows, ["name", "value"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "name  value       "
    assert lines[1] == "----  ------------"
    assert lines[2] == "x     longer-value"
    assert lines[3] == "yy                "      # None renders as empty


# --- CLI surface --------------------------------------------------------------

def test_parser_exposes_the_documented_subcommands(anvil_ledger):
    import argparse
    parser = anvil_ledger.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == {
        "init", "insert-check", "select-bundle", "count-phase", "start-session",
        "end-session", "track-edit", "recall", "recall-issues",
        "memory-set", "memory-get", "memory-list", "snapshot-diff",
    }
    assert sub.required is True
    for name, p in sub.choices.items():
        assert p.get_default("func") is not None, f"{name} has no func default"


def test_main_returns_zero_on_success(anvil_ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("ANVIL_DB_PATH", str(tmp_path / "m.db"))
    assert anvil_ledger.main(["init"]) == 0


def test_missing_subcommand_exits_two(tmp_path):
    db = tmp_path / "a.db"
    assert ledger(db).returncode == 2


# --- schema migration ---------------------------------------------------------

def test_an_existing_db_gains_new_tables_on_first_use(tmp_path):
    """connect() re-applies the idempotent schema on every open, so a DB created
    before review_snapshots existed picks the table up without an explicit init."""
    db = seed_db(tmp_path / "a.db")
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE review_snapshots")
    conn.commit()
    conn.close()
    ok(ledger(db, "count-phase", "t", "after"))
    tables = {row[0] for row in query(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "review_snapshots" in tables


# --- session-scoped reads -----------------------------------------------------

OLD = "2025-09-01 10:00:00"
OLD_CHECK = "2025-09-01 10:05:00"
NEW = "2026-01-01 10:00:00"
NEW_CHECK = "2026-01-01 10:05:00"


def _reused_task(tmp_path):
    """Task `t` run twice: a stale September session and a fresh one."""
    return seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 1, "task_id": "t", "created_at": OLD},
                  {"id": 2, "task_id": "t", "created_at": NEW}],
        checks=[
            {"task_id": "t", "phase": "after", "check_name": "STALE-mypy", "ts": OLD_CHECK},
            {"task_id": "t", "phase": "review", "check_name": "STALE-review", "ts": OLD_CHECK},
            {"task_id": "t", "phase": "after", "check_name": "fresh-pytest", "ts": NEW_CHECK},
        ],
    )


def test_count_phase_ignores_checks_older_than_the_newest_session(tmp_path):
    db = _reused_task(tmp_path)
    assert ok(ledger(db, "count-phase", "t", "after")).stdout.strip() == "1"
    # The only review row is stale, so the 5c gate must read 0, not 1.
    assert ok(ledger(db, "count-phase", "t", "review")).stdout.strip() == "0"


def test_select_bundle_ignores_checks_older_than_the_newest_session(tmp_path):
    db = _reused_task(tmp_path)
    r = ok(ledger(db, "select-bundle", "t"))
    assert "fresh-pytest" in r.stdout
    assert "STALE" not in r.stdout
    assert "--all-sessions" in r.stderr     # the hidden rows are announced


def test_select_bundle_all_sessions_shows_the_history(tmp_path):
    db = _reused_task(tmp_path)
    out = ok(ledger(db, "select-bundle", "t", "--all-sessions")).stdout
    assert "STALE-mypy" in out and "STALE-review" in out and "fresh-pytest" in out


def test_a_task_without_a_session_still_counts_every_row(tmp_path):
    db = seed_db(tmp_path / "a.db", checks=[
        {"task_id": "t", "phase": "after", "check_name": "first", "ts": OLD_CHECK},
        {"task_id": "t", "phase": "after", "check_name": "second", "ts": NEW_CHECK},
    ])
    assert ok(ledger(db, "count-phase", "t", "after")).stdout.strip() == "2"
    r = ok(ledger(db, "select-bundle", "t"))
    assert "first" in r.stdout and "second" in r.stdout
    assert r.stderr == ""


def test_another_tasks_session_does_not_scope_this_task(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 1, "task_id": "other", "created_at": NEW}],
        checks=[{"task_id": "t", "phase": "after", "ts": OLD_CHECK}],
    )
    assert ok(ledger(db, "count-phase", "t", "after")).stdout.strip() == "1"


def test_start_session_warns_when_a_task_id_is_reused(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    first = ok(ledger(db, "start-session", "t", "/repo", "main"))
    assert first.stderr == ""
    second = ok(ledger(db, "start-session", "t", "/repo", "main"))
    assert second.stdout.strip() == "2"          # still prints only the new id
    assert "task_id 't' reused (1 earlier sessions, latest " in second.stderr
    assert "count-phase/select-bundle now show only this session's checks" in second.stderr


def test_start_session_is_quiet_for_a_fresh_task_id(tmp_path):
    db = tmp_path / "a.db"
    ok(ledger(db, "init"))
    ok(ledger(db, "start-session", "other"))
    r = ok(ledger(db, "start-session", "t"))
    assert r.stderr == ""


# --- insert-check result line -------------------------------------------------

def test_insert_check_prints_ok_for_a_pass(tmp_path):
    db = tmp_path / "a.db"
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "ruff",
                  "--tool", "ruff", "--passed", "1", "--output", "All checks passed!"))
    assert r.stdout.strip() == "ok"


def test_insert_check_announces_a_failure(tmp_path):
    db = tmp_path / "a.db"
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "ruff",
                  "--tool", "ruff", "--exit-code", "1", "--passed", "0",
                  "--output", "\n   \napp/x.py:3:1: F401 unused import\nFound 1 error."))
    assert r.stdout.strip() == "FAIL ruff: app/x.py:3:1: F401 unused import"


def test_insert_check_failure_line_is_clipped(tmp_path):
    db = tmp_path / "a.db"
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
                  "--tool", "x", "--passed", "0", "--output", "y" * 500))
    assert r.stdout.strip() == "FAIL c: " + "y" * 200


def test_insert_check_failure_without_output(tmp_path):
    db = tmp_path / "a.db"
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
                  "--tool", "x", "--passed", "0"))
    assert r.stdout.strip() == "FAIL c"


def test_insert_check_reports_the_stored_value_not_the_flag(tmp_path):
    """A missing output file forces passed=0 even when the caller said 1 -- the
    printed line must follow the stored row, not the argument."""
    db = tmp_path / "a.db"
    r = ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "after", "--check", "c",
                  "--tool", "x", "--exit-code", "0", "--passed", "1",
                  "--output-file", str(tmp_path / "never-written.log")))
    assert r.stdout.startswith("FAIL c: output file missing")


# --- snapshot-diff ------------------------------------------------------------

def _staged_repo(tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.txt": "alpha\n", "b.txt": "beta\n",
                                          "c.txt": "gamma\n"})
    (repo / "a.txt").write_bytes("alpha changed\n".encode("utf-8"))
    (repo / "d.txt").write_bytes("delta\n".encode("utf-8"))
    git(repo, "rm", "-q", "b.txt")
    git(repo, "add", "a.txt", "d.txt")
    return repo


def test_snapshot_diff_writes_the_patch_and_stores_the_manifest(tmp_path):
    db = tmp_path / "a.db"
    repo = _staged_repo(tmp_path)
    out = tmp_path / "diff.patch"
    ok(ledger(db, "start-session", "t"))
    r = ok(ledger(db, "snapshot-diff", "--task-id", "t", "--out", str(out), cwd=repo))

    patch = out.read_bytes()
    assert b"alpha changed" in patch and b"b.txt" in patch and b"d.txt" in patch
    sha = hashlib.sha256(patch).hexdigest()
    assert r.stdout.strip() == f"snapshot 1: 3 files, sha256 {sha[:12]}"

    (session_id, task_id, stored_sha, manifest_json), = query(
        db, "SELECT session_id, task_id, patch_sha256, manifest FROM review_snapshots")
    assert (session_id, task_id, stored_sha) == (1, "t", sha)
    manifest = {e["path"]: e for e in json.loads(manifest_json)}
    assert set(manifest) == {"a.txt", "b.txt", "d.txt"}
    assert manifest["a.txt"]["status"] == "M"
    assert manifest["a.txt"]["dst_sha"] == git(repo, "rev-parse", ":a.txt").strip()
    assert manifest["a.txt"]["dst_mode"] == "100644"
    assert manifest["d.txt"]["status"] == "A"
    assert manifest["b.txt"]["status"] == "D"
    assert re.fullmatch(r"0+", manifest["b.txt"]["dst_sha"])
    assert manifest["b.txt"]["dst_mode"] == "000000"


def test_snapshot_diff_records_the_checks_high_water_mark(tmp_path):
    db = tmp_path / "a.db"
    repo = _staged_repo(tmp_path)
    for _ in range(3):
        ok(ledger(db, "insert-check", "--task-id", "t", "--phase", "review", "--check", "c",
                  "--tool", "x", "--passed", "1"))
    ok(ledger(db, "snapshot-diff", "--task-id", "t", "--out", str(tmp_path / "p"), cwd=repo))
    assert query(db, "SELECT checks_high_water FROM review_snapshots") == [(3,)]


def test_a_pre_release_snapshot_table_gains_the_high_water_column(tmp_path):
    db = seed_db(tmp_path / "a.db")
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE review_snapshots")
    conn.execute("CREATE TABLE review_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "session_id INTEGER, task_id TEXT NOT NULL, patch_sha256 TEXT NOT NULL, "
                 "manifest TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO review_snapshots (task_id, patch_sha256, manifest) "
                 "VALUES ('t', 'x', '[]')")
    conn.commit()
    conn.close()
    ok(ledger(db, "count-phase", "t", "after"))
    assert query(db, "SELECT checks_high_water FROM review_snapshots") == [(0,)]


def test_snapshot_diff_binds_to_the_newest_session(tmp_path):
    db = tmp_path / "a.db"
    repo = _staged_repo(tmp_path)
    ok(ledger(db, "start-session", "t"))
    ok(ledger(db, "start-session", "other"))
    ok(ledger(db, "start-session", "t"))
    ok(ledger(db, "snapshot-diff", "--task-id", "t", "--out", str(tmp_path / "p"), cwd=repo))
    assert query(db, "SELECT session_id FROM review_snapshots") == [(3,)]


def test_snapshot_diff_does_not_follow_renames(tmp_path):
    db = tmp_path / "a.db"
    repo = make_repo(tmp_path / "repo", {"old.txt": "same content\n" * 20})
    git(repo, "mv", "old.txt", "new.txt")
    ok(ledger(db, "snapshot-diff", "--task-id", "t", "--out", str(tmp_path / "p"), cwd=repo))
    manifest = json.loads(query(db, "SELECT manifest FROM review_snapshots")[0][0])
    assert sorted((e["path"], e["status"]) for e in manifest) == [
        ("new.txt", "A"), ("old.txt", "D")]


def test_snapshot_diff_on_an_empty_index_exits_one(tmp_path):
    db = tmp_path / "a.db"
    repo = make_repo(tmp_path / "repo", {"a.txt": "alpha\n"})
    (repo / "a.txt").write_text("unstaged edit\n", encoding="utf-8")
    out = tmp_path / "diff.patch"
    out.write_text("stale patch from an earlier round", encoding="utf-8")
    ok(ledger(db, "init"))
    r = ledger(db, "snapshot-diff", "--task-id", "t", "--out", str(out), cwd=repo)
    assert r.returncode == 1
    assert "ERROR: nothing staged" in r.stderr
    assert out.read_bytes() == b""            # a stale patch cannot be reviewed
    assert query(db, "SELECT COUNT(*) FROM review_snapshots") == [(0,)]


def test_snapshot_diff_outside_a_repo_exits_one(tmp_path):
    db = tmp_path / "a.db"
    work = tmp_path / "not-a-repo"
    work.mkdir()
    out = tmp_path / "p"
    out.write_text("stale patch from an earlier round", encoding="utf-8")
    r = ledger(db, "snapshot-diff", "--task-id", "t", "--out", str(out), cwd=work)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert out.read_bytes() == b""            # truncated even when git fails


def test_snapshot_diff_refuses_an_index_that_changes_mid_snapshot(
        anvil_ledger, tmp_path, monkeypatch, capsys):
    """The manifest is read before and after the patch; a mismatch means the
    patch may not describe the recorded manifest, so nothing is recorded."""
    db = tmp_path / "a.db"
    monkeypatch.setenv("ANVIL_DB_PATH", str(db))
    monkeypatch.chdir(tmp_path)
    reads = iter([[{"path": "a", "status": "M", "dst_mode": "100644", "dst_sha": "1"}],
                  [{"path": "a", "status": "M", "dst_mode": "100644", "dst_sha": "2"}]])
    monkeypatch.setattr(anvil_ledger, "staged_manifest", lambda cwd: next(reads))
    monkeypatch.setattr(anvil_ledger, "run_git", lambda args, cwd: b"diff --git a/a b/a\n")
    out = tmp_path / "p"
    assert anvil_ledger.main(["snapshot-diff", "--task-id", "t", "--out", str(out)]) == 1
    assert "index changed while snapshotting" in capsys.readouterr().err
    assert out.read_bytes() == b""
    assert not db.exists()                   # returned before recording anything
