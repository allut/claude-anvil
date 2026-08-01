"""anvil-config.py pure logic: merging, env precedence, redaction, small helpers."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest


def write_config(anvil_config, payload):
    path = anvil_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- config_path / default_config --------------------------------------------

def test_config_path_honours_the_env_override(anvil_config, tmp_path, monkeypatch):
    monkeypatch.setenv("ANVIL_CONFIG_PATH", str(tmp_path / "elsewhere.json"))
    assert anvil_config.config_path() == tmp_path / "elsewhere.json"


def test_config_path_default_is_under_home(anvil_config, monkeypatch):
    monkeypatch.delenv("ANVIL_CONFIG_PATH", raising=False)
    assert anvil_config.config_path() == Path(os.path.expanduser("~/.claude-anvil/config.json"))


def test_default_config_golden_snapshot(anvil_config):
    assert anvil_config.default_config() == {
        "version": 1,
        "setup_completed": "",
        "reviewers": {
            "claude": {"enabled": True, "model": "sonnet"},
            "openai": {"enabled": False,
                       "endpoint": "https://api.openai.com/v1/chat/completions",
                       "api_key": "", "model": "gpt-4o", "json_mode": "on"},
            "gemini": {"enabled": False, "api_key": "",
                       "model": "gemini-2.5-flash", "endpoint": ""},
            "ollama": {"enabled": False, "host": "http://localhost:11434",
                       "model": "qwen2.5-coder:7b"},
        },
        "roster": {"medium": ["claude"], "large": ["claude", "gemini", "ollama"]},
        "caveman": {"enabled": False, "level": "full"},
    }


def test_default_config_returns_a_fresh_object_each_call(anvil_config):
    a = anvil_config.default_config()
    a["reviewers"]["claude"]["model"] = "mutated"
    assert anvil_config.default_config()["reviewers"]["claude"]["model"] == "sonnet"


def test_providers_and_roster_priority_agree(anvil_config):
    assert set(anvil_config.ROSTER_PRIORITY) == set(anvil_config.PROVIDERS)


# --- load_config --------------------------------------------------------------

def test_load_config_returns_none_when_absent(anvil_config):
    assert anvil_config.load_config() is None


def test_load_config_exits_on_corrupt_json(anvil_config, capsys):
    anvil_config.config_path().parent.mkdir(parents=True, exist_ok=True)
    anvil_config.config_path().write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        anvil_config.load_config()
    assert exc.value.code == 1
    assert "corrupted" in capsys.readouterr().err


# --- merge_with_defaults ------------------------------------------------------

@pytest.mark.parametrize("cfg", [None, [], "string", 7])
def test_merge_non_dict_returns_defaults(anvil_config, cfg):
    assert anvil_config.merge_with_defaults(cfg) == anvil_config.default_config()


def test_merge_fills_missing_reviewer_keys(anvil_config):
    out = anvil_config.merge_with_defaults({"reviewers": {"openai": {"api_key": "k"}}})
    assert out["reviewers"]["openai"]["api_key"] == "k"
    assert out["reviewers"]["openai"]["model"] == "gpt-4o"
    assert out["reviewers"]["ollama"] == anvil_config.default_config()["reviewers"]["ollama"]


def test_merge_drops_none_valued_overrides(anvil_config):
    out = anvil_config.merge_with_defaults(
        {"reviewers": {"openai": {"model": None, "endpoint": None, "api_key": "k"}}})
    assert out["reviewers"]["openai"]["model"] == "gpt-4o"
    assert out["reviewers"]["openai"]["endpoint"] == "https://api.openai.com/v1/chat/completions"


def test_merge_ignores_unknown_reviewers(anvil_config):
    out = anvil_config.merge_with_defaults({"reviewers": {"mystery": {"enabled": True}}})
    assert set(out["reviewers"]) == set(anvil_config.PROVIDERS)


def test_merge_ignores_a_non_list_roster(anvil_config):
    out = anvil_config.merge_with_defaults({"roster": {"large": "claude"}})
    assert out["roster"]["large"] == ["claude", "gemini", "ollama"]


def test_merge_filters_non_string_roster_entries(anvil_config):
    out = anvil_config.merge_with_defaults({"roster": {"large": ["claude", 5, None, "gemini"]}})
    assert out["roster"]["large"] == ["claude", "gemini"]


def test_merge_accepts_an_empty_roster_list(anvil_config):
    assert anvil_config.merge_with_defaults({"roster": {"medium": []}})["roster"]["medium"] == []


def test_merge_preserves_version_and_setup_completed(anvil_config):
    out = anvil_config.merge_with_defaults({"version": 99, "setup_completed": "2024-01-01T00:00:00Z"})
    assert out["version"] == 99
    assert out["setup_completed"] == "2024-01-01T00:00:00Z"


@pytest.mark.parametrize("level", ["lite", "full", "ultra",
                                   "wenyan-lite", "wenyan-full", "wenyan-ultra"])
def test_merge_keeps_valid_caveman_levels(anvil_config, level):
    out = anvil_config.merge_with_defaults({"caveman": {"enabled": True, "level": level}})
    assert out["caveman"] == {"enabled": True, "level": level}


@pytest.mark.parametrize("level", ["shouting", "", None, 3])
def test_merge_replaces_invalid_caveman_levels(anvil_config, level):
    out = anvil_config.merge_with_defaults({"caveman": {"enabled": True, "level": level}})
    assert out["caveman"]["level"] == anvil_config.CAVEMAN_DEFAULT_LEVEL


def test_merge_ignores_a_non_dict_caveman_block(anvil_config):
    out = anvil_config.merge_with_defaults({"caveman": "ultra"})
    assert out["caveman"] == {"enabled": False, "level": "full"}


def test_merge_coerces_caveman_enabled_to_bool(anvil_config):
    assert anvil_config.merge_with_defaults(
        {"caveman": {"enabled": "yes", "level": "lite"}})["caveman"]["enabled"] is True


# --- resolve_enabled ----------------------------------------------------------

@pytest.mark.parametrize("raw", ["true", "TRUE", " on ", "1", "yes", "Yes"])
def test_resolve_enabled_true_tokens(anvil_config, monkeypatch, raw):
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", raw)
    assert anvil_config.resolve_enabled("openai", {"enabled": False}) is True


@pytest.mark.parametrize("raw", ["false", "FALSE", " off ", "0", "no", "No"])
def test_resolve_enabled_false_tokens(anvil_config, monkeypatch, raw):
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", raw)
    assert anvil_config.resolve_enabled("openai", {"enabled": True}) is False


def test_resolve_enabled_unknown_token_warns_and_falls_back_to_config(
        anvil_config, monkeypatch, capsys):
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "maybe")
    assert anvil_config.resolve_enabled("openai", {"enabled": True}) is True
    err = capsys.readouterr().err
    assert "is not a boolean" in err and "falling back to config.json" in err


def test_resolve_enabled_empty_env_is_ignored(anvil_config, monkeypatch, capsys):
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "   ")
    assert anvil_config.resolve_enabled("openai", {"enabled": True}) is True
    assert capsys.readouterr().err == ""


def test_resolve_enabled_uses_default_when_there_is_no_block(anvil_config):
    assert anvil_config.resolve_enabled("openai", None) is False
    assert anvil_config.resolve_enabled("openai", None, default=True) is True


def test_resolve_enabled_env_beats_a_missing_block(anvil_config, monkeypatch):
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "on")
    assert anvil_config.resolve_enabled("openai", None, default=False) is True


def test_resolve_enabled_block_without_the_key_uses_default(anvil_config):
    assert anvil_config.resolve_enabled("openai", {}, default=True) is True
    assert anvil_config.resolve_enabled("openai", {}) is False


def test_resolve_enabled_for_a_provider_with_no_env_var(anvil_config):
    assert anvil_config.resolve_enabled("nosuchprovider", {"enabled": True}) is True


def test_true_and_false_tokens_are_disjoint(anvil_config):
    assert not (anvil_config._TRUE_TOKENS & anvil_config._FALSE_TOKENS)


# --- enabled_reviewers --------------------------------------------------------

def test_enabled_reviewers_reads_the_merged_config(anvil_config):
    merged = anvil_config.merge_with_defaults({})
    assert anvil_config.enabled_reviewers(merged) == {"claude"}


def test_enabled_reviewers_honours_env_overrides(anvil_config, monkeypatch):
    monkeypatch.setenv("ANVIL_GEMINI_ENABLED", "true")
    monkeypatch.setenv("ANVIL_CLAUDE_ENABLED", "off")
    merged = anvil_config.merge_with_defaults({})
    assert anvil_config.enabled_reviewers(merged) == {"gemini"}


# --- resolve_caveman ----------------------------------------------------------

@pytest.mark.parametrize("level", ["lite", "full", "ultra",
                                   "wenyan-lite", "wenyan-full", "wenyan-ultra"])
def test_resolve_caveman_env_levels_roundtrip(anvil_config, monkeypatch, level):
    monkeypatch.setenv("ANVIL_CAVEMAN_LEVEL", level.upper())
    assert anvil_config.resolve_caveman() == level


@pytest.mark.parametrize("raw", ["off", "none", "disabled", "", "  ", "OFF"])
def test_resolve_caveman_off_tokens(anvil_config, monkeypatch, capsys, raw):
    monkeypatch.setenv("ANVIL_CAVEMAN_LEVEL", raw)
    assert anvil_config.resolve_caveman() == "off"
    assert capsys.readouterr().err == ""


def test_resolve_caveman_invalid_env_warns_and_is_off(anvil_config, monkeypatch, capsys):
    monkeypatch.setenv("ANVIL_CAVEMAN_LEVEL", "shouty")
    assert anvil_config.resolve_caveman() == "off"
    assert "is not a valid level" in capsys.readouterr().err


def test_resolve_caveman_env_beats_config(anvil_config, monkeypatch):
    write_config(anvil_config, {"caveman": {"enabled": True, "level": "ultra"}})
    monkeypatch.setenv("ANVIL_CAVEMAN_LEVEL", "lite")
    assert anvil_config.resolve_caveman() == "lite"


def test_resolve_caveman_empty_env_beats_an_enabled_config(anvil_config, monkeypatch):
    """An explicitly empty env value is distinct from unset: it disables."""
    write_config(anvil_config, {"caveman": {"enabled": True, "level": "ultra"}})
    monkeypatch.setenv("ANVIL_CAVEMAN_LEVEL", "")
    assert anvil_config.resolve_caveman() == "off"


def test_resolve_caveman_reads_config_when_env_is_unset(anvil_config):
    write_config(anvil_config, {"caveman": {"enabled": True, "level": "wenyan-full"}})
    assert anvil_config.resolve_caveman() == "wenyan-full"


def test_resolve_caveman_config_needs_enabled_true(anvil_config):
    write_config(anvil_config, {"caveman": {"enabled": False, "level": "ultra"}})
    assert anvil_config.resolve_caveman() == "off"


def test_resolve_caveman_config_with_a_bad_level_uses_the_default(anvil_config):
    write_config(anvil_config, {"caveman": {"enabled": True, "level": "nope"}})
    assert anvil_config.resolve_caveman() == anvil_config.CAVEMAN_DEFAULT_LEVEL


def test_resolve_caveman_with_no_config_is_off(anvil_config):
    assert anvil_config.resolve_caveman() == "off"


# --- resolve_value ------------------------------------------------------------

def test_resolve_value_env_beats_config(anvil_config, monkeypatch):
    write_config(anvil_config, {"reviewers": {"openai": {"model": "from-config"}}})
    monkeypatch.setenv("ANVIL_OPENAI_MODEL", "from-env")
    assert anvil_config.resolve_value("openai", "model", None, "fallback") == "from-env"


def test_resolve_value_falls_back_to_config_then_default(anvil_config):
    write_config(anvil_config, {"reviewers": {"openai": {"model": "from-config"}}})
    assert anvil_config.resolve_value("openai", "model", None, "fallback") == "from-config"
    assert anvil_config.resolve_value("ollama", "nosuchkey", None, "fallback") == "fallback"


def test_resolve_value_with_no_config_returns_the_default(anvil_config):
    assert anvil_config.resolve_value("openai", "model", None, "fallback") == "fallback"


def test_resolve_value_uses_the_env_var_map_when_none_is_passed(anvil_config, monkeypatch):
    monkeypatch.setenv("ANVIL_OLLAMA_HOST", "http://elsewhere:1")
    assert anvil_config.resolve_value("ollama", "host", None, "d") == "http://elsewhere:1"


def test_resolve_value_explicit_env_var_wins_over_the_map(anvil_config, monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_VAR", "custom")
    monkeypatch.setenv("ANVIL_OLLAMA_HOST", "mapped")
    assert anvil_config.resolve_value("ollama", "host", "MY_CUSTOM_VAR", "d") == "custom"


def test_resolve_value_empty_env_falls_through_to_config(anvil_config, monkeypatch):
    write_config(anvil_config, {"reviewers": {"openai": {"model": "from-config"}}})
    monkeypatch.setenv("ANVIL_OPENAI_MODEL", "")
    assert anvil_config.resolve_value("openai", "model", None, "d") == "from-config"


def test_resolve_value_empty_config_value_falls_through_to_default(anvil_config):
    write_config(anvil_config, {"reviewers": {"gemini": {"endpoint": ""}}})
    assert anvil_config.resolve_value("gemini", "endpoint", None, "d") == "d"


def test_resolve_value_decrypts_api_key_fields(anvil_config, monkeypatch):
    write_config(anvil_config, {"reviewers": {"gemini": {"api_key": "dpapi:AAAA"}}})
    monkeypatch.setattr(anvil_config, "_dpapi_decrypt", lambda raw: "sk-plain")
    assert anvil_config.resolve_value("gemini", "api_key", None, "") == "sk-plain"


def test_resolve_value_api_key_that_cannot_be_decrypted_falls_to_default(
        anvil_config, monkeypatch):
    write_config(anvil_config, {"reviewers": {"gemini": {"api_key": "dpapi:AAAA"}}})
    monkeypatch.setattr(anvil_config, "_dpapi_decrypt", lambda raw: None)
    assert anvil_config.resolve_value("gemini", "api_key", None, "fb") == "fb"


@pytest.mark.parametrize("raw", ["", " ", "0", "false", "value"])
def test_resolve_value_and_review_setting_agree_on_env_truthiness(
        anvil_config, anvil_review, monkeypatch, raw):
    """Cross-check: resolve_value uses `if v:` and anvil-review._setting uses
    `not in (None, "")`. They must agree, or the reviewer and the config CLI
    would resolve the same env var differently."""
    monkeypatch.setenv("ANVIL_OPENAI_MODEL", raw)
    from_config = anvil_config.resolve_value("openai", "model", None, "DEFAULT")
    from_review = anvil_review._setting("openai", "model", "ANVIL_OPENAI_MODEL", "DEFAULT")
    assert from_config == from_review


# --- small helpers ------------------------------------------------------------

def test_key_id(anvil_config):
    assert anvil_config._key_id("gemini") == "anvil-gemini-api-key"


@pytest.mark.parametrize("code,expected", [
    (404, "model-missing"),
    (400, "unauthorized"),
    (401, "unauthorized"),
    (403, "unauthorized"),
    (429, "unauthorized"),
    (500, "unreachable"),
    (503, "unreachable"),
])
def test_classify_http_error(anvil_config, code, expected):
    err = urllib.error.HTTPError("http://x", code, "Reason", None, None)
    status, detail = anvil_config._classify_http_error(err)
    assert status == expected
    assert detail == f"HTTP {code} Reason"


@pytest.mark.parametrize("raw,expected", [
    ("keychain", "(in keychain)"),
    ("dpapi:AQAAA", "(DPAPI-encrypted)"),
    ("sk-abcdefgh", "...efgh"),
    ("abc", "...abc"),
    ("", "(not set)"),
])
def test_api_key_display(anvil_config, raw, expected):
    assert anvil_config._api_key_display("openai", raw) == expected


def test_api_key_display_never_leaks_the_whole_key(anvil_config):
    assert "sk-supersecret" not in anvil_config._api_key_display("openai", "sk-supersecretXYZW")


# --- _strip_context7_tools ----------------------------------------------------

def test_strip_context7_tools_removes_only_the_context7_entries(anvil_config):
    src = ("---\nallowed-tools: Bash, Read, "
           "mcp__plugin_claude-anvil_context7__query-docs, "
           "mcp__plugin_claude-anvil_context7__resolve-library-id, Task\n---\n")
    out = anvil_config._strip_context7_tools(src)
    assert "context7" not in out
    assert "allowed-tools: Bash, Read, Task" in out


def test_strip_context7_tools_preserves_a_line_that_would_become_empty(anvil_config):
    src = "allowed-tools: mcp__plugin_claude-anvil_context7__query-docs\n"
    assert anvil_config._strip_context7_tools(src) == src


def test_strip_context7_tools_leaves_other_lines_alone(anvil_config):
    src = "name: anvil\ndescription: mentions allowed-tools inline\n"
    assert anvil_config._strip_context7_tools(src) == src


def test_strip_context7_tools_handles_multiple_frontmatter_blocks(anvil_config):
    src = "allowed-tools: Bash, mcp__plugin_claude-anvil_context7__query-docs\nx\nallowed-tools: Read\n"
    out = anvil_config._strip_context7_tools(src)
    assert out == "allowed-tools: Bash\nx\nallowed-tools: Read\n"


# --- atomic_write -------------------------------------------------------------

def test_atomic_write_creates_parents_and_writes_content(anvil_config, tmp_path):
    target = tmp_path / "a" / "b" / "c.json"
    anvil_config.atomic_write(target, '{"x": 1}\n')
    assert target.read_text(encoding="utf-8") == '{"x": 1}\n'


def test_atomic_write_leaves_no_temp_files_behind(anvil_config, tmp_path):
    target = tmp_path / "c.json"
    anvil_config.atomic_write(target, "one")
    anvil_config.atomic_write(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".anvil-config-")] == []


def test_atomic_write_cleans_up_when_writing_fails(anvil_config, tmp_path, monkeypatch):
    target = tmp_path / "c.json"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(anvil_config.os, "replace", boom)
    with pytest.raises(OSError):
        anvil_config.atomic_write(target, "x")
    assert not target.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".anvil-config-")] == []


@pytest.mark.skipif(sys.platform == "win32", reason="os.chmod modes are a no-op on Windows")
def test_atomic_write_sets_0600_on_posix(anvil_config, tmp_path):
    target = tmp_path / "c.json"
    anvil_config.atomic_write(target, "x")
    assert (target.stat().st_mode & 0o777) == 0o600
