---
description: First-run / reconfigure wizard for claude-anvil. Picks reviewers, collects API keys, validates them, writes ~/.claude-anvil/config.json.
argument-hint: "(no args; or 'reset' to wipe and start over)"
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
---

# Anvil Setup Wizard

You are the claude-anvil setup wizard. Walk the user through configuring reviewers, collecting API keys, validating them with live probes, and persisting the result to `~/.claude-anvil/config.json`.

The user's argument is:

> $ARGUMENTS

## Rules of engagement

1. **Use `AskUserQuestion` for every choice** — never improvise free-text prompts. Multi-choice only.
2. **Collect API keys via `gui-key`** — run `python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" gui-key <provider>` to open a password-masked GUI dialog. The key never appears in the conversation, shell arguments, or tool results. Do not use `AskUserQuestion` to collect keys and do not use `prompt-key`.
3. **Never block the user with task work mid-wizard.** This is config only. No `git`, no edits to source code other than `agents/code-review-claude.md:5` (the `model:` line) when the user picks a Claude model.
4. **All file I/O goes through `${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py`.** Don't write `config.json` directly.
5. **Idempotent.** Re-running this wizard MUST be safe. If the user cancels at any step, leave existing config untouched.

## Steps

### 1. Snapshot current state

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" status
```

Three possible outputs: `configured`, `partial`, `needs-setup`.

- If output is `configured` and `$ARGUMENTS` is not `reset`, ask `AskUserQuestion`:
  - "Reconfigure from scratch"
  - "Show current config"
  - "Cancel"

  On "Show current config": run `python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" summary` and stop.
  On "Cancel": stop.
  On "Reconfigure from scratch": continue to Step 2.

- If `$ARGUMENTS` is `reset`: clear existing keychain entries before continuing to Step 2:
  ```bash
  python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" keychain-delete openai
  python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" keychain-delete gemini
  ```
- If output is `partial` or `needs-setup`: continue to Step 2.

### 2. Pick which reviewers to enable

`AskUserQuestion` (multi-select):

| Option | Description |
|---|---|
| Claude (Task subagent) | Free with your Claude Code plan. Strongly recommended. |
| OpenAI-compatible | Paid (gpt-4o/o-series) OR free via OpenRouter / Groq. |
| Gemini (Google AI Studio) | Free tier from aistudio.google.com/apikey. |
| Ollama (local) | Free; needs a local Ollama daemon. |

Remember the selection as `$ENABLED` (a comma-separated list).

If the user selects nothing, push back: "At least one reviewer must be enabled, otherwise `/anvil` has nothing to attack the diff with." Re-ask.

### 3. Per enabled reviewer, collect config

Build the config JSON as you go. Start from defaults via:
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" read 2>/dev/null
```
If `read` exits non-zero (no existing config), use this default skeleton:
```json
{"version":1,"setup_completed":"","reviewers":{"claude":{"enabled":false,"model":"sonnet"},"openai":{"enabled":false,"endpoint":"https://api.openai.com/v1/chat/completions","api_key":"","model":"gpt-4o","json_mode":"on"},"gemini":{"enabled":false,"api_key":"","model":"gemini-2.5-flash","endpoint":""},"ollama":{"enabled":false,"host":"http://localhost:11434","model":"qwen2.5-coder:7b"}},"roster":{"medium":["claude"],"large":["claude","gemini","ollama"]}}
```

For each reviewer in `$ENABLED`, run the matching block. Set `reviewers.<name>.enabled = true`; set `enabled = false` for any not selected.

#### 3a. Claude

`AskUserQuestion` "Pick the model the Claude reviewer subagent will run":
- `sonnet` — balanced, default
- `haiku` — fastest, cheapest
- `opus` — deepest, slowest, most expensive

Set `reviewers.claude.model` to the choice.

Then `Edit` `agents/code-review-claude.md` to update the `model:` line at line 5. Replace `model: sonnet` (or whatever currently there) with `model: <choice>`.

#### 3b. OpenAI-compatible

`AskUserQuestion` "Pick a provider":
- `OpenAI (paid, gpt-4o)` — endpoint `https://api.openai.com/v1/chat/completions`, default model `gpt-4o`
- `OpenRouter (free tier available)` — endpoint `https://openrouter.ai/api/v1/chat/completions`, default model `openai/gpt-oss-120b:free`
- `Groq (free Llama-3 variants)` — endpoint `https://api.groq.com/openai/v1/chat/completions`, default model `llama-3.3-70b-versatile`
- `Custom` — ask via `AskUserQuestion` for the endpoint URL and model id (provide a small set of common ones if possible)

`AskUserQuestion` "Ready to enter your API key for this provider?":
- `Yes — open the key dialog` — A password-masked dialog box will appear. The key never appears in the conversation.
- `Skip this reviewer` — Disable the OpenAI-compatible reviewer and continue.

If the user picks "Skip this reviewer", set `reviewers.openai.enabled = false` and skip to the next enabled reviewer.

If "Yes": tell the user "A dialog box will appear — enter your key there. It won't appear in this conversation.", then run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" set openai \
    enabled=true \
    endpoint="<chosen endpoint>" \
    model="<chosen model>" \
    json_mode="on"
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" gui-key openai
```

The `gui-key` command prints `ok (api_key stored in <location>)` — relay this to the user so they know where the key was saved (macOS Keychain, DPAPI-encrypted, or plaintext).

`AskUserQuestion` "Enable strict JSON mode? (Set to off only if your model rejects `response_format: json_object` — usually free-tier OpenRouter/Groq.)":
- `on (default, recommended)`
- `off (for models that reject response_format)`

If `off`: `set openai json_mode=off`.

#### 3c. Gemini

`AskUserQuestion` "Pick the Gemini model":
- `gemini-2.5-flash` (default, higher free-tier quota: 1,500 req/day)
- `gemini-2.5-pro` (deeper analysis, lower free-tier quota: 50 req/day)

`AskUserQuestion` "Ready to enter your Gemini API key?":
- `Yes — open the key dialog` — A password-masked dialog box will appear. The key never appears in the conversation.
- `Skip this reviewer` — Disable the Gemini reviewer and continue.

If the user picks "Skip this reviewer", set `reviewers.gemini.enabled = false` and skip to the next enabled reviewer.

If "Yes": tell the user "A dialog box will appear — enter your key there. It won't appear in this conversation.", then run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" set gemini \
    enabled=true \
    model="<choice>"
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" gui-key gemini
```

The `gui-key` command prints `ok (api_key stored in <location>)` — relay this to the user so they know where the key was saved.

#### 3d. Ollama

`AskUserQuestion` "Ollama daemon host":
- `http://localhost:11434 (default)`
- `Custom` (then ask for the URL)

`AskUserQuestion` "Pick a code-review model":
- `qwen2.5-coder:7b` (small, fast)
- `qwen2.5-coder:14b` (recommended)
- `deepseek-coder-v2:16b` (deepest)
- `Custom` (ask for the tag)

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" set ollama \
    enabled=true \
    host="<host>" \
    model="<choice>"
```

### 4. Validate each enabled reviewer

For each enabled reviewer, run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" validate <provider>
```

The helper prints JSON: `{"status": "ok"|"unauthorized"|"unreachable"|"model-missing", "detail": "..."}`.

- `ok` → continue.
- `unauthorized` → tell the user their key was rejected; loop back to that reviewer's step. Max 2 retries; on the third failure ask `AskUserQuestion`: "Skip this reviewer / Try again / Cancel wizard".
- `unreachable` → for Ollama, check `ollama serve` is running and re-prompt; for the cloud providers it's usually a network error — retry once, then offer to skip.
- `model-missing` → see Step 5 (Ollama) or re-prompt for a different model.

### 5. Ollama model pull (only if Ollama is enabled and validate returned `model-missing`)

```bash
ollama pull <chosen-model>
```
Stream output to the user so they see progress. After the pull, re-run `validate ollama`. If it still reports `model-missing`, surface the failure and let the user pick a different model.

### 6. Derive rosters

Compute roster fields based on what was enabled. Priority order: `claude > gemini > openai > ollama`.

```
medium = first enabled reviewer in priority order
large  = up to 3 enabled reviewers in priority order
```

Build the final config JSON (read current with `anvil-config.py read`, replace `roster.medium` and `roster.large`, set `setup_completed` to a UTC ISO timestamp), then save:

```bash
python -c "
import json, datetime, sys, subprocess
cfg = json.loads(subprocess.check_output(['python', '${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py', 'read']))
priority = ['claude', 'gemini', 'openai', 'ollama']
enabled = [p for p in priority if cfg['reviewers'].get(p, {}).get('enabled')]
cfg['roster']['medium'] = enabled[:1]
cfg['roster']['large']  = enabled[:3]
cfg['setup_completed']  = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
print(json.dumps(cfg))
" | python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" save
```

### 7. Initialize the ledger and record completion

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-ledger.py" init
python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-ledger.py" memory-set anvil-setup-completed "$(python -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
```

### 8. Summary

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" summary` and present the masked summary to the user, plus:

- Which reviewers passed validation (`ok`/`skipped`/`unauthorized`/etc.)
- The chosen Claude model (file `agents/code-review-claude.md` was rewritten on disk).
- Roster: medium / large.
- Rollback hint:
  > To redo setup later, run `/anvil-setup reset` or `rm ~/.claude-anvil/config.json`.
  > Note: `/anvil-setup reset` also removes any keychain entries. If you delete config.json manually, run `python scripts/anvil-config.py keychain-delete openai` and `keychain-delete gemini` to remove keychain entries too.
- Key storage hint (show output of `python "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" keychain-status`).
- Next-step hint:
  > Try `/anvil <task>` now.

## Failure handling

If the user `Ctrl-C`s mid-wizard or cancels via `AskUserQuestion`, do NOT save partial config. Existing `~/.claude-anvil/config.json` (if any) must be left untouched.

If `anvil-config.py save` returns non-zero, surface the stderr to the user and stop. Do not retry silently.
