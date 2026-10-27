#!/usr/bin/env python3
"""anvil-ledger.py -- SQLite-backed verification ledger + session memory.

The /anvil loop invokes this for every ledger operation. Uses Python's stdlib
sqlite3 -- no external dependencies, works the same on Windows/macOS/Linux.

Schema lives in ../sql/schema.sql and is (re-)applied on every connection
(idempotent DDL), so an existing DB picks up new tables on first use.

count-phase and select-bundle read only checks recorded since the newest
session of the task, so a reused task_id cannot satisfy a gate with stale rows.

CLI summary:
    anvil-ledger.py init
    anvil-ledger.py insert-check --task-id ID --phase PHASE --check NAME
                                 --tool TOOL [--command CMD] [--exit-code N]
                                 --passed 0|1 [--output STR | --output-file PATH]
    anvil-ledger.py select-bundle TASK_ID [--all-sessions]
    anvil-ledger.py count-phase TASK_ID PHASE
    anvil-ledger.py start-session TASK_ID [REPO_PATH] [BRANCH]  -> prints session_id
    anvil-ledger.py end-session SESSION_ID SUMMARY
    anvil-ledger.py track-edit SESSION_ID FILE_PATH TOOL_NAME
    anvil-ledger.py recall FILE_PATH_PATTERN
    anvil-ledger.py recall-issues FILE_PATH_PATTERN
    anvil-ledger.py memory-set KEY VALUE
    anvil-ledger.py memory-get KEY
    anvil-ledger.py memory-list
    anvil-ledger.py snapshot-diff --task-id ID --out FILE    (run inside the repo)
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SCHEMA_PATH = REPO_DIR / "sql" / "schema.sql"

SNIPPET_MAX = 4000

sys.path.insert(0, str(SCRIPT_DIR))
from anvil_shared import GitError, db_path, run_git, staged_manifest  # noqa: E402

# The patch the reviewers read. Same index as anvil_shared.STAGED_RAW_ARGS; color and
# external diff drivers off so a user's git config cannot change what is reviewed.
PATCH_ARGS = ("diff", "--cached", "--no-renames", "--no-color", "--no-ext-diff")
FAIL_LINE_MAX = 200


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    # Always: the DDL is idempotent and there is no schema versioning, so this is
    # how a DB created by an older release gains tables added since.
    apply_schema(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    if not SCHEMA_PATH.exists():
        sys.exit(f"anvil-ledger: schema missing at {SCHEMA_PATH}")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # CREATE TABLE IF NOT EXISTS never alters an existing table. review_snapshots
    # was briefly created without checks_high_water by pre-release builds.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(review_snapshots)")}
    if "checks_high_water" not in cols:
        conn.execute("ALTER TABLE review_snapshots "
                     "ADD COLUMN checks_high_water INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def clip(text: str, n: int = SNIPPET_MAX) -> str:
    return text if len(text) <= n else text[:n]


def _newest_session(conn: sqlite3.Connection, task_id: str):
    """(id, created_at) of the task's newest session, or None."""
    return conn.execute(
        "SELECT id, created_at FROM sessions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _session_floor(conn: sqlite3.Connection, task_id: str):
    """created_at of the task's newest session, or None for a task with no session.

    The same bound the commit gate applies (`ts >= sessions.created_at`): a check
    recorded before the newest session belongs to an earlier run of a reused
    task_id and must not count. None keeps the old flow (no start-session) reading
    every row.
    """
    row = _newest_session(conn, task_id)
    return row[1] if row else None


def print_table(rows: list[sqlite3.Row], headers: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = [len(h) for h in headers]
    data = [[str(r[h] if r[h] is not None else "") for h in headers] for r in rows]
    for row in data:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    sep = "  "
    print(sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print(sep.join("-" * w for w in widths))
    for row in data:
        print(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))))


# --- commands -----------------------------------------------------------------

def cmd_init(_args: argparse.Namespace) -> None:
    connect()  # applies the schema
    print(f"anvil ledger ready at {db_path()}")


def cmd_insert_check(args: argparse.Namespace) -> None:
    output = ""
    if args.output_file:
        p = Path(args.output_file)
        if p.exists():
            output = p.read_text(encoding="utf-8", errors="replace")
        else:
            # Output file missing means the command never wrote output (likely never ran).
            # Force passed=0 regardless of what the caller reported so a failed `cd` in a
            # compound `cd X && cmd > $LOG` can't be silently recorded as a pass.
            if args.passed == 1:
                args.passed = 0
                args.exit_code = -1
                output = "output file missing — command likely did not run"
                print(
                    f"anvil-ledger: warning: --output-file {args.output_file!r} not found; forcing passed=0, recording diagnostic message",
                    file=sys.stderr,
                )
            else:
                print(
                    f"anvil-ledger: warning: --output-file {args.output_file!r} not found; recording empty output",
                    file=sys.stderr,
                )
    elif args.output is not None:
        output = args.output
    output = clip(output)
    conn = connect()
    conn.execute(
        """INSERT INTO anvil_checks
               (task_id, phase, check_name, tool, command, exit_code, output_snippet, passed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (args.task_id, args.phase, args.check, args.tool,
         args.command or "", args.exit_code, output, args.passed),
    )
    # Feed review-phase findings into the FTS index so recall-issues can surface them.
    if args.phase == "review" and output:
        row = _newest_session(conn, args.task_id)
        if row:
            conn.execute(
                "INSERT INTO search_index (content, session_id, source_type) VALUES (?, ?, 'review')",
                (clip(output, 500), row[0]),
            )
    conn.commit()
    # Report what was stored, not what was asked for: a missing output file has
    # already forced passed=0 above. A failure must read as one -- a bare "ok"
    # after --passed 0 has been mistaken for success. Exit code stays 0 so `&&`
    # chains in the loop keep running.
    if args.passed == 1:
        print("ok")
    else:
        first = next((ln.strip() for ln in output.splitlines() if ln.strip()), "")
        first = first[:FAIL_LINE_MAX]
        print(f"FAIL {args.check}: {first}" if first else f"FAIL {args.check}")


def cmd_select_bundle(args: argparse.Namespace) -> None:
    conn = connect()
    conn.row_factory = sqlite3.Row
    floor = None if args.all_sessions else _session_floor(conn, args.task_id)
    where = "task_id = ?" if floor is None else "task_id = ? AND ts >= ?"
    params = (args.task_id,) if floor is None else (args.task_id, floor)
    rows = conn.execute(
        f"""SELECT phase, check_name, tool,
                  COALESCE(exit_code, '') AS exit_code,
                  CASE passed WHEN 1 THEN 'pass' ELSE 'FAIL' END AS result,
                  substr(COALESCE(output_snippet, ''), 1, 120) AS detail
           FROM anvil_checks
           WHERE {where}
           ORDER BY CASE phase WHEN 'baseline' THEN 1 WHEN 'after' THEN 2 WHEN 'review' THEN 3 END, id""",
        params,
    ).fetchall()
    print_table(rows, ["phase", "check_name", "tool", "exit_code", "result", "detail"])
    if floor is not None:
        (hidden,) = conn.execute(
            "SELECT COUNT(*) FROM anvil_checks WHERE task_id = ? AND ts < ?",
            (args.task_id, floor),
        ).fetchone()
        if hidden:
            print(f"anvil-ledger: {hidden} check(s) from earlier sessions of this task_id "
                  "hidden; --all-sessions shows them.", file=sys.stderr)


def cmd_count_phase(args: argparse.Namespace) -> None:
    conn = connect()
    floor = _session_floor(conn, args.task_id)
    if floor is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM anvil_checks WHERE task_id = ? AND phase = ?",
            (args.task_id, args.phase),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM anvil_checks WHERE task_id = ? AND phase = ? AND ts >= ?",
            (args.task_id, args.phase, floor),
        ).fetchone()
    print(row[0])


def cmd_start_session(args: argparse.Namespace) -> None:
    repo = args.repo_path or str(Path.cwd())
    conn = connect()
    n_prev, latest = conn.execute(
        "SELECT COUNT(*), MAX(created_at) FROM sessions WHERE task_id = ?", (args.task_id,)
    ).fetchone()
    if n_prev:
        print(f"anvil-ledger: task_id '{args.task_id}' reused ({n_prev} earlier sessions, "
              f"latest {str(latest)[:10]}); count-phase/select-bundle now show only this "
              "session's checks.", file=sys.stderr)
    cur = conn.execute(
        "INSERT INTO sessions (task_id, repo_path, branch) VALUES (?, ?, ?)",
        (args.task_id, repo, args.branch or ""),
    )
    conn.commit()
    print(cur.lastrowid)


def cmd_end_session(args: argparse.Namespace) -> None:
    conn = connect()
    conn.execute(
        "UPDATE sessions SET summary = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
        (args.summary or "", args.session_id),
    )
    if args.summary:
        conn.execute(
            "INSERT INTO search_index (content, session_id, source_type) VALUES (?, ?, 'summary')",
            (args.summary, args.session_id),
        )
    conn.commit()
    print("ok")


def cmd_track_edit(args: argparse.Namespace) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO session_files (session_id, file_path, tool_name) VALUES (?, ?, ?)",
        (args.session_id, args.file_path, args.tool_name.lower()),
    )
    conn.commit()
    print("ok")


def _escape_like(pattern: str) -> str:
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def cmd_recall(args: argparse.Namespace) -> None:
    escaped = _escape_like(args.pattern)
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.id AS session, s.task_id, s.branch, sf.file_path, sf.tool_name,
                  substr(COALESCE(s.summary, ''), 1, 80) AS summary, s.created_at
           FROM session_files sf
           JOIN sessions s ON sf.session_id = s.id
           WHERE sf.file_path LIKE ? ESCAPE '\\'
           ORDER BY s.created_at DESC
           LIMIT 5""",
        (f"%{escaped}%",),
    ).fetchall()
    print_table(rows, ["session", "task_id", "branch", "file_path", "tool_name", "summary", "created_at"])


def cmd_recall_issues(args: argparse.Namespace) -> None:
    escaped = _escape_like(args.pattern)
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT substr(content, 1, 200) AS hit, session_id, source_type
           FROM search_index
           WHERE search_index MATCH 'regression OR broke OR failed OR reverted OR bug'
             AND session_id IN (
               SELECT s.id FROM session_files sf
               JOIN sessions s ON sf.session_id = s.id
               WHERE sf.file_path LIKE ? ESCAPE '\\'
               ORDER BY s.created_at DESC LIMIT 10
             )
           LIMIT 10""",
        (f"%{escaped}%",),
    ).fetchall()
    print_table(rows, ["hit", "session_id", "source_type"])


def cmd_memory_set(args: argparse.Namespace) -> None:
    conn = connect()
    conn.execute(
        """INSERT INTO memory (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
        (args.key, args.value),
    )
    conn.commit()
    print("ok")


def cmd_memory_get(args: argparse.Namespace) -> None:
    conn = connect()
    row = conn.execute("SELECT value FROM memory WHERE key = ?", (args.key,)).fetchone()
    print(row[0] if row else "")


def cmd_memory_list(_args: argparse.Namespace) -> None:
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, substr(value, 1, 120) AS value, updated_at FROM memory ORDER BY updated_at DESC"
    ).fetchall()
    print_table(rows, ["key", "value", "updated_at"])


def cmd_snapshot_diff(args: argparse.Namespace) -> int:
    """Freeze the staged index the reviewers are about to see.

    Writes the patch to --out and records its manifest, which the commit gate
    later compares the index against. --out is truncated before git runs, so on
    any failure a stale patch from an earlier round can never be reviewed.

    The patch and the manifest come from two git calls. The manifest is read on
    both sides of the patch and must not change, so the gate can never be bound
    to an index the reviewers' patch does not describe.
    """
    cwd = os.getcwd()
    out = Path(args.out)
    out.write_bytes(b"")
    try:
        manifest = staged_manifest(cwd)
        patch = run_git(PATCH_ARGS, cwd)
        if staged_manifest(cwd) != manifest:
            print("ERROR: the index changed while snapshotting; re-run snapshot-diff",
                  file=sys.stderr)
            return 1
    except (GitError, ValueError, subprocess.SubprocessError) as e:
        print(f"ERROR: git failed: {e}", file=sys.stderr)
        return 1
    out.write_bytes(patch)
    if not manifest:
        print("ERROR: nothing staged", file=sys.stderr)
        return 1
    sha = hashlib.sha256(patch).hexdigest()
    conn = connect()
    session = _newest_session(conn, args.task_id)
    # The high-water mark is read inside the INSERT itself, so no check can land
    # between reading it and recording the snapshot.
    cur = conn.execute(
        "INSERT INTO review_snapshots "
        "(session_id, task_id, patch_sha256, manifest, checks_high_water) "
        "VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(id), 0) FROM anvil_checks))",
        (session[0] if session else None, args.task_id, sha, json.dumps(manifest)),
    )
    conn.commit()
    print(f"snapshot {cur.lastrowid}: {len(manifest)} files, sha256 {sha[:12]}")
    return 0


# --- arg parsing --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anvil-ledger.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(__doc__ or ""),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    ic = sub.add_parser("insert-check")
    ic.add_argument("--task-id", required=True)
    ic.add_argument("--phase", required=True, choices=["baseline", "after", "review"])
    ic.add_argument("--check", required=True)
    ic.add_argument("--tool", required=True)
    ic.add_argument("--command", default="")
    ic.add_argument("--exit-code", type=int, default=None)
    ic.add_argument("--passed", required=True, type=int, choices=[0, 1])
    ic.add_argument("--output", default=None)
    ic.add_argument("--output-file", default=None)
    ic.set_defaults(func=cmd_insert_check)

    sb = sub.add_parser("select-bundle")
    sb.add_argument("task_id")
    sb.add_argument("--all-sessions", action="store_true",
                    help="include checks from earlier sessions of this task_id")
    sb.set_defaults(func=cmd_select_bundle)

    cp = sub.add_parser("count-phase")
    cp.add_argument("task_id")
    cp.add_argument("phase", choices=["baseline", "after", "review"])
    cp.set_defaults(func=cmd_count_phase)

    ss = sub.add_parser("start-session")
    ss.add_argument("task_id")
    ss.add_argument("repo_path", nargs="?", default=None)
    ss.add_argument("branch", nargs="?", default=None)
    ss.set_defaults(func=cmd_start_session)

    es = sub.add_parser("end-session")
    es.add_argument("session_id", type=int)
    es.add_argument("summary", nargs="?", default="")
    es.set_defaults(func=cmd_end_session)

    te = sub.add_parser("track-edit")
    te.add_argument("session_id", type=int)
    te.add_argument("file_path")
    te.add_argument("tool_name")
    te.set_defaults(func=cmd_track_edit)

    r = sub.add_parser("recall"); r.add_argument("pattern"); r.set_defaults(func=cmd_recall)
    ri = sub.add_parser("recall-issues"); ri.add_argument("pattern"); ri.set_defaults(func=cmd_recall_issues)

    ms = sub.add_parser("memory-set"); ms.add_argument("key"); ms.add_argument("value"); ms.set_defaults(func=cmd_memory_set)
    mg = sub.add_parser("memory-get"); mg.add_argument("key"); mg.set_defaults(func=cmd_memory_get)
    sub.add_parser("memory-list").set_defaults(func=cmd_memory_list)

    sd = sub.add_parser("snapshot-diff")
    sd.add_argument("--task-id", required=True)
    sd.add_argument("--out", required=True)
    sd.set_defaults(func=cmd_snapshot_diff)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except sqlite3.Error as e:
        # A locked, corrupt or unreachable DB is an operational failure, not a
        # crash to show the user as a traceback.
        print(f"anvil-ledger: sqlite error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"anvil-ledger: {e}", file=sys.stderr)
        return 1
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
