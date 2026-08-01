"""Shared utilities imported by multiple anvil scripts."""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DB_PATH = "~/.claude-anvil/anvil.db"


def db_path() -> Path:
    # An empty or whitespace-only ANVIL_DB_PATH is treated as unset. os.environ.get
    # would otherwise hand back "", and Path("") is the CWD -- a silently wrong,
    # project-local ledger instead of the shared one.
    raw = os.environ.get("ANVIL_DB_PATH", "").strip() or DEFAULT_DB_PATH
    return Path(os.path.expanduser(os.path.expandvars(raw)))
