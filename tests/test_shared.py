"""anvil_shared.db_path -- env override, ~ expansion, $VAR expansion."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_default_is_under_home(anvil_shared, monkeypatch, tmp_path):
    monkeypatch.delenv("ANVIL_DB_PATH", raising=False)
    expected = Path(os.path.expanduser("~/.claude-anvil/anvil.db"))
    assert anvil_shared.db_path() == expected
    # isolate_env repointed HOME/USERPROFILE, so this must not touch the real one.
    assert str(tmp_path) in str(expected)


def test_explicit_absolute_path_wins(anvil_shared, monkeypatch, tmp_path):
    target = tmp_path / "custom" / "ledger.db"
    monkeypatch.setenv("ANVIL_DB_PATH", str(target))
    assert anvil_shared.db_path() == target


def test_tilde_relative_path_is_expanded(anvil_shared, monkeypatch):
    monkeypatch.setenv("ANVIL_DB_PATH", "~/nested/x.db")
    result = anvil_shared.db_path()
    assert "~" not in str(result)
    assert result == Path(os.path.expanduser("~/nested/x.db"))


def test_posix_style_var_is_expanded(anvil_shared, monkeypatch, tmp_path):
    monkeypatch.setenv("ANVIL_TEST_DBDIR", str(tmp_path / "vardir"))
    monkeypatch.setenv("ANVIL_DB_PATH", "$ANVIL_TEST_DBDIR/x.db")
    assert anvil_shared.db_path() == tmp_path / "vardir" / "x.db"


@pytest.mark.skipif(os.name != "nt", reason="%VAR% syntax is Windows-only")
def test_windows_style_var_is_expanded(anvil_shared, monkeypatch, tmp_path):
    monkeypatch.setenv("ANVIL_TEST_DBDIR", str(tmp_path / "vardir"))
    monkeypatch.setenv("ANVIL_DB_PATH", "%ANVIL_TEST_DBDIR%/x.db")
    assert anvil_shared.db_path() == tmp_path / "vardir" / "x.db"


def test_expandvars_runs_before_expanduser(anvil_shared, monkeypatch):
    """anvil_shared.py:10 expands vars first, so a var holding '~' still expands."""
    monkeypatch.setenv("ANVIL_TEST_DBROOT", "~/fromvar")
    monkeypatch.setenv("ANVIL_DB_PATH", "$ANVIL_TEST_DBROOT/x.db")
    assert anvil_shared.db_path() == Path(os.path.expanduser("~/fromvar/x.db"))


def test_undefined_var_is_left_verbatim(anvil_shared, monkeypatch):
    monkeypatch.delenv("ANVIL_TEST_NOPE", raising=False)
    monkeypatch.setenv("ANVIL_DB_PATH", "$ANVIL_TEST_NOPE/x.db")
    # os.path.expandvars leaves unknown names untouched rather than blanking them.
    assert anvil_shared.db_path() == Path("$ANVIL_TEST_NOPE/x.db")


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_env_value_falls_back_to_the_default(anvil_shared, monkeypatch, raw):
    """A present-but-empty ANVIL_DB_PATH is treated as unset. Without this,
    Path("") is the CWD and the ledger silently becomes project-local."""
    monkeypatch.setenv("ANVIL_DB_PATH", raw)
    result = anvil_shared.db_path()
    assert result == Path(os.path.expanduser("~/.claude-anvil/anvil.db"))
    assert result != Path("")
