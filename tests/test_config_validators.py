"""anvil-config.py credential probes. _http_get_json is always monkeypatched."""
from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

STATUS_VOCABULARY = {"ok", "unauthorized", "model-missing", "unreachable"}


def http_error(code, reason="Reason"):
    return urllib.error.HTTPError("http://x", code, reason,
                                  email.message.Message(), io.BytesIO(b""))


@pytest.fixture
def probe(anvil_config, monkeypatch):
    """Install a canned _http_get_json; returns the list of (url, headers) calls."""
    calls = []

    def install(result):
        def _get(url, headers=None, timeout=None):
            calls.append((url, headers, timeout))
            if isinstance(result, BaseException):
                raise result
            return result
        monkeypatch.setattr(anvil_config, "_http_get_json", _get)
        return calls

    return install


# --- validate_claude (offline) ------------------------------------------------

@pytest.mark.parametrize("model", ["sonnet", "haiku", "opus",
                                   "claude-sonnet-4-5", "claude-anything"])
def test_validate_claude_accepts_known_shapes(anvil_config, monkeypatch, model):
    monkeypatch.setenv("ANVIL_CLAUDE_MODEL", model)
    result = anvil_config.validate_claude()
    assert result["status"] == "ok"
    assert model in result["detail"]


@pytest.mark.parametrize("model", ["gpt-4o", "sonnet-4", "Sonnet", ""])
def test_validate_claude_rejects_unknown_models(anvil_config, monkeypatch, model):
    monkeypatch.setenv("ANVIL_CLAUDE_MODEL", model)
    # An empty env value falls through to the "sonnet" default, which is valid.
    expected = "ok" if model == "" else "model-missing"
    assert anvil_config.validate_claude()["status"] == expected


def test_validate_claude_defaults_to_sonnet(anvil_config):
    assert anvil_config.validate_claude()["status"] == "ok"


# --- validate_openai ----------------------------------------------------------

def test_validate_openai_without_a_key_short_circuits(anvil_config, monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("no HTTP call should happen without a key")

    monkeypatch.setattr(anvil_config, "_http_get_json", _explode)
    result = anvil_config.validate_openai()
    assert result["status"] == "unauthorized"
    assert "is empty" in result["detail"]


def test_validate_openai_derives_the_models_url(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    calls = probe((200, {"data": [{"id": "gpt-4o"}]}, "{}"))
    assert anvil_config.validate_openai()["status"] == "ok"
    url, headers, _ = calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert headers == {"Authorization": "Bearer sk-x"}


def test_validate_openai_derives_the_models_url_for_a_custom_endpoint(
        anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANVIL_OPENAI_ENDPOINT",
                       "https://openrouter.ai/api/v1/chat/completions?x=1")
    monkeypatch.setenv("ANVIL_OPENAI_MODEL", "openai/gpt-oss-120b:free")
    calls = probe((200, {"data": [{"id": "openai/gpt-oss-120b:free"}]}, "{}"))
    assert anvil_config.validate_openai()["status"] == "ok"
    assert calls[0][0] == "https://openrouter.ai/api/v1/models"


def test_validate_openai_reports_a_missing_model(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANVIL_OPENAI_MODEL", "gpt-9")
    probe((200, {"data": [{"id": "gpt-4o"}]}, "{}"))
    result = anvil_config.validate_openai()
    assert result["status"] == "model-missing"
    assert "gpt-9" in result["detail"]


def test_validate_openai_accepts_an_empty_model_list(anvil_config, monkeypatch, probe):
    """No ids listed -> cannot disprove the model, so the probe stays 'ok'."""
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    probe((200, {"data": []}, "{}"))
    assert anvil_config.validate_openai()["status"] == "ok"


def test_validate_openai_accepts_a_non_json_body(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    probe((200, None, "<html>hi</html>"))
    result = anvil_config.validate_openai()
    assert result["status"] == "ok"
    assert "non-JSON body" in result["detail"]


@pytest.mark.parametrize("code,expected", [(401, "unauthorized"), (403, "unauthorized"),
                                           (404, "model-missing"), (500, "unreachable")])
def test_validate_openai_maps_http_errors(anvil_config, monkeypatch, probe, code, expected):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    probe(http_error(code))
    assert anvil_config.validate_openai()["status"] == expected


@pytest.mark.parametrize("exc", [urllib.error.URLError("dns"), TimeoutError("slow"),
                                 ConnectionError("reset")])
def test_validate_openai_maps_transport_errors(anvil_config, monkeypatch, probe, exc):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "sk-x")
    probe(exc)
    assert anvil_config.validate_openai()["status"] == "unreachable"


# --- validate_gemini ----------------------------------------------------------

def test_validate_gemini_without_a_key(anvil_config):
    assert anvil_config.validate_gemini()["status"] == "unauthorized"


def test_validate_gemini_matches_a_model_by_substring(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_GEMINI_API_KEY", "gk")
    calls = probe((200, {"models": [{"name": "models/gemini-2.5-flash"}]}, "{}"))
    assert anvil_config.validate_gemini()["status"] == "ok"
    assert calls[0][0].startswith("https://generativelanguage.googleapis.com/v1beta/models?key=")


def test_validate_gemini_reports_a_missing_model(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_GEMINI_API_KEY", "gk")
    monkeypatch.setenv("ANVIL_GEMINI_MODEL", "gemini-9-ultra")
    probe((200, {"models": [{"name": "models/gemini-2.5-flash"}]}, "{}"))
    assert anvil_config.validate_gemini()["status"] == "model-missing"


def test_validate_gemini_maps_a_400_to_unauthorized(anvil_config, monkeypatch, probe):
    """Gemini answers 400 API_KEY_INVALID rather than 401."""
    monkeypatch.setenv("ANVIL_GEMINI_API_KEY", "gk")
    probe(http_error(400))
    assert anvil_config.validate_gemini()["status"] == "unauthorized"


def test_validate_gemini_maps_transport_errors(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_GEMINI_API_KEY", "gk")
    probe(urllib.error.URLError("offline"))
    assert anvil_config.validate_gemini()["status"] == "unreachable"


# --- validate_ollama ----------------------------------------------------------

@pytest.mark.parametrize("model,tags", [
    ("qwen2.5-coder:7b", ["qwen2.5-coder:7b"]),           # exact
    ("qwen2.5-coder", ["qwen2.5-coder:7b"]),              # startswith f"{model}:"
    ("coder", ["qwen2.5-coder:7b"]),                      # `model in t` substring
])
def test_validate_ollama_tag_matching(anvil_config, monkeypatch, probe, model, tags):
    monkeypatch.setenv("ANVIL_OLLAMA_MODEL", model)
    probe((200, {"models": [{"name": t} for t in tags]}, "{}"))
    assert anvil_config.validate_ollama()["status"] == "ok"


def test_validate_ollama_reports_an_unpulled_model(anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_OLLAMA_MODEL", "deepseek-coder-v2:16b")
    probe((200, {"models": [{"name": "llama3:latest"}]}, "{}"))
    result = anvil_config.validate_ollama()
    assert result["status"] == "model-missing"
    assert "llama3:latest" in result["detail"]


def test_validate_ollama_with_no_models_pulled(anvil_config, probe):
    probe((200, {"models": []}, "{}"))
    result = anvil_config.validate_ollama()
    assert result["status"] == "model-missing"
    assert "have: none" in result["detail"]


def test_validate_ollama_uses_the_tags_endpoint_and_a_short_timeout(
        anvil_config, monkeypatch, probe):
    monkeypatch.setenv("ANVIL_OLLAMA_HOST", "http://box:11434/")
    calls = probe((200, {"models": [{"name": "qwen2.5-coder:7b"}]}, "{}"))
    anvil_config.validate_ollama()
    url, _, timeout = calls[0]
    assert url == "http://box:11434/api/tags"
    assert timeout == 5.0


def test_validate_ollama_daemon_down(anvil_config, probe):
    probe(urllib.error.URLError("connection refused"))
    result = anvil_config.validate_ollama()
    assert result["status"] == "unreachable"
    assert "not reachable" in result["detail"]


# --- vocabulary ---------------------------------------------------------------

def test_validators_table_covers_every_provider(anvil_config):
    assert set(anvil_config.VALIDATORS) == set(anvil_config.PROVIDERS)


@pytest.mark.parametrize("name", ["openai", "gemini", "ollama", "claude"])
def test_every_validator_returns_a_status_from_the_shared_vocabulary(
        anvil_config, monkeypatch, probe, name):
    monkeypatch.setenv("ANVIL_OPENAI_API_KEY", "k")
    monkeypatch.setenv("ANVIL_GEMINI_API_KEY", "k")
    probe(http_error(404))
    result = anvil_config.VALIDATORS[name]()
    assert set(result) == {"status", "detail"}
    assert result["status"] in STATUS_VOCABULARY
