"""anvil-config.py create-shortcuts: plugin-root resolution and file generation.

Everything runs against the tmp_path HOME installed by the isolate_env fixture,
so the developer's real ~/.claude-anvil and ~/.claude/commands are never touched.
The junction/symlink is created for real -- that is the platform-specific bit
worth exercising natively on each CI leg.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT


@pytest.fixture
def fake_plugin(tmp_path):
    """A minimal plugin root: just the two command sources create-shortcuts needs."""
    root = tmp_path / "installed" / "plugin"
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "anvil.md").write_text(
        "---\n"
        "allowed-tools: Bash, Read, mcp__plugin_claude-anvil_context7__query-docs, Task\n"
        "---\n"
        'run: python3 "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-ledger.py" init\n',
        encoding="utf-8")
    (root / "commands" / "anvil-setup.md").write_text(
        "---\n"
        "allowed-tools: Bash, mcp__plugin_claude-anvil_context7__query-docs\n"
        "---\n"
        'run: python3 "${CLAUDE_PLUGIN_ROOT}/scripts/anvil-config.py" status\n',
        encoding="utf-8")
    return root


@pytest.fixture
def home_paths():
    stable_link = Path(os.path.expanduser("~/.claude-anvil/plugin-root"))
    commands_dir = Path(os.path.expanduser("~/.claude/commands"))
    return stable_link, commands_dir


# --- _resolve_plugin_root -----------------------------------------------------

def test_resolve_prefers_a_valid_argument(anvil_config, fake_plugin, tmp_path):
    assert anvil_config._resolve_plugin_root(str(fake_plugin), tmp_path / "nolink") == \
        fake_plugin.resolve()


def test_resolve_returns_none_when_nothing_is_valid(anvil_config, tmp_path):
    assert anvil_config._resolve_plugin_root(str(tmp_path / "nope"), tmp_path / "nolink") is None
    assert anvil_config._resolve_plugin_root(None, tmp_path / "nolink") is None
    assert anvil_config._resolve_plugin_root("   ", tmp_path / "nolink") is None


def test_resolve_rejects_a_directory_missing_a_command_source(anvil_config, tmp_path):
    half = tmp_path / "half"
    (half / "commands").mkdir(parents=True)
    (half / "commands" / "anvil.md").write_text("x", encoding="utf-8")
    assert anvil_config._resolve_plugin_root(str(half), tmp_path / "nolink") is None


def test_resolve_falls_back_to_the_installed_link(anvil_config, fake_plugin, tmp_path, capsys):
    link = tmp_path / "plugin-root"
    _link(link, fake_plugin)
    assert anvil_config._resolve_plugin_root(None, link) == fake_plugin.resolve()
    assert capsys.readouterr().err == ""


def test_resolve_notes_when_a_bad_argument_falls_back_to_the_link(
        anvil_config, fake_plugin, tmp_path, capsys):
    link = tmp_path / "plugin-root"
    _link(link, fake_plugin)
    assert anvil_config._resolve_plugin_root("C:/bogus/path", link) == fake_plugin.resolve()
    err = capsys.readouterr().err
    assert "is not a valid plugin root" in err
    assert "falling back to installed junction" in err


def test_has_shortcut_sources(anvil_config, fake_plugin, tmp_path):
    assert anvil_config._has_shortcut_sources(fake_plugin) is True
    assert anvil_config._has_shortcut_sources(tmp_path) is False


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            pytest.skip(f"cannot create a junction here: {r.stderr.decode().strip()}")
    else:
        link.symlink_to(target)


# --- cmd_create_shortcuts -----------------------------------------------------

def test_create_shortcuts_writes_both_files_and_the_link(
        anvil_config, fake_plugin, home_paths, capsys):
    stable_link, commands_dir = home_paths
    assert anvil_config.main(["create-shortcuts", str(fake_plugin)]) == 0

    assert (commands_dir / "anvil.md").is_file()
    assert (commands_dir / "anvil-setup.md").is_file()
    assert Path(os.path.realpath(stable_link)) == fake_plugin.resolve()

    out = capsys.readouterr().out
    assert "Bare shortcuts created:" in out
    assert str(stable_link) in out


def test_create_shortcuts_substitutes_the_plugin_root_placeholder(
        anvil_config, fake_plugin, home_paths):
    stable_link, commands_dir = home_paths
    anvil_config.main(["create-shortcuts", str(fake_plugin)])
    content = (commands_dir / "anvil.md").read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}" not in content
    # Forward slashes: the paths land inside bash bodies in the .md files.
    assert stable_link.as_posix() in content
    assert "\\" not in content.split("scripts/anvil-ledger.py")[0].split("python3 ")[-1]


def test_create_shortcuts_keeps_the_context7_tools(
        anvil_config, fake_plugin, home_paths):
    """allowed-tools is copied verbatim: stripping the context7 tools only made
    /anvil prompt for permission on every library-docs lookup."""
    _, commands_dir = home_paths
    anvil_config.main(["create-shortcuts", str(fake_plugin)])
    for name in ("anvil.md", "anvil-setup.md"):
        assert "mcp__plugin_claude-anvil_context7__query-docs" in \
            (commands_dir / name).read_text(encoding="utf-8")


def test_create_shortcuts_is_repeatable(anvil_config, fake_plugin, home_paths):
    stable_link, commands_dir = home_paths
    assert anvil_config.main(["create-shortcuts", str(fake_plugin)]) == 0
    first = (commands_dir / "anvil.md").read_text(encoding="utf-8")
    assert anvil_config.main(["create-shortcuts", str(fake_plugin)]) == 0
    assert (commands_dir / "anvil.md").read_text(encoding="utf-8") == first
    assert Path(os.path.realpath(stable_link)) == fake_plugin.resolve()


def test_create_shortcuts_recreating_the_link_does_not_delete_the_target(
        anvil_config, fake_plugin, home_paths):
    """os.rmdir removes a junction without touching what it points at; the code
    deliberately never falls back to shutil.rmtree here."""
    anvil_config.main(["create-shortcuts", str(fake_plugin)])
    anvil_config.main(["create-shortcuts", str(fake_plugin)])
    assert (fake_plugin / "commands" / "anvil.md").is_file()


def test_create_shortcuts_falls_back_to_the_installed_link(
        anvil_config, fake_plugin, home_paths, capsys):
    stable_link, commands_dir = home_paths
    anvil_config.main(["create-shortcuts", str(fake_plugin)])   # establishes the link
    capsys.readouterr()
    # No argument at all: the junction realpath must be picked up.
    assert anvil_config.main(["create-shortcuts"]) == 0
    assert (commands_dir / "anvil.md").is_file()
    assert Path(os.path.realpath(stable_link)) == fake_plugin.resolve()


def test_create_shortcuts_without_a_resolvable_root_exits_one(
        anvil_config, home_paths, capsys):
    stable_link, commands_dir = home_paths
    assert anvil_config.main(["create-shortcuts", "C:/definitely/not/here"]) == 1
    assert "could not resolve plugin root" in capsys.readouterr().err
    assert not commands_dir.exists()
    assert not stable_link.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="mklink is Windows-only")
def test_create_shortcuts_reports_a_junction_failure(
        anvil_config, fake_plugin, monkeypatch, capsys):
    import subprocess

    def _fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, b"", b"Cannot create a file")

    monkeypatch.setattr(anvil_config.subprocess, "run", _fail)
    assert anvil_config.main(["create-shortcuts", str(fake_plugin)]) == 1
    assert "failed to create junction" in capsys.readouterr().err


def test_create_shortcuts_against_the_real_plugin_directory(anvil_config, home_paths):
    """End to end on the shipped command sources, still inside the tmp HOME."""
    stable_link, commands_dir = home_paths
    assert anvil_config.main(["create-shortcuts", str(REPO_ROOT / "plugin")]) == 0
    generated = (commands_dir / "anvil.md").read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}" not in generated
    assert f"{stable_link.as_posix()}/scripts/anvil-ledger.py" in generated
    allowed = [ln for ln in generated.splitlines() if ln.startswith("allowed-tools:")]
    assert allowed, "the shipped anvil.md has no allowed-tools frontmatter line"
    assert any("context7" in ln for ln in allowed)
    # The stamp lands after the frontmatter, so the frontmatter still opens the file.
    assert generated.startswith("---")
    shipped = anvil_config._plugin_version(REPO_ROOT / "plugin")
    assert anvil_config._shortcut_version(generated) == shipped
    head, _, _ = generated.partition(f"<!-- claude-anvil-shortcut version={shipped} -->")
    assert head.count("\n---") == 1


# --- version helpers ------------------------------------------------------------

def _set_version(root: Path, version: str) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "claude-anvil", "version": version}), encoding="utf-8")


def _make_plugin(base: Path, version: str) -> Path:
    root = base / version
    (root / "commands").mkdir(parents=True)
    for name in ("anvil.md", "anvil-setup.md"):
        (root / "commands" / name).write_text(
            f"---\nallowed-tools: Bash\n---\nrelease {version}\n", encoding="utf-8")
    _set_version(root, version)
    return root


def _register(*entries: tuple[str, Path, str]) -> Path:
    """Write ~/.claude/plugins/installed_plugins.json in the tmp HOME."""
    plugins: dict = {}
    for key, path, version in entries:
        plugins.setdefault(key, []).append(
            {"scope": "user", "installPath": str(path), "version": version})
    reg = Path(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8")
    return reg


@pytest.mark.parametrize("have,want,lags", [
    ("0.1.19", "0.1.20", True),
    ("0.1.9", "0.1.10", True),      # numeric, not lexicographic
    ("0.1.20", "0.1.20", False),
    ("0.1.21", "0.1.20", False),    # dev working tree ahead of the release
    ("0.2", "0.1.20", False),
    ("abc123", "0.1.20", False),    # unparseable never counts as lagging
    ("0.1.²", "0.1.20", False),  # isdigit() is true for "²", int() is not
    (None, "0.1.20", False),
    ("0.1.19", None, False),
])
def test_version_lags(anvil_config, have, want, lags):
    assert anvil_config._version_lags(have, want) is lags


def test_installed_plugin_picks_the_newest_claude_anvil(anvil_config, tmp_path):
    old = _make_plugin(tmp_path / "cache", "0.1.19")
    new = _make_plugin(tmp_path / "cache", "0.1.20")
    other = _make_plugin(tmp_path / "other", "9.9.9")
    _register(("claude-anvil@m", old, "0.1.19"), ("claude-anvil@m", new, "0.1.20"),
              ("not-claude-anvil@m", other, "9.9.9"))
    assert anvil_config._installed_plugin() == (new, "0.1.20")


def test_installed_plugin_skips_unusable_entries(anvil_config, tmp_path):
    gone = tmp_path / "cache" / "deleted"
    _register(("claude-anvil@m", gone, "0.1.20"))
    assert anvil_config._installed_plugin() is None


@pytest.mark.parametrize("payload", ["not json", "[]", '{"plugins": []}',
                                     '{"plugins": {"claude-anvil@m": "x"}}'])
def test_installed_plugin_tolerates_a_malformed_registry(anvil_config, payload):
    reg = _register()
    reg.write_text(payload, encoding="utf-8")
    assert anvil_config._installed_plugin() is None


def test_installed_plugin_honours_claude_config_dir(anvil_config, tmp_path, monkeypatch):
    new = _make_plugin(tmp_path / "cache", "0.1.20")
    cfg_dir = tmp_path / "relocated"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    (cfg_dir / "plugins").mkdir(parents=True)
    (cfg_dir / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"claude-anvil@m": [{"installPath": str(new), "version": "0.1.20"}]}}),
        encoding="utf-8")
    assert anvil_config._installed_plugin() == (new, "0.1.20")


# --- resolution against the installed plugin (issue #4) ------------------------

def test_resolve_replaces_a_stale_junction_with_the_installed_plugin(
        anvil_config, tmp_path, capsys):
    old = _make_plugin(tmp_path / "cache", "0.1.19")
    new = _make_plugin(tmp_path / "cache", "0.1.20")
    _register(("claude-anvil@m", new, "0.1.20"))
    link = tmp_path / "plugin-root"
    _link(link, old)
    assert anvil_config._resolve_plugin_root(None, link) == new
    err = capsys.readouterr().err
    assert "0.1.19" in err and "using the installed plugin" in err


def test_resolve_replaces_a_stale_argument_too(anvil_config, tmp_path, capsys):
    old = _make_plugin(tmp_path / "cache", "0.1.19")
    new = _make_plugin(tmp_path / "cache", "0.1.20")
    _register(("claude-anvil@m", new, "0.1.20"))
    assert anvil_config._resolve_plugin_root(str(old), tmp_path / "nolink") == new
    assert "using the installed plugin" in capsys.readouterr().err


def test_resolve_keeps_a_root_ahead_of_the_installed_plugin(anvil_config, tmp_path, capsys):
    """The dev setup: the junction points at a working tree that is ahead of
    (or equal to) the installed release, and must stay there."""
    installed = _make_plugin(tmp_path / "cache", "0.1.20")
    tree = _make_plugin(tmp_path / "tree", "0.1.21")
    _register(("claude-anvil@m", installed, "0.1.20"))
    link = tmp_path / "plugin-root"
    _link(link, tree)
    assert anvil_config._resolve_plugin_root(None, link) == tree.resolve()
    assert capsys.readouterr().err == ""


def test_resolve_keeps_a_root_with_no_readable_version(anvil_config, fake_plugin, tmp_path):
    installed = _make_plugin(tmp_path / "cache", "0.1.20")
    _register(("claude-anvil@m", installed, "0.1.20"))
    assert anvil_config._resolve_plugin_root(str(fake_plugin), tmp_path / "nolink") == \
        fake_plugin.resolve()


def test_resolve_falls_back_to_the_installed_plugin_without_a_link(anvil_config, tmp_path):
    installed = _make_plugin(tmp_path / "cache", "0.1.20")
    _register(("claude-anvil@m", installed, "0.1.20"))
    assert anvil_config._resolve_plugin_root(None, tmp_path / "nolink") == installed


def test_create_shortcuts_repoints_a_stale_link_at_the_installed_plugin(
        anvil_config, tmp_path, home_paths, capsys):
    """Issue #4's repro: update N -> N+1, run create-shortcuts with no root."""
    stable_link, commands_dir = home_paths
    old = _make_plugin(tmp_path / "cache", "0.1.19")
    new = _make_plugin(tmp_path / "cache", "0.1.20")
    assert anvil_config.main(["create-shortcuts", str(old)]) == 0
    _register(("claude-anvil@m", new, "0.1.20"))
    capsys.readouterr()

    assert anvil_config.main(["create-shortcuts"]) == 0
    assert Path(os.path.realpath(stable_link)) == new.resolve()
    shortcut = (commands_dir / "anvil.md").read_text(encoding="utf-8")
    assert "release 0.1.20" in shortcut
    assert anvil_config._shortcut_version(shortcut) == "0.1.20"
    assert "Version: 0.1.20" in capsys.readouterr().out


# --- stamp ------------------------------------------------------------------------

@pytest.mark.parametrize("content,expected", [
    ("---\na: b\n---\nbody\n", "---\na: b\n---\n<!-- claude-anvil-shortcut version=1.2.3 -->\nbody\n"),
    ("---\na: b\n---", "---\na: b\n---\n<!-- claude-anvil-shortcut version=1.2.3 -->\n"),
    ("no frontmatter\n", "<!-- claude-anvil-shortcut version=1.2.3 -->\nno frontmatter\n"),
])
def test_stamp_shortcut(anvil_config, content, expected):
    assert anvil_config._stamp_shortcut(content, "1.2.3") == expected
    assert anvil_config._shortcut_version(expected) == "1.2.3"


def test_stamp_reads_back_through_crlf(anvil_config):
    stamped = anvil_config._stamp_shortcut("---\r\na: b\r\n---\r\nbody\r\n", "1.2.3")
    assert anvil_config._shortcut_version(stamped) == "1.2.3"


def test_stamp_without_a_version_is_unknown(anvil_config):
    stamped = anvil_config._stamp_shortcut("x\n", None)
    assert anvil_config._shortcut_version(stamped) == "unknown"


# --- status warnings ------------------------------------------------------------

def _status_err(anvil_config, capsys) -> tuple[str, str]:
    capsys.readouterr()
    assert anvil_config.main(["status"]) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_status_is_quiet_without_a_registry(anvil_config, capsys):
    out, err = _status_err(anvil_config, capsys)
    assert out.strip() == "needs-setup"
    assert err == ""


def test_status_warns_when_these_scripts_lag_the_installed_plugin(
        anvil_config, tmp_path, capsys):
    newer = _make_plugin(tmp_path / "cache", "999.0.0")
    _register(("claude-anvil@m", newer, "999.0.0"))
    out, err = _status_err(anvil_config, capsys)
    assert out.strip() == "needs-setup"     # stdout contract unchanged
    assert "anvil-config: version-skew:" in err
    assert "999.0.0" in err
    # No root argument: resolution decides (a lagging junction -> installed plugin,
    # a current dev working tree -> kept).
    assert err.rstrip().endswith(
        f'"{(newer / "scripts" / "anvil-config.py").as_posix()}" create-shortcuts')


def test_status_quiet_when_scripts_match_the_installed_plugin(
        anvil_config, tmp_path, capsys):
    shipped = anvil_config._plugin_version(REPO_ROOT / "plugin")
    same = _make_plugin(tmp_path / "cache", shipped)
    _register(("claude-anvil@m", same, shipped))
    _, err = _status_err(anvil_config, capsys)
    assert err == ""


def test_status_flags_an_unstamped_shortcut(anvil_config, tmp_path, home_paths, capsys):
    _, commands_dir = home_paths
    shipped = anvil_config._plugin_version(REPO_ROOT / "plugin")
    _register(("claude-anvil@m", _make_plugin(tmp_path / "cache", shipped), shipped))
    commands_dir.mkdir(parents=True)
    (commands_dir / "anvil.md").write_text("---\nx: y\n---\nold copy\n", encoding="utf-8")
    _, err = _status_err(anvil_config, capsys)
    assert "anvil-config: stale-shortcut:" in err
    assert "predates shortcut version stamps" in err
    assert "version-skew" not in err


def test_status_flags_a_shortcut_from_an_older_release(
        anvil_config, tmp_path, home_paths, capsys):
    _, commands_dir = home_paths
    old = _make_plugin(tmp_path / "cache", "0.0.1")
    shipped = anvil_config._plugin_version(REPO_ROOT / "plugin")
    assert anvil_config.main(["create-shortcuts", str(old)]) == 0
    # The registry appears only after the shortcut was written from 0.0.1.
    _register(("claude-anvil@m", _make_plugin(tmp_path / "cache", shipped), shipped))
    _, err = _status_err(anvil_config, capsys)
    assert "stale-shortcut:" in err and "0.0.1" in err


def test_status_quiet_for_a_current_shortcut(anvil_config, tmp_path, home_paths, capsys):
    shipped = anvil_config._plugin_version(REPO_ROOT / "plugin")
    current = _make_plugin(tmp_path / "cache", shipped)
    _register(("claude-anvil@m", current, shipped))
    assert anvil_config.main(["create-shortcuts", str(current)]) == 0
    _, err = _status_err(anvil_config, capsys)
    assert err == ""
