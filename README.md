# claude-anvil

> Evidence-first coding agent for **Claude Code CLI**. Port of [burkeholland/anvil](https://github.com/burkeholland/anvil) from GitHub Copilot CLI.

Your coding agent should prove its work. This one does.

`/anvil` verifies every change before you see it — builds, tests, lints, runs IDE diagnostics, then has other AI models (Claude, GPT, Gemini, Ollama) try to break it. Every check is INSERTed into a SQLite ledger; the final Evidence Bundle is a `SELECT`, not prose. If the `INSERT` didn't happen, the verification didn't happen.

## What you get

- **Adversarial review.** Every change gets attacked by 1 (Medium task) or 3 (Large / 🔴 task) different models in parallel. Claude runs as a Task subagent; GPT / Gemini / Ollama run via Bash scripts. They disagree with each other. That's the point.
- **SQL verification ledger.** Every build, test, lint, diagnostics, and reviewer verdict lives in `anvil_checks`. The Evidence Bundle is `SELECT ... ORDER BY phase` — the agent can't fake it.
- **Baseline snapshots.** Before touching anything, anvil records your project's state. After, it diffs. Regressions are caught before you see the code.
- **Pushback.** Anvil is a senior engineer, not an order taker. If your request introduces tech debt, duplicates existing code, or has a dangerous edge case, it tells you before writing a single line.
- **Session memory.** A PostToolUse hook tracks every file anvil edits. Next session, if you touch that file again, anvil recalls past failures and accounts for them.
- **Git autopilot.** Checks git state, stashes uncommitted work, creates a branch, commits the result with a clean rollback command.
- **Context7 docs.** Bundled MCP server fetches up-to-date library docs so anvil doesn't hallucinate APIs.

## Requirements

- **Claude Code CLI** (2.1.x or newer; plugin support).
- **Python 3.9+** on your `PATH` — used for the ledger and external reviewer scripts (stdlib only, no `pip install`).
- **Git** (obviously).
- **Node.js** (only needed for the bundled Context7 MCP server, launched via `npx`).
- Optional reviewer credentials (see **Reviewer setup** below). Anvil gracefully degrades if a reviewer isn't configured.

## Install

### From GitHub (recommended)

In any Claude Code session:

```
/plugin marketplace add allut/claude-anvil
/plugin install claude-anvil@allut-claude-anvil
```

### For development / local install

```bash
git clone https://github.com/allut/claude-anvil.git
claude --plugin-dir ./claude-anvil
```

Initialize the ledger once (it'll auto-init on first `/anvil` run, but this lets you verify the install):

```bash
python scripts/anvil-ledger.py init
# -> anvil ledger ready at ~/.claude-anvil/anvil.db
```

## Configure

Run `/anvil-setup` from any Claude Code session. The wizard will:

- Ask which reviewers to enable (Claude / Gemini / Ollama / OpenAI-compatible).
- Collect API keys, validate each with a small live request, and write them to `~/.claude-anvil/config.json` (chmod `0600` on POSIX).
- Pick the Claude reviewer's model (`sonnet` / `haiku` / `opus`).
- Offer to `ollama pull` if Ollama is enabled and the model isn't pulled yet.
- Optionally create bare shortcuts so you can type `/anvil` instead of `/claude-anvil:anvil` (see below).

You can re-run `/anvil-setup` any time to reconfigure, or `/anvil-setup reset` to wipe and start over.

### Bare command shortcuts (optional)

The wizard's final step offers to write `~/.claude/commands/anvil.md` and `~/.claude/commands/anvil-setup.md`, which expose the commands without the plugin namespace prefix. After accepting, you can use `/anvil` and `/anvil-setup` directly in any conversation.

Re-running `/anvil-setup` refreshes the shortcuts when the plugin updates. To create them manually:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" create-shortcuts "${CLAUDE_PLUGIN_ROOT}"
```

### Reviewer options

| Reviewer | Cost | Get a key |
|---|---|---|
| **Claude** (Task subagent) | Covered by your Claude Code plan | Already installed — wizard picks the model (`sonnet` default, `haiku` faster/cheaper, `opus` deepest) |
| **Ollama** (local) | Free | Install from [ollama.com](https://ollama.com/download); the wizard offers to `ollama pull` your chosen model |
| **Gemini** (Google AI Studio) | Free tier | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **OpenAI-compatible** | **Paid** for openai.com GPT-5; free via [OpenRouter](https://openrouter.ai) / [Groq](https://groq.com) | The wizard offers OpenAI / OpenRouter / Groq presets, or a custom endpoint |

Default roster after wizard: Medium = first enabled reviewer; Large = up to 3 enabled reviewers (priority `claude > gemini > openai > ollama`).

**Power users:** any `ANVIL_*` env var still wins over `config.json`, so the legacy `.env`-based flow keeps working — see `.env.example` for the full list. The dispatcher (`anvil-review.py`) reads env first, then `config.json`, then defaults.

**Provider quirks handled automatically:**

- **GPT-5 and o1/o3**: the `temperature` field is omitted (those models reject anything but `1`).
- **OpenRouter / Groq**: `HTTP-Referer` and `X-Title` attribution headers are sent unconditionally. If a free-tier model rejects `response_format: {type: json_object}`, the wizard's "JSON mode = off" toggle (or `ANVIL_OPENAI_JSON_MODE=off`) makes anvil fall back to extracting JSON from the reply body.

A commit gate blocks `git commit` during an active `/anvil` session when the ledger is missing evidence in any of the three phases (baseline, after, review). Commits outside an anvil session are unaffected.

## Use

From any Claude Code session inside the project you want to edit:

```
/anvil fix the off-by-one in getNthItem(), plus a regression test
```

What happens next, on a Medium task:

1. **Boost** — your request is rewritten into a precise spec (hidden unless the intent changed).
2. **Git hygiene** — checks for uncommitted changes / being on `main`; pushes back if anything looks risky.
3. **Recall** — queries the SQLite ledger for past anvil runs that touched the same files, so surprises from previous sessions get surfaced.
4. **Survey** — greps the codebase for existing helpers you should extend instead of re-inventing.
5. **Baseline** — runs the project's build/tests/diagnostics and INSERTs the results as `phase='baseline'`.
6. **Implement** — edits the code.
7. **The Forge** — reruns build/tests/diagnostics (as `phase='after'`), then stages the diff and unleashes one reviewer (Claude by default). The reviewer's JSON verdict is INSERTed as `phase='review', check_name='review-claude'`.
8. **Evidence Bundle** — rendered from `SELECT * FROM anvil_checks WHERE task_id = ...`. Any regression, any reviewer finding, shows up.
9. **Commit** — auto-commits with a one-line rollback.

Large tasks (multi-file, auth/crypto/payments, 🔴 files) additionally show a plan step you approve via `AskUserQuestion`, run **three reviewers in parallel**, and include an operational-readiness check (secrets, observability, graceful degradation).

Small tasks (typos, renames, config tweaks) skip the ledger — just Quick Verify + optional commit.

## Inspect the ledger

```bash
# All checks for a task
python scripts/anvil-ledger.py select-bundle fix-login-crash

# Has a file been touched by past anvil runs?
python scripts/anvil-ledger.py recall auth.ts

# Did anything break on this file in the past?
python scripts/anvil-ledger.py recall-issues auth.ts

# List learned facts (build commands, codebase conventions, etc.)
python scripts/anvil-ledger.py memory-list
```

The raw DB is at `~/.claude-anvil/anvil.db`. Open it with any SQLite browser.

## Troubleshooting

- **`EPERM: operation not permitted, rename … allut-claude-anvil → allut-claude-anvil.bak`** (Windows) — Claude Code backs up the old plugin directory before updating it. Windows blocks the rename if any process holds a handle to a file inside that directory (common culprits: Windows Defender real-time scanning, Windows Explorer, or Claude Code itself). Fix:
  1. Exit Claude Code completely.
  2. Double-click `plugin/scripts/fix-windows-plugin.bat` (or run it from any terminal — no PowerShell execution policy change required). It removes the stale plugin directories and prints a confirmation.
  3. Reopen Claude Code and run `/plugin marketplace add allut/claude-anvil` again.

  To prevent recurrence, add `%USERPROFILE%\.claude\plugins` to Windows Defender's exclusion list (*Windows Security → Virus & threat protection → Manage settings → Exclusions*).

- **`anvil-ledger.py: schema missing`** — run `python scripts/anvil-ledger.py init` from the plugin directory so the script can resolve `sql/schema.sql`.
- **"Ollama unreachable"** — is the daemon running? `curl http://localhost:11434/api/tags`.
- **Gemini 429 / quota exceeded** — the default `gemini-2.5-flash` gives 1,500 req/day free. If you're still hitting limits, re-run `/anvil-setup` and generate a new API key at https://aistudio.google.com/apikey, or switch to a different reviewer.
- **Reviewer returned "concern: no parseable verdict"** — the model ignored the JSON-only instruction. Happens occasionally with smaller/older models; `anvil-review.py` attempts to extract the first JSON object from the reply and falls back to `concern` if that fails.
- **PostToolUse hook isn't tracking edits** — the hook only runs while an anvil session is open (`sessions.ended_at IS NULL`). If `/anvil` crashed partway through, run `python scripts/anvil-ledger.py end-session <id> "crashed"` to close it.

## Security note

The Setup Wizard stores API keys in the OS credential store when available — no plaintext lands in `config.json`:

| Platform | Storage |
|---|---|
| macOS | macOS Keychain (`security` CLI); `config.json` holds the marker `"keychain"` |
| Windows | DPAPI-encrypted Base64 inline in `config.json`; unreadable on any other machine or user account |
| Linux | libsecret via `secret-tool`; `config.json` holds the marker `"keychain"` |
| Fallback | Plaintext in `config.json` (`chmod 0600`) if no keychain backend is detected |

Run `python scripts/anvil-config.py keychain-status` to see which backend is active and where each key is stored.

The legacy `.env` file (if you use it) is also gitignored. Double-check your shell history and process listing regardless of storage tier.

**Keys vs. diffs:** API keys are sent only to their issuing service (Gemini key → Google, OpenAI key → OpenAI) as authentication credentials — they are not shared with other providers. What *is* sent to each enabled provider is the staged code diff for review; don't point GPT/Gemini at diffs you can't share with a third party.

## Credits

The Anvil Loop prompt is adapted from [burkeholland/anvil](https://github.com/burkeholland/anvil) (MIT). The Copilot CLI primitives (`ask_user`, `store_memory`, `session_store`, Copilot multi-vendor subagents) were translated to their Claude Code equivalents.

## License

MIT. See [LICENSE](./LICENSE).
