"""Manifest / packaging consistency. Cheap checks that catch drift."""
from __future__ import annotations

import json
import py_compile
import re
import sqlite3
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, SCHEMA_SQL, SCRIPTS

PLUGIN = REPO_ROOT / "plugin"
SCRIPT_STEMS = (
    "anvil_shared",
    "anvil-config",
    "anvil-ledger",
    "anvil-review",
    "anvil-gate-commit",
    "anvil-track-edit",
)


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- schema -------------------------------------------------------------------

def test_schema_applies_to_a_fresh_db_and_is_idempotent(tmp_path):
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.executescript(sql)
    conn.executescript(sql)  # every statement is CREATE ... IF NOT EXISTS
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    conn.close()
    assert {"anvil_checks", "sessions", "session_files", "search_index", "memory"} <= names


def test_anvil_checks_phase_constraint_matches_the_documented_vocabulary(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    for phase in ("baseline", "after", "review"):
        conn.execute(
            "INSERT INTO anvil_checks (task_id, phase, check_name, tool, passed) "
            "VALUES ('t', ?, 'c', 'x', 1)", (phase,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO anvil_checks (task_id, phase, check_name, tool, passed) "
            "VALUES ('t', 'bogus', 'c', 'x', 1)")
    conn.close()


# --- hooks.json ---------------------------------------------------------------

def test_hook_matchers_and_command_paths():
    hooks = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]
    assert [h["matcher"] for h in hooks["PreToolUse"]] == ["Bash"]
    assert [h["matcher"] for h in hooks["PostToolUse"]] == ["Edit|Write|MultiEdit"]

    commands = [entry["command"]
                for group in hooks.values()
                for h in group
                for entry in h["hooks"]]
    assert len(commands) == 2
    for command in commands:
        m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+?\.py)", command)
        assert m, f"no ${{CLAUDE_PLUGIN_ROOT}}-relative script in {command!r}"
        assert (PLUGIN / m.group(1)).exists(), f"{m.group(1)} does not exist"

    assert any("anvil-gate-commit.py" in c for c in commands)
    assert any("anvil-track-edit.py" in c for c in commands)


def test_hook_commands_never_execute_python3_to_probe_for_it():
    """On Windows `python3` is commonly a 0-byte Microsoft Store alias that opens
    the Store. The hooks must resolve it by inspecting the path (`command -v` +
    `[ -s ]`), never by running it."""
    hooks = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]
    commands = [entry["command"]
                for group in hooks.values()
                for h in group
                for entry in h["hooks"]]
    for command in commands:
        assert "command -v python3" in command
        assert "-s " in command, "the 0-byte Store alias must be rejected by size"
        assert not re.search(r"python3\s+-c", command), \
            f"hook probes by executing python3: {command!r}"
        assert "$ANVIL_PYTHON" in command


# --- plugin.json / marketplace.json -------------------------------------------

def test_plugin_manifest_paths_exist():
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert (PLUGIN / manifest["commands"]).is_dir()
    assert (PLUGIN / manifest["mcpServers"]).is_file()
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), manifest["version"]


def test_marketplace_entry_matches_the_plugin_manifest():
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    market = _json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
    entries = [p for p in market["plugins"] if p["name"] == manifest["name"]]
    assert len(entries) == 1, f"no marketplace entry for {manifest['name']}"
    entry = entries[0]
    assert (REPO_ROOT / entry["source"]).resolve() == PLUGIN.resolve()
    assert entry["license"] == manifest["license"]
    assert entry["repository"] == manifest["repository"]


def test_every_declared_command_file_exists():
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    commands_dir = PLUGIN / manifest["commands"]
    for name in ("anvil.md", "anvil-setup.md"):
        assert (commands_dir / name).is_file()


# --- .env.example -------------------------------------------------------------

def test_every_env_var_map_entry_is_documented(anvil_config):
    documented = (PLUGIN / ".env.example").read_text(encoding="utf-8")
    missing = sorted(v for v in anvil_config.ENV_VAR_MAP.values()
                     if not re.search(rf"^#?\s*{re.escape(v)}\b", documented, re.MULTILINE))
    assert missing == [], f"undocumented in plugin/.env.example: {missing}"


def test_env_example_documents_the_non_provider_vars():
    documented = (PLUGIN / ".env.example").read_text(encoding="utf-8")
    for var in ("ANVIL_DB_PATH", "ANVIL_MEDIUM_REVIEWER", "ANVIL_LARGE_REVIEWERS",
                "ANVIL_REVIEW_HTTP_TIMEOUT", "ANVIL_REVIEW_TOTAL_TIMEOUT",
                "ANVIL_CAVEMAN_LEVEL"):
        assert re.search(rf"^#?\s*{var}\b", documented, re.MULTILINE), f"{var} undocumented"


# --- agent definition ---------------------------------------------------------

def test_code_review_agent_frontmatter():
    text = (PLUGIN / "agents" / "code-review-claude.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    fields = dict(
        (k.strip(), v.strip())
        for k, v in (line.split(":", 1) for line in front.strip().splitlines() if ":" in line)
    )
    assert fields["name"] == "code-review-claude"
    assert fields["model"] in {"sonnet", "haiku", "opus"}


# --- scripts compile ----------------------------------------------------------

@pytest.mark.parametrize("stem", SCRIPT_STEMS)
def test_script_compiles(stem, tmp_path):
    source = SCRIPTS / f"{stem}.py"
    assert source.is_file()
    py_compile.compile(str(source), cfile=str(tmp_path / f"{stem}.pyc"), doraise=True)


@pytest.mark.parametrize("stem", ["anvil-config", "anvil-ledger", "anvil-review"])
def test_cli_scripts_expose_help(stem):
    r = subprocess.run([sys.executable, str(SCRIPTS / f"{stem}.py"), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "usage:" in r.stdout.lower()
