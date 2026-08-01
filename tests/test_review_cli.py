"""anvil-review.py main(): exit codes, the stub taxonomy, and the ledger record.

`_setting` and `_provider_enabled` are always monkeypatched -- letting them run
would spawn a real anvil-config.py subprocess.
"""
from __future__ import annotations

import email.message
import io
import json
import urllib.error

import pytest

RECORD_KEYS = {"provider", "model", "task_id", "truncated", "error",
               "verdict", "summary", "findings", "advisory", "passed"}


@pytest.fixture
def review(anvil_review, monkeypatch, tmp_path):
    """anvil-review with the config subprocess stubbed out and a diff on disk."""
    monkeypatch.setattr(anvil_review, "_provider_enabled", lambda provider: True)
    monkeypatch.setattr(anvil_review, "_setting",
                        lambda provider, key, env_var, default="": {"model": "cfg-model"}
                        .get(key, default))
    diff = tmp_path / "d.patch"
    diff.write_text("diff --git a/x b/x\n+one line\n", encoding="utf-8")

    class _Bound:
        mod = anvil_review
        diff_file = diff
        out_file = tmp_path / "verdict.json"

        @staticmethod
        def run(provider="openai", task_id="t", diff_file=None, out=None):
            return anvil_review.main([
                "--provider", provider, "--task-id", task_id,
                "--diff-file", str(diff_file if diff_file is not None else diff),
                "--out", str(out if out is not None else _Bound.out_file),
            ])

        @staticmethod
        def handler(fn):
            monkeypatch.setitem(anvil_review.PROVIDERS, "openai", fn)

        @staticmethod
        def raises(exc):
            def _fn(diff, truncated):
                raise exc
            monkeypatch.setitem(anvil_review.PROVIDERS, "openai", _fn)

        @staticmethod
        def record():
            return json.loads(_Bound.out_file.read_text(encoding="utf-8"))

    return _Bound


def http_error(code, body=b"", reason="Reason"):
    return urllib.error.HTTPError("http://x", code, reason,
                                  email.message.Message(), io.BytesIO(body))


# --- happy path ---------------------------------------------------------------

def test_successful_review_writes_the_ten_key_record(review, capsys):
    review.handler(lambda diff, truncated: (
        {"verdict": "concern", "summary": "two nits",
         "findings": [{"severity": "low"}, {"severity": "medium"}]}, "gpt-4o"))
    assert review.run() == 0

    record = review.record()
    assert set(record) == RECORD_KEYS
    assert record["provider"] == "openai"
    assert record["model"] == "gpt-4o"
    assert record["task_id"] == "t"
    assert record["truncated"] is False
    assert record["error"] is None
    assert record["verdict"] == "concern"
    assert record["passed"] == 1
    assert len(record["findings"]) == 2
    assert record["advisory"] == []

    assert capsys.readouterr().out.strip() == (
        f"reviewer=openai verdict=concern findings=2 advisory=0 "
        f"model=gpt-4o out={review.out_file}")


def test_fail_verdict_records_passed_zero(review):
    review.handler(lambda d, t: (
        {"verdict": "fail", "summary": "broken",
         "findings": [{"severity": "high", "what": "boom"}]}, "m"))
    assert review.run() == 0
    record = review.record()
    assert record["verdict"] == "fail"
    assert record["passed"] == 0
    assert record["error"] is None
    assert record["advisory"] == []


def test_an_unsupported_fail_is_downgraded_before_reaching_the_ledger(review, capsys):
    """Regression guard: a `fail` whose findings are all non-high used to write a
    passed=0 row asserting the code is broken. It is now `concern`/passed=1 with
    an advisory, and the note is echoed to stderr so the loop cannot miss it."""
    review.handler(lambda d, t: (
        {"verdict": "fail", "summary": "the diff resolves several bugs",
         "findings": [{"severity": "medium", "what": "x"}]}, "m"))
    assert review.run() == 0
    record = review.record()
    assert record["verdict"] == "concern"
    assert record["passed"] == 1
    assert len(record["advisory"]) == 1
    out, err = capsys.readouterr()
    assert "advisory=1" in out
    assert "downgraded fail->concern" in err


def test_findings_outside_the_diff_are_flagged_but_still_recorded(review):
    review.handler(lambda d, t: (
        {"verdict": "fail", "summary": "s",
         "findings": [{"severity": "high", "file": "totally/unrelated.py:4"}]}, "m"))
    assert review.run() == 0
    record = review.record()
    assert record["verdict"] == "fail"          # a high finding still supports `fail`
    assert record["passed"] == 0
    assert any("does not touch" in a for a in record["advisory"])


def test_handler_receives_the_diff_text(review):
    seen = {}

    def _fn(diff, truncated):
        seen["diff"] = diff
        seen["truncated"] = truncated
        return {"verdict": "pass"}, "m"

    review.handler(_fn)
    review.run()
    assert "+one line" in seen["diff"]
    assert seen["truncated"] is False


def test_out_path_defaults_to_the_system_tempdir(review):
    review.handler(lambda d, t: ({"verdict": "pass"}, "m"))
    assert review.mod.main(["--provider", "openai", "--task-id", "my task",
                            "--diff-file", str(review.diff_file)]) == 0
    expected = review.mod.default_out_path("openai", "my task")
    assert expected.exists()
    assert json.loads(expected.read_text(encoding="utf-8"))["verdict"] == "pass"


def test_out_parent_directories_are_created(review, tmp_path):
    review.handler(lambda d, t: ({"verdict": "pass"}, "m"))
    nested = tmp_path / "a" / "b" / "v.json"
    assert review.run(out=nested) == 0
    assert nested.exists()


# --- missing diff -------------------------------------------------------------

def test_missing_diff_file_exits_two_and_writes_nothing(review, capsys, tmp_path):
    assert review.run(diff_file=tmp_path / "gone.patch") == 2
    assert not review.out_file.exists()
    assert "diff file not found" in capsys.readouterr().err


# --- disabled provider --------------------------------------------------------

def test_disabled_provider_writes_a_passing_stub_and_exits_zero(
        anvil_review, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(anvil_review, "_provider_enabled", lambda provider: False)
    monkeypatch.setattr(anvil_review, "_setting",
                        lambda provider, key, env_var, default="": "cfg-model")
    out = tmp_path / "v.json"
    assert anvil_review.main(["--provider", "gemini", "--task-id", "t",
                              "--diff-file", str(tmp_path / "absent.patch"),
                              "--out", str(out)]) == 0

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["verdict"] == "pass"
    assert record["passed"] == 1            # the intentional disabled-skip stub
    assert "disabled in config" in record["error"]
    assert record["findings"] == []
    assert set(record) == RECORD_KEYS
    assert record["advisory"] == []
    assert "model=disabled" in capsys.readouterr().out


def test_disabled_provider_is_checked_before_the_diff_file(
        anvil_review, monkeypatch, tmp_path):
    """The stub is produced even with no diff on disk, so a disabled reviewer
    never turns into an exit-2 that stalls the /anvil loop."""
    monkeypatch.setattr(anvil_review, "_provider_enabled", lambda provider: False)
    monkeypatch.setattr(anvil_review, "_setting",
                        lambda provider, key, env_var, default="": "")
    out = tmp_path / "v.json"
    assert anvil_review.main(["--provider", "ollama", "--task-id", "t",
                              "--diff-file", "/definitely/not/here.patch",
                              "--out", str(out)]) == 0
    assert out.exists()


# --- error taxonomy -----------------------------------------------------------

@pytest.mark.parametrize("code,fragment", [
    (401, "authentication failed (HTTP 401)"),
    (403, "authentication failed (HTTP 403)"),
    (400, "bad request (HTTP 400) — check model name"),
])
def test_auth_and_bad_request_errors(review, code, fragment):
    review.raises(http_error(code, b"upstream said no"))
    assert review.run() == 0
    record = review.record()
    assert fragment in record["error"]
    assert "upstream said no" in record["error"]
    assert record["passed"] == 0
    assert record["verdict"] == "concern"


def test_other_http_errors_use_code_and_reason(review):
    review.raises(http_error(418, b"teapot", reason="I'm a teapot"))
    review.run()
    assert review.record()["error"].startswith("openai HTTP 418 I'm a teapot: teapot")


def test_http_error_body_is_clipped_to_300_characters(review):
    review.raises(http_error(500, b"x" * 1000))
    review.run()
    assert review.record()["error"].count("x") == 300


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("dns down"),
    TimeoutError("request exceeded 180s wall-clock deadline"),
    ConnectionError("reset by peer"),
])
def test_transport_errors_become_unreachable(review, exc):
    review.raises(exc)
    review.run()
    record = review.record()
    assert record["error"].startswith("openai unreachable:")
    assert record["passed"] == 0


def test_runtime_error_is_surfaced_verbatim(review):
    review.raises(RuntimeError("ANVIL_OPENAI_API_KEY is not set"))
    review.run()
    assert review.record()["error"] == "ANVIL_OPENAI_API_KEY is not set"


def test_unexpected_exceptions_hit_the_catch_all(review):
    review.raises(KeyError("choices"))
    review.run()
    record = review.record()
    assert record["error"] == "openai error: KeyError('choices')"
    assert record["passed"] == 0


def test_stub_verdicts_carry_no_advisory(review):
    """The guard is skipped when `error` is set. Stubs synthesize `concern` with no
    findings, so running it would emit an empty-findings note on every outage."""
    review.raises(RuntimeError("nope"))
    review.run()
    record = review.record()
    assert record["verdict"] == "concern" and record["findings"] == []
    assert record["advisory"] == []


def test_the_model_falls_back_to_the_configured_one_when_the_call_fails(review):
    review.raises(RuntimeError("nope"))
    review.run()
    assert review.record()["model"] == "cfg-model"


# --- "no parseable verdict" regression guard ----------------------------------

@pytest.mark.parametrize("raw", [{}, [], [{"verdict": "pass"}], "text", None, 0])
def test_a_non_dict_or_empty_reply_is_recorded_as_passed_zero(review, raw):
    """Commit 7251309's invariant: an HTTP call that succeeds but yields no
    verdict object must not look like a clean review."""
    review.handler(lambda d, t: (raw, "m"))
    assert review.run() == 0
    record = review.record()
    assert record["error"] == "openai returned no parseable JSON verdict"
    assert record["passed"] == 0
    assert record["verdict"] == "concern"
    assert record["summary"] == "openai returned no parseable JSON verdict"


def test_a_dict_without_a_verdict_key_still_counts_as_a_reply(review):
    review.handler(lambda d, t: ({"summary": "looks fine"}, "m"))
    review.run()
    record = review.record()
    assert record["error"] is None
    assert record["verdict"] == "concern"     # normalize_verdict's default
    assert record["passed"] == 1


# --- truncation ---------------------------------------------------------------

def test_truncated_diff_is_flagged_in_the_record(review, tmp_path):
    big = tmp_path / "big.patch"
    big.write_text("z" * (review.mod.DIFF_HARD_LIMIT + 10), encoding="utf-8")
    review.handler(lambda d, t: ({"verdict": "pass"}, "m"))
    review.run(diff_file=big)
    assert review.record()["truncated"] is True


# --- argparse -----------------------------------------------------------------

def test_provider_choices_match_the_providers_table(anvil_review):
    assert set(anvil_review.PROVIDERS) == {"openai", "gemini", "ollama"}


def test_claude_is_an_ingest_provider_not_a_callable_one(anvil_review):
    assert anvil_review._EXECUTING_PROVIDERS == {"claude"}
    assert "claude" not in anvil_review.PROVIDERS


@pytest.mark.parametrize("argv", [
    ["--provider", "nope", "--task-id", "t", "--diff-file", "d"],
    ["--provider", "claude", "--task-id", "t"],   # ingest still needs --diff-file
    ["--task-id", "t", "--diff-file", "d"],
    ["--provider", "openai", "--diff-file", "d"],
    ["--provider", "openai", "--task-id", "t"],
])
def test_invalid_argv_exits(anvil_review, argv):
    with pytest.raises(SystemExit):
        anvil_review.main(argv)


# --- ingest mode (--provider claude) ------------------------------------------
#
# The Claude reviewer is spawned by the /anvil loop as a Task subagent, so its
# verdict reaches the ledger through this path instead of through a handler.
# Before it existed, that verdict was the one nothing guarded.

def _ingest(anvil_review, tmp_path, payload, *, diff="diff --git a/x.py b/x.py\n",
            task_id="t"):
    """Write a subagent verdict + diff to disk, run ingest, return (rc, record)."""
    out = tmp_path / "verdict.json"
    out.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                   encoding="utf-8")
    argv = ["--provider", "claude", "--task-id", task_id, "--out", str(out)]
    if diff is None:
        argv += ["--diff-file", str(tmp_path / "absent.patch")]
    else:
        diff_path = tmp_path / "d.patch"
        diff_path.write_text(diff, encoding="utf-8")
        argv += ["--diff-file", str(diff_path)]
    rc = anvil_review.main(argv)
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        record = None       # ingest refused; the file was left untouched
    return rc, record


def test_ingest_rewrites_the_file_with_the_ledger_record(anvil_review, tmp_path, capsys):
    rc, rec = _ingest(anvil_review, tmp_path, {
        "verdict": "pass", "summary": "clean", "checks_run": ["python -m pytest"],
        "findings": [],
    })
    assert rc == 0
    assert RECORD_KEYS | {"checks_run", "unverified"} <= set(rec)
    assert rec["provider"] == "claude"
    assert rec["verdict"] == "pass"
    assert rec["passed"] == 1
    assert rec["unverified"] is False
    assert rec["advisory"] == []
    assert "reviewer=claude verdict=pass" in capsys.readouterr().out


def test_ingest_downgrades_a_pass_that_names_no_checks(anvil_review, tmp_path, capsys):
    rc, rec = _ingest(anvil_review, tmp_path, {
        "verdict": "pass", "summary": "looks fine", "findings": [],
    })
    assert rc == 0
    assert rec["verdict"] == "concern"
    assert rec["unverified"] is True
    assert rec["passed"] == 1          # guards never manufacture a FAIL row
    assert any("unverified" in n for n in rec["advisory"])
    assert "unverified=1" in capsys.readouterr().out


def test_ingest_applies_the_coherence_guard_too(anvil_review, tmp_path):
    """A `fail` with no high-severity finding is downgraded, exactly as for HTTP providers."""
    _, rec = _ingest(anvil_review, tmp_path, {
        "verdict": "fail", "summary": "nits", "checks_run": ["pytest"],
        "findings": [{"severity": "low", "file": "x.py", "what": "w"}],
    })
    assert rec["verdict"] == "concern"
    assert any("fail->concern" in n for n in rec["advisory"])


def test_ingest_leaves_a_supported_fail_as_passed_zero(anvil_review, tmp_path):
    _, rec = _ingest(anvil_review, tmp_path, {
        "verdict": "fail", "summary": "real bug", "checks_run": ["pytest"],
        "findings": [{"severity": "high", "file": "x.py", "what": "w"}],
    })
    assert rec["verdict"] == "fail"
    assert rec["passed"] == 0


def test_ingest_tolerates_a_missing_diff_and_says_so(anvil_review, tmp_path):
    rc, rec = _ingest(anvil_review, tmp_path, {
        "verdict": "pass", "summary": "s", "checks_run": ["pytest"], "findings": [],
    }, diff=None)
    assert rc == 0
    assert rec["verdict"] == "pass"
    assert any("diff file was unavailable" in n for n in rec["advisory"])


def test_ingest_of_unparseable_json_is_an_error_stub(anvil_review, tmp_path):
    _, rec = _ingest(anvil_review, tmp_path, "I could not produce JSON, sorry.")
    assert rec["error"]
    assert rec["passed"] == 0
    assert rec["unverified"] is False   # no verdict to call unverified


def test_ingest_extracts_a_fenced_verdict(anvil_review, tmp_path):
    _, rec = _ingest(
        anvil_review, tmp_path,
        '```json\n{"verdict":"pass","summary":"s","checks_run":["pytest"],"findings":[]}\n```')
    assert rec["error"] is None
    assert rec["verdict"] == "pass"


@pytest.mark.parametrize("payload", ["", "   \n  "])
def test_ingest_with_no_verdict_to_read_exits_two(anvil_review, tmp_path, payload, capsys):
    rc, _ = _ingest(anvil_review, tmp_path, payload)
    assert rc == 2
    assert "no verdict file to ingest" in capsys.readouterr().err


def test_ingest_of_an_absent_file_exits_two(anvil_review, tmp_path):
    (tmp_path / "d.patch").write_text("diff", encoding="utf-8")
    assert anvil_review.main([
        "--provider", "claude", "--task-id", "t",
        "--diff-file", str(tmp_path / "d.patch"),
        "--out", str(tmp_path / "nope.json"),
    ]) == 2


def test_ingest_never_consults_provider_enabled(anvil_review, tmp_path, monkeypatch):
    """A reviewer switched off in config must not leave an unguarded file behind."""
    def _boom(provider):
        raise AssertionError("_provider_enabled must not run in ingest mode")
    monkeypatch.setattr(anvil_review, "_provider_enabled", _boom)
    rc, rec = _ingest(anvil_review, tmp_path, {
        "verdict": "pass", "summary": "s", "checks_run": ["pytest"], "findings": [],
    })
    assert rc == 0 and rec["verdict"] == "pass"


def test_checks_run_is_absent_from_http_provider_records(review):
    """Providers with no shell cannot answer checks_run; demanding it invites invention."""
    review.handler(lambda diff, truncated: ({"verdict": "pass", "summary": "s",
                                             "findings": []}, "m"))
    review.run()
    assert "checks_run" not in review.record()
    assert "unverified" not in review.record()


def test_ingest_survives_a_diff_path_that_is_a_directory(anvil_review, tmp_path):
    """An unreadable diff costs an advisory, never the verdict."""
    out = tmp_path / "verdict.json"
    out.write_text(json.dumps({"verdict": "pass", "summary": "s",
                               "checks_run": ["pytest"], "findings": []}), encoding="utf-8")
    bad_diff = tmp_path / "diffdir"
    bad_diff.mkdir()
    rc = anvil_review.main(["--provider", "claude", "--task-id", "t",
                            "--diff-file", str(bad_diff), "--out", str(out)])
    assert rc == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["verdict"] == "pass"
    assert any("diff file was unavailable" in n for n in rec["advisory"])
