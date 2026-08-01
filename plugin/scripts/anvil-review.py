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

A fourth reviewer, `claude`, is spawned by the /anvil loop as a Task subagent
rather than called from here. `--provider claude` therefore runs in *ingest*
mode: it re-reads the verdict file the subagent wrote and puts it through the
same guards every HTTP provider's verdict goes through, rewriting the file in
place. Before this existed, `claude` was the one reviewer whose verdict reached
the ledger on nothing but "the file exists and parses".

Usage:
    anvil-review.py --provider openai|gemini|ollama
                    --task-id TASK_ID
                    --diff-file PATH
                    [--out PATH]        # default <tmpdir>/anvil-review-{provider}-{task_id}.json (tmpdir via tempfile.gettempdir())

    anvil-review.py --provider claude   # ingest: guard a verdict written elsewhere
                    --task-id TASK_ID
                    --diff-file PATH    # used for the off-diff advisory; tolerated if absent
                    [--out PATH]        # the file to read AND rewrite

Output:
    - Stdout: one line, e.g. "reviewer=openai verdict=pass findings=0 model=gpt-4o"
    - File:   the full JSON verdict (plus metadata) at --out. Includes a top-level
              "passed" (0|1) machine signal for the ledger: 0 for a real
              stub/outage (error set and verdict != "pass") or a "fail" verdict,
              1 for genuine pass/concern and the intentional disabled-skip stub.
              See compute_passed(). Also includes "advisory": a (possibly empty)
              list of strings naming incoherence the guard found in the reviewer's
              own payload -- see apply_coherence_guard(). Ingest mode adds
              "checks_run" (commands the reviewer says it ran) and "unverified"
              (true when it named none) -- see apply_provenance_guard().
    - Exit 0 always when a verdict file was written (real or stub).
    - Exit 2 if the diff file is missing (no verdict can be produced), or, in
      ingest mode, if there is no verdict file to ingest.
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
    "Report ONLY defects that still exist in the code AFTER this diff is applied.\n"
    "Do NOT list bugs the diff fixes. Do NOT restate the diff's own comments,\n"
    "docstrings or documentation as findings -- a diff that explains the bugs it\n"
    "repairs is not a diff full of bugs. Before emitting each finding, confirm it\n"
    "points at a line the diff adds or leaves in place; if it describes a line the\n"
    "diff removes, drop it.\n"
    "Your verdict must agree with your summary: do not pair an approving summary\n"
    "with a 'fail' verdict. If you find no surviving defects, return\n"
    '"verdict": "pass" with an empty findings array.\n\n'
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
    "A 'fail' carrying no high-severity finding is automatically downgraded to\n"
    "'concern', so do not use 'fail' as a generic negative signal.\n"
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


# ---------- coherence guard --------------------------------------------------
#
# A reviewer can return well-formed JSON that is nonetheless self-contradictory:
# an approving summary paired with "verdict": "fail", or findings that describe
# bugs *this diff repairs* rather than defects that survive it. Both shapes pass
# every structural check and land in the ledger as an authoritative passed=0 row.
# The ledger's whole value is that it cannot be hallucinated, so an incoherent
# payload must be visibly flagged rather than silently trusted.
#
# The guard corrects only what the documented schema already decides (a "fail"
# means at least one high-severity finding) and otherwise *annotates*: reviewer
# quality is not verifiable from inside the loop, so anything else becomes an
# advisory string for the /anvil loop and the user to weigh.

_MAX_ADVISORY_FILES = 5  # keep the off-diff advisory readable on very noisy verdicts
_MAX_ADVISORY_SEVERITIES = 5
_SEVERITY_VOCAB = {"high", "medium", "low"}


def _finding_severity(finding: object) -> str:
    return str(finding.get("severity", "")).strip().lower() if isinstance(finding, dict) else ""


def _normalize_path(raw: object) -> str:
    """Reduce a diff path or a finding's `file` to a bare comparable repo path."""
    p = str(raw or "").strip().replace("\\", "/")
    # Trailing :line, :line:col, or a :start-end range. Reviewers cite ranges
    # routinely; leaving one attached made the path unmatchable and raised a
    # false off-diff advisory against a file the diff plainly touches.
    p = re.sub(r":\d+(?:-\d+)?(?::\d+(?:-\d+)?)?$", "", p)
    p = re.sub(r"^\./", "", p)
    p = re.sub(r"^[ab]/", "", p)  # git's a/ b/ prefixes
    return p.strip("/")


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_DIFF_HEADER_RE = re.compile(r"^(?:\+\+\+|---) (?:[ab]/)?(.+)$", re.MULTILINE)


def diff_paths(diff: str) -> set[str]:
    """Every file path the diff appears to touch, normalized.

    Deliberately over-collects: the `+++`/`---` pattern also matches a *content*
    line that happens to start with `+++ ` or `--- `. Over-collecting only makes
    the off-diff advisory more permissive (fewer flags), never more accusatory,
    which is the safe direction for a heuristic that can produce false alarms.
    """
    paths: set[str] = set()
    for m in _DIFF_GIT_RE.finditer(diff or ""):
        paths.add(_normalize_path(m.group(1)))
        paths.add(_normalize_path(m.group(2)))
    for m in _DIFF_HEADER_RE.finditer(diff or ""):
        paths.add(_normalize_path(m.group(1)))
    paths.discard("")
    paths.discard("dev/null")
    return paths


def _cites_a_diffed_file(finding_path: str, paths: set[str]) -> bool:
    """Lenient path match. Absent/unmatchable paths count as cited (never accuse)."""
    if not finding_path:
        return True
    for p in paths:
        if (finding_path == p
                or p.endswith("/" + finding_path)
                or finding_path.endswith("/" + p)
                or finding_path.rsplit("/", 1)[-1] == p.rsplit("/", 1)[-1]):
            return True
    return False


def normalize_checks_run(raw: object) -> list[str]:
    """Coerce a reviewer's `checks_run` field to a list of non-empty command strings."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, (dict, list)):
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def apply_provenance_guard(verdict: dict, checks_run: list[str]) -> tuple[dict, list[str], bool]:
    """Return (possibly adjusted verdict, advisory notes, unverified flag).

    Only meaningful for a reviewer that can execute commands (see
    _EXECUTING_PROVIDERS). A reviewer with no shell cannot name commands it ran,
    and demanding the field from one would only teach it to invent entries.

    `checks_run` is **self-reported and is not a trust boundary.** An agent
    willing to type '"verdict": "pass"' will type '"checks_run": ["pytest"]' just
    as cheaply. What the field buys is narrower and still worth having: a clean
    verdict that does not even *claim* work is no longer indistinguishable, in
    the ledger, from one that does. Treat it as friction plus a legible
    "unverified" state, never as proof the review happened.

    Adjusts:
      - "pass" with no checks_run -> "concern". Both map to passed=1, so this
        keeps the guard family's invariant: it can never manufacture a FAIL row.

    Annotates only:
      - "fail"/"concern" with no checks_run. A "fail" is left standing: it
        already means passed=0, and downgrading an unearned failure would hide a
        real defect -- the opposite of the direction this guard errs in.
    """
    adjusted = dict(verdict)
    if checks_run:
        return adjusted, [], False
    advisory: list[str] = []
    if adjusted.get("verdict") == "pass":
        adjusted["verdict"] = "concern"
        advisory.append(
            "verdict downgraded pass->concern (unverified): the reviewer named no command it "
            "ran to reach a clean verdict; recorded as unverified rather than as a clean review"
        )
    else:
        advisory.append(
            f"verdict '{adjusted.get('verdict')}' reported with an empty checks_run: the reviewer "
            "named no command it ran, so the verdict is unverified"
        )
    return adjusted, advisory, True


def apply_coherence_guard(verdict: dict, diff: str = "",
                          truncated: bool = False) -> tuple[dict, list[str]]:
    """Return (possibly adjusted verdict, advisory notes).

    Adjusts (schema-mandated, deterministic):
      - "fail" with no high-severity finding -> "concern".
      - "pass" with a high-severity finding  -> "concern". Both "pass" and
        "concern" map to passed=1, so this never manufactures a FAIL row.

    A finding whose `severity` is a non-empty string outside {high, medium, low}
    ("critical", "blocker", a typo) counts as high for both rules and raises its
    own advisory. Anything else would let a vocabulary drift upstream -- a prompt
    tweak, a provider quirk -- silently downgrade a genuine failure to passed=1,
    which is the very outcome this guard exists to prevent. An absent or empty
    severity is not a drifted label and does not count as high.
    Annotates only (no adjustment):
      - "pass" alongside medium-severity findings.
      - "fail"/"concern" with an empty findings array.
      - findings citing files the diff does not touch. This is a *proxy* for
        "the finding describes a removed line": it catches a reviewer narrating
        code outside the change set, but not one narrating a fixed bug inside a
        file the diff does touch. The prompt is the primary defence there.
        Suppressed entirely when `truncated` is set: `diff` is then only the
        first DIFF_HARD_LIMIT characters, so every file whose header falls past
        the cutoff would be reported as untouched and a legitimate finding about
        the tail of a large diff would be talked down as off-diff chatter.

    Callers must skip this for stub verdicts (`error` set): those synthesize
    "concern" with no findings, which would trip the empty-findings note on
    every outage and drown the real signal.
    """
    adjusted = dict(verdict)
    advisory: list[str] = []
    findings = adjusted.get("findings") or []
    severities = {_finding_severity(f) for f in findings}
    unrecognized = sorted(s for s in severities if s and s not in _SEVERITY_VOCAB)
    if unrecognized:
        shown = ", ".join(unrecognized[:_MAX_ADVISORY_SEVERITIES])
        more = (f" (+{len(unrecognized) - _MAX_ADVISORY_SEVERITIES} more)"
                if len(unrecognized) > _MAX_ADVISORY_SEVERITIES else "")
        advisory.append(
            "finding(s) use severity label(s) outside the high/medium/low schema, counted "
            f"as high severity so a real failure is not downgraded: {shown}{more}"
        )
    # Unrecognized labels count as high: erring toward "blocking" keeps a genuine
    # fail intact; erring the other way is a false clean verdict.
    has_high = "high" in severities or bool(unrecognized)
    current = adjusted.get("verdict")

    if current == "fail" and not has_high:
        adjusted["verdict"] = current = "concern"
        advisory.append(
            f"verdict downgraded fail->concern: 'fail' requires at least one high-severity "
            f"finding, but none of the {len(findings)} reported finding(s) is high severity"
        )
    elif current == "pass" and has_high:
        adjusted["verdict"] = current = "concern"
        advisory.append(
            "verdict upgraded pass->concern: the reviewer reported high-severity finding(s) "
            "while calling the diff clean"
        )
    elif current == "pass" and "medium" in severities:
        advisory.append("verdict 'pass' reported alongside medium-severity finding(s)")

    if current in {"fail", "concern"} and not findings:
        advisory.append(f"verdict '{current}' reported with an empty findings array")

    paths = set() if truncated else diff_paths(diff)
    if paths:
        off = sorted({
            str(f.get("file"))
            for f in findings
            if isinstance(f, dict) and f.get("file")
            and not _cites_a_diffed_file(_normalize_path(f.get("file")), paths)
        })
        if off:
            shown = ", ".join(off[:_MAX_ADVISORY_FILES])
            more = f" (+{len(off) - _MAX_ADVISORY_FILES} more)" if len(off) > _MAX_ADVISORY_FILES else ""
            advisory.append(
                "finding(s) cite files this diff does not touch, so they may describe "
                f"pre-existing or already-fixed code rather than a defect in the diff: {shown}{more}"
            )
    return adjusted, advisory


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

# Reviewers this script *calls*. The Claude reviewer is spawned by the /anvil loop
# as a Task subagent instead, so it has no handler here -- but its verdict must
# still go through the same guards as everyone else's, which is what the ingest
# path below is for. Without it, `claude` was the one provider whose verdict the
# ledger accepted on nothing but "the file exists and parses".
_EXECUTING_PROVIDERS = {"claude"}  # reviewers with a shell, so checks_run is answerable


# ---------- ingest (verdicts produced outside this script) --------------------

def ingest_verdict(provider: str, task_id: str, in_path: Path,
                   diff_path: Path | None) -> tuple[dict, Path]:
    """Re-read a verdict written by a subagent and apply the same guards.

    Returns (record, out_path). The file is rewritten in place: the loop reads
    exactly one artifact, and there is no second, ungarded copy to pick up by
    mistake. Raises FileNotFoundError when there is no verdict to ingest --
    a missing file means the reviewer never ran, which is the caller's
    review-loop-failure case, not a verdict.
    """
    text = in_path.read_text(encoding="utf-8", errors="replace") if in_path.exists() else ""
    if not text.strip():
        raise FileNotFoundError(str(in_path))

    # An unreadable diff must never abort the ingest: the verdict is the artifact
    # worth saving, and bailing here would leave the raw, unguarded file on disk --
    # the exact outcome this path exists to prevent. The diff only feeds the
    # off-diff advisory, so losing it costs an advisory, not a verdict.
    diff, truncated, missing_diff = "", False, True
    if diff_path is not None:
        try:
            diff, truncated = read_diff(diff_path)
            missing_diff = False
        except OSError:
            diff, truncated = "", False

    raw = extract_json(text)
    error = None if isinstance(raw, dict) and raw else (
        f"{provider} verdict file held no parseable JSON verdict"
    )
    verdict = normalize_verdict(raw, error or "reviewer returned no parseable verdict")
    checks_run = normalize_checks_run(raw.get("checks_run")) if isinstance(raw, dict) else []

    advisory: list[str] = []
    unverified = False
    if error is None:
        verdict, advisory = apply_coherence_guard(verdict, diff, truncated)
        if provider in _EXECUTING_PROVIDERS:
            verdict, prov_advisory, unverified = apply_provenance_guard(verdict, checks_run)
            advisory.extend(prov_advisory)
        if missing_diff:
            advisory.append(
                "diff file was unavailable at ingest, so findings could not be checked "
                "against the files this diff touches"
            )

    record = {
        "provider": provider,
        "model": "",
        "task_id": task_id,
        "truncated": truncated,
        "error": error,
        **verdict,
        "checks_run": checks_run,
        "unverified": unverified,
        "advisory": advisory,
        "passed": compute_passed(verdict["verdict"], error),
    }
    in_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record, in_path


# ---------- main -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anvil-review.py")
    parser.add_argument("--provider", required=True,
                        choices=sorted(set(PROVIDERS) | _EXECUTING_PROVIDERS))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--diff-file", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    # Ingest mode: the verdict already exists on disk, written by a subagent this
    # script did not call. Deliberately ahead of the enabled check -- refusing to
    # guard a verdict because the provider is switched off in config would leave
    # the raw, unguarded file behind for the loop to read.
    if args.provider in _EXECUTING_PROVIDERS:
        in_path = Path(args.out) if args.out else default_out_path(args.provider, args.task_id)
        try:
            record, out_path = ingest_verdict(
                args.provider, args.task_id, in_path, Path(args.diff_file))
        except FileNotFoundError:
            print(f"anvil-review: no verdict file to ingest: {in_path}", file=sys.stderr)
            return 2
        print(
            f"reviewer={args.provider} verdict={record['verdict']} "
            f"findings={len(record['findings'])} advisory={len(record['advisory'])} "
            f"unverified={int(record['unverified'])} out={out_path}"
        )
        return 0

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
            "advisory": [],
            "passed": compute_passed("pass", f"{args.provider} is disabled in config (enabled=false)"),
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(stub, indent=2), encoding="utf-8")
        print(f"reviewer={args.provider} verdict=pass findings=0 advisory=0 "
              f"model=disabled out={out_path}")
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
    # Only genuine verdicts go through the guard; stubs synthesize "concern" with no
    # findings and would trip the empty-findings note on every outage.
    advisory: list[str] = []
    if error is None:
        verdict, advisory = apply_coherence_guard(verdict, diff, truncated)
    record = {
        "provider": args.provider,
        "model": model,
        "task_id": args.task_id,
        "truncated": truncated,
        "error": error,
        **verdict,
        "advisory": advisory,
        "passed": compute_passed(verdict["verdict"], error),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(
        f"reviewer={args.provider} verdict={record['verdict']} "
        f"findings={len(record['findings'])} advisory={len(advisory)} "
        f"model={model or 'unknown'} out={out_path}"
    )
    for note in advisory:
        print(f"anvil-review: advisory ({args.provider}): {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
