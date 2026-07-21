# Bug Report: `create-shortcuts` re-sync command fails on Windows — `cat` of a junction yields empty path

**Severity**: Low
**Session**: stub-verdict-passed-flag
**Phase at time of failure**: implementation (post-edit shortcut re-sync)
**Affected component**: `CLAUDE.md` (re-sync note, line 47), `plugin/scripts/anvil-config.py create-shortcuts`, and any plan step reusing the documented idiom

---

## Summary

After editing a file under `plugin/commands/`, the documented re-sync step is `python plugin/scripts/anvil-config.py create-shortcuts "$(cat ~/.claude-anvil/plugin-root 2>/dev/null || echo "${CLAUDE_PLUGIN_ROOT}")"`. On Windows, `~/.claude-anvil/plugin-root` is a **directory junction**, not a text file containing a path. `cat` of a directory returns an empty string, and `${CLAUDE_PLUGIN_ROOT}` is unset in a plain Bash shell, so `create-shortcuts` was invoked with an empty argument and errored: `source not found: C:\Users\Aleksei\claude-anvil\commands\anvil.md`. Expected: the command resolves the installed plugin root and re-syncs the shortcuts. Actual: it aborted until the plugin directory was passed explicitly.

---

## Reconstructed Timeline / Failure Sequence

| Step | What happened |
|------|--------------|
| 1 | Edited `plugin/commands/anvil.md`; needed to re-sync installed shortcut per CLAUDE.md. |
| 2 | Ran documented command with `$(cat ~/.claude-anvil/plugin-root …)`. |
| 3 | `cat` of the junction returned empty; `${CLAUDE_PLUGIN_ROOT}` unset → arg empty. |
| 4 | `create-shortcuts` resolved source to `…\claude-anvil\commands\anvil.md` (wrong; real path is `…\plugin\commands\…`) and exited 1. |
| 5 | Re-ran with explicit `"C:/Users/Aleksei/claude-anvil/plugin"` → succeeded. |

---

## Root Cause

`~/.claude-anvil/plugin-root` is created as a filesystem **junction/symlink to a directory** (confirmed: `plugin-root -> /c/Users/Aleksei/claude-anvil/plugin`). The re-sync idiom treats it as a **file whose contents are the path**. `cat <directory>` does not print the target path — it errors or yields nothing — so the command substitution collapses to an empty string. The `|| echo "${CLAUDE_PLUGIN_ROOT}"` fallback only helps when that env var is exported, which it is not in an ad-hoc Bash shell.

---

## Impact

Low. No data loss and no false ledger records. The re-sync silently produces the wrong source path and fails fast, so a user could believe shortcuts were re-synced when they were not — the exact "stale shortcut" class of bug CLAUDE.md warns about. Purely a documentation/idiom defect; the underlying `create-shortcuts` works correctly when given a valid path.

---

## Reproduction Steps

1. On Windows, with the plugin installed (junction at `~/.claude-anvil/plugin-root`).
2. In a plain Bash shell (no `CLAUDE_PLUGIN_ROOT` exported).
3. Run `python plugin/scripts/anvil-config.py create-shortcuts "$(cat ~/.claude-anvil/plugin-root 2>/dev/null || echo "${CLAUDE_PLUGIN_ROOT}")"`.
4. Observe `anvil-config: source not found: …\commands\anvil.md` and exit code 1.

---

## Fix

**Immediate**: Pass the real plugin directory: `python plugin/scripts/anvil-config.py create-shortcuts "C:/Users/Aleksei/claude-anvil/plugin"`.

**Systemic**: Resolve the junction **target** instead of `cat`-ing it. Options:
- Docs: use `readlink -f ~/.claude-anvil/plugin-root` (or `python -c "import os;print(os.path.realpath(os.path.expanduser('~/.claude-anvil/plugin-root')))"`) in the re-sync one-liner, and reference `~/.claude-anvil/plugin-root` directly as the path argument (it *is* the plugin dir via the junction).
- Code: have `create-shortcuts` treat an empty/invalid argument by falling back to the existing `plugin-root` junction rather than emitting a misleading "source not found".

---

## Files Involved

- `CLAUDE.md` — re-sync note (line 47) documents the failing `cat`-based idiom.
- `plugin/scripts/anvil-config.py` — `create-shortcuts` could fall back to the junction on empty input.
