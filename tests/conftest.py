"""Shared pytest fixtures for the claude-anvil suite.

Four responsibilities, in order of load-bearingness:

1. Module loader for the hyphenated script filenames (``import`` cannot spell
   ``anvil-config``), via ``importlib.util.spec_from_file_location``.
2. ``autouse`` environment isolation, so no test can read or write the
   developer's real ``~/.claude-anvil`` / ``~/.claude``.
3. ``autouse`` reset of the two module-level caches that would otherwise leak
   between tests and silently break env-precedence assertions.
4. ``autouse`` network kill-switch, so an accidental real HTTP call fails loudly
   instead of hanging CI.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugin" / "scripts"
SCHEMA_SQL = REPO_ROOT / "plugin" / "sql" / "schema.sql"

# The scripts themselves do `sys.path.insert(0, SCRIPT_DIR)` so they can
# `from anvil_shared import db_path`. Doing it here too makes a plain
# `import anvil_shared` work no matter which module loads first, and keeps the
# entry stable instead of accumulating one per loaded script.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# --- module loader ------------------------------------------------------------

@contextlib.contextmanager
def _stdout_reconfigure_shim():
    """Tolerate ``anvil-ledger.py``'s module-scope ``sys.stdout.reconfigure(...)``.

    That call runs at import time (anvil-ledger.py:30). Under some pytest capture
    modes ``sys.stdout`` is replaced by an object without ``.reconfigure``, which
    would turn a plain import into an AttributeError.
    """
    stdout = sys.stdout
    if hasattr(stdout, "reconfigure"):
        yield
        return
    try:
        stdout.reconfigure = lambda *a, **k: None  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        # Cannot annotate the capture object -> swap in a throwaway real stream.
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        try:
            yield
        finally:
            sys.stdout = stdout
        return
    try:
        yield
    finally:
        with contextlib.suppress(AttributeError):
            del stdout.reconfigure  # type: ignore[attr-defined]


def load_script(stem: str):
    """Import ``plugin/scripts/<stem>.py`` under the module name ``<stem_>``."""
    path = SCRIPTS / f"{stem}.py"
    mod_name = stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    # Registering before exec_module is required so `from __future__ import
    # annotations` / dataclass-style deferred annotation resolution can find it.
    sys.modules[mod_name] = mod
    with _stdout_reconfigure_shim():
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def anvil_shared():
    import anvil_shared as mod  # the one underscore-named script
    return mod


@pytest.fixture(scope="session")
def anvil_config():
    return load_script("anvil-config")


@pytest.fixture(scope="session")
def anvil_ledger():
    return load_script("anvil-ledger")


@pytest.fixture(scope="session")
def anvil_review():
    return load_script("anvil-review")


@pytest.fixture(scope="session")
def anvil_gate():
    return load_script("anvil-gate-commit")


@pytest.fixture(scope="session")
def anvil_track():
    return load_script("anvil-track-edit")


# --- isolation ----------------------------------------------------------------

_HOME_VARS = ("HOME", "USERPROFILE")


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """Point every ambient path at ``tmp_path`` and drop all ANVIL_* overrides."""
    for name in list(os.environ):
        if name.startswith("ANVIL_"):
            monkeypatch.delenv(name, raising=False)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for var in _HOME_VARS:
        monkeypatch.setenv(var, str(home))
    # ntpath.expanduser prefers USERPROFILE but falls back to HOMEDRIVE+HOMEPATH.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)

    monkeypatch.setenv("ANVIL_DB_PATH", str(tmp_path / "anvil.db"))
    monkeypatch.setenv("ANVIL_CONFIG_PATH", str(tmp_path / "config.json"))

    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(tmpdir))
    # tempfile caches gettempdir()'s answer on first use; force a re-derive both
    # on the way in and on the way out.
    previous = tempfile.tempdir
    tempfile.tempdir = None
    try:
        yield
    finally:
        tempfile.tempdir = previous


@pytest.fixture(autouse=True)
def reset_module_caches(request):
    """Clear the two module-global caches that survive across tests."""
    yield
    for name, attr, reset in (
        ("anvil_config", "_KEYCHAIN_BACKEND_CACHE", "sentinel"),
        ("anvil_review", "_SETTING_CACHE", "clear"),
    ):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if reset == "clear":
            getattr(mod, attr).clear()
        else:
            setattr(mod, attr, mod._UNSET)


# --- network kill-switch ------------------------------------------------------

class _BlockedNetwork(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Any real socket use fails loudly. Opt out with ``@pytest.mark.network``."""
    if request.node.get_closest_marker("network"):
        yield
        return

    def _blocked(*args, **kwargs):
        raise _BlockedNetwork(
            "network access is disabled in the test suite; mock the HTTP layer "
            "or mark the test with @pytest.mark.network"
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield
