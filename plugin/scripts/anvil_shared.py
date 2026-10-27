"""Shared utilities imported by multiple anvil scripts."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


DEFAULT_DB_PATH = "~/.claude-anvil/anvil.db"

# The staged-index listing shared by `anvil-ledger.py snapshot-diff` (what the
# reviewers saw) and the commit gate (what is about to be committed). Both sides
# must run the exact same command or the comparison is meaningless. --no-renames
# keeps a rename as D + A, so every entry is keyed by one path.
STAGED_RAW_ARGS = ("diff", "--cached", "--no-renames", "--raw", "--no-abbrev", "-z")

GIT_TIMEOUT = 30


class GitError(RuntimeError):
    """git ran but exited non-zero (not a repository, corrupt index, ...)."""


def db_path() -> Path:
    # An empty or whitespace-only ANVIL_DB_PATH is treated as unset. os.environ.get
    # would otherwise hand back "", and Path("") is the CWD -- a silently wrong,
    # project-local ledger instead of the shared one.
    raw = os.environ.get("ANVIL_DB_PATH", "").strip() or DEFAULT_DB_PATH
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def run_git(args, cwd) -> bytes:
    """Run git without a shell and return stdout bytes.

    Raises GitError on a non-zero exit, OSError when git is not installed, and
    subprocess.TimeoutExpired when it hangs.
    """
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       timeout=GIT_TIMEOUT)
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(msg[0] if msg else f"git exited {r.returncode}")
    return r.stdout


def parse_raw_z(out: bytes) -> list[dict]:
    """Parse `git diff --raw -z` into [{path, status, dst_mode, dst_sha}].

    Each record is `:srcmode dstmode srcsha dstsha STATUS\\0path\\0`; a copy or
    rename carries a second path (the destination). A deletion has dst_mode
    000000 and the all-zero sha.
    """
    fields = out.split(b"\0")
    entries: list[dict] = []
    i = 0
    while i < len(fields):
        meta = fields[i]
        i += 1
        if not meta.startswith(b":"):
            continue
        parts = meta[1:].decode("ascii", "replace").split()
        if len(parts) < 5:
            raise ValueError(f"unexpected git --raw record: {meta!r}")
        _src_mode, dst_mode, _src_sha, dst_sha, status = parts[:5]
        n_paths = 2 if status[:1] in ("R", "C") else 1
        paths = fields[i:i + n_paths]
        i += n_paths
        if len(paths) != n_paths:
            raise ValueError(f"truncated git --raw record: {meta!r}")
        entries.append({
            "path": paths[-1].decode("utf-8", "surrogateescape"),
            "status": status[:1],
            "dst_mode": dst_mode,
            "dst_sha": dst_sha,
        })
    return entries


def staged_manifest(cwd) -> list[dict]:
    """The index of the repository at ``cwd``, relative to HEAD."""
    return parse_raw_z(run_git(STAGED_RAW_ARGS, cwd))
