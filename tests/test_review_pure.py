"""anvil-review.py pure functions: JSON extraction, verdict normalization,
env parsing, path/diff helpers, model classification."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


# --- extract_json -------------------------------------------------------------

def test_extract_json_raw_object(anvil_review):
    assert anvil_review.extract_json('{"verdict": "pass"}') == {"verdict": "pass"}


def test_extract_json_tolerates_surrounding_whitespace(anvil_review):
    assert anvil_review.extract_json('\n\n  {"a": 1}  \n') == {"a": 1}


def test_extract_json_json_fence(anvil_review):
    text = '```json\n{"verdict": "concern", "findings": []}\n```'
    assert anvil_review.extract_json(text)["verdict"] == "concern"


def test_extract_json_bare_fence(anvil_review):
    assert anvil_review.extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_nested_braces(anvil_review):
    text = '{"findings": [{"severity": "high", "meta": {"deep": {"deeper": 1}}}]}'
    assert anvil_review.extract_json(text)["findings"][0]["meta"]["deep"]["deeper"] == 1


def test_extract_json_from_surrounding_prose(anvil_review):
    text = 'Sure, here is my review:\n{"verdict": "fail"}\nHope that helps!'
    assert anvil_review.extract_json(text) == {"verdict": "fail"}


@pytest.mark.parametrize("bad_escape,expected", [
    (r'{"what": "50\% off"}', "50% off"),
    (r'{"what": "my\_var"}', "my_var"),
])
def test_extract_json_repairs_invalid_escapes(anvil_review, bad_escape, expected):
    assert anvil_review.extract_json(bad_escape) == {"what": expected}


def test_fix_escapes_preserves_valid_escapes(anvil_review):
    text = r'{"a": "line\nbreak\ttab \"q\" é \/ \b\f\r"}'
    assert anvil_review._fix_escapes(text) == text


def test_extract_json_top_level_array_is_returned_verbatim(anvil_review):
    """Annotated `-> dict | None`, but json.loads succeeds first, so an array
    comes straight back. main() relies on this to flag it as "no verdict"."""
    assert anvil_review.extract_json('[{"verdict": "pass"}]') == [{"verdict": "pass"}]


@pytest.mark.parametrize("text", ["", None, "no json here at all", "{unclosed"])
def test_extract_json_returns_none_when_nothing_parses(anvil_review, text):
    assert anvil_review.extract_json(text) is None


def test_extract_json_brace_scanner_is_string_unaware(anvil_review):
    """Known weakness (anvil-review.py:189-206): the balanced-brace fallback
    counts braces inside string values, so a `}` in a string defeats it once the
    direct json.loads path is unavailable (here: because of the wrapping prose)."""
    text = 'Review: {"what": "closes with } here", "verdict": "pass"} done'
    assert anvil_review.extract_json(text) is None
    # ...whereas the very same object parses fine without the prose.
    assert anvil_review.extract_json(
        '{"what": "closes with } here", "verdict": "pass"}')["verdict"] == "pass"


def test_extract_json_skips_an_unparseable_first_object(anvil_review):
    text = 'noise {not json} then {"verdict": "pass"} end'
    assert anvil_review.extract_json(text) == {"verdict": "pass"}


# --- compute_passed -----------------------------------------------------------

@pytest.mark.parametrize("verdict,error,expected", [
    ("fail", None, 0),                       # genuine high-severity failure
    ("fail", "boom", 0),
    ("concern", "boom", 0),                  # stub/outage
    ("pass", None, 1),
    ("concern", None, 1),
    ("pass", "openai is disabled in config (enabled=false)", 1),  # disabled-skip stub
])
def test_compute_passed(anvil_review, verdict, error, expected):
    assert anvil_review.compute_passed(verdict, error) == expected


def test_compute_passed_treats_empty_error_string_as_no_error(anvil_review):
    assert anvil_review.compute_passed("concern", "") == 1


# --- normalize_verdict --------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "a string", 42, [], ["x"]])
def test_normalize_verdict_non_dict_becomes_concern(anvil_review, raw):
    assert anvil_review.normalize_verdict(raw, "fallback") == {
        "verdict": "concern", "summary": "fallback", "findings": []}


def test_normalize_verdict_lowercases(anvil_review):
    assert anvil_review.normalize_verdict({"verdict": "PASS"}, "f")["verdict"] == "pass"


def test_normalize_verdict_unknown_value_becomes_concern(anvil_review):
    assert anvil_review.normalize_verdict({"verdict": "catastrophe"}, "f")["verdict"] == "concern"


def test_normalize_verdict_non_list_findings_become_empty(anvil_review):
    assert anvil_review.normalize_verdict(
        {"verdict": "pass", "findings": {"a": 1}}, "f")["findings"] == []


def test_normalize_verdict_missing_summary_uses_fallback(anvil_review):
    assert anvil_review.normalize_verdict({"verdict": "pass"}, "fb")["summary"] == "fb"
    assert anvil_review.normalize_verdict(
        {"verdict": "pass", "summary": ""}, "fb")["summary"] == "fb"


def test_normalize_verdict_keeps_findings_and_summary(anvil_review):
    findings = [{"severity": "high", "file": "a.py:1"}]
    out = anvil_review.normalize_verdict(
        {"verdict": "fail", "summary": "bad", "findings": findings}, "fb")
    assert out == {"verdict": "fail", "summary": "bad", "findings": findings}


def test_normalize_verdict_output_has_exactly_three_keys(anvil_review):
    assert set(anvil_review.normalize_verdict({"verdict": "pass", "extra": 1}, "f")) == \
        {"verdict", "summary", "findings"}


# --- _env_float ---------------------------------------------------------------

def test_env_float_reads_a_valid_value(anvil_review, monkeypatch):
    monkeypatch.setenv("ANVIL_TEST_F", " 12.5 ")
    assert anvil_review._env_float("ANVIL_TEST_F", 1.0) == 12.5


def test_env_float_unset_is_silent(anvil_review, monkeypatch, capsys):
    monkeypatch.delenv("ANVIL_TEST_F", raising=False)
    assert anvil_review._env_float("ANVIL_TEST_F", 7.0) == 7.0
    assert capsys.readouterr().err == ""


def test_env_float_empty_is_silent(anvil_review, monkeypatch, capsys):
    monkeypatch.setenv("ANVIL_TEST_F", "   ")
    assert anvil_review._env_float("ANVIL_TEST_F", 7.0) == 7.0
    assert capsys.readouterr().err == ""


def test_env_float_junk_warns_and_defaults(anvil_review, monkeypatch, capsys):
    monkeypatch.setenv("ANVIL_TEST_F", "soon")
    assert anvil_review._env_float("ANVIL_TEST_F", 9.0) == 9.0
    err = capsys.readouterr().err
    assert "ANVIL_TEST_F" in err and "not a number" in err


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-3", "-0.0"])
def test_env_float_rejects_non_finite_and_non_positive(anvil_review, monkeypatch, capsys, raw):
    monkeypatch.setenv("ANVIL_TEST_F", raw)
    assert anvil_review._env_float("ANVIL_TEST_F", 4.0) == 4.0
    assert "finite number > 0" in capsys.readouterr().err


# --- default_out_path ---------------------------------------------------------

def test_default_out_path_sanitizes_task_id(anvil_review):
    p = anvil_review.default_out_path("gemini", "feat/add thing:v2")
    assert p.name == "anvil-review-gemini-feat_add_thing_v2.json"


def test_default_out_path_keeps_safe_characters(anvil_review):
    assert anvil_review.default_out_path("openai", "fix-login_crash.2").name == \
        "anvil-review-openai-fix-login_crash.2.json"


def test_default_out_path_lives_in_the_system_tempdir(anvil_review):
    p = anvil_review.default_out_path("ollama", "t")
    assert p.parent == Path(tempfile.gettempdir())


# --- read_diff ----------------------------------------------------------------

def test_read_diff_under_the_limit_is_untouched(anvil_review, tmp_path):
    f = tmp_path / "d.patch"
    f.write_text("diff --git a/x b/x\n", encoding="utf-8")
    text, truncated = anvil_review.read_diff(f)
    assert truncated is False
    assert text == "diff --git a/x b/x\n"


def test_read_diff_at_exactly_the_limit_is_not_truncated(anvil_review, tmp_path):
    f = tmp_path / "d.patch"
    f.write_text("x" * anvil_review.DIFF_HARD_LIMIT, encoding="utf-8")
    text, truncated = anvil_review.read_diff(f)
    assert truncated is False
    assert len(text) == anvil_review.DIFF_HARD_LIMIT


def test_read_diff_over_the_limit_is_truncated_with_a_marker(anvil_review, tmp_path):
    f = tmp_path / "d.patch"
    f.write_text("q" * (anvil_review.DIFF_HARD_LIMIT + 500), encoding="utf-8")
    text, truncated = anvil_review.read_diff(f)
    assert truncated is True
    assert text.startswith("q" * 100)
    assert text.endswith("\n\n[... diff truncated by anvil-review ...]\n")
    assert text.count("q") == anvil_review.DIFF_HARD_LIMIT


def test_read_diff_replaces_undecodable_bytes(anvil_review, tmp_path):
    f = tmp_path / "d.patch"
    f.write_bytes(b"ok \xff\xfe end")
    text, truncated = anvil_review.read_diff(f)
    assert truncated is False
    assert "ok " in text and " end" in text


# --- build_user_prompt --------------------------------------------------------

def test_build_user_prompt_flags_truncation(anvil_review):
    assert anvil_review.build_user_prompt("d", True).startswith("NOTE: this diff was truncated")
    assert anvil_review.build_user_prompt("d", False).startswith("Staged diff:")


def test_build_user_prompt_fences_the_diff(anvil_review):
    assert "```diff\nDIFF\n```" in anvil_review.build_user_prompt("DIFF", False)


# --- _openai_model_is_reasoning -----------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("gpt-5", True),
    ("GPT-5-mini", True),
    ("gpt-5.1-turbo", True),
    ("o1", True),
    ("o1-preview", True),
    ("O3-mini", True),
    ("gpt-4o", False),
    ("gpt-4o-mini", False),
    ("o4-mini", False),          # not covered by the gpt-5/o1/o3 prefixes
    ("llama-3.3-70b-versatile", False),
    ("", False),
])
def test_openai_model_is_reasoning(anvil_review, model, expected):
    assert anvil_review._openai_model_is_reasoning(model) is expected
