"""anvil-config.py create-shortcuts: plugin-root resolution and file generation.

Everything runs against the tmp_path HOME installed by the isolate_env fixture,
so the developer's real ~/.claude-anvil and ~/.claude/commands are never touched.
The junction/symlink is created for real -- that is the platform-specific bit
worth exercising natively on each CI leg.
"""
from __future__ import annotations

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


def test_create_shortcuts_strips_context7_from_anvil_md_only(
        anvil_config, fake_plugin, home_paths):
    _, commands_dir = home_paths
    anvil_config.main(["create-shortcuts", str(fake_plugin)])
    assert "context7" not in (commands_dir / "anvil.md").read_text(encoding="utf-8")
    # anvil-setup.md is untouched (its allowed-tools line would otherwise be emptied).
    assert "context7" in (commands_dir / "anvil-setup.md").read_text(encoding="utf-8")


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
    # Only the allowed-tools frontmatter line is filtered; the prose that
    # documents the Context7 tools is left as-is.
    allowed = [ln for ln in generated.splitlines() if ln.startswith("allowed-tools:")]
    assert allowed, "the shipped anvil.md has no allowed-tools frontmatter line"
    assert all("context7" not in ln for ln in allowed)
