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

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from anvil_shared import db_path  # noqa: E402

GIT_COMMIT_RE = re.compile(r"(?<![A-Za-z0-9_-])git\s+commit\b")
BENIGN_FLAGS_RE = re.compile(r"\b(--help|-h|--dry-run)\b")

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
                "SELECT id, task_id FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        else:
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
            ledger_path_quoted=_LEDGER_PATH_QUOTED,
        )
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
