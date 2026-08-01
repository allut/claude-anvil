"""anvil-gate-commit.py -- the PreToolUse commit gate.

Contract under test: exit 0 = allow, exit 2 = block with free-form stderr.
The gate never writes stdout and never emits JSON.
"""
from __future__ import annotations

import pytest

from helpers import hook_payload, run_cli, seed_db

OPEN = None  # ended_at IS NULL

T0 = "2024-01-01 00:00:00"
T1 = "2024-01-01 00:00:01"
T_BEFORE = "2023-12-31 23:59:59"


def gate(db, stdin, session_id=None):
    return run_cli(
        "anvil-gate-commit",
        [],
        stdin=stdin,
        env={"ANVIL_DB_PATH": str(db), "ANVIL_SESSION_ID": session_id},
    )


def commit_payload(command="git commit -m x"):
    return hook_payload("Bash", command=command)


# --- is_gated_git_commit ------------------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("git commit -m x", True),
        ("git   commit", True),
        ("git\tcommit -am wip", True),
        ("  git commit  ", True),
        ("mygit commit", False),          # lookbehind (?<![A-Za-z0-9_-])
        ("my-git commit", False),
        ("git-commit", False),
        ("gitcommit", False),
        ("git committed", False),         # \b after "commit"
        ("", False),
        ("git status", False),
        # Bug probe -- BENIGN_FLAGS_RE (anvil-gate-commit.py:29) is
        #   \b(--help|-h|--dry-run)\b
        # and the LEADING \b can never match: a word boundary before "-" needs a
        # word character to its left, but a flag is always preceded by a space.
        # So the benign-flag escape hatch is dead code and `git commit --dry-run`
        # is gated like a real commit. Pinned as current behavior; see follow-ups.
        ("git commit --dry-run", True),
        ("git commit --help", True),
        ("git commit -h", True),
        # ...it does fire when a word character precedes the flag, which is the
        # only shape the leading \b admits.
        ("git commit x-h", False),
        # Documented over-trigger: the regex is not shell-aware, so a `git commit`
        # inside a quoted string still trips the gate. Pinned so any future regex
        # change has to be a deliberate decision.
        ("echo 'git commit' && ls", True),
    ],
)
def test_is_gated_git_commit(anvil_gate, command, expected):
    assert anvil_gate.is_gated_git_commit(command) is expected


# --- fail-open matrix ---------------------------------------------------------

def test_blank_stdin_allows(tmp_path):
    db = seed_db(tmp_path / "a.db")
    r = gate(db, "")
    assert r.returncode == 0
    assert r.stdout == ""


def test_whitespace_only_stdin_allows(tmp_path):
    db = seed_db(tmp_path / "a.db")
    assert gate(db, "   \n ").returncode == 0


def test_malformed_json_allows_and_warns(tmp_path):
    db = seed_db(tmp_path / "a.db")
    r = gate(db, "{not json")
    assert r.returncode == 0
    assert "bad hook input" in r.stderr
    assert r.stdout == ""


def test_non_bash_tool_allows(tmp_path):
    db = seed_db(tmp_path / "a.db",
                 sessions=[{"id": 1, "task_id": "t", "created_at": T0}],
                 checks=[{"task_id": "t", "phase": "baseline", "ts": T1}])
    r = run_cli("anvil-gate-commit", [],
                stdin=hook_payload("Edit", command="git commit -m x"),
                env={"ANVIL_DB_PATH": str(db)})
    assert r.returncode == 0


def test_missing_db_allows(tmp_path):
    r = gate(tmp_path / "does-not-exist.db", commit_payload())
    assert r.returncode == 0
    assert r.stderr == ""


def test_corrupt_db_fails_open(tmp_path):
    bad = tmp_path / "corrupt.db"
    bad.write_text("this is not a sqlite database", encoding="utf-8")
    r = gate(bad, commit_payload())
    assert r.returncode == 0
    assert "sqlite error (fail-open)" in r.stderr


def test_no_matching_session_allows(tmp_path):
    db = seed_db(tmp_path / "a.db")
    assert gate(db, commit_payload()).returncode == 0


# --- gating logic -------------------------------------------------------------

def _session_with(phases, tmp_path, ended_at=OPEN):
    return seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 7, "task_id": "task", "created_at": T0, "ended_at": ended_at}],
        checks=[{"task_id": "task", "phase": p, "ts": T1} for p in phases],
    )


def test_all_three_phases_present_allows(tmp_path):
    db = _session_with(("baseline", "after", "review"), tmp_path)
    assert gate(db, commit_payload()).returncode == 0


@pytest.mark.parametrize("missing", ["after", "review"])
def test_missing_phase_blocks(tmp_path, missing):
    phases = [p for p in ("baseline", "after", "review") if p != missing]
    db = _session_with(phases, tmp_path)
    r = gate(db, commit_payload())
    assert r.returncode == 2
    assert r.stdout == ""            # the gate never writes stdout
    assert "commit blocked" in r.stderr
    assert "task" in r.stderr
    assert f"{missing}=0" in r.stderr


def test_block_message_is_not_json(tmp_path):
    db = _session_with(("baseline",), tmp_path)
    r = gate(db, commit_payload())
    assert r.returncode == 2
    assert not r.stderr.lstrip().startswith("{")
    assert "end-session 7" in r.stderr


def test_checks_older_than_session_do_not_count(tmp_path):
    """Evidence from a previous run of the same task_id must not unlock this one."""
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 3, "task_id": "task", "created_at": T0}],
        checks=[
            {"task_id": "task", "phase": "baseline", "ts": T1},
            {"task_id": "task", "phase": "after", "ts": T_BEFORE},
            {"task_id": "task", "phase": "review", "ts": T_BEFORE},
        ],
    )
    r = gate(db, commit_payload())
    assert r.returncode == 2
    assert "after=0" in r.stderr and "review=0" in r.stderr


# --- session selection --------------------------------------------------------

def test_explicit_session_id_is_used_even_when_ended(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[
            {"id": 1, "task_id": "ended", "created_at": T0, "ended_at": T1},
            {"id": 2, "task_id": "open", "created_at": T0},
        ],
        checks=[{"task_id": "open", "phase": p, "ts": T1}
                for p in ("baseline", "after", "review")],
    )
    # Session 1 has zero evidence; auto-detect would have picked session 2 and allowed.
    r = gate(db, commit_payload(), session_id="1")
    assert r.returncode == 2
    assert "task ended" in r.stderr


def test_non_numeric_session_id_falls_back_to_autodetect(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 5, "task_id": "auto", "created_at": T0}],
        checks=[{"task_id": "auto", "phase": "baseline", "ts": T1}],
    )
    r = gate(db, commit_payload(), session_id="not-a-number")
    assert r.returncode == 2
    assert "task auto" in r.stderr


def test_autodetect_ignores_sessions_without_baseline_evidence(tmp_path):
    """A throwaway session with no baseline check is invisible to the gate."""
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 9, "task_id": "throwaway", "created_at": T0}],
    )
    assert gate(db, commit_payload()).returncode == 0


def test_autodetect_ignores_ended_sessions(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 9, "task_id": "done", "created_at": T0, "ended_at": T1}],
        checks=[{"task_id": "done", "phase": "baseline", "ts": T1}],
    )
    assert gate(db, commit_payload()).returncode == 0


def test_autodetect_picks_newest_qualifying_session(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[
            {"id": 1, "task_id": "older", "created_at": T0},
            {"id": 2, "task_id": "newer", "created_at": T0},
        ],
        checks=[
            {"task_id": "older", "phase": "baseline", "ts": T1},
            {"task_id": "newer", "phase": "baseline", "ts": T1},
        ],
    )
    r = gate(db, commit_payload())
    assert r.returncode == 2
    assert "task newer" in r.stderr


def test_empty_session_id_env_uses_autodetect(tmp_path):
    db = seed_db(
        tmp_path / "a.db",
        sessions=[{"id": 4, "task_id": "auto", "created_at": T0}],
        checks=[{"task_id": "auto", "phase": "baseline", "ts": T1}],
    )
    r = gate(db, commit_payload(), session_id="")
    assert r.returncode == 2
    assert "task auto" in r.stderr


def test_non_commit_bash_command_is_never_blocked(tmp_path):
    db = _session_with(("baseline",), tmp_path)
    r = gate(db, commit_payload("git push origin main"))
    assert r.returncode == 0
