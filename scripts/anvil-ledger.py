#!/usr/bin/env python3
"""anvil-ledger.py -- SQLite-backed verification ledger + session memory.

The /anvil loop invokes this for every ledger operation. Uses Python's stdlib
sqlite3 -- no external dependencies, works the same on Windows/macOS/Linux.

Schema lives in ../sql/schema.sql and is (re-)applied on every `init` call so
upgrades pick up new tables automatically.

CLI summary:
    anvil-ledger.py init
    anvil-ledger.py insert-check --task-id ID --phase PHASE --check NAME
                                 --tool TOOL [--command CMD] [--exit-code N]
                                 --passed 0|1 [--output STR | --output-file PATH]
    anvil-ledger.py select-bundle TASK_ID
    anvil-ledger.py count-phase TASK_ID PHASE
    anvil-ledger.py start-session TASK_ID [REPO_PATH] [BRANCH]  -> prints session_id
    anvil-ledger.py end-session SESSION_ID SUMMARY
    anvil-ledger.py track-edit SESSION_ID FILE_PATH TOOL_NAME
    anvil-ledger.py recall FILE_PATH_PATTERN
    anvil-ledger.py recall-issues FILE_PATH_PATTERN
    anvil-ledger.py memory-set KEY VALUE
    anvil-ledger.py memory-get KEY
    anvil-ledger.py memory-list
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SCHEMA_PATH = REPO_DIR / "sql" / "schema.sql"

SNIPPET_MAX = 4000


def db_path() -> Path:
    raw = os.environ.get("ANVIL_DB_PATH", "~/.claude-anvil/anvil.db")
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    if fresh:
        apply_schema(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    if not SCHEMA_PATH.exists():
        sys.exit(f"anvil-ledger: schema missing at {SCHEMA_PATH}")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def clip(text: str, n: int = SNIPPET_MAX) -> str:
    return text if len(text) <= n else text[:n]


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
    conn = connect()
    apply_schema(conn)
    print(f"anvil ledger ready at {db_path()}")


def cmd_insert_check(args: argparse.Namespace) -> None:
    output = ""
    if args.output_file:
        output = Path(args.output_file).read_text(encoding="utf-8", errors="replace")
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
    conn.commit()
    print("ok")


def cmd_select_bundle(args: argparse.Namespace) -> None:
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT phase, check_name, tool,
                  COALESCE(exit_code, '') AS exit_code,
                  CASE passed WHEN 1 THEN 'pass' ELSE 'FAIL' END AS result,
                  substr(COALESCE(output_snippet, ''), 1, 120) AS detail
           FROM anvil_checks
           WHERE task_id = ?
           ORDER BY CASE phase WHEN 'baseline' THEN 1 WHEN 'after' THEN 2 WHEN 'review' THEN 3 END, id""",
        (args.task_id,),
    ).fetchall()
    print_table(rows, ["phase", "check_name", "tool", "exit_code", "result", "detail"])


def cmd_count_phase(args: argparse.Namespace) -> None:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM anvil_checks WHERE task_id = ? AND phase = ?",
        (args.task_id, args.phase),
    ).fetchone()
    print(row[0])


def cmd_start_session(args: argparse.Namespace) -> None:
    repo = args.repo_path or str(Path.cwd())
    conn = connect()
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


def cmd_recall(args: argparse.Namespace) -> None:
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.id AS session, s.task_id, s.branch, sf.file_path, sf.tool_name,
                  substr(COALESCE(s.summary, ''), 1, 80) AS summary, s.created_at
           FROM session_files sf
           JOIN sessions s ON sf.session_id = s.id
           WHERE sf.file_path LIKE ?
           ORDER BY s.created_at DESC
           LIMIT 5""",
        (f"%{args.pattern}%",),
    ).fetchall()
    print_table(rows, ["session", "task_id", "branch", "file_path", "tool_name", "summary", "created_at"])


def cmd_recall_issues(args: argparse.Namespace) -> None:
    conn = connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT substr(content, 1, 200) AS hit, session_id, source_type
           FROM search_index
           WHERE search_index MATCH 'regression OR broke OR failed OR reverted OR bug'
             AND session_id IN (
               SELECT s.id FROM session_files sf
               JOIN sessions s ON sf.session_id = s.id
               WHERE sf.file_path LIKE ?
               ORDER BY s.created_at DESC LIMIT 10
             )
           LIMIT 10""",
        (f"%{args.pattern}%",),
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
