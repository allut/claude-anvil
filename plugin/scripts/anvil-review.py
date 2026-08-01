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
                    [--out PATH]        # default <tmpdir>/anvil-review-{provider}-{task_id}.json (tmpdir via tempfile.gettempdir())

Output:
    - Stdout: one line, e.g. "reviewer=openai verdict=pass findings=0 model=gpt-4o"
    - File:   the full JSON verdict (plus metadata) at --out. Includes a top-level
              "passed" (0|1) machine signal for the ledger: 0 for a real
              stub/outage (error set and verdict != "pass") or a "fail" verdict,
              1 for genuine pass/concern and the intentional disabled-skip stub.
              See compute_passed().
    - Exit 0 always when a verdict file was written (real or stub).
    - Exit 2 if the diff file is missing (no verdict can be produced).
    Callers distinguish real from stub verdicts via the "error" field in the JSON output.

Timeouts:
    Every HTTP call is bounded by two true wall-clock deadlines, so a stalled
    provider always returns control instead of hanging the /anvil loop:
      ANVIL_REVIEW_HTTP_TIMEOUT   seconds per attempt   (default 180)
      ANVIL_REVIEW_TOTAL_TIMEOUT  seconds per call,     (default 420)
                                  covering all retries + backoff sleeps
    Exceeding either produces a TimeoutError, which becomes a graceful stub verdict.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SETTING_CACHE: dict[tuple[str, str], str] = {}  # (provider, key) → resolved value; avoids repeated subprocess launches


def _setting(provider: str, key: str, env_var: str, default: str = "") -> str:
    """Resolve a config value: env var → anvil-config.py (keychain/DPAPI/config) → default.
    default must be a single-line string (stdout is taken line-by-line; embedded newlines would produce garbage).
    Results are cached per (provider, key) to avoid spawning a subprocess for every call site."""
    v = os.environ.get(env_var)
    if v not in (None, ""):
        return v
    cache_key = (provider, key)
    if cache_key in _SETTING_CACHE:
        return _SETTING_CACHE[cache_key]
    script = _SCRIPT_DIR / "anvil-config.py"
    try:
        r = subprocess.run(
            [sys.executable, str(script), "get", provider, key,
             "--env-var", env_var, "--default", default],
            capture_output=True, timeout=20, text=True,  # >15s to outlast DPAPI's inner PowerShell timeout
        )
        if r.returncode == 0:
            # Take only the last non-empty line so any future startup warnings don't corrupt the value.
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            result = lines[-1].strip() if lines else default
            _SETTING_CACHE[cache_key] = result
            return result
    except Exception:
        pass
    return default

def _provider_enabled(provider: str) -> bool:
    """Is this reviewer switched on? anvil-config.py owns the answer.

    Deliberately does NOT read ANVIL_<PROVIDER>_ENABLED here: `anvil-config.py get
    <provider> enabled` is the single validated resolver for that flag (it applies the
    env-wins rule, canonicalizes to "true"/"false", and warns on non-boolean values).
    Duplicating the parsing locally is how the two sides drift apart. Child stderr is
    forwarded so those warnings actually reach the user.

    Fails open (True) when the helper can't be run — an unreachable config script must
    never silently skip a reviewer and fabricate a clean verdict.
    """
    script = _SCRIPT_DIR / "anvil-config.py"
    try:
        r = subprocess.run(
            [sys.executable, str(script), "get", provider, "enabled", "--default", "true"],
            capture_output=True, timeout=20, text=True,
        )
    except Exception:
        return True
    if r.stderr.strip():
        print(r.stderr.strip(), file=sys.stderr)
    if r.returncode != 0:
        return True
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return lines[-1].strip().lower() != "false" if lines else True


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
    # Fall back: first balanced {...} substring. Brace counting is string-aware,
    # because reviewer findings routinely contain braces inside their text
    # ("wrap it in ${...}"), and a naive counter closes the object early there.
    # String tracking starts only once we are inside an object, so unbalanced
    # quotes in the surrounding prose cannot corrupt the scan.
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(stripped):
        if depth == 0:
            if ch == "{":
                start = i
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
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


def compute_passed(verdict: str, error: str | None) -> int:
    """Machine signal for the ledger `passed` flag.

    Returns 0 when the review did not genuinely succeed:
      - verdict == "fail" (a real high-severity failure), OR
      - an error occurred AND the verdict is not "pass" (a stub/outage:
        bad model slug, auth failure, unreachable host — these fall through
        normalize_verdict to "concern"/"fail", never "pass").
    Returns 1 otherwise, which includes the intentional disabled-skip stub
    (error set but verdict pinned to "pass") and all genuine pass/concern verdicts.
    """
    if verdict == "fail":
        return 0
    if error and verdict != "pass":
        return 0
    return 1


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


# Transient upstream failures worth retrying (rate limits, gateway/overload errors).
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_MAX_HTTP_ATTEMPTS = 3
_HTTP_RETRY_BASE_SECONDS = 2.0
# Wall-clock deadlines. ATTEMPT bounds a single request; TOTAL bounds the whole
# http_post_json call (every attempt plus the backoff sleeps between them).
_DEFAULT_ATTEMPT_TIMEOUT = 180.0
_DEFAULT_TOTAL_TIMEOUT = 420.0


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back to default on junk."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"anvil-review: {name}={raw!r} is not a number; using {default:.0f}s", file=sys.stderr)
        return default
    # float() happily accepts "nan" and "inf"; both survive a `<= 0` test and then blow
    # up later inside Thread.join() with ValueError/OverflowError.
    if not math.isfinite(value) or value <= 0:
        print(f"anvil-review: {name}={raw!r} must be a finite number > 0; "
              f"using {default:.0f}s", file=sys.stderr)
        return default
    return value


def _call_with_deadline(fn, deadline: float):
    """Run fn() on a daemon thread and abandon it if it exceeds `deadline` seconds.

    Why this exists: urllib's `timeout=` is a *per-socket-operation* timeout, not a
    total deadline. An upstream that keeps the connection alive and trickles bytes
    (common on queued free-tier inference endpoints) can run far past the configured
    timeout without any single recv() exceeding it, so socket.timeout never fires and
    the call never returns. This wrapper gives the caller a true wall-clock bound and
    raises TimeoutError, which main()'s existing handler turns into a stub verdict.

    The worker is a plain daemon threading.Thread on purpose:
      - Python cannot kill a thread, so on expiry it is abandoned, not stopped. Daemon
        status means an abandoned thread never blocks interpreter shutdown.
      - concurrent.futures.ThreadPoolExecutor is deliberately NOT used: its atexit
        handler joins worker threads, which would hang the process on a stuck socket —
        exactly the failure this function exists to prevent.
    """
    box: dict = {}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- ferried to the caller verbatim
            box["error"] = exc

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise TimeoutError(f"request exceeded {deadline:.0f}s wall-clock deadline")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def http_post_json(url: str, payload: dict, headers: dict,
                   timeout: float | None = None,
                   total_timeout: float | None = None) -> dict:
    import ssl
    import time
    attempt_timeout = (
        timeout if timeout is not None
        else _env_float("ANVIL_REVIEW_HTTP_TIMEOUT", _DEFAULT_ATTEMPT_TIMEOUT)
    )
    budget = (
        total_timeout if total_timeout is not None
        else _env_float("ANVIL_REVIEW_TOTAL_TIMEOUT", _DEFAULT_TOTAL_TIMEOUT)
    )
    started = time.monotonic()

    def _remaining() -> float:
        return budget - (time.monotonic() - started)

    body = json.dumps(payload).encode("utf-8")
    try:
        import certifi
        _ctx: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        print("anvil-review: certifi not found — using system CA store (may fail on macOS python.org Python); "
              "run: pip3 install certifi", file=sys.stderr)
        _ctx = None
    except Exception:
        _ctx = None

    for attempt in range(1, _MAX_HTTP_ATTEMPTS + 1):
        remaining = _remaining()
        if remaining <= 0:
            raise TimeoutError(
                f"total {budget:.0f}s wall-clock budget exhausted after {attempt - 1} attempt(s)"
            )
        deadline = min(attempt_timeout, remaining)
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)

        def _do_request(_req=req, _deadline=deadline) -> dict:
            # Socket-level timeout is still set as a first line of defence; the
            # surrounding _call_with_deadline is what actually guarantees a return.
            with urllib.request.urlopen(_req, timeout=_deadline, context=_ctx) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))

        try:
            return _call_with_deadline(_do_request, deadline)
        except urllib.error.HTTPError as e:
            if e.code not in _RETRYABLE_HTTP_STATUSES or attempt == _MAX_HTTP_ATTEMPTS:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = (
                float(retry_after)
                if retry_after and retry_after.strip().isdigit()
                else _HTTP_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            )
            if delay >= _remaining():
                # No budget left to sleep and retry — surface the upstream error itself,
                # which is more informative than a timeout.
                raise
            print(
                f"anvil-review: HTTP {e.code} (attempt {attempt}/{_MAX_HTTP_ATTEMPTS}), "
                f"retrying in {delay:.0f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises


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
    model = _setting("gemini", "model", "ANVIL_GEMINI_MODEL", "gemini-2.5-flash")
    endpoint = _setting(
        "gemini", "endpoint", "ANVIL_GEMINI_ENDPOINT",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )
    if not api_key:
        raise RuntimeError("ANVIL_GEMINI_API_KEY is not set")
    payload = {
        "systemInstruction": {"parts": [{"text": SHARED_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": build_user_prompt(diff, truncated)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    data = http_post_json(endpoint, payload, {"x-goog-api-key": api_key})
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

    if not _provider_enabled(args.provider):
        out_path = Path(args.out) if args.out else default_out_path(args.provider, args.task_id)
        stub = {
            "provider": args.provider,
            "model": _setting(args.provider, "model", f"ANVIL_{args.provider.upper()}_MODEL", ""),
            "task_id": args.task_id,
            "truncated": False,
            "error": f"{args.provider} is disabled in config (enabled=false)",
            "verdict": "pass",
            "summary": f"{args.provider} skipped (enabled=false); no findings",
            "findings": [],
            "passed": compute_passed("pass", f"{args.provider} is disabled in config (enabled=false)"),
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(stub, indent=2), encoding="utf-8")
        print(f"reviewer={args.provider} verdict=pass findings=0 model=disabled out={out_path}")
        return 0

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

    # An HTTP call can succeed yet yield no genuine verdict object: the model
    # emitted garbage/truncated JSON, ignored json_mode/format=json, or wrapped the
    # verdict in an array so extract_json returned a non-dict. In all these shapes no
    # exception fires, so `error` stays None and normalize_verdict defaults to
    # "concern" — indistinguishable from a genuine clean review. Require a non-empty
    # dict here (an empty {} from a failed parse, or a truthy non-dict like an
    # array-wrapped verdict, both mean "no verdict") so compute_passed() flags every
    # such reply (passed=0) like any other stub/outage.
    if error is None and not (isinstance(raw, dict) and raw):
        error = f"{args.provider} returned no parseable JSON verdict"

    fallback_summary = error or "reviewer returned no parseable verdict"
    verdict = normalize_verdict(raw, fallback_summary)
    record = {
        "provider": args.provider,
        "model": model,
        "task_id": args.task_id,
        "truncated": truncated,
        "error": error,
        **verdict,
        "passed": compute_passed(verdict["verdict"], error),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(
        f"reviewer={args.provider} verdict={record['verdict']} "
        f"findings={len(record['findings'])} model={model or 'unknown'} "
        f"out={out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
