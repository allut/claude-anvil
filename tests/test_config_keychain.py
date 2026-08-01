"""anvil-config.py keychain abstraction.

The six platform shims are monkeypatched everywhere except the one real DPAPI
round-trip, which is Windows-only. No `security` / `secret-tool` / `powershell`
process is ever spawned by the portable tests.
"""
from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.fixture
def shims(anvil_config, monkeypatch):
    """Record every platform shim call instead of performing it."""
    calls = []
    monkeypatch.setattr(anvil_config, "_macos_write",
                        lambda kid, v: calls.append(("macos_write", kid, v)))
    monkeypatch.setattr(anvil_config, "_macos_read",
                        lambda kid: calls.append(("macos_read", kid)) or "mac-secret")
    monkeypatch.setattr(anvil_config, "_macos_delete",
                        lambda kid: calls.append(("macos_delete", kid)))
    monkeypatch.setattr(anvil_config, "_linux_write",
                        lambda kid, v: calls.append(("linux_write", kid, v)))
    monkeypatch.setattr(anvil_config, "_linux_read",
                        lambda kid: calls.append(("linux_read", kid)) or "linux-secret")
    monkeypatch.setattr(anvil_config, "_linux_delete",
                        lambda kid: calls.append(("linux_delete", kid)))
    monkeypatch.setattr(anvil_config, "_dpapi_encrypt",
                        lambda v: calls.append(("dpapi_encrypt", v)) or "dpapi:BLOB")
    monkeypatch.setattr(anvil_config, "_dpapi_decrypt",
                        lambda v: calls.append(("dpapi_decrypt", v)) or "dpapi-secret")
    return calls


def use_backend(anvil_config, monkeypatch, backend):
    monkeypatch.setattr(anvil_config, "_keychain_backend", lambda: backend)


# --- _keychain_store ----------------------------------------------------------

def test_store_on_macos(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, "macos")
    assert anvil_config._keychain_store("openai", "sk-x") == ("keychain", "macOS Keychain")
    assert shims == [("macos_write", "anvil-openai-api-key", "sk-x")]


def test_store_on_linux(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, "linux-secret-tool")
    value, label = anvil_config._keychain_store("gemini", "gk")
    assert value == "keychain"
    assert "secret-tool" in label
    assert shims == [("linux_write", "anvil-gemini-api-key", "gk")]


def test_store_on_windows_returns_the_encrypted_blob(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, "windows-dpapi")
    value, label = anvil_config._keychain_store("openai", "sk-x")
    assert value == "dpapi:BLOB"
    assert label == "config.json (DPAPI-encrypted)"
    assert shims == [("dpapi_encrypt", "sk-x")]


def test_store_without_a_backend_falls_back_to_plaintext(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, None)
    assert anvil_config._keychain_store("openai", "sk-x") == ("sk-x", "config.json (plaintext)")
    assert shims == []


# --- _keychain_load -----------------------------------------------------------

def test_load_keychain_marker_on_macos(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, "macos")
    assert anvil_config._keychain_load("openai", "keychain") == "mac-secret"


def test_load_keychain_marker_on_linux(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, "linux-secret-tool")
    assert anvil_config._keychain_load("openai", "keychain") == "linux-secret"


def test_load_keychain_marker_with_no_backend_returns_none(anvil_config, monkeypatch, shims):
    """A "keychain" marker with no backend (e.g. config copied between machines)
    has nothing to read from -- including on Windows, where DPAPI stores the blob
    inline rather than behind the marker."""
    for backend in (None, "windows-dpapi"):
        use_backend(anvil_config, monkeypatch, backend)
        assert anvil_config._keychain_load("openai", "keychain") is None


def test_load_dpapi_blob(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, "windows-dpapi")
    assert anvil_config._keychain_load("openai", "dpapi:BLOB") == "dpapi-secret"
    assert shims == [("dpapi_decrypt", "dpapi:BLOB")]


def test_load_plaintext_value_is_returned_as_is(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, None)
    assert anvil_config._keychain_load("openai", "sk-plain") == "sk-plain"


def test_load_empty_value_is_none(anvil_config, monkeypatch, shims):
    use_backend(anvil_config, monkeypatch, None)
    assert anvil_config._keychain_load("openai", "") is None


# --- _keychain_remove ---------------------------------------------------------

@pytest.mark.parametrize("backend,expected", [
    ("macos", [("macos_delete", "anvil-openai-api-key")]),
    ("linux-secret-tool", [("linux_delete", "anvil-openai-api-key")]),
    ("windows-dpapi", []),      # the blob lives in config.json; nothing to purge
    (None, []),
])
def test_remove_dispatch(anvil_config, monkeypatch, shims, backend, expected):
    use_backend(anvil_config, monkeypatch, backend)
    anvil_config._keychain_remove("openai")
    assert shims == expected


# --- _keychain_backend --------------------------------------------------------

@pytest.fixture
def fake_which(anvil_config, monkeypatch):
    """Stub subprocess.run so `which security` / `which secret-tool` never runs."""
    def install(returncode):
        seen = []

        def _run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, b"", b"")

        monkeypatch.setattr(anvil_config.subprocess, "run", _run)
        return seen

    return install


def test_backend_on_windows_is_dpapi_without_probing(anvil_config, monkeypatch, fake_which):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = fake_which(0)
    assert anvil_config._keychain_backend() == "windows-dpapi"
    assert seen == [], "Windows must not run a capability probe"


@pytest.mark.parametrize("returncode,expected", [(0, "macos"), (1, None)])
def test_backend_on_macos_probes_for_security(anvil_config, monkeypatch, fake_which,
                                              returncode, expected):
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = fake_which(returncode)
    assert anvil_config._keychain_backend() == expected
    assert seen == [["which", "security"]]


@pytest.mark.parametrize("returncode,expected", [(0, "linux-secret-tool"), (1, None)])
def test_backend_elsewhere_probes_for_secret_tool(anvil_config, monkeypatch, fake_which,
                                                  returncode, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    seen = fake_which(returncode)
    assert anvil_config._keychain_backend() == expected
    assert seen == [["which", "secret-tool"]]


@pytest.mark.parametrize("exc", [FileNotFoundError, OSError,
                                 lambda *a, **k: subprocess.TimeoutExpired("which", 3)])
def test_backend_probe_failure_means_no_backend(anvil_config, monkeypatch, exc):
    monkeypatch.setattr(sys, "platform", "linux")

    def _run(cmd, **kwargs):
        raise exc() if isinstance(exc, type) else exc()

    monkeypatch.setattr(anvil_config.subprocess, "run", _run)
    assert anvil_config._keychain_backend() is None


def test_backend_result_is_cached(anvil_config, monkeypatch, fake_which):
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = fake_which(0)
    assert anvil_config._keychain_backend() == "macos"

    def _explode(*a, **k):
        raise AssertionError("the backend probe must only run once")

    monkeypatch.setattr(anvil_config.subprocess, "run", _explode)
    assert anvil_config._keychain_backend() == "macos"
    assert len(seen) == 1


def test_backend_cache_is_reset_between_tests(anvil_config):
    """Guards the conftest fixture that restores the _UNSET sentinel; without it
    the previous test's 'macos' answer would leak into this one."""
    assert anvil_config._KEYCHAIN_BACKEND_CACHE is anvil_config._UNSET


# --- real DPAPI (Windows only) ------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_roundtrip_through_real_powershell(anvil_config):
    secret = "sk-test-é-1234!@#"
    encrypted = anvil_config._dpapi_encrypt(secret)
    assert encrypted.startswith("dpapi:")
    assert secret not in encrypted
    assert anvil_config._dpapi_decrypt(encrypted) == secret


def test_dpapi_decrypt_rejects_a_value_without_the_prefix(anvil_config):
    assert anvil_config._dpapi_decrypt("plain-text") is None


def test_dpapi_decrypt_rejects_a_non_base64_payload(anvil_config, monkeypatch):
    """The base64 guard must reject before anything reaches PowerShell."""
    def _explode(*a, **k):
        raise AssertionError("PowerShell must not be invoked for a malformed blob")

    monkeypatch.setattr(anvil_config.subprocess, "run", _explode)
    assert anvil_config._dpapi_decrypt("dpapi:not base64!;DROP") is None
