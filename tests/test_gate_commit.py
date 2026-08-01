"""anvil-gate-commit.py -- the PreToolUse commit gate.

Contract under test: exit 0 = allow, exit 2 = block with free-form stderr.
The gate never writes stdout and never emits JSON.
"""
from __future__ import annotations

import shlex

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
        # Benign flags are whole-token matches, so these are genuinely waved
        # through (the old \b-anchored regex could never fire on them).
        ("git commit --dry-run", False),
        ("git commit --help", False),
        ("git commit -h", False),
        # ...but only as a whole token. "x-h" is not the -h flag.
        ("git commit x-h", True),
        # Shell-aware: a `git commit` inside a quoted string is a string, not a
        # commit, so it no longer trips the gate.
        ("echo 'git commit' && ls", False),
        ("git status && echo 'git commit'", False),
        # A real commit later in the same line is still caught.
        ("echo hi && git commit -m x", True),
        # git's own global options do not hide the subcommand.
        ("git -c user.name=x commit -m y", True),
        ("git -C /repo commit", True),
        ("git --no-pager commit", True),
        ("git -C /repo status", False),
        # Deliberate over-block: the rule does not prove that `commit` is the
        # subcommand rather than an option value. See _segment_is_gated_commit.
        ("git -C commit status", True),
        # An explicit interpreter path is still git.
        ("/usr/bin/git commit -m x", True),
        ("git.exe commit -m x", True),
    ],
)
def test_is_gated_git_commit(anvil_gate, command, expected):
    assert anvil_gate.is_gated_git_commit(command) is expected


@pytest.mark.parametrize("command", [
    'git commit -m "note about --dry-run"',
    "git commit -m 'ship it --help'",
    'git commit -m "x" --author="a -h b"',
])
def test_benign_flags_cannot_be_spoofed_from_inside_a_quoted_argument(anvil_gate, command):
    """Regression guard. A substring match on the raw command line would read
    the flag inside the commit message and wave a real commit through the gate.
    shlex keeps the message as one token, so the flag is never seen."""
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command", [
    "curl -h && git commit -m x",
    "terraform apply --dry-run && git commit -m x",
    "git status --help; git commit -m x",
    "git commit -m x | tee -h",
    "sudo -h git commit -m x",
])
def test_a_benign_flag_on_another_command_does_not_excuse_the_commit(anvil_gate, command):
    """Regression guard (reviewer finding, high). The benign-flag check is scoped
    to the segment that owns the `git` token; a `-h`/`--dry-run` belonging to any
    other command on the line must not wave a real commit through."""
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command,expected", [
    ("git commit --dry-run && ls -h", False),
    ("ls -l && git commit --help", False),
    ("git commit --dry-run && git commit -m x", True),
])
def test_benign_flags_still_apply_within_their_own_command(anvil_gate, command, expected):
    assert anvil_gate.is_gated_git_commit(command) is expected


@pytest.mark.parametrize("command", [
    "git commit&&ls",
    "git commit;ls",
    "git commit|cat",
    "ls&&git commit -m x",
    "(git commit -m x)",
    "git commit>out.txt",
    "ls -h&&git commit -m x",
])
def test_operators_without_surrounding_whitespace_still_split_commands(anvil_gate, command):
    """Regression guard (reviewer finding, high). shlex.split only isolates an
    operator when spaces surround it, so `git commit&&ls` tokenized to
    ["git", "commit&&ls"] and evaded the gate entirely. _tokenize uses
    punctuation_chars=True so spacing cannot hide an operator."""
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command", [
    "git > /dev/null commit -m x",
    "git 1>/tmp/x commit -m x",
    "git < /dev/null commit -m x",
    "git 2>&1 commit -m x",
    "git &> /dev/null commit -m x",
    "git commit -m x > out",
    "git commit>out.txt",
])
def test_redirections_do_not_hide_the_subcommand(anvil_gate, command):
    """Regression guard (reviewer finding, high). bash allows a redirection
    anywhere inside a simple command, so `git > /dev/null commit -m x` is a real
    commit. Treating the redirection as a command separator put `git` in one
    segment and `commit` in the next, and the whole line went ungated."""
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command", [
    "git -c 1 > commit.log commit -m x",
    "git -C 2 > out commit -m x",
    "git --git-dir 1 > out commit -m x",
    "git -c 1 >> out commit -m x",
    "git -c 2 2>&1 commit -m x",
])
def test_an_option_value_that_looks_like_an_fd_does_not_hide_the_commit(anvil_gate, command):
    """Regression guard (reviewer finding, high). A bare digit before a
    redirection is ambiguous once the tokenizer drops whitespace -- it may be an
    fd prefix or the value of -c/-C. The blunt rule never has to decide."""
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command,expected", [
    # Documented over-blocks: `commit` appears as an argument or a filename, and
    # the rule does not try to tell that apart from a real subcommand.
    ("git status > commit", True),
    ("git log --grep commit", True),
    ("git log --oneline", False),
    ("git status", False),
    ("git diff --staged", False),
    # No git executable at all -> never gated, whatever the words are.
    ("echo git > commit", True),
    ("echo commit", False),
    ("npm run commit", False),
])
def test_the_blunt_rule_over_blocks_rather_than_under_blocks(anvil_gate, command, expected):
    """Pins the deliberate imprecision. Every case here that returns True is a
    false block, accepted so that no real commit can slip through; the False
    cases confirm ordinary git usage is untouched."""
    assert anvil_gate.is_gated_git_commit(command) is expected


@pytest.mark.parametrize("command", [
    'eval "git commit -m x"',
    "eval 'git commit -m x'",
])
def test_eval_arguments_are_rescanned(anvil_gate, command):
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command", [
    'bash -c "git commit -m x"',
    "sh -c 'git commit -m x'",
    'bash -lc "git commit -m x"',
    "echo $(git commit -m x)",
    "`git commit -m x`",
    'bash -c "bash -c \\"git commit -m x\\""',
])
def test_wrapped_and_substituted_commits_are_still_caught(anvil_gate, command):
    """Regression guard (reviewer finding, high). shlex collapses `bash -c "..."`
    into one token and glues `$(` onto the executable, so a naive token walk sees
    no `git` at all -- a regression against the old substring regex."""
    assert anvil_gate.is_gated_git_commit(command) is True


@pytest.mark.parametrize("command", [
    "echo 'bash -c \"git commit\"'",
    "grep -r 'git commit' .",
])
def test_wrapper_recursion_does_not_reintroduce_the_quoted_string_false_positive(
        anvil_gate, command):
    """Only a real shell's -c argument is re-scanned. Quoting it into echo/grep
    keeps it inert."""
    assert anvil_gate.is_gated_git_commit(command) is False


def test_wrapper_recursion_is_depth_bounded(anvil_gate):
    """A pathological nest must terminate rather than recurse without limit."""
    command = "git commit -m x"
    for _ in range(anvil_gate._MAX_WRAPPER_DEPTH + 3):
        command = "bash -c " + shlex.quote(command)
    assert anvil_gate.is_gated_git_commit(command) in (True, False)


@pytest.mark.parametrize("command,expected", [
    ('git commit -m "unbalanced', True),
    ("echo 'unbalanced && ls", False),
])
def test_untokenizable_commands_fall_back_to_the_regex(anvil_gate, command, expected):
    """shlex.split raises on unbalanced quotes. The gate then falls back to
    GIT_COMMIT_RE and stays closed rather than allowing an unparseable commit."""
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
