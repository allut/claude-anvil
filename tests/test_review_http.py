"""anvil-review.py HTTP layer: payload shapes, retry policy, wall-clock deadlines.

urlopen and time.sleep are always monkeypatched; nothing here touches a socket.
"""
from __future__ import annotations

import email.message
import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from helpers import fake_urlopen


def settings(monkeypatch, mod, values):
    """Replace _setting with a lookup over ``{(provider, key): value}``."""
    def _setting(provider, key, env_var, default=""):
        return values.get((provider, key), default)
    monkeypatch.setattr(mod, "_setting", _setting)


def capture_post(monkeypatch, mod, result):
    """Replace http_post_json with a recorder; returns the list of calls."""
    calls = []

    def _post(url, payload, headers, timeout=None, total_timeout=None):
        calls.append({"url": url, "payload": payload, "headers": headers})
        return result

    monkeypatch.setattr(mod, "http_post_json", _post)
    return calls


def http_error(code, body=b"", headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("http://x", code, "Reason", msg, io.BytesIO(body))


OPENAI_OK = {"choices": [{"message": {"content": '{"verdict": "pass", "findings": []}'}}]}
GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": '{"verdict": "pass"}'}]}}]}
OLLAMA_OK = {"message": {"content": '{"verdict": "concern", "findings": []}'}}


# --- provider payload shapes --------------------------------------------------

def test_openai_payload_and_headers(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {
        ("openai", "endpoint"): "https://openrouter.ai/api/v1/chat/completions",
        ("openai", "api_key"): "sk-test",
        ("openai", "model"): "gpt-4o",
        ("openai", "json_mode"): "on",
    })
    calls = capture_post(monkeypatch, anvil_review, OPENAI_OK)
    raw, model = anvil_review.call_openai("DIFF", False)

    assert raw == {"verdict": "pass", "findings": []}
    assert model == "gpt-4o"
    call = calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["payload"]["messages"][0] == {"role": "system",
                                              "content": anvil_review.SHARED_PROMPT}
    assert "DIFF" in call["payload"]["messages"][1]["content"]
    assert call["payload"]["response_format"] == {"type": "json_object"}
    assert call["payload"]["temperature"] == 0.2
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    # OpenRouter attribution headers; api.openai.com ignores them.
    assert call["headers"]["HTTP-Referer"] == "https://github.com/burkeholland/anvil"
    assert call["headers"]["X-Title"] == "claude-anvil"


def test_openai_json_mode_off_drops_response_format(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("openai", "api_key"): "k",
                                         ("openai", "json_mode"): " OFF "})
    calls = capture_post(monkeypatch, anvil_review, OPENAI_OK)
    anvil_review.call_openai("d", False)
    assert "response_format" not in calls[0]["payload"]


def test_openai_reasoning_model_omits_temperature(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("openai", "api_key"): "k",
                                         ("openai", "model"): "gpt-5-mini"})
    calls = capture_post(monkeypatch, anvil_review, OPENAI_OK)
    anvil_review.call_openai("d", False)
    assert "temperature" not in calls[0]["payload"]


def test_openai_without_a_key_raises_before_any_http(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("openai", "api_key"): ""})

    def _explode(*a, **k):
        raise AssertionError("http_post_json must not be reached")

    monkeypatch.setattr(anvil_review, "http_post_json", _explode)
    with pytest.raises(RuntimeError, match="ANVIL_OPENAI_API_KEY is not set"):
        anvil_review.call_openai("d", False)


def test_openai_unparseable_content_becomes_an_empty_dict(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("openai", "api_key"): "k"})
    capture_post(monkeypatch, anvil_review,
                 {"choices": [{"message": {"content": "I refuse."}}]})
    raw, _ = anvil_review.call_openai("d", False)
    assert raw == {}


def test_gemini_payload(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("gemini", "api_key"): "gk",
                                         ("gemini", "model"): "gemini-2.5-flash"})
    calls = capture_post(monkeypatch, anvil_review, GEMINI_OK)
    raw, model = anvil_review.call_gemini("DIFF", True)

    assert raw == {"verdict": "pass"}
    assert model == "gemini-2.5-flash"
    call = calls[0]
    assert call["payload"]["systemInstruction"] == {"parts": [{"text": anvil_review.SHARED_PROMPT}]}
    assert call["payload"]["generationConfig"]["responseMimeType"] == "application/json"
    assert call["headers"] == {"x-goog-api-key": "gk"}
    assert "truncated" in call["payload"]["contents"][0]["parts"][0]["text"]


def test_gemini_endpoint_is_derived_from_the_model(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("gemini", "api_key"): "gk",
                                         ("gemini", "model"): "gemini-3-pro"})
    calls = capture_post(monkeypatch, anvil_review, GEMINI_OK)
    anvil_review.call_gemini("d", False)
    assert calls[0]["url"].endswith("/models/gemini-3-pro:generateContent")


def test_gemini_without_a_key_raises(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("gemini", "api_key"): ""})
    with pytest.raises(RuntimeError, match="ANVIL_GEMINI_API_KEY is not set"):
        anvil_review.call_gemini("d", False)


def test_ollama_payload(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {("ollama", "host"): "http://box:11434/",
                                         ("ollama", "model"): "qwen2.5-coder:7b"})
    calls = capture_post(monkeypatch, anvil_review, OLLAMA_OK)
    raw, model = anvil_review.call_ollama("d", False)

    assert raw == {"verdict": "concern", "findings": []}
    assert model == "qwen2.5-coder:7b"
    assert calls[0]["url"] == "http://box:11434/api/chat"     # trailing slash stripped
    assert calls[0]["payload"]["format"] == "json"
    assert calls[0]["payload"]["stream"] is False
    assert calls[0]["headers"] == {}


def test_ollama_needs_no_api_key(anvil_review, monkeypatch):
    settings(monkeypatch, anvil_review, {})
    capture_post(monkeypatch, anvil_review, OLLAMA_OK)
    assert anvil_review.call_ollama("d", False)[0]["verdict"] == "concern"


# --- http_post_json: happy path + retries ------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


def post(anvil_review, **kwargs):
    return anvil_review.http_post_json("http://x", {"a": 1}, {"H": "v"}, **kwargs)


def test_http_post_json_sends_json_and_parses_the_reply(anvil_review, monkeypatch, no_sleep):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        fake_urlopen([(200, '{"ok": true}')], recorder=seen))
    assert post(anvil_review, timeout=5, total_timeout=30) == {"ok": True}
    req, timeout = seen[0]
    assert json.loads(req.data.decode()) == {"a": 1}
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("H") == "v"
    assert timeout == 5


def test_retryable_status_retries_then_succeeds(anvil_review, monkeypatch, no_sleep):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(
        [http_error(429), (200, '{"ok": 1}')], recorder=seen))
    assert post(anvil_review, timeout=5, total_timeout=60) == {"ok": 1}
    assert len(seen) == 2
    assert no_sleep == [2.0]


def test_non_retryable_status_raises_immediately(anvil_review, monkeypatch, no_sleep):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        fake_urlopen([http_error(400)], recorder=seen))
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(anvil_review, timeout=5, total_timeout=60)
    assert exc.value.code == 400
    assert len(seen) == 1
    assert no_sleep == []


@pytest.mark.parametrize("code", sorted({429, 500, 502, 503, 504}))
def test_every_retryable_status_is_retried(anvil_review, monkeypatch, no_sleep, code):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        fake_urlopen([http_error(code), (200, "{}")], recorder=seen))
    post(anvil_review, timeout=5, total_timeout=60)
    assert len(seen) == 2


def test_attempts_are_capped_at_three_with_exponential_backoff(
        anvil_review, monkeypatch, no_sleep):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(
        [http_error(503), http_error(503), http_error(503)], recorder=seen))
    with pytest.raises(urllib.error.HTTPError):
        post(anvil_review, timeout=5, total_timeout=600)
    assert len(seen) == anvil_review._MAX_HTTP_ATTEMPTS == 3
    assert no_sleep == [2.0, 4.0]


def test_numeric_retry_after_is_honoured(anvil_review, monkeypatch, no_sleep):
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(
        [http_error(429, headers={"Retry-After": " 7 "}), (200, "{}")]))
    post(anvil_review, timeout=5, total_timeout=600)
    assert no_sleep == [7.0]


def test_non_numeric_retry_after_falls_back_to_exponential_backoff(
        anvil_review, monkeypatch, no_sleep):
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(
        [http_error(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
         (200, "{}")]))
    post(anvil_review, timeout=5, total_timeout=600)
    assert no_sleep == [2.0]


def test_backoff_longer_than_the_remaining_budget_reraises_the_upstream_error(
        anvil_review, monkeypatch, no_sleep):
    """anvil-review.py:366-369 -- surfacing the real HTTP error beats sleeping
    into a timeout that says nothing about the cause."""
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen([http_error(429)]))
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(anvil_review, timeout=1, total_timeout=1.0)   # backoff of 2s > remaining
    assert exc.value.code == 429
    assert no_sleep == []


def test_exhausted_budget_raises_before_the_first_attempt(anvil_review, monkeypatch, no_sleep):
    def _never(*a, **k):
        raise AssertionError("no request should be attempted")

    monkeypatch.setattr(urllib.request, "urlopen", _never)
    with pytest.raises(TimeoutError, match=r"wall-clock budget exhausted after 0 attempt\(s\)"):
        post(anvil_review, timeout=5, total_timeout=-1)


def test_per_attempt_deadline_is_clamped_to_the_remaining_budget(
        anvil_review, monkeypatch, no_sleep):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        fake_urlopen([(200, "{}")], recorder=seen))
    post(anvil_review, timeout=999, total_timeout=3)
    assert seen[0][1] <= 3


def test_timeouts_default_to_the_env_vars(anvil_review, monkeypatch, no_sleep):
    monkeypatch.setenv("ANVIL_REVIEW_HTTP_TIMEOUT", "11")
    monkeypatch.setenv("ANVIL_REVIEW_TOTAL_TIMEOUT", "600")
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        fake_urlopen([(200, "{}")], recorder=seen))
    post(anvil_review)
    assert seen[0][1] == 11.0


def test_transport_errors_propagate_unwrapped(anvil_review, monkeypatch, no_sleep):
    monkeypatch.setattr(urllib.request, "urlopen",
                        fake_urlopen([urllib.error.URLError("dns is down")]))
    with pytest.raises(urllib.error.URLError):
        post(anvil_review, timeout=5, total_timeout=60)


# --- _call_with_deadline ------------------------------------------------------

def test_call_with_deadline_returns_the_value(anvil_review):
    assert anvil_review._call_with_deadline(lambda: 42, 5.0) == 42


def test_call_with_deadline_returns_none_for_a_none_result(anvil_review):
    assert anvil_review._call_with_deadline(lambda: None, 5.0) is None


def test_call_with_deadline_ferries_the_worker_exception_verbatim(anvil_review):
    sentinel = ValueError("boom")

    def _raise():
        raise sentinel

    with pytest.raises(ValueError) as exc:
        anvil_review._call_with_deadline(_raise, 5.0)
    assert exc.value is sentinel


def test_call_with_deadline_abandons_a_slow_worker(anvil_review):
    started = threading.Event()
    finished = threading.Event()

    def _slow():
        started.set()
        time.sleep(2.0)
        finished.set()

    before = threading.active_count()
    with pytest.raises(TimeoutError, match="wall-clock deadline"):
        anvil_review._call_with_deadline(_slow, 0.2)

    assert started.is_set()
    # Abandoned, not joined: the worker is still running when we regain control.
    assert not finished.is_set()
    assert threading.active_count() > before


def test_call_with_deadline_worker_is_a_daemon_thread(anvil_review, monkeypatch):
    """Daemon status is what lets an abandoned worker not block interpreter exit.
    ThreadPoolExecutor is deliberately not used -- its atexit join would hang."""
    created = []
    real_thread = threading.Thread

    def _spy(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        created.append(t)
        return t

    monkeypatch.setattr(anvil_review.threading, "Thread", _spy)
    anvil_review._call_with_deadline(lambda: 1, 5.0)
    assert created and created[0].daemon is True
