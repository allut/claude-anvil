#!/usr/bin/env python3
"""anvil-config.py -- claude-anvil configuration helper.

Owns ~/.claude-anvil/config.json (override via $ANVIL_CONFIG_PATH).

Schema (version 1):
    {
      "version": 1,
      "setup_completed": "<ISO-8601 UTC timestamp>",
      "reviewers": {
        "claude": {"enabled": true,  "model": "sonnet"},
        "openai": {"enabled": false, "endpoint": "...", "api_key": "",
                    "model": "gpt-4o", "json_mode": "on"},
        "gemini": {"enabled": false, "api_key": "", "model": "gemini-2.5-flash",
                    "endpoint": ""},
        "ollama": {"enabled": false, "host": "http://localhost:11434",
                    "model": "qwen2.5-coder:7b"}
      },
      "roster": {"medium": ["claude"], "large": ["claude", "gemini", "ollama"]}
    }

API keys are stored in the OS keychain when available:
  - macOS:   macOS Keychain via the `security` CLI
  - Windows: DPAPI-encrypted Base64 stored inline in config.json
             (value prefix "dpapi:"; unreadable on other machines/accounts)
  - Linux:   libsecret via the `secret-tool` CLI
  - Fallback: plaintext in config.json (chmod 0600 on POSIX)
In all cases config.json stores only "keychain" (or the dpapi blob) -- the
plaintext key never lands in the JSON file on platforms with keychain support.

Env vars always win over config.json -- this preserves the legacy `.env`-based
workflow. The Python reviewer scripts call `get` to resolve credentials with
that contract enforced in one place.

Subcommands:
    status                              -> prints "configured"/"partial"/"needs-setup"
    path                                -> prints absolute config path
    read                                -> prints raw JSON (exit 1 if missing)
    get PROVIDER KEY [--env-var NAME]
        [--default VAL]                 -> resolves env -> keychain -> config -> default
    roster {medium,large}               -> comma-separated reviewer list
    save                                -> reads JSON config from stdin and writes it (atomic, chmod 0600)
    set PROVIDER KEY=VALUE [KEY=VALUE]  -> merges into config; api_key goes to keychain
    validate PROVIDER                   -> tiny live HTTP probe; prints JSON status
    summary                             -> human-readable masked summary
    keychain-status                     -> shows which backend is active and where each key lives
    keychain-delete PROVIDER            -> removes keychain entry for a provider's api_key
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_REL = "~/.claude-anvil/config.json"
SCHEMA_VERSION = 1
PROVIDERS = ("claude", "openai", "gemini", "ollama")
HTTP_TIMEOUT = 15.0
KEYCHAIN_SERVICE = "anvil"
API_KEY_FIELDS = frozenset({"api_key"})


# --- path + IO ---------------------------------------------------------------

def config_path() -> Path:
    raw = os.environ.get("ANVIL_CONFIG_PATH", DEFAULT_CONFIG_REL)
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def default_config() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "setup_completed": "",
        "reviewers": {
            "claude": {"enabled": True, "model": "sonnet"},
            "openai": {
                "enabled": False,
                "endpoint": "https://api.openai.com/v1/chat/completions",
                "api_key": "",
                "model": "gpt-4o",
                "json_mode": "on",
            },
            "gemini": {
                "enabled": False,
                "api_key": "",
                "model": "gemini-2.5-flash",
                "endpoint": "",
            },
            "ollama": {
                "enabled": False,
                "host": "http://localhost:11434",
                "model": "qwen2.5-coder:7b",
            },
        },
        "roster": {
            "medium": ["claude"],
            "large": ["claude", "gemini", "ollama"],
        },
    }


def load_config() -> dict[str, Any] | None:
    p = config_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return None
    except json.JSONDecodeError as e:
        print(
            f"anvil-config: config.json is corrupted ({e}); "
            f"delete {p} and re-run /anvil-setup",
            file=sys.stderr,
        )
        sys.exit(1)


def merge_with_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of cfg with any missing keys filled from default_config()."""
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    out = copy.deepcopy(base)
    out["version"] = cfg.get("version", SCHEMA_VERSION)
    out["setup_completed"] = cfg.get("setup_completed", "")
    reviewers = cfg.get("reviewers") or {}
    for name, defaults in base["reviewers"].items():
        merged = dict(defaults)
        merged.update({k: v for k, v in (reviewers.get(name) or {}).items() if v is not None})
        out["reviewers"][name] = merged
    roster = cfg.get("roster") or {}
    for k in ("medium", "large"):
        if isinstance(roster.get(k), list):
            out["roster"][k] = [r for r in roster[k] if isinstance(r, str)]
    return out


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".anvil-config-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows / non-POSIX -- best effort


# --- OS keychain abstraction --------------------------------------------------

_UNSET = object()
_KEYCHAIN_BACKEND_CACHE: Any = _UNSET


def _keychain_backend() -> str | None:
    """Return 'macos', 'windows-dpapi', 'linux-secret-tool', or None."""
    global _KEYCHAIN_BACKEND_CACHE
    if _KEYCHAIN_BACKEND_CACHE is not _UNSET:
        return _KEYCHAIN_BACKEND_CACHE  # type: ignore[return-value]
    result: str | None = None
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["which", "security"], capture_output=True, timeout=3)
            result = "macos" if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            result = None
    elif sys.platform == "win32":
        result = "windows-dpapi"
    else:
        try:
            r = subprocess.run(["which", "secret-tool"], capture_output=True, timeout=3)
            result = "linux-secret-tool" if r.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            result = None
    _KEYCHAIN_BACKEND_CACHE = result
    return result


def _key_id(provider: str) -> str:
    return f"anvil-{provider}-api-key"


# macOS Keychain via `security` CLI

def _macos_write(key_id: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", key_id, "-w", value],
        check=True, capture_output=True, timeout=10,
    )


def _macos_read(key_id: str) -> str | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key_id, "-w"],
        capture_output=True, timeout=10,
    )
    return r.stdout.decode().strip() if r.returncode == 0 else None


def _macos_delete(key_id: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key_id],
        capture_output=True, timeout=10,
    )


# Windows DPAPI via PowerShell (value stored as "dpapi:<base64>" in config.json)

def _dpapi_encrypt(value: str) -> str:
    # Base64-encode the input so no special chars reach PowerShell.
    b64_in = base64.b64encode(value.encode()).decode()
    ps = (
        "Add-Type -AssemblyName System.Security;"
        f"$b=[Convert]::FromBase64String('{b64_in}');"
        "[Convert]::ToBase64String("
        "[System.Security.Cryptography.ProtectedData]::Protect($b,$null,'CurrentUser'))"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode().strip())
    return "dpapi:" + r.stdout.decode().strip()


def _dpapi_decrypt(encoded: str) -> str | None:
    if not encoded.startswith("dpapi:"):
        return None
    b64_cipher = encoded[6:]
    ps = (
        "Add-Type -AssemblyName System.Security;"
        f"$c=[Convert]::FromBase64String('{b64_cipher}');"
        "$p=[System.Security.Cryptography.ProtectedData]::Unprotect($c,$null,'CurrentUser');"
        "[Convert]::ToBase64String($p)"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
    if r.returncode != 0:
        return None
    try:
        return base64.b64decode(r.stdout.decode().strip()).decode()
    except Exception:
        return None


# Linux libsecret via `secret-tool`

def _linux_write(key_id: str, value: str) -> None:
    subprocess.run(
        ["secret-tool", "store", "--label", f"anvil {key_id}",
         "service", KEYCHAIN_SERVICE, "key", key_id],
        input=value.encode(), check=True, capture_output=True, timeout=10,
    )


def _linux_read(key_id: str) -> str | None:
    r = subprocess.run(
        ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE, "key", key_id],
        capture_output=True, timeout=10,
    )
    return r.stdout.decode().strip() if r.returncode == 0 else None


def _linux_delete(key_id: str) -> None:
    subprocess.run(
        ["secret-tool", "clear", "service", KEYCHAIN_SERVICE, "key", key_id],
        capture_output=True, timeout=10,
    )


def _keychain_store(provider: str, value: str) -> tuple[str, str]:
    """Write value to keychain. Returns (config_value_to_store, human_label)."""
    backend = _keychain_backend()
    kid = _key_id(provider)
    if backend == "macos":
        _macos_write(kid, value)
        return "keychain", "macOS Keychain"
    if backend == "linux-secret-tool":
        _linux_write(kid, value)
        return "keychain", "Linux secret-tool (libsecret)"
    if backend == "windows-dpapi":
        encrypted = _dpapi_encrypt(value)
        return encrypted, "config.json (DPAPI-encrypted)"
    return value, "config.json (plaintext)"


def _keychain_load(provider: str, raw_config_value: str) -> str | None:
    """Resolve a stored api_key value.  Returns the plaintext key, or None."""
    if raw_config_value == "keychain":
        backend = _keychain_backend()
        kid = _key_id(provider)
        if backend == "macos":
            return _macos_read(kid)
        if backend == "linux-secret-tool":
            return _linux_read(kid)
        return None
    if raw_config_value.startswith("dpapi:"):
        return _dpapi_decrypt(raw_config_value)
    return raw_config_value if raw_config_value else None


def _keychain_remove(provider: str) -> None:
    backend = _keychain_backend()
    kid = _key_id(provider)
    if backend == "macos":
        _macos_delete(kid)
    elif backend == "linux-secret-tool":
        _linux_delete(kid)
    # DPAPI: the encrypted blob lives in config.json; clearing api_key there is enough.


# --- env-var contract -------------------------------------------------------

ENV_VAR_MAP: dict[tuple[str, str], str] = {
    ("openai", "api_key"): "ANVIL_OPENAI_API_KEY",
    ("openai", "endpoint"): "ANVIL_OPENAI_ENDPOINT",
    ("openai", "model"): "ANVIL_OPENAI_MODEL",
    ("openai", "json_mode"): "ANVIL_OPENAI_JSON_MODE",
    ("gemini", "api_key"): "ANVIL_GEMINI_API_KEY",
    ("gemini", "endpoint"): "ANVIL_GEMINI_ENDPOINT",
    ("gemini", "model"): "ANVIL_GEMINI_MODEL",
    ("ollama", "host"): "ANVIL_OLLAMA_HOST",
    ("ollama", "model"): "ANVIL_OLLAMA_MODEL",
    ("claude", "model"): "ANVIL_CLAUDE_MODEL",
}


def resolve_value(provider: str, key: str, env_var: str | None, default: str) -> str:
    if not env_var:
        env_var = ENV_VAR_MAP.get((provider, key))
    if env_var:
        v = os.environ.get(env_var)
        if v:
            return v
    cfg = load_config()
    if cfg:
        merged = merge_with_defaults(cfg)
        v = (merged.get("reviewers", {}).get(provider) or {}).get(key)
        if v not in (None, ""):
            raw = str(v)
            if key in API_KEY_FIELDS:
                resolved = _keychain_load(provider, raw)
                if resolved:
                    return resolved
            else:
                return raw
    return default


# --- HTTP probes -------------------------------------------------------------

def _http_get_json(url: str, headers: dict[str, str] | None = None,
                   timeout: float = HTTP_TIMEOUT) -> tuple[int, dict | list | None, str]:
    """Returns (status, parsed_json_or_none, raw_body_text). Raises on transport error."""
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(body), body
        except json.JSONDecodeError:
            return resp.status, None, body


def _classify_http_error(e: urllib.error.HTTPError) -> tuple[str, str]:
    # Gemini returns 400 (API_KEY_INVALID) for bad keys; OpenAI/OpenRouter use 401.
    # Treat any 4xx except 404 as an auth/config problem; 5xx as transport issues.
    if e.code == 404:
        return "model-missing", f"HTTP {e.code} {e.reason}"
    if 400 <= e.code < 500:
        return "unauthorized", f"HTTP {e.code} {e.reason}"
    return "unreachable", f"HTTP {e.code} {e.reason}"


def validate_openai() -> dict[str, str]:
    endpoint = resolve_value("openai", "endpoint", None,
                             "https://api.openai.com/v1/chat/completions")
    api_key = resolve_value("openai", "api_key", None, "")
    model = resolve_value("openai", "model", None, "gpt-4o")
    if not api_key:
        return {"status": "unauthorized", "detail": "ANVIL_OPENAI_API_KEY / config.api_key is empty"}
    # Derive /v1/models from a chat-completions endpoint.
    parsed = urllib.parse.urlparse(endpoint)
    base_path = parsed.path.rsplit("/chat/completions", 1)[0].rstrip("/")
    url = urllib.parse.urlunparse(parsed._replace(path=f"{base_path}/models", query="", fragment=""))
    try:
        _, data, body = _http_get_json(url, {"Authorization": f"Bearer {api_key}"})
    except urllib.error.HTTPError as e:
        status, detail = _classify_http_error(e)
        return {"status": status, "detail": detail}
    except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, ssl.SSLError) as e:
        return {"status": "unreachable", "detail": str(e)}
    if not isinstance(data, dict):
        return {"status": "ok", "detail": f"reachable (non-JSON body, {len(body)} bytes)"}
    ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
    if model and ids and model not in ids:
        return {"status": "model-missing", "detail": f"model '{model}' not in /models list"}
    return {"status": "ok", "detail": f"reachable; {len(ids)} models listed"}


def validate_gemini() -> dict[str, str]:
    api_key = resolve_value("gemini", "api_key", None, "")
    model = resolve_value("gemini", "model", None, "gemini-2.5-flash")
    if not api_key:
        return {"status": "unauthorized", "detail": "ANVIL_GEMINI_API_KEY / config.api_key is empty"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        _, data, body = _http_get_json(url)
    except urllib.error.HTTPError as e:
        status, detail = _classify_http_error(e)
        return {"status": status, "detail": detail}
    except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, ssl.SSLError) as e:
        return {"status": "unreachable", "detail": str(e)}
    if not isinstance(data, dict):
        return {"status": "ok", "detail": f"reachable (non-JSON body, {len(body)} bytes)"}
    names = [m.get("name", "") for m in (data.get("models") or []) if isinstance(m, dict)]
    if model and names and not any(model in n for n in names):
        return {"status": "model-missing", "detail": f"model '{model}' not in /models list"}
    return {"status": "ok", "detail": f"reachable; {len(names)} models listed"}


def validate_ollama() -> dict[str, str]:
    host = resolve_value("ollama", "host", None, "http://localhost:11434").rstrip("/")
    model = resolve_value("ollama", "model", None, "qwen2.5-coder:7b")
    url = f"{host}/api/tags"
    try:
        _, data, body = _http_get_json(url, timeout=5.0)
    except urllib.error.HTTPError as e:
        status, detail = _classify_http_error(e)
        return {"status": status, "detail": detail}
    except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, ssl.SSLError) as e:
        return {"status": "unreachable", "detail": f"daemon at {host} not reachable: {e}"}
    if not isinstance(data, dict):
        return {"status": "ok", "detail": f"reachable (non-JSON body, {len(body)} bytes)"}
    tags = [m.get("name", "") for m in (data.get("models") or []) if isinstance(m, dict)]
    if model and not any(t == model or t.startswith(f"{model}:") or model in t for t in tags):
        return {"status": "model-missing", "detail": f"model '{model}' not pulled (have: {', '.join(tags) or 'none'})"}
    return {"status": "ok", "detail": f"daemon up; {len(tags)} models pulled"}


def validate_claude() -> dict[str, str]:
    # Claude reviewer runs as a Task subagent under the user's existing Claude
    # Code session, so credentials are implicit. Only sanity-check the model.
    model = resolve_value("claude", "model", None, "sonnet")
    if model not in {"sonnet", "haiku", "opus"} and not model.startswith("claude-"):
        return {"status": "model-missing", "detail": f"unrecognized model '{model}'"}
    return {"status": "ok", "detail": f"will run as Task subagent (model={model})"}


VALIDATORS = {
    "openai": validate_openai,
    "gemini": validate_gemini,
    "ollama": validate_ollama,
    "claude": validate_claude,
}


# --- subcommands -------------------------------------------------------------

def cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    if cfg is None:
        print("needs-setup")
        return 0
    merged = merge_with_defaults(cfg)
    enabled = [k for k, v in merged["reviewers"].items() if v.get("enabled")]
    if merged.get("setup_completed") and enabled:
        print("configured")
    else:
        print("partial")
    return 0


def cmd_path(_args: argparse.Namespace) -> int:
    print(str(config_path()))
    return 0


def cmd_read(_args: argparse.Namespace) -> int:
    p = config_path()
    if not p.exists():
        print(f"anvil-config: no config at {p}", file=sys.stderr)
        return 1
    sys.stdout.write(p.read_text(encoding="utf-8"))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    val = resolve_value(args.provider, args.key, args.env_var, args.default or "")
    print(val)
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    if args.kind == "medium":
        env = os.environ.get("ANVIL_MEDIUM_REVIEWER", "").strip()
        if env:
            parts = [p.strip() for p in env.split(",") if p.strip()]
            if len(parts) > 1:
                print(
                    f"anvil-config: ANVIL_MEDIUM_REVIEWER has {len(parts)} entries; "
                    f"only the first will be used for medium tasks",
                    file=sys.stderr,
                )
            print(parts[0] if parts else "")
            return 0
    else:
        env = os.environ.get("ANVIL_LARGE_REVIEWERS", "").strip()
        if env:
            parts = [p.strip() for p in env.split(",") if p.strip()]
            if len(parts) > 3:
                print(
                    f"anvil-config: ANVIL_LARGE_REVIEWERS has {len(parts)} entries; only the first 3 will be used",
                    file=sys.stderr,
                )
            print(",".join(parts[:3]))
            return 0
    cfg = load_config()
    if cfg is None:
        # No config -- fall back to default roster.
        merged = default_config()
    else:
        merged = merge_with_defaults(cfg)
    enabled = {k for k, v in merged["reviewers"].items() if v.get("enabled")}
    roster = merged["roster"].get(args.kind, [])
    out = [r for r in roster if r in enabled] or list(enabled)
    if args.kind == "medium":
        print(out[0] if out else "")
    else:
        print(",".join(out[:3]))
    return 0


def cmd_save(_args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"anvil-config: invalid JSON on stdin: {e}", file=sys.stderr)
        return 2
    merged = merge_with_defaults(payload)
    atomic_write(config_path(), json.dumps(merged, indent=2) + "\n")
    print(str(config_path()))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    if args.provider not in PROVIDERS:
        print(f"anvil-config: unknown provider '{args.provider}'", file=sys.stderr)
        return 2
    cfg = load_config() or default_config()
    cfg = merge_with_defaults(cfg)
    block = cfg["reviewers"].setdefault(args.provider, {})
    key_stored_in: str | None = None
    for assignment in args.assignments:
        if "=" not in assignment:
            print(f"anvil-config: expected KEY=VALUE, got {assignment!r}", file=sys.stderr)
            return 2
        k, v = assignment.split("=", 1)
        if k == "enabled" and v.lower() in ("true", "false"):
            block[k] = v.lower() == "true"
        elif k in API_KEY_FIELDS and v:
            try:
                config_val, label = _keychain_store(args.provider, v)
                block[k] = config_val
                key_stored_in = label
            except Exception as e:
                print(f"anvil-config: keychain store failed ({e}), using plaintext", file=sys.stderr)
                block[k] = v
                key_stored_in = "config.json (plaintext)"
        else:
            block[k] = v
    atomic_write(config_path(), json.dumps(cfg, indent=2) + "\n")
    if key_stored_in:
        print(f"ok (api_key stored in {key_stored_in})")
    else:
        print("ok")
    return 0


def cmd_prompt_key(args: argparse.Namespace) -> int:
    """Read an API key interactively (no echo) and store it. Key never appears in argv or tool results."""
    provider = args.provider
    try:
        import getpass
        key = getpass.getpass(f"Enter {provider} API key (input hidden): ")
    except (EOFError, OSError):
        key = sys.stdin.readline().rstrip("\n")
    if not key:
        print(f"anvil-config: no key entered, {provider} key unchanged", file=sys.stderr)
        return 1
    cfg = load_config() or default_config()
    cfg = merge_with_defaults(cfg)
    block = cfg["reviewers"].setdefault(provider, {})
    try:
        config_val, label = _keychain_store(provider, key)
        block["api_key"] = config_val
    except Exception as e:
        print(f"anvil-config: keychain store failed ({e}), using plaintext", file=sys.stderr)
        block["api_key"] = key
        label = "config.json (plaintext)"
    atomic_write(config_path(), json.dumps(cfg, indent=2) + "\n")
    print(f"ok (api_key stored in {label})")
    return 0


def _gui_key_dialog(provider: str) -> str:
    """Show a custom modal Tkinter password dialog. Returns the entered key or ''."""
    try:
        import tkinter as tk
    except ImportError:
        return ""

    BG          = "#f5f5f7"
    HEADER_BG   = "#1c1c2e"
    HEADER_FG   = "#ffffff"
    SUBTITLE_FG = "#a0a0b0"
    ACCENT      = "#4f8ef7"
    ACCENT_FG   = "#ffffff"
    ACCENT_HOV  = "#3a7ae8"
    CANCEL_BG   = "#e0e0e6"
    CANCEL_FG   = "#333333"
    ENTRY_BG    = "#ffffff"
    ENTRY_FG    = "#1a1a2e"

    result: list[str] = [""]

    root = tk.Tk()
    root.withdraw()

    dlg = tk.Toplevel(root)
    dlg.title("Anvil — API Key Setup")
    dlg.resizable(False, False)
    dlg.configure(bg=BG)
    dlg.attributes("-topmost", True)

    header = tk.Frame(dlg, bg=HEADER_BG, pady=18)
    header.pack(fill="x")
    tk.Label(header, text=provider.capitalize(), bg=HEADER_BG, fg=HEADER_FG,
             font=("Segoe UI", 18, "bold")).pack()
    tk.Label(header, text="API Key Required", bg=HEADER_BG, fg=SUBTITLE_FG,
             font=("Segoe UI", 9)).pack(pady=(2, 0))

    body = tk.Frame(dlg, bg=BG, padx=28, pady=20)
    body.pack(fill="both")
    tk.Label(body, text=f"Enter your {provider.capitalize()} API key:",
             bg=BG, fg="#555566", font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(0, 6))

    entry_row = tk.Frame(body, bg=BG)
    entry_row.pack(fill="x")

    entry_var = tk.StringVar()
    entry = tk.Entry(entry_row, textvariable=entry_var, show="*",
                     bg=ENTRY_BG, fg=ENTRY_FG, font=("Segoe UI", 10),
                     relief="flat", bd=0, highlightthickness=1,
                     highlightbackground="#c8c8d0", highlightcolor=ACCENT, width=34)
    entry.pack(side="left", ipady=6)
    entry.focus_set()

    _showing: list[bool] = [False]

    def _toggle_show() -> None:
        _showing[0] = not _showing[0]
        entry.config(show="" if _showing[0] else "*")
        toggle_btn.config(text="Hide" if _showing[0] else "Show")

    toggle_btn = tk.Button(entry_row, text="Show", command=_toggle_show,
                           bg=CANCEL_BG, fg=CANCEL_FG, font=("Segoe UI", 8),
                           relief="flat", cursor="hand2", padx=8, pady=4, bd=0)
    toggle_btn.pack(side="left", padx=(6, 0))

    btn_row = tk.Frame(dlg, bg=BG, padx=28, pady=(4, 20))
    btn_row.pack(fill="x")

    def _confirm(event=None) -> None:
        result[0] = entry_var.get()
        dlg.destroy()

    def _cancel(event=None) -> None:
        result[0] = ""
        dlg.destroy()

    cancel_btn = tk.Button(btn_row, text="Cancel", command=_cancel,
                           bg=CANCEL_BG, fg=CANCEL_FG, font=("Segoe UI", 9),
                           relief="flat", cursor="hand2", padx=14, pady=6, bd=0)
    cancel_btn.pack(side="right", padx=(6, 0))

    ok_btn = tk.Button(btn_row, text="OK", command=_confirm,
                       bg=ACCENT, fg=ACCENT_FG, font=("Segoe UI", 9, "bold"),
                       relief="flat", cursor="hand2", padx=20, pady=6, bd=0,
                       activebackground=ACCENT_HOV, activeforeground=ACCENT_FG)
    ok_btn.pack(side="right")
    ok_btn.bind("<Enter>", lambda e: ok_btn.config(bg=ACCENT_HOV))
    ok_btn.bind("<Leave>", lambda e: ok_btn.config(bg=ACCENT))

    dlg.bind("<Return>", _confirm)
    dlg.bind("<KP_Enter>", _confirm)
    dlg.bind("<Escape>", _cancel)
    dlg.protocol("WM_DELETE_WINDOW", _cancel)

    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    dlg.grab_set()
    root.wait_window(dlg)
    root.destroy()
    return result[0]


def cmd_gui_key(args: argparse.Namespace) -> int:
    """Collect an API key via GUI dialog (no TTY needed). Falls back to getpass, then stdin."""
    provider = args.provider
    key = ""

    # Try custom Tkinter dialog first — works on Windows/macOS/Linux with a display.
    try:
        key = _gui_key_dialog(provider)
    except Exception:
        # Fall back to getpass (needs a real TTY)
        try:
            import getpass
            key = getpass.getpass(f"Enter {provider} API key (input hidden): ")
        except (EOFError, OSError):
            key = sys.stdin.readline().rstrip("\n")

    if not key:
        print(f"anvil-config: no key entered, {provider} key unchanged", file=sys.stderr)
        return 1
    cfg = load_config() or default_config()
    cfg = merge_with_defaults(cfg)
    block = cfg["reviewers"].setdefault(provider, {})
    try:
        config_val, label = _keychain_store(provider, key)
        block["api_key"] = config_val
    except Exception as e:
        print(f"anvil-config: keychain store failed ({e}), using plaintext", file=sys.stderr)
        block["api_key"] = key
        label = "config.json (plaintext)"
    atomic_write(config_path(), json.dumps(cfg, indent=2) + "\n")
    print(f"ok (api_key stored in {label})")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    fn = VALIDATORS.get(args.provider)
    if fn is None:
        print(json.dumps({"status": "unreachable",
                          "detail": f"unknown provider {args.provider}"}))
        return 2
    result = fn()
    print(json.dumps(result))
    return 0 if result.get("status") == "ok" else 1


def _api_key_display(provider: str, raw: str) -> str:
    if raw == "keychain":
        return "(in keychain)"
    if raw.startswith("dpapi:"):
        return "(DPAPI-encrypted)"
    if raw:
        return f"...{raw[-4:]}"
    return "(not set)"


def cmd_summary(_args: argparse.Namespace) -> int:
    cfg = load_config()
    if cfg is None:
        print(f"(no config at {config_path()})")
        return 0
    merged = merge_with_defaults(cfg)
    backend = _keychain_backend()
    print(f"config:           {config_path()}")
    print(f"version:          {merged.get('version')}")
    print(f"setup_completed:  {merged.get('setup_completed') or '(not set)'}")
    print(f"key_storage:      {backend or 'plaintext (no keychain available)'}")
    for name, block in merged["reviewers"].items():
        flag = "ENABLED " if block.get("enabled") else "disabled"
        bits = []
        for k, v in block.items():
            if k == "enabled":
                continue
            if k in API_KEY_FIELDS:
                v = _api_key_display(name, str(v) if v else "")
            bits.append(f"{k}={v}")
        print(f"  {name:<7} {flag}  {' '.join(bits)}")
    print(f"roster.medium:    {','.join(merged['roster'].get('medium', []))}")
    print(f"roster.large:     {','.join(merged['roster'].get('large', []))}")
    return 0


def cmd_keychain_status(_args: argparse.Namespace) -> int:
    backend = _keychain_backend()
    print(f"backend: {backend or 'none — keys will be stored in plaintext'}")
    cfg = load_config()
    if not cfg:
        print("(no config file)")
        return 0
    merged = merge_with_defaults(cfg)
    for provider in ("openai", "gemini"):
        raw = str((merged.get("reviewers", {}).get(provider) or {}).get("api_key", ""))
        if raw == "keychain":
            label = "stored in keychain"
        elif raw.startswith("dpapi:"):
            label = "DPAPI-encrypted in config.json"
        elif raw:
            label = "PLAINTEXT in config.json — re-run /anvil-setup to encrypt"
        else:
            label = "not set"
        print(f"  {provider} api_key: {label}")
    return 0


def cmd_keychain_delete(args: argparse.Namespace) -> int:
    if args.provider not in PROVIDERS:
        print(f"anvil-config: unknown provider '{args.provider}'", file=sys.stderr)
        return 2
    _keychain_remove(args.provider)
    # Also blank the config.json field so no stale marker remains.
    cfg = load_config()
    if cfg:
        merged = merge_with_defaults(cfg)
        block = merged.get("reviewers", {}).get(args.provider, {})
        raw = str(block.get("api_key", ""))
        if raw in ("keychain",) or raw.startswith("dpapi:"):
            block["api_key"] = ""
            atomic_write(config_path(), json.dumps(merged, indent=2) + "\n")
    print(f"keychain entry for {args.provider} removed")
    return 0


# --- arg parsing -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="anvil-config.py",
                                description="claude-anvil configuration helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("path").set_defaults(func=cmd_path)
    sub.add_parser("read").set_defaults(func=cmd_read)

    g = sub.add_parser("get")
    g.add_argument("provider")
    g.add_argument("key")
    g.add_argument("--env-var", default=None)
    g.add_argument("--default", default="")
    g.set_defaults(func=cmd_get)

    r = sub.add_parser("roster")
    r.add_argument("kind", choices=["medium", "large"])
    r.set_defaults(func=cmd_roster)

    sub.add_parser("save").set_defaults(func=cmd_save)

    s = sub.add_parser("set")
    s.add_argument("provider", choices=PROVIDERS)
    s.add_argument("assignments", nargs="+")
    s.set_defaults(func=cmd_set)

    v = sub.add_parser("validate")
    v.add_argument("provider", choices=sorted(VALIDATORS))
    v.set_defaults(func=cmd_validate)

    sub.add_parser("summary").set_defaults(func=cmd_summary)

    sub.add_parser("keychain-status").set_defaults(func=cmd_keychain_status)

    kd = sub.add_parser("keychain-delete")
    kd.add_argument("provider", choices=PROVIDERS)
    kd.set_defaults(func=cmd_keychain_delete)

    pk = sub.add_parser("prompt-key")
    pk.add_argument("provider", choices=PROVIDERS)
    pk.set_defaults(func=cmd_prompt_key)

    gk = sub.add_parser("gui-key")
    gk.add_argument("provider", choices=PROVIDERS)
    gk.set_defaults(func=cmd_gui_key)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
