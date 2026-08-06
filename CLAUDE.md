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
    anvil_shared.py            # shared helpers (e.g. db_path) imported by ledger + hook scripts
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
- `~/.claude-anvil/plugin-root` — junction/symlink created by `create-shortcuts`; points to the installed plugin directory
- `~/.claude/commands/anvil.md` and `anvil-setup.md` — bare shortcuts written by `create-shortcuts`
- `$ANVIL_TMPDIR/anvil-diff-<task_id>.patch` — staged diff snapshot used by reviewers (resolved via `python -c "import tempfile; print(tempfile.gettempdir())"` — on Windows this is `%LOCALAPPDATA%\Temp`, not MSYS2's `/tmp`)
- `$ANVIL_TMPDIR/anvil-review-<provider>-<task_id>.json` — per-reviewer JSON verdicts (same temp dir)

> **Note**: After editing any file under `plugin/commands/`, re-run `create-shortcuts` to sync the installed copies: `python plugin/scripts/anvil-config.py create-shortcuts` (resolves the installed `~/.claude-anvil/plugin-root` junction automatically). Stale shortcuts are the most common source of Windows `/tmp` path bugs.

## Key design invariants

**anvil-config.py is the single owner of config.json.** Never write to it directly. Use the `save`, `set`, `enable`, `disable`, `set-caveman`, or `gui-key` subcommands. `enable`/`disable PROVIDER` flip `reviewers.<name>.enabled` and sync roster membership without touching `api_key`/`endpoint`/`model` — that is the supported way to turn a reviewer off without re-running the setup wizard or re-entering credentials. The schema carries a top-level `caveman` block (`{"enabled": false, "level": "full"}`) alongside `reviewers` and `roster`; `set-caveman LEVEL|off` is the only writer for it and `caveman` resolves the active level (env → config → `off`). The `caveman` skill itself is an optional external dependency (not bundled with claude-anvil, lives at `~/.claude/skills/caveman/`); when it's absent `/anvil` degrades to normal prose regardless of the configured level.

**anvil-ledger.py is the single owner of the SQLite DB.** Never shell out to raw `sqlite3`. All SQL goes through the ledger CLI.

**API keys never appear in argv, shell args, or tool results.** On Windows they are DPAPI-encrypted and stored in config.json with a `dpapi:` prefix. On macOS/Linux they go to the OS keychain. `gui-key` collects them via a Tkinter password dialog.

**Env vars win over config.json.** The full env var list is in `.env.example`. This is enforced in `anvil-config.py resolve_value()` — except the per-provider `enabled` flag, which has its own validated resolver `resolve_enabled()` (`ANVIL_<PROVIDER>_ENABLED`, `true/1/yes/on` vs `false/0/no/off`, non-boolean values warn and fall back to config). `get PROVIDER enabled` routes through it and `anvil-review.py` asks via that subcommand rather than parsing the env var itself — keep it that way, or the reviewer gate and the roster commands will drift apart. `_TRUE_TOKENS`/`_FALSE_TOKENS` are the single vocabulary for *every* reader and writer of that flag: `resolve_enabled()` uses them for both the env var and a string left in config.json (never `bool()`, which calls `"0"` truthy), and `set PROVIDER enabled=…` uses them to store a real JSON bool, exiting 2 on anything else. Env precedence is also enforced in `resolve_caveman()` (for `ANVIL_CAVEMAN_LEVEL`, which overrides the config `caveman` block; valid levels are `lite/full/ultra/wenyan-lite/wenyan-full/wenyan-ultra`, and `off/none/disabled/empty` disables it).

**`hooks/hooks.json` wires two hooks:**
- `PreToolUse Bash` → `anvil-gate-commit.py` — blocks commits not issued by /anvil
- `PostToolUse Edit|Write|MultiEdit` → `anvil-track-edit.py` — feeds session file tracking

Neither hook may hardcode `python3`: on a stock python.org Windows install `python3`
is a Microsoft Store alias that opens the Store instead of running the script. Both
commands resolve the interpreter as `$ANVIL_PYTHON`, else `python3` when
`command -v python3` resolves to a **non-empty** file (`[ -s ]` — the Store alias is a
0-byte reparse point), else `python`. Never probe by *running* `python3`: on the
affected machines that is the failure mode, not the test for it. Keep the script
invocation as a single `exec`-shaped call so the hook's exit code (2 = block)
propagates unchanged.

**`anvil-gate-commit.py` tokenizes with `shlex`, never with substring regexes.** Both
the `git … commit` detection and the benign-flag escape hatch (`--help`, `-h`,
`--dry-run`) match whole shell tokens. A regex over the raw command line is wrong in
both directions: it fires on `echo 'git commit'`, and — worse — it would let
`git commit -m "note about --dry-run"` past the gate. `GIT_COMMIT_RE` survives only as
the fail-closed fallback for command lines `shlex.split` cannot parse.

**The detector is deliberately blunt, and must stay that way.** A segment is gated
when it contains a git executable and a bare `commit` token, with no benign flag from
the git token onward. It does not attempt to prove that `commit` is the subcommand
rather than an option value, a redirection target or an argument.

That imprecision is the design, not a shortcut. An earlier version modelled bash's
grammar properly — a global-option walk with `_GIT_OPTS_WITH_VALUE`, redirection
stripping, fd-prefix disambiguation. Four consecutive adversarial review rounds each
found a *different* bypass in it (`git -c x commit`, `git > f commit`,
`git -c 1 > f commit`, …), every one discovered after the change had passed the full
test suite, and every one under-blocking: a real commit went ungated. Precision here
has a bad track record; do not reintroduce it without a much stronger reason than
"the current rule over-blocks".

Three properties remain load-bearing and have named tests:
- **Per-command scoping.** The line is split on shell operators and each candidate is
  judged only by its own segment's flags, from the `git` token onward. Scanning the
  whole line lets `curl -h && git commit -m x` through.
- **Wrapper recursion.** `bash -c "git commit"` / `eval "git commit"` leave the whole
  command as one token; `_scan` recurses, bounded by `_MAX_WRAPPER_DEPTH`.
- **Operator isolation.** `_tokenize` builds `shlex.shlex(..., punctuation_chars=True)`
  rather than calling `shlex.split`. `split` only isolates an operator when whitespace
  surrounds it, so `git commit&&ls` came back as `["git", "commit&&ls"]` and evaded the
  gate completely. Never swap `_tokenize` back for `shlex.split`.

Accepted over-blocks (pinned by tests): `git status > commit`, `git log --grep commit`,
`git -C commit status`. Accepted under-blocks: user-defined git aliases (`git ci`) and
shell functions — detecting those means running `git config` from inside a PreToolUse
hook on every Bash call. The gate is an evidence-discipline aid, not a security
boundary; it is fail-open by design (see the module docstring).


## Reviewer architecture

The `/anvil` loop supports four reviewer providers:
- **claude** — runs as a `Task(subagent_type="code-review-claude")` call; reads `agents/code-review-claude.md`; model set in that file at line 5
- **openai** — OpenAI-compatible Chat Completions (also supports OpenRouter, Groq); called via `anvil-review.py --provider openai`
- **gemini** — Google Gemini generateContent REST API; `anvil-review.py --provider gemini`
- **ollama** — local Ollama `/api/chat`; `anvil-review.py --provider ollama`

All three `anvil-review.py` providers return the same JSON schema:
```json
{"verdict": "pass|concern|fail", "summary": "...", "findings": [...], "advisory": [...]}
```

**A reviewer's verdict is not trusted on its own — `apply_coherence_guard()` runs first.** A reviewer handed a self-describing diff (one whose comments and docs explain each bug it repairs) can satisfy the schema perfectly by transcribing that documentation: well-formed findings, every `fix` field describing what the diff already does, an approving `summary`, and `verdict: "fail"`. `compute_passed()` then writes an authoritative `passed=0` row against code the reviewer praised, and the ledger's one guarantee — that it cannot be hallucinated — is what breaks. Two layers guard this:

- **The prompt is the primary defence.** `SHARED_PROMPT` (and `agents/code-review-claude.md`) forbid reporting bugs the diff fixes, forbid restating the diff's own documentation as findings, and require the verdict to agree with the summary. Keep those instructions in both places — the Claude reviewer shares the failure mode but not the prompt.
- **The guard is the net.** It corrects only what the schema already decides (`fail` with no high-severity finding → `concern`; `pass` with a high-severity finding → `concern` — both map to `passed=1`, so it can never manufacture a FAIL row) and otherwise *annotates* via `advisory`. **A severity label outside `{high, medium, low}` counts as high** (and raises its own advisory): a non-empty label the guard does not recognize — `critical`, `blocker`, a typo, a future prompt's vocabulary — must never be the reason a genuine failure is downgraded to `passed=1`. An absent or empty `severity` is not a drifted label and does not count as high, because reviewer quality is not verifiable from inside the loop. Non-corrective notes: `pass` beside medium findings, `fail`/`concern` with empty findings, and findings citing files the diff does not touch. That last one is a **proxy** for "the finding describes a removed line" — it catches a reviewer narrating code outside the change set, never one narrating a fixed bug inside a file the diff does touch. Do not oversell it as a defect detector.

The guard is skipped when `error` is set: stub verdicts synthesize `concern` with no findings and would trip the empty-findings note on every provider outage. The off-diff advisory alone is additionally skipped when the diff was truncated at `DIFF_HARD_LIMIT` — the reviewer was not shown the tail, so every file past the cutoff would be reported as untouched and a real finding about it would be talked down as noise. `diff_paths()` deliberately over-collects (its `+++`/`---` pattern also matches diff *content* lines starting that way) — over-collecting only suppresses advisories, which is the safe direction for a heuristic that can raise false alarms.

**Every provider's verdict goes through `anvil-review.py`, including Claude's.** Claude is spawned by the loop as a `Task` subagent, so `anvil-review.py` never *calls* it — but `--provider claude` runs an **ingest** path (`ingest_verdict()`) that re-reads the file the subagent wrote, applies the same guards, and rewrites it in place. Before that existed, `claude` was the one reviewer whose verdict reached the ledger on nothing but "the file exists and parses": step 5c did a bare `json.load` and mapped `verdict != "fail"` → `passed=1`. Keep `claude` in `_EXECUTING_PROVIDERS` and out of `PROVIDERS` — the argparse choices are the union, and the ingest branch runs *before* `_provider_enabled()`, because refusing to guard a verdict on account of a config flag would leave the raw file behind for the loop to read.

**`checks_run` is friction, not a trust boundary — never document it as one.** A reviewer can fabricate a verdict outright: return `{"verdict": "pass", "findings": []}` having done no work at all. `apply_provenance_guard()` downgrades a Claude `pass` with an empty `checks_run` to `concern`, marks the record `unverified`, and the loop files it as `review-claude-unverified` rather than `review-claude`. What that buys is a ledger row that reads *unverified* instead of *clean* — nothing more. The field is self-reported by the same agent, so anyone willing to type `"verdict": "pass"` will type `"checks_run": ["pytest"]`. Three constraints hold it in place:
- **Claude only.** openai/gemini/ollama are text-in/text-out with no shell; demanding `checks_run` from them would only teach them to invent entries. `checks_run`/`unverified` are absent from HTTP-provider records for the same reason — an always-empty field invites exactly the misreading above.
- **Coherence guard first, provenance second.** Running provenance first turns an honest unverified `pass` into a `concern`, which then trips the coherence guard's empty-findings note. Ordering is load-bearing and has a test.
- **An unearned `fail` is annotated, never downgraded.** `pass` → `concern` keeps the family invariant that no guard manufactures a FAIL row; downgrading a `fail` would run the other way and hide a real defect.

The two defences that actually matter are elsewhere: the anti-fabrication clause in `agents/code-review-claude.md` ("Earn the verdict" — every verdict backed by work done, and *"I could not verify"* a first-class `concern` answer), and the harness's fabrication warning. That warning arrives only in the notification text; no code in the loop can see it — which is why `--self-approved` exists as a model-driven flag rather than something `ingest_verdict()` detects on its own.

**`[Self Approval]` is disclosed, not discarded.** An earlier version of this rule routed *any* harness warning naming the subagent's output — `[Self Approval]` included — to `review-loop-failure`. That was wrong: in-family review is the `/anvil` architecture, not an anomaly. `claude` is the default Medium reviewer and the first Large reviewer, and every run has it reviewing a diff the same session authored, so the blanket rule fired on essentially every task and permanently voided the reviewer the loop depends on. The narrow rule: only a flag asserting the output is **fabricated** — not the product of real work — discards (`review-loop-failure`, `--passed 0`, triggers 6b). A flag saying the approver and the diff's author are the same party is a disclosure input: `ingest_verdict(..., self_approved=True)` records it as a `self_approved` field plus an unconditional `advisory` note (fired even when the verdict file itself is unparseable, since it is a fact about the notification, not an inference from the payload) and never touches `verdict` or `passed`. The loop files it as `review-claude-self-approved` — a row that counts toward the step-5c gate and does not cap Confidence, unlike `review-claude-unverified`. `main()` rejects `--self-approved` for any provider outside `_EXECUTING_PROVIDERS` (exit 2): it is a Claude-reviewer-only concept, kept out of HTTP-provider records for the same reason `checks_run`/`unverified` are.

**Reviewer HTTP calls are bounded by two true wall-clock deadlines** — `ANVIL_REVIEW_HTTP_TIMEOUT` per attempt (default 180s) and `ANVIL_REVIEW_TOTAL_TIMEOUT` per call including retries and backoff (default 420s). `urlopen(timeout=…)` alone is per-socket-operation, not a total deadline, so `http_post_json` runs each request on a daemon thread and joins with a deadline. Use `threading.Thread(daemon=True)`, never `ThreadPoolExecutor` — its atexit handler joins workers and would hang the process on the very stuck socket this guards against.

## Useful script invocations

```bash
# Check config status
python plugin/scripts/anvil-config.py status

# Show full masked config summary
python plugin/scripts/anvil-config.py summary

# Check keychain storage
python plugin/scripts/anvil-config.py keychain-status

# Create bare /anvil and /anvil-setup shortcuts in ~/.claude/commands/
python plugin/scripts/anvil-config.py create-shortcuts "${CLAUDE_PLUGIN_ROOT}"

# Initialize / upgrade ledger schema
python plugin/scripts/anvil-ledger.py init

# Query what sessions touched a file
python plugin/scripts/anvil-ledger.py recall "filename"

# List memory keys
python plugin/scripts/anvil-ledger.py memory-list

# Validate a reviewer's credentials are reachable
python plugin/scripts/anvil-config.py validate gemini

# Turn a reviewer off / back on without touching its API key or endpoint
python plugin/scripts/anvil-config.py disable openai
python plugin/scripts/anvil-config.py enable openai
```

## Changing the Claude reviewer model

Edit `plugin/agents/code-review-claude.md` line 5 (`model: sonnet|haiku|opus`). The `/anvil-setup` wizard also does this automatically.

## Windows notes

- DPAPI encryption requires PowerShell to be in PATH (it always is on Windows).
- The `gui-key` subcommand uses Tkinter for the password dialog; Python 3.x on Windows ships Tkinter by default.
- `fix-windows-plugin.ps1` / `.bat` resolve EPERM issues when Claude Code can't create symlinks.
