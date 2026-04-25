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
                    "model": "gpt-5", "json_mode": "on"},
        "gemini": {"enabled": false, "api_key": "", "model": "gemini-2.5-pro",
                    "endpoint": ""},
        "ollama": {"enabled": false, "host": "http://localhost:11434",
                    "model": "qwen2.5-coder:7b"}
      },
      "roster": {"medium": ["claude"], "large": ["claude", "gemini", "ollama"]}
    }

Env vars always win over config.json -- this preserves the legacy `.env`-based
workflow. The Python reviewer scripts call `get` to resolve credentials with
that contract enforced in one place.

Subcommands:
    status                              -> prints "configured"/"partial"/"needs-setup"
    path                                -> prints absolute config path
    read                                -> prints raw JSON (exit 1 if missing)
    get PROVIDER KEY [--env-var NAME]
        [--default VAL]                 -> resolves env -> config -> default
    roster {medium,large}               -> comma-separated reviewer list
    save                                -> reads JSON config from stdin and writes it (atomic, chmod 0600)
    set PROVIDER KEY=VALUE [KEY=VALUE]  -> merges into config without rewriting all fields
    validate PROVIDER                   -> tiny live HTTP probe; prints JSON status
    summary                             -> human-readable masked summary
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_REL = "~/.claude-anvil/config.json"
SCHEMA_VERSION = 1
PROVIDERS = ("claude", "openai", "gemini", "ollama")
HTTP_TIMEOUT = 15.0


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
                "model": "gpt-5",
                "json_mode": "on",
            },
            "gemini": {
                "enabled": False,
                "api_key": "",
                "model": "gemini-2.5-pro",
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
    except (OSError, json.JSONDecodeError):
        return None


def merge_with_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of cfg with any missing keys filled from default_config()."""
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    out = base
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
            return str(v)
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
    model = resolve_value("openai", "model", None, "gpt-5")
    if not api_key:
        return {"status": "unauthorized", "detail": "ANVIL_OPENAI_API_KEY / config.api_key is empty"}
    # Derive /v1/models from a chat-completions endpoint.
    base = re.sub(r"/chat/completions/?$", "", endpoint.rstrip("/"))
    url = f"{base}/models"
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
    model = resolve_value("gemini", "model", None, "gemini-2.5-pro")
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
            print(env.split(",")[0].strip())
            return 0
    else:
        env = os.environ.get("ANVIL_LARGE_REVIEWERS", "").strip()
        if env:
            print(env)
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
    for assignment in args.assignments:
        if "=" not in assignment:
            print(f"anvil-config: expected KEY=VALUE, got {assignment!r}", file=sys.stderr)
            return 2
        k, v = assignment.split("=", 1)
        if v.lower() in ("true", "false") and k == "enabled":
            block[k] = v.lower() == "true"
        else:
            block[k] = v
    atomic_write(config_path(), json.dumps(cfg, indent=2) + "\n")
    print("ok")
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


def cmd_summary(_args: argparse.Namespace) -> int:
    cfg = load_config()
    if cfg is None:
        print(f"(no config at {config_path()})")
        return 0
    merged = merge_with_defaults(cfg)
    print(f"config:           {config_path()}")
    print(f"version:          {merged.get('version')}")
    print(f"setup_completed:  {merged.get('setup_completed') or '(not set)'}")
    for name, block in merged["reviewers"].items():
        flag = "ENABLED " if block.get("enabled") else "disabled"
        bits = []
        for k, v in block.items():
            if k == "enabled":
                continue
            if k == "api_key" and v:
                v = f"...{str(v)[-4:]}"
            bits.append(f"{k}={v}")
        print(f"  {name:<7} {flag}  {' '.join(bits)}")
    print(f"roster.medium:    {','.join(merged['roster'].get('medium', []))}")
    print(f"roster.large:     {','.join(merged['roster'].get('large', []))}")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
