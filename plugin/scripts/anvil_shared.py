"""Shared utilities imported by multiple anvil scripts."""
from __future__ import annotations

import os
from pathlib import Path


def db_path() -> Path:
    raw = os.environ.get("ANVIL_DB_PATH", "~/.claude-anvil/anvil.db")
    return Path(os.path.expanduser(os.path.expandvars(raw)))
