"""anvil-ledger.py -- real sqlite on tmp_path, mostly through the out-of-process CLI."""
from __future__ import annotations

import sqlite3

import pytest

from helpers import query, run_cli, seed_db


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
        "memory-set", "memory-get", "memory-list",
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
