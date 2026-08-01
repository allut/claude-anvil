#!/usr/bin/env python3
"""anvil-gate-commit.py -- PreToolUse gate for `git commit`.

Blocks `git commit` when an /anvil session is active but the ledger is
missing evidence in any of the three phases (baseline, after, review).

Exit codes follow Claude Code's hook convention:
  0 -> allow the tool call (default for anything not a gated `git commit`).
  2 -> block the tool call; stderr is surfaced back to the model.

Fail-open: if stdin is malformed, the DB is unreachable, or the schema is
unexpected, we exit 0. A broken gate that stops every commit would cause
more damage than no gate at all.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from anvil_shared import db_path  # noqa: E402

# Only used when the command line cannot be tokenized (unbalanced quotes). The
# token walk below is the real detector -- see is_gated_git_commit.
GIT_COMMIT_RE = re.compile(r"(?<![A-Za-z0-9_-])git\s+commit\b")

# Flags that make `git commit` a no-op. Matched as whole shell tokens, never as
# substrings: a regex would let `git commit -m "note about --dry-run"` past the
# gate, since the flag spelling would appear inside the quoted message.
BENIGN_FLAGS = frozenset({"--help", "-h", "--dry-run"})

# Tokens that separate one command from the next. A benign flag is only benign
# for the command it belongs to: `curl -h && git commit -m x` contains a real
# commit, and scanning the whole line for "-h" would wave it through.
_SHELL_OPERATORS = frozenset({"&&", "||", ";", ";;", "|", "|&", "&", "\n",
                             "(", ")"})

# Shells whose `-c` argument is itself a command line: `bash -c "git commit"`.
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "busybox",
                             "bash.exe", "sh.exe", "zsh.exe"})

# `eval <words>` concatenates its arguments and re-parses them as a command line,
# so a quoted `eval "git commit -m x"` arrives here as a single opaque token.
# Prefixes like sudo/env/nohup need no special case: their arguments are already
# separate tokens, so the ordinary scan finds the `git` token on its own.
_EVAL_WRAPPERS = frozenset({"eval"})
_DASH_C_RE = re.compile(r"^-[A-Za-z]*c$")
_MAX_WRAPPER_DEPTH = 3

# Command substitution and grouping glue onto the executable token: shlex turns
# `echo $(git commit)` into ["echo", "$(git", "commit)"].
_SUBST_LEAD = "$({`"
_SUBST_TRAIL = ")}`"


def _bare(token: str) -> str:
    """Strip command-substitution/grouping punctuation from around a token."""
    return token.lstrip(_SUBST_LEAD).rstrip(_SUBST_TRAIL)

_LEDGER_PATH = str(_SCRIPT_DIR / "anvil-ledger.py")
# Double-quote the path for display; escape any embedded double-quotes so
# the copy-paste command remains syntactically valid.
_LEDGER_PATH_QUOTED = '"' + _LEDGER_PATH.replace('"', '\\"') + '"'

BLOCK_MESSAGE = (
    "anvil: commit blocked -- ledger evidence missing for task {task_id}\n"
    "  baseline={n_baseline}  after={n_after}  review={n_review}\n"
    "Run the /anvil Forge (Steps 3b, 5, 5c) before committing. To commit "
    "outside the workflow, end the session with:\n"
    "  python {ledger_path_quoted} end-session {session_id} \"<recap>\"\n"
)


def _is_git_executable(token: str) -> bool:
    """True for `git`, `/usr/bin/git`, `C:/Program Files/Git/bin/git.exe`, `$(git`."""
    base = _bare(token).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base in ("git", "git.exe")


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """Break a token stream into individual commands on shell operators."""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _segment_is_gated_commit(segment: list[str]) -> bool:
    """True if this single command looks like a `git commit` with no benign flag.

    Deliberately blunt: the segment must contain a git executable and a bare
    `commit` token, and that is the whole test. It does NOT try to prove `commit`
    is the subcommand rather than an argument, a redirection target or an option
    value.

    That precision is what four consecutive adversarial review rounds each broke,
    every time by a different shell construct (`git -c x commit`, `git > f commit`,
    `git -c 1 > f commit`, ...). Each attempt to model bash's grammar more exactly
    introduced a new hole, and every hole under-blocked -- a real commit went
    ungated. The blunt rule cannot under-block for any of those shapes.

    The cost is occasional over-blocking: `git log --grep commit`, `git status >
    commit`, and `git -C commit status` are all gated even though none commits.
    That is the safe direction for a gate whose job is to insist on ledger
    evidence, and it is easy for a user to work around (end the session, or run
    the command a different way). Under-blocking is silent and defeats the point.
    """
    git_at = next((i for i, t in enumerate(segment) if _is_git_executable(t)), None)
    if git_at is None:
        return False
    if not any(_bare(t) == "commit" for t in segment[git_at:]):
        return False
    # A benign flag only excuses the commit when it belongs to the git invocation
    # itself, so the scan starts at the git token: `sudo -h git commit` is real.
    return not any(t in BENIGN_FLAGS for t in segment[git_at:])


def _wrapped_command_is_gated(segment: list[str], depth: int) -> bool:
    """Recurse into `bash -c "<command>"` / `eval "<command>"`, which the
    tokenizer leaves as a single opaque token."""
    for k, token in enumerate(segment):
        base = _bare(token).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in _EVAL_WRAPPERS:
            if _scan(" ".join(segment[k + 1:]), depth + 1):
                return True
            continue
        if base not in _SHELL_WRAPPERS:
            continue
        for m in range(k + 1, len(segment) - 1):
            if _DASH_C_RE.match(segment[m]):
                if _scan(segment[m + 1], depth + 1):
                    return True
                break
    return False


def _tokenize(command: str) -> list[str]:
    """Split a command line into words AND standalone shell operators.

    `shlex.split` only isolates an operator when whitespace happens to surround
    it, so `git commit&&ls` would come back as ["git", "commit&&ls"] and slip the
    gate. `punctuation_chars=True` makes runs of shell metacharacters their own
    tokens regardless of spacing, while still honouring quotes.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _scan(command: str, depth: int) -> bool:
    try:
        tokens = _tokenize(command)
    except ValueError:
        # Unbalanced quotes -- we cannot reason about the command. Fail closed
        # on the regex rather than waving through something that says "commit".
        return bool(GIT_COMMIT_RE.search(command))
    for segment in _split_segments(tokens):
        if _segment_is_gated_commit(segment):
            return True
        if depth < _MAX_WRAPPER_DEPTH and _wrapped_command_is_gated(segment, depth):
            return True
    return False


def is_gated_git_commit(command: str) -> bool:
    """Should this Bash command be held against the ledger?

    Tokenizes with shlex so that shell quoting is respected in both directions:
    a `git commit` inside a quoted echo is not a commit, and a benign flag
    spelled inside a quoted commit message is not a benign flag. The line is
    then split per command, so each `git commit` is judged only by its own flags.
    """
    if not command:
        return False
    return _scan(command, 0)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except Exception as e:
        print(f"anvil-gate-commit: bad hook input: {e}", file=sys.stderr)
        return 0

    if (data.get("tool_name") or "") != "Bash":
        return 0
    tool_input = data.get("tool_input")
    command = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not is_gated_git_commit(command):
        return 0

    path = db_path()
    if not path.exists():
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
                "SELECT id, task_id, created_at FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        else:
            # Only consider sessions that have at least one baseline check;
            # sessions with zero evidence (e.g. throwaway test sessions) are
            # invisible to the gate so they cannot spuriously block commits.
            # anvil_checks has no session_id column — join via task_id instead.
            row = conn.execute(
                "SELECT s.id, s.task_id, s.created_at "
                "FROM sessions s "
                "WHERE s.ended_at IS NULL "
                "  AND EXISTS ("
                "    SELECT 1 FROM anvil_checks c "
                "    WHERE c.task_id = s.task_id AND c.phase = 'baseline'"
                "      AND c.ts >= s.created_at"
                "  ) "
                "ORDER BY s.id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return 0
        session_id, task_id, session_created_at = row
        counts = {}
        for phase in ("baseline", "after", "review"):
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM anvil_checks "
                "WHERE task_id = ? AND phase = ? AND ts >= ?",
                (task_id, phase, session_created_at),
            ).fetchone()
            counts[phase] = n
    except sqlite3.Error as e:
        print(f"anvil-gate-commit: sqlite error (fail-open): {e}", file=sys.stderr)
        return 0

    if all(counts[p] > 0 for p in ("baseline", "after", "review")):
        return 0

    sys.stderr.write(
        BLOCK_MESSAGE.format(
            task_id=task_id,
            session_id=session_id,
            n_baseline=counts["baseline"],
            n_after=counts["after"],
            n_review=counts["review"],
            ledger_path_quoted=_LEDGER_PATH_QUOTED,
        )
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
