#!/usr/bin/env python3
"""anvil-review.py -- Adversarial code review dispatcher.

Given a staged diff file and a provider name, call out to one of:
  - openai  (OpenAI-compatible Chat Completions: OpenAI, Azure, OpenRouter, Groq, ...)
  - gemini  (Google Gemini generateContent REST API)
  - ollama  (local Ollama /api/chat)

The shared reviewer prompt is pinned here so all providers attack the diff
from the same baseline. Each provider is asked to return strict JSON:

    {"verdict": "pass"|"concern"|"fail",
     "summary": "one-line overall take",
     "findings": [{"severity": "high|medium|low",
                   "file": "path:line?",
                   "what": "...", "why": "...", "fix": "..."}]}

Usage:
    anvil-review.py --provider openai|gemini|ollama
                    --task-id TASK_ID
                    --diff-file PATH
                    [--out PATH]        # default /tmp/anvil-review-{provider}-{task_id}.json

Output:
    - Stdout: one line, e.g. "reviewer=openai verdict=pass findings=0 model=gpt-4o"
    - File:   the full JSON verdict (plus metadata) at --out
    - Exit 0 if we got a verdict (even "concern" / "fail" ones).
    - Exit 1 if the provider was unreachable or misconfigured. A stub verdict
      ({"verdict": "concern", "summary": "<reason>"}) is still written so the
      /anvil loop can record that the reviewer didn't produce a real signal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# --- config.json fallback ----------------------------------------------------
# Env vars always win. If an env var is unset, fall back to whichever value
# was written by the /anvil-setup wizard at ~/.claude-anvil/config.json.
# Read once per process to keep call_* hot paths cheap.

_CONFIG_CACHE: dict | None = None
_CONFIG_LOADED = False


def _config_path() -> Path:
    raw = os.environ.get("ANVIL_CONFIG_PATH", "~/.claude-anvil/config.json")
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def _dpapi_decrypt(blob: str) -> str | None:
    """Decrypt a 'dpapi:<base64>' blob via PowerShell (Windows only)."""
    b64 = blob[6:]
    ps = (
        "Add-Type -AssemblyName System.Security;"
        f"$c=[Convert]::FromBase64String('{b64}');"
        "$p=[System.Security.Cryptography.ProtectedData]::Unprotect($c,$null,'CurrentUser');"
        "[Convert]::ToBase64String($p)"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return None
        import base64 as _b64
        return _b64.b64decode(r.stdout.decode().strip()).decode()
    except Exception:
        return None


def _config_value(provider: str, key: str, default: str = "") -> str:
    global _CONFIG_CACHE, _CONFIG_LOADED
    if not _CONFIG_LOADED:
        _CONFIG_LOADED = True
        try:
            _CONFIG_CACHE = json.loads(_config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _CONFIG_CACHE = None
    if not _CONFIG_CACHE:
        return default
    block = (_CONFIG_CACHE.get("reviewers") or {}).get(provider) or {}
    val = block.get(key)
    if val in (None, ""):
        return default
    val = str(val)
    if val.startswith("dpapi:"):
        decrypted = _dpapi_decrypt(val)
        return decrypted if decrypted else default
    return val


def _setting(provider: str, key: str, env_var: str, default: str = "") -> str:
    v = os.environ.get(env_var)
    if v not in (None, ""):
        return v
    return _config_value(provider, key, default)

SHARED_PROMPT = (
    "You are a senior code reviewer performing an adversarial review.\n"
    "Review the staged diff below.\n\n"
    "Find: bugs, security vulnerabilities, logic errors, race conditions,\n"
    "edge cases, missing error handling, and architectural violations.\n"
    "Ignore: style, formatting, naming preferences.\n"
    "For each issue, give: what the bug is, why it matters, and a concrete fix.\n"
    "If nothing is wrong, say so explicitly with an empty findings array.\n\n"
    "Output STRICT JSON ONLY, no markdown fences, no prose before or after.\n"
    "Schema:\n"
    '{"verdict": "pass" | "concern" | "fail",\n'
    ' "summary": "one short sentence overall take",\n'
    ' "findings": [ {"severity": "high"|"medium"|"low",\n'
    '               "file": "path:line?",\n'
    '               "what": "...",\n'
    '               "why": "...",\n'
    '               "fix": "..." } ] }\n\n'
    "'pass' = nothing actionable. 'concern' = non-blocking issues. 'fail' = at\n"
    "least one high-severity issue the author should address before merging.\n"
)

DIFF_HARD_LIMIT = 120_000  # characters -- keeps large PR diffs tractable for any model


# ---------- helpers ----------------------------------------------------------

def default_out_path(provider: str, task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    return Path(tempfile.gettempdir()) / f"anvil-review-{provider}-{safe}.json"


def read_diff(path: Path) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > DIFF_HARD_LIMIT:
        return text[:DIFF_HARD_LIMIT] + "\n\n[... diff truncated by anvil-review ...]\n", True
    return text, False


def build_user_prompt(diff: str, truncated: bool) -> str:
    note = "NOTE: this diff was truncated for length.\n\n" if truncated else ""
    return f"{note}Staged diff:\n\n```diff\n{diff}\n```"


def _fix_escapes(text: str) -> str:
    r"""Replace invalid JSON escape sequences (e.g. \%, \_) with the bare character.

    Valid JSON escapes: \" \\ \/ \b \f \n \r \t \uXXXX.
    LLMs sometimes emit \% or \_ inside code snippets in JSON strings; those
    cause json.loads to raise JSONDecodeError even when the structure is fine.
    """
    return re.sub(r'\\([^"\\/bfnrtu])', r'\1', text)


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply, tolerating markdown fences."""
    if not text:
        return None
    stripped = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Retry after sanitizing invalid escape sequences emitted by some models.
    try:
        return json.loads(_fix_escapes(stripped))
    except json.JSONDecodeError:
        pass
    # Fall back: first balanced {...} substring.
    depth = 0
    start = -1
    for i, ch in enumerate(stripped):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = stripped[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    try:
                        return json.loads(_fix_escapes(candidate))
                    except json.JSONDecodeError:
                        start = -1
    return None


def normalize_verdict(raw: dict | None, fallback_summary: str) -> dict:
    if not isinstance(raw, dict):
        return {"verdict": "concern", "summary": fallback_summary, "findings": []}
    verdict = str(raw.get("verdict", "concern")).lower()
    if verdict not in {"pass", "concern", "fail"}:
        verdict = "concern"
    findings = raw.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    return {
        "verdict": verdict,
        "summary": str(raw.get("summary") or fallback_summary),
        "findings": findings,
    }


def http_post_json(url: str, payload: dict, headers: dict, timeout: float = 180.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ---------- provider calls ---------------------------------------------------

def _openai_model_is_reasoning(model: str) -> bool:
    # gpt-5 and the o-series only accept temperature=1 (i.e. no override).
    m = model.lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3")


def call_openai(diff: str, truncated: bool) -> tuple[dict, str]:
    endpoint = _setting("openai", "endpoint", "ANVIL_OPENAI_ENDPOINT",
                        "https://api.openai.com/v1/chat/completions")
    api_key = _setting("openai", "api_key", "ANVIL_OPENAI_API_KEY", "")
    model = _setting("openai", "model", "ANVIL_OPENAI_MODEL", "gpt-4o")
    json_mode = _setting("openai", "json_mode", "ANVIL_OPENAI_JSON_MODE", "on").strip().lower() != "off"
    if not api_key:
        raise RuntimeError("ANVIL_OPENAI_API_KEY is not set")
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SHARED_PROMPT},
            {"role": "user", "content": build_user_prompt(diff, truncated)},
        ],
    }
    if not _openai_model_is_reasoning(model):
        payload["temperature"] = 0.2
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        # OpenRouter uses these for attribution; api.openai.com ignores them.
        "HTTP-Referer": "https://github.com/burkeholland/anvil",
        "X-Title": "claude-anvil",
    }
    data = http_post_json(endpoint, payload, headers)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return extract_json(content) or {}, model


def call_gemini(diff: str, truncated: bool) -> tuple[dict, str]:
    api_key = _setting("gemini", "api_key", "ANVIL_GEMINI_API_KEY", "")
    model = _setting("gemini", "model", "ANVIL_GEMINI_MODEL", "gemini-2.5-pro")
    endpoint = _setting(
        "gemini", "endpoint", "ANVIL_GEMINI_ENDPOINT",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )
    if not api_key:
        raise RuntimeError("ANVIL_GEMINI_API_KEY is not set")
    url = f"{endpoint}?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": SHARED_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": build_user_prompt(diff, truncated)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    data = http_post_json(url, payload, {})
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    content = "".join(p.get("text", "") for p in parts)
    return extract_json(content) or {}, model


def call_ollama(diff: str, truncated: bool) -> tuple[dict, str]:
    host = _setting("ollama", "host", "ANVIL_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = _setting("ollama", "model", "ANVIL_OLLAMA_MODEL", "qwen2.5-coder:7b")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SHARED_PROMPT},
            {"role": "user", "content": build_user_prompt(diff, truncated)},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
        "format": "json",
    }
    data = http_post_json(f"{host}/api/chat", payload, {})
    content = data.get("message", {}).get("content", "")
    return extract_json(content) or {}, model


PROVIDERS = {
    "openai": call_openai,
    "gemini": call_gemini,
    "ollama": call_ollama,
}


# ---------- main -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anvil-review.py")
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--diff-file", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    diff_path = Path(args.diff_file)
    if not diff_path.exists():
        print(f"anvil-review: diff file not found: {diff_path}", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else default_out_path(args.provider, args.task_id)

    diff, truncated = read_diff(diff_path)
    handler = PROVIDERS[args.provider]
    error: str | None = None
    model = _setting(args.provider, "model", f"ANVIL_{args.provider.upper()}_MODEL", "")
    raw: dict = {}

    try:
        raw, model = handler(diff, truncated)
    except RuntimeError as e:
        error = str(e)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = ""
        if e.code in (401, 403):
            error = f"{args.provider} authentication failed (HTTP {e.code}): {body}"
        elif e.code == 400:
            error = f"{args.provider} bad request (HTTP 400) — check model name or request body format: {body}"
        else:
            error = f"{args.provider} HTTP {e.code} {e.reason}: {body}"
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        error = f"{args.provider} unreachable: {e}"
    except Exception as e:  # noqa: BLE001 -- catch-all keeps the /anvil loop alive
        error = f"{args.provider} error: {e!r}"

    fallback_summary = error or "reviewer returned no parseable verdict"
    verdict = normalize_verdict(raw, fallback_summary)
    record = {
        "provider": args.provider,
        "model": model,
        "task_id": args.task_id,
        "truncated": truncated,
        "error": error,
        **verdict,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(
        f"reviewer={args.provider} verdict={record['verdict']} "
        f"findings={len(record['findings'])} model={model or 'unknown'} "
        f"out={out_path}"
    )
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main())
