-- claude-anvil verification ledger + session memory
-- Location: $ANVIL_DB_PATH (default ~/.claude-anvil/anvil.db)
-- All DDL is idempotent; `anvil-ledger.sh init` runs this on first use and on upgrades.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Every verification check (baseline, after, review) for every Medium/Large task.
-- The Evidence Bundle is a SELECT over this table -- no verification exists unless it's here.
CREATE TABLE IF NOT EXISTS anvil_checks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    phase          TEXT NOT NULL CHECK(phase IN ('baseline', 'after', 'review')),
    check_name     TEXT NOT NULL,
    tool           TEXT NOT NULL,
    command        TEXT,
    exit_code      INTEGER,
    output_snippet TEXT,
    passed         INTEGER NOT NULL CHECK(passed IN (0, 1)),
    ts             DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_anvil_checks_task ON anvil_checks(task_id, phase);

-- One row per /anvil invocation. Created at step 0; summary + ended_at filled in at step 8.
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    repo_path  TEXT NOT NULL,
    branch     TEXT,
    summary    TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    ended_at   DATETIME
);
CREATE INDEX IF NOT EXISTS ix_sessions_ended_at ON sessions(ended_at);
CREATE INDEX IF NOT EXISTS ix_sessions_created_at ON sessions(created_at);

-- Populated by the PostToolUse hook on every Edit/Write/MultiEdit, plus explicit track-edit calls.
-- Enables the Recall step ("has this file been touched by a past session?").
CREATE TABLE IF NOT EXISTS session_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    file_path  TEXT NOT NULL,
    tool_name  TEXT NOT NULL,
    ts         DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_session_files_path ON session_files(file_path);
CREATE INDEX IF NOT EXISTS ix_session_files_session ON session_files(session_id);

-- Full-text index for recall-issues: any past session note, reviewer finding, or end-session summary
-- can be MATCHed for 'broke OR regression OR bug OR failed OR reverted'.
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    content,
    session_id UNINDEXED,
    source_type UNINDEXED
);

-- Key/value store for facts the agent learns (build commands, codebase conventions, etc.)
CREATE TABLE IF NOT EXISTS memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT UNIQUE NOT NULL,
    value      TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
