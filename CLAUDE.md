# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`claude-anvil` is a Claude Code plugin that adds an evidence-first coding loop to Claude Code. It ports [burkeholland/anvil](https://github.com/burkeholland/anvil). The plugin exposes two slash commands (`/anvil`, `/anvil-setup`) and ships Python scripts, a SQLite schema, and a hooks config that wire everything together.

## No build step

This project is pure Python using only stdlib. There is nothing to compile or install from this repo. The Python scripts run directly; `__pycache__/` is gitignored.

## Plugin structure

```
plugin/
  .claude-plugin/plugin.json   # plugin manifest (name, version, commands, MCP)
  commands/
    anvil.md                   # /anvil skill — the full Anvil loop
    anvil-setup.md             # /anvil-setup skill — first-run config wizard
  agents/
    code-review-claude.md      # subagent definition for the Claude adversarial reviewer
  scripts/
    anvil-config.py            # owns ~/.claude-anvil/config.json; all config I/O
    anvil-ledger.py            # SQLite ledger + session memory CLI
    anvil-review.py            # dispatches review calls to OpenAI/Gemini/Ollama
    anvil-gate-commit.py       # PreToolUse hook: blocks raw git commits outside /anvil
    anvil-track-edit.py        # PostToolUse hook: records every Edit/Write/MultiEdit
    fix-windows-plugin.ps1/.bat  # one-time Windows plugin setup helpers
  sql/
    schema.sql                 # idempotent DDL; applied via `anvil-ledger.py init`
  hooks/hooks.json             # hook wiring (PreToolUse Bash → gate-commit, PostToolUse Edit|Write|MultiEdit → track-edit)
  .mcp.json                    # MCP server: context7 (library docs)
  .env.example                 # env var reference for all ANVIL_* overrides
```

## Runtime state (not in repo)

- `~/.claude-anvil/config.json` — reviewer config; written by `anvil-config.py save`
- `~/.claude-anvil/anvil.db` — SQLite ledger; override with `$ANVIL_DB_PATH`
- `/tmp/anvil-diff-<task_id>.patch` — staged diff snapshot used by reviewers
- `/tmp/anvil-review-<provider>-<task_id>.json` — per-reviewer JSON verdicts

## Key design invariants

**anvil-config.py is the single owner of config.json.** Never write to it directly. Use the `save`, `set`, or `gui-key` subcommands.

**anvil-ledger.py is the single owner of the SQLite DB.** Never shell out to raw `sqlite3`. All SQL goes through the ledger CLI.

**API keys never appear in argv, shell args, or tool results.** On Windows they are DPAPI-encrypted and stored in config.json with a `dpapi:` prefix. On macOS/Linux they go to the OS keychain. `gui-key` collects them via a Tkinter password dialog.

**Env vars win over config.json.** The full env var list is in `.env.example`. This is enforced in `anvil-config.py resolve_value()`.

**`hooks/hooks.json` wires two hooks:**
- `PreToolUse Bash` → `anvil-gate-commit.py` — blocks commits not issued by /anvil
- `PostToolUse Edit|Write|MultiEdit` → `anvil-track-edit.py` — feeds session file tracking

## Reviewer architecture

The `/anvil` loop supports four reviewer providers:
- **claude** — runs as a `Task(subagent_type="code-review-claude")` call; reads `agents/code-review-claude.md`; model set in that file at line 5
- **openai** — OpenAI-compatible Chat Completions (also supports OpenRouter, Groq); called via `anvil-review.py --provider openai`
- **gemini** — Google Gemini generateContent REST API; `anvil-review.py --provider gemini`
- **ollama** — local Ollama `/api/chat`; `anvil-review.py --provider ollama`

All three `anvil-review.py` providers return the same JSON schema:
```json
{"verdict": "pass|concern|fail", "summary": "...", "findings": [...]}
```

## Useful script invocations

```bash
# Check config status
python plugin/scripts/anvil-config.py status

# Show full masked config summary
python plugin/scripts/anvil-config.py summary

# Check keychain storage
python plugin/scripts/anvil-config.py keychain-status

# Initialize / upgrade ledger schema
python plugin/scripts/anvil-ledger.py init

# Query what sessions touched a file
python plugin/scripts/anvil-ledger.py recall "filename"

# List memory keys
python plugin/scripts/anvil-ledger.py memory-list

# Validate a reviewer's credentials are reachable
python plugin/scripts/anvil-config.py validate gemini
```

## Changing the Claude reviewer model

Edit `plugin/agents/code-review-claude.md` line 5 (`model: sonnet|haiku|opus`). The `/anvil-setup` wizard also does this automatically.

## Windows notes

- DPAPI encryption requires PowerShell to be in PATH (it always is on Windows).
- The `gui-key` subcommand uses Tkinter for the password dialog; Python 3.x on Windows ships Tkinter by default.
- `fix-windows-plugin.ps1` / `.bat` resolve EPERM issues when Claude Code can't create symlinks.
