"""anvil-config.py CLI surface, driven in-process through main(argv).

main() returns an int and does not call sys.exit, so every case is a plain
function call plus capsys.
"""
from __future__ import annotations

import argparse
import copy
import io
import json

import pytest


@pytest.fixture
def cfg(anvil_config):
    """Bound helpers for the module under test."""
    class _Bound:
        mod = anvil_config

        @staticmethod
        def run(*argv):
            return anvil_config.main(list(argv))

        @staticmethod
        def write(payload):
            path = anvil_config.config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return path

        @staticmethod
        def read():
            return json.loads(anvil_config.config_path().read_text(encoding="utf-8"))

    return _Bound


def configured(**overrides):
    base = {
        "version": 1,
        "setup_completed": "2024-01-01T00:00:00Z",
        "reviewers": {
            "claude": {"enabled": True, "model": "sonnet"},
            "openai": {"enabled": True,
                       "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                       "api_key": "dpapi:SECRETBLOB", "model": "gpt-4o", "json_mode": "off"},
            "gemini": {"enabled": True, "api_key": "keychain",
                       "model": "gemini-2.5-flash", "endpoint": ""},
            "ollama": {"enabled": False, "host": "http://localhost:11434",
                       "model": "qwen2.5-coder:7b"},
        },
        "roster": {"medium": ["claude"], "large": ["claude", "gemini", "openai"]},
        "caveman": {"enabled": False, "level": "full"},
    }
    base.update(overrides)
    return base


# --- status / path / read -----------------------------------------------------

def test_status_needs_setup_without_a_config(cfg, capsys):
    assert cfg.run("status") == 0
    assert capsys.readouterr().out.strip() == "needs-setup"


def test_status_configured(cfg, capsys):
    cfg.write(configured())
    assert cfg.run("status") == 0
    assert capsys.readouterr().out.strip() == "configured"


def test_status_partial_without_setup_completed(cfg, capsys):
    cfg.write(configured(setup_completed=""))
    cfg.run("status")
    assert capsys.readouterr().out.strip() == "partial"


def test_status_partial_when_nothing_is_enabled(cfg, capsys, monkeypatch):
    for p in ("CLAUDE", "OPENAI", "GEMINI", "OLLAMA"):
        monkeypatch.setenv(f"ANVIL_{p}_ENABLED", "false")
    cfg.write(configured())
    cfg.run("status")
    assert capsys.readouterr().out.strip() == "partial"


def test_path_prints_the_resolved_config_path(cfg, capsys):
    assert cfg.run("path") == 0
    assert capsys.readouterr().out.strip() == str(cfg.mod.config_path())


def test_read_without_a_config_exits_one(cfg, capsys):
    assert cfg.run("read") == 1
    assert "no config at" in capsys.readouterr().err


def test_read_echoes_the_raw_file(cfg, capsys):
    path = cfg.write(configured())
    assert cfg.run("read") == 0
    assert capsys.readouterr().out == path.read_text(encoding="utf-8")


# --- get ----------------------------------------------------------------------

def test_get_returns_a_config_value(cfg, capsys):
    cfg.write(configured())
    cfg.run("get", "openai", "model")
    assert capsys.readouterr().out.strip() == "gpt-4o"


def test_get_env_wins(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_OPENAI_MODEL", "from-env")
    cfg.run("get", "openai", "model")
    assert capsys.readouterr().out.strip() == "from-env"


def test_get_uses_the_supplied_default(cfg, capsys):
    cfg.run("get", "ollama", "nosuchkey", "--default", "fallback")
    assert capsys.readouterr().out.strip() == "fallback"


def test_get_enabled_prints_literal_booleans(cfg, capsys):
    cfg.write(configured())
    cfg.run("get", "openai", "enabled")
    assert capsys.readouterr().out.strip() == "true"
    cfg.run("get", "ollama", "enabled")
    assert capsys.readouterr().out.strip() == "false"


def test_get_enabled_routes_through_resolve_enabled(cfg, capsys, monkeypatch):
    """CLAUDE.md invariant: anvil-review.py's reviewer gate asks via this
    subcommand, so it must apply resolve_enabled's env rules -- including the
    warn-and-fall-back behaviour for a non-boolean value."""
    cfg.write(configured())
    calls = []
    real = cfg.mod.resolve_enabled
    monkeypatch.setattr(cfg.mod, "resolve_enabled",
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    cfg.run("get", "openai", "enabled")
    assert calls, "cmd_get did not route through resolve_enabled"
    assert capsys.readouterr().out.strip() == "true"


def test_get_enabled_env_override(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "no")
    cfg.run("get", "openai", "enabled")
    assert capsys.readouterr().out.strip() == "false"


def test_get_enabled_non_boolean_env_warns_and_uses_config(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "sometimes")
    cfg.run("get", "openai", "enabled")
    out = capsys.readouterr()
    assert out.out.strip() == "true"
    assert "is not a boolean" in out.err


def test_get_enabled_without_a_config_honours_the_default_flag(cfg, capsys):
    cfg.run("get", "openai", "enabled", "--default", "true")
    assert capsys.readouterr().out.strip() == "true"
    cfg.run("get", "openai", "enabled", "--default", "false")
    assert capsys.readouterr().out.strip() == "false"


# --- roster -------------------------------------------------------------------

def test_roster_medium_from_config(cfg, capsys):
    cfg.write(configured())
    cfg.run("roster", "medium")
    assert capsys.readouterr().out.strip() == "claude"


def test_roster_large_from_config(cfg, capsys):
    cfg.write(configured())
    cfg.run("roster", "large")
    assert capsys.readouterr().out.strip() == "claude,gemini,openai"


def test_roster_drops_disabled_reviewers(cfg, capsys):
    conf = configured()
    conf["reviewers"]["gemini"]["enabled"] = False
    cfg.write(conf)
    cfg.run("roster", "large")
    assert capsys.readouterr().out.strip() == "claude,openai"


def test_roster_medium_env_override(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_MEDIUM_REVIEWER", "gemini")
    cfg.run("roster", "medium")
    assert capsys.readouterr().out.strip() == "gemini"


def test_roster_medium_env_with_several_entries_warns_and_takes_the_first(
        cfg, capsys, monkeypatch):
    monkeypatch.setenv("ANVIL_MEDIUM_REVIEWER", "gemini,claude,ollama")
    cfg.run("roster", "medium")
    out = capsys.readouterr()
    assert out.out.strip() == "gemini"
    assert "has 3 entries" in out.err


def test_roster_large_env_caps_at_three_with_a_warning(cfg, capsys, monkeypatch):
    monkeypatch.setenv("ANVIL_LARGE_REVIEWERS", "claude,gemini,openai,ollama")
    cfg.run("roster", "large")
    out = capsys.readouterr()
    assert out.out.strip() == "claude,gemini,openai"
    assert "only the first 3" in out.err


def test_roster_env_is_ignored_when_blank(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_LARGE_REVIEWERS", "   ")
    cfg.run("roster", "large")
    assert capsys.readouterr().out.strip() == "claude,gemini,openai"


def test_roster_falls_back_to_pinned_priority_when_the_roster_names_nobody_enabled(
        cfg, capsys):
    conf = configured()
    conf["roster"]["large"] = ["ollama"]          # ollama is disabled
    cfg.write(conf)
    cfg.run("roster", "large")
    # ROSTER_PRIORITY order, not set-iteration order.
    assert capsys.readouterr().out.strip() == "claude,gemini,openai"


def test_roster_without_a_config_uses_default_config(cfg, capsys):
    cfg.run("roster", "large")
    assert capsys.readouterr().out.strip() == "claude"
    cfg.run("roster", "medium")
    assert capsys.readouterr().out.strip() == "claude"


def test_roster_medium_prints_an_empty_line_when_nothing_is_enabled(cfg, capsys, monkeypatch):
    monkeypatch.setenv("ANVIL_CLAUDE_ENABLED", "false")
    cfg.write(configured(reviewers={
        "claude": {"enabled": False}, "openai": {"enabled": False},
        "gemini": {"enabled": False}, "ollama": {"enabled": False}}))
    cfg.run("roster", "medium")
    assert capsys.readouterr().out.strip() == ""


# --- save ---------------------------------------------------------------------

def test_save_reads_stdin_and_merges_defaults(cfg, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"setup_completed": "now"})))
    assert cfg.run("save") == 0
    assert capsys.readouterr().out.strip() == str(cfg.mod.config_path())
    saved = cfg.read()
    assert saved["setup_completed"] == "now"
    assert saved["reviewers"]["openai"]["model"] == "gpt-4o"


def test_save_rejects_invalid_json(cfg, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{nope"))
    assert cfg.run("save") == 2
    assert "invalid JSON on stdin" in capsys.readouterr().err
    assert not cfg.mod.config_path().exists()


# --- set ----------------------------------------------------------------------

def test_set_writes_a_plain_value(cfg):
    cfg.write(configured())
    assert cfg.run("set", "ollama", "model=deepseek-coder-v2:16b") == 0
    assert cfg.read()["reviewers"]["ollama"]["model"] == "deepseek-coder-v2:16b"


def test_set_accepts_several_assignments(cfg):
    cfg.write(configured())
    cfg.run("set", "ollama", "model=m", "host=http://h:1")
    block = cfg.read()["reviewers"]["ollama"]
    assert block["model"] == "m" and block["host"] == "http://h:1"


def test_set_keeps_everything_after_the_first_equals(cfg):
    cfg.write(configured())
    cfg.run("set", "openai", "endpoint=https://x/v1/chat/completions?a=b")
    assert cfg.read()["reviewers"]["openai"]["endpoint"] == "https://x/v1/chat/completions?a=b"


def test_set_without_an_equals_sign_exits_two(cfg, capsys):
    cfg.write(configured())
    assert cfg.run("set", "ollama", "model") == 2
    assert "expected KEY=VALUE" in capsys.readouterr().err


@pytest.mark.parametrize("raw,expected", [("true", True), ("TRUE", True),
                                          ("false", False), ("False", False)])
def test_set_enabled_true_false_becomes_a_bool(cfg, raw, expected):
    cfg.write(configured())
    cfg.run("set", "ollama", f"enabled={raw}")
    assert cfg.read()["reviewers"]["ollama"]["enabled"] is expected


def test_set_enabled_one_is_stored_as_a_string(cfg, anvil_config):
    """Bug probe: only the literal 'true'/'false' spellings are coerced
    (anvil-config.py:710-711). `enabled=1` lands as the *string* "1", which
    bool() at :434 then reads as truthy -- right answer, wrong type. `enabled=0`
    is the dangerous one: the string "0" is also truthy."""
    cfg.write(configured())
    cfg.run("set", "ollama", "enabled=1")
    assert cfg.read()["reviewers"]["ollama"]["enabled"] == "1"
    assert cfg.run("get", "ollama", "enabled") == 0

    cfg.run("set", "ollama", "enabled=0")
    assert cfg.read()["reviewers"]["ollama"]["enabled"] == "0"
    assert anvil_config.resolve_enabled("ollama", {"enabled": "0"}) is True


def test_set_api_key_goes_through_the_keychain(cfg, monkeypatch, capsys):
    cfg.write(configured())
    monkeypatch.setattr(cfg.mod, "_keychain_store",
                        lambda provider, value: ("dpapi:ENCRYPTED", "config.json (DPAPI-encrypted)"))
    assert cfg.run("set", "gemini", "api_key=sk-supersecret") == 0
    out = capsys.readouterr().out
    assert "sk-supersecret" not in out
    assert "DPAPI-encrypted" in out
    assert cfg.read()["reviewers"]["gemini"]["api_key"] == "dpapi:ENCRYPTED"


def test_set_api_key_falls_back_to_plaintext_when_the_keychain_fails(cfg, monkeypatch, capsys):
    cfg.write(configured())

    def boom(provider, value):
        raise RuntimeError("no keychain here")

    monkeypatch.setattr(cfg.mod, "_keychain_store", boom)
    cfg.run("set", "gemini", "api_key=sk-plain")
    assert "keychain store failed" in capsys.readouterr().err
    assert cfg.read()["reviewers"]["gemini"]["api_key"] == "sk-plain"


def test_set_unknown_provider_is_rejected_by_argparse(cfg):
    with pytest.raises(SystemExit):
        cfg.run("set", "mystery", "model=x")


# --- enable / disable ---------------------------------------------------------

def test_disable_flips_the_flag_and_drops_it_from_both_rosters(cfg):
    cfg.write(configured())
    assert cfg.run("disable", "openai") == 0
    saved = cfg.read()
    assert saved["reviewers"]["openai"]["enabled"] is False
    assert "openai" not in saved["roster"]["large"]
    assert "openai" not in saved["roster"]["medium"]


def test_disable_leaves_credentials_byte_identical(cfg):
    original = configured()
    cfg.write(original)
    before = copy.deepcopy(original["reviewers"]["openai"])
    cfg.run("disable", "openai")
    after = cfg.read()["reviewers"]["openai"]
    for key in ("api_key", "endpoint", "model", "json_mode"):
        assert after[key] == before[key], key


def test_enable_adds_to_both_rosters_without_touching_credentials(cfg):
    conf = configured()
    conf["reviewers"]["ollama"]["model"] = "custom-model"
    cfg.write(conf)
    assert cfg.run("enable", "ollama") == 0
    saved = cfg.read()
    assert saved["reviewers"]["ollama"]["enabled"] is True
    assert "ollama" in saved["roster"]["medium"] and "ollama" in saved["roster"]["large"]
    assert saved["reviewers"]["ollama"]["model"] == "custom-model"


def test_enable_is_idempotent(cfg):
    cfg.write(configured())
    cfg.run("enable", "ollama")
    first = cfg.read()
    cfg.run("enable", "ollama")
    assert cfg.read() == first


def test_disable_is_idempotent(cfg):
    cfg.write(configured())
    cfg.run("disable", "openai")
    first = cfg.read()
    cfg.run("disable", "openai")
    assert cfg.read() == first


def test_disable_prints_the_four_deterministic_status_lines(cfg, capsys):
    cfg.write(configured())
    cfg.run("disable", "openai")
    assert capsys.readouterr().out == (
        "ok (openai disabled; credentials untouched)\n"
        "  enabled reviewers: claude, gemini\n"
        "  roster.medium:     claude\n"
        "  roster.large:      claude,gemini\n"
    )


def test_enable_prints_the_four_deterministic_status_lines(cfg, capsys):
    cfg.write(configured())
    cfg.run("enable", "ollama")
    assert capsys.readouterr().out == (
        "ok (ollama enabled; credentials untouched)\n"
        "  enabled reviewers: claude, gemini, ollama, openai\n"
        "  roster.medium:     claude,ollama\n"
        "  roster.large:      claude,gemini,openai,ollama\n"
    )


def test_disable_warns_when_an_opposite_polarity_env_var_overrides_it(
        cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "true")
    cfg.run("disable", "openai")
    err = capsys.readouterr().err
    assert "overrides config.json" in err
    assert "ANVIL_OPENAI_ENABLED" in err


def test_disable_does_not_warn_when_the_env_var_agrees(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "false")
    cfg.run("disable", "openai")
    assert "overrides config.json" not in capsys.readouterr().err


def test_disable_notes_a_non_boolean_env_var_instead_of_warning(cfg, capsys, monkeypatch):
    cfg.write(configured())
    monkeypatch.setenv("ANVIL_OPENAI_ENABLED", "perhaps")
    cfg.run("disable", "openai")
    err = capsys.readouterr().err
    assert "is not a boolean and is" in err
    assert "overrides config.json" not in err


def test_disabling_the_last_reviewer_warns(cfg, capsys):
    conf = configured()
    for name in conf["reviewers"]:
        conf["reviewers"][name]["enabled"] = False
    conf["reviewers"]["claude"]["enabled"] = True
    cfg.write(conf)
    cfg.run("disable", "claude")
    assert "no reviewers are enabled" in capsys.readouterr().err


def test_enable_unknown_provider_is_rejected_by_argparse(cfg):
    with pytest.raises(SystemExit):
        cfg.run("enable", "mystery")


# --- caveman ------------------------------------------------------------------

@pytest.mark.parametrize("level", ["lite", "full", "ultra",
                                   "wenyan-lite", "wenyan-full", "wenyan-ultra"])
def test_set_caveman_roundtrips_every_valid_level(cfg, capsys, level):
    assert cfg.run("set-caveman", level) == 0
    assert capsys.readouterr().out.strip() == f"ok (caveman {level})"
    cfg.run("caveman")
    assert capsys.readouterr().out.strip() == level


@pytest.mark.parametrize("word", ["off", "none", "disabled", "OFF"])
def test_set_caveman_off_words(cfg, capsys, word):
    cfg.run("set-caveman", "ultra")
    capsys.readouterr()
    assert cfg.run("set-caveman", word) == 0
    assert capsys.readouterr().out.strip() == "ok (caveman off)"
    cfg.run("caveman")
    assert capsys.readouterr().out.strip() == "off"


def test_set_caveman_off_preserves_the_stored_level(cfg):
    cfg.run("set-caveman", "wenyan-ultra")
    cfg.run("set-caveman", "off")
    saved = cfg.read()["caveman"]
    assert saved == {"enabled": False, "level": "wenyan-ultra"}


def test_set_caveman_rejects_an_unknown_level(cfg, capsys):
    assert cfg.run("set-caveman", "shouty") == 2
    assert "invalid caveman level" in capsys.readouterr().err
    assert not cfg.mod.config_path().exists()


def test_caveman_env_beats_the_stored_level(cfg, capsys, monkeypatch):
    cfg.run("set-caveman", "ultra")
    capsys.readouterr()
    monkeypatch.setenv("ANVIL_CAVEMAN_LEVEL", "lite")
    cfg.run("caveman")
    assert capsys.readouterr().out.strip() == "lite"


def test_caveman_without_a_config_is_off(cfg, capsys):
    cfg.run("caveman")
    assert capsys.readouterr().out.strip() == "off"


# --- validate -----------------------------------------------------------------

def test_validate_exits_zero_only_when_status_is_ok(cfg, capsys, monkeypatch):
    monkeypatch.setenv("ANVIL_CLAUDE_MODEL", "opus")
    assert cfg.run("validate", "claude") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    monkeypatch.setenv("ANVIL_CLAUDE_MODEL", "gpt-4o")
    assert cfg.run("validate", "claude") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "model-missing"


def test_validate_unknown_provider_is_rejected_by_argparse(cfg):
    with pytest.raises(SystemExit):
        cfg.run("validate", "mystery")


# --- summary / keychain-status ------------------------------------------------

def test_summary_without_a_config(cfg, capsys):
    assert cfg.run("summary") == 0
    assert "(no config at" in capsys.readouterr().out


def test_summary_masks_api_keys(cfg, capsys):
    cfg.write(configured())
    assert cfg.run("summary") == 0
    out = capsys.readouterr().out
    assert "SECRETBLOB" not in out
    assert "(DPAPI-encrypted)" in out
    assert "(in keychain)" in out
    assert "roster.medium:    claude" in out
    assert "caveman:          off" in out


def test_summary_shows_the_caveman_level_when_enabled(cfg, capsys):
    cfg.write(configured(caveman={"enabled": True, "level": "ultra"}))
    cfg.run("summary")
    assert "caveman:          ultra" in capsys.readouterr().out


def test_keychain_status_without_a_config(cfg, capsys):
    assert cfg.run("keychain-status") == 0
    assert "(no config file)" in capsys.readouterr().out


def test_keychain_status_labels_each_storage_shape(cfg, capsys, monkeypatch):
    monkeypatch.setattr(cfg.mod, "_keychain_backend", lambda: "windows-dpapi")
    conf = configured()
    conf["reviewers"]["openai"]["api_key"] = "sk-plaintext"
    conf["reviewers"]["gemini"]["api_key"] = "keychain"
    cfg.write(conf)
    cfg.run("keychain-status")
    out = capsys.readouterr().out
    assert "backend: windows-dpapi" in out
    assert "openai api_key: PLAINTEXT in config.json" in out
    assert "gemini api_key: stored in keychain" in out
    assert "sk-plaintext" not in out


def test_keychain_delete_clears_the_stored_marker(cfg, capsys, monkeypatch):
    monkeypatch.setattr(cfg.mod, "_keychain_remove", lambda provider: None)
    cfg.write(configured())
    assert cfg.run("keychain-delete", "gemini") == 0
    assert cfg.read()["reviewers"]["gemini"]["api_key"] == ""
    assert "removed" in capsys.readouterr().out


# --- parser -------------------------------------------------------------------

DOCUMENTED_SUBCOMMANDS = {
    "status", "path", "read", "get", "roster", "save", "set", "enable", "disable",
    "validate", "summary", "caveman", "set-caveman", "keychain-status",
    "keychain-delete", "prompt-key", "gui-key", "create-shortcuts",
}


def test_parser_subcommand_set_is_exactly_the_documented_one(anvil_config):
    parser = anvil_config.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == DOCUMENTED_SUBCOMMANDS
    assert len(DOCUMENTED_SUBCOMMANDS) == 18


def test_every_subcommand_is_wired_to_a_handler(anvil_config):
    parser = anvil_config.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert sub.required is True
    for name, p in sub.choices.items():
        assert callable(p.get_default("func")), f"{name} has no callable func default"


def test_missing_subcommand_exits(anvil_config):
    with pytest.raises(SystemExit):
        anvil_config.main([])


def test_main_returns_zero_when_a_handler_returns_none(anvil_config, monkeypatch, capsys):
    """main() coerces a None return to 0 (anvil-config.py:1323)."""
    monkeypatch.setattr(anvil_config, "cmd_path", lambda args: None)
    assert anvil_config.main(["path"]) == 0
