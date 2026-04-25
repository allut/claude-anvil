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
import sqlite3
import sys
from pathlib import Path

GIT_COMMIT_RE = re.compile(r"(?<![A-Za-z0-9_-])git\s+commit\b")
BENIGN_FLAGS_RE = re.compile(r"\b(--help|-h|--dry-run)\b")

BLOCK_MESSAGE = (
    "anvil: commit blocked -- ledger evidence missing for task {task_id}\n"
    "  baseline={n_baseline}  after={n_after}  review={n_review}\n"
    "Run the /anvil Forge (Steps 3b, 5, 5c) before committing. To commit "
    "outside the workflow, end the session with:\n"
    '  python "${{CLAUDE_PLUGIN_ROOT}}/scripts/anvil-ledger.py" end-session '
    "{session_id} \"<recap>\"\n"
)


def db_path() -> Path:
    raw = os.environ.get("ANVIL_DB_PATH", "~/.claude-anvil/anvil.db")
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def is_gated_git_commit(command: str) -> bool:
    if not command:
        return False
    if not GIT_COMMIT_RE.search(command):
        return False
    if BENIGN_FLAGS_RE.search(command):
        return False
    return True


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
    command = (data.get("tool_input") or {}).get("command") or ""
    if not is_gated_git_commit(command):
        return 0

    path = db_path()
    if not path.exists():
        return 0

    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        row = conn.execute(
            "SELECT id, task_id FROM sessions WHERE ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return 0
        session_id, task_id = row
        counts = {}
        for phase in ("baseline", "after", "review"):
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM anvil_checks WHERE task_id = ? AND phase = ?",
                (task_id, phase),
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
        )
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
