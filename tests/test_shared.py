"""anvil_shared -- db_path (env override, ~ / $VAR expansion) and parse_raw_z."""
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


# --- parse_raw_z ----------------------------------------------------------------

SHA_A = "a" * 40
SHA_B = "b" * 40
NULL = "0" * 40


def test_parse_raw_z_reads_modify_add_delete(anvil_shared):
    out = (f":100644 100644 {SHA_A} {SHA_B} M\0src/x.py\0"
           f":000000 100755 {NULL} {SHA_A} A\0bin/run\0"
           f":100644 000000 {SHA_B} {NULL} D\0old.txt\0").encode()
    assert anvil_shared.parse_raw_z(out) == [
        {"path": "src/x.py", "status": "M", "dst_mode": "100644", "dst_sha": SHA_B},
        {"path": "bin/run", "status": "A", "dst_mode": "100755", "dst_sha": SHA_A},
        {"path": "old.txt", "status": "D", "dst_mode": "000000", "dst_sha": NULL},
    ]


def test_parse_raw_z_keys_a_rename_by_its_destination(anvil_shared):
    """--no-renames should prevent this, but a record with two paths must not
    shift every later record by one field."""
    out = (f":100644 100644 {SHA_A} {SHA_A} R100\0a.txt\0b.txt\0"
           f":100644 100644 {SHA_A} {SHA_B} M\0c.txt\0").encode()
    parsed = anvil_shared.parse_raw_z(out)
    assert [(e["path"], e["status"]) for e in parsed] == [("b.txt", "R"), ("c.txt", "M")]


def test_parse_raw_z_keeps_unusual_paths_verbatim(anvil_shared):
    """-z output is unquoted: spaces, tabs and non-ASCII arrive as raw bytes."""
    name = "dir with space/tab\there/é.txt"
    out = f":100644 100644 {SHA_A} {SHA_B} M\0".encode() + name.encode("utf-8") + b"\0"
    assert anvil_shared.parse_raw_z(out)[0]["path"] == name


def test_parse_raw_z_on_empty_output(anvil_shared):
    assert anvil_shared.parse_raw_z(b"") == []


@pytest.mark.parametrize("out", [
    b":100644 100644 deadbeef M\0x\0",              # too few fields
    f":100644 100644 {SHA_A} {SHA_B} M".encode(),   # no path at all
])
def test_parse_raw_z_rejects_malformed_records(anvil_shared, out):
    with pytest.raises(ValueError):
        anvil_shared.parse_raw_z(out)
