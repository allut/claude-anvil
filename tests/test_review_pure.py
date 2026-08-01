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


def test_extract_json_brace_scanner_ignores_braces_inside_strings(anvil_review):
    """The balanced-brace fallback is string-aware. Reviewer findings routinely
    contain braces in their prose, which used to close the object early and lose
    the whole verdict once the direct json.loads path was unavailable."""
    text = 'Review: {"what": "closes with } here", "verdict": "pass"} done'
    assert anvil_review.extract_json(text)["verdict"] == "pass"
    # ...and the same object still parses without the wrapping prose.
    assert anvil_review.extract_json(
        '{"what": "closes with } here", "verdict": "pass"}')["verdict"] == "pass"


def test_extract_json_brace_scanner_handles_an_escaped_quote_in_a_string(anvil_review):
    text = r'Review: {"what": "he said \"} done\" loudly", "verdict": "concern"} end'
    assert anvil_review.extract_json(text)["verdict"] == "concern"


def test_extract_json_brace_scanner_handles_an_opening_brace_inside_a_string(anvil_review):
    text = 'Review: {"fix": "wrap it in ${VALUE}", "verdict": "fail"} end'
    assert anvil_review.extract_json(text)["verdict"] == "fail"


def test_extract_json_ignores_a_stray_closing_brace_before_the_object(anvil_review):
    assert anvil_review.extract_json('oops } stray {"verdict": "pass"}') == {"verdict": "pass"}


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


# --- diff_paths / _normalize_path ---------------------------------------------

def test_normalize_path_strips_git_prefix_and_line_number(anvil_review):
    assert anvil_review._normalize_path("b/plugin/scripts/x.py:42") == "plugin/scripts/x.py"
    assert anvil_review._normalize_path("a/x.py:42:7") == "x.py"
    assert anvil_review._normalize_path("./x.py") == "x.py"
    assert anvil_review._normalize_path("plugin\\scripts\\x.py") == "plugin/scripts/x.py"
    assert anvil_review._normalize_path(None) == ""


def test_diff_paths_collects_both_sides_and_drops_dev_null(anvil_review):
    diff = (
        "diff --git a/plugin/scripts/x.py b/plugin/scripts/x.py\n"
        "--- a/plugin/scripts/x.py\n+++ b/plugin/scripts/x.py\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
    )
    assert anvil_review.diff_paths(diff) == {"plugin/scripts/x.py", "gone.py"}


def test_diff_paths_on_an_empty_diff_is_empty(anvil_review):
    assert anvil_review.diff_paths("") == set()


def test_cites_a_diffed_file_matches_leniently(anvil_review):
    paths = {"plugin/scripts/anvil-review.py"}
    for cited in ("plugin/scripts/anvil-review.py", "anvil-review.py",
                  "repo/plugin/scripts/anvil-review.py", ""):
        assert anvil_review._cites_a_diffed_file(cited, paths) is True
    assert anvil_review._cites_a_diffed_file("tests/test_other.py", paths) is False


# --- apply_coherence_guard ----------------------------------------------------

def _v(verdict, findings=(), summary="s"):
    return {"verdict": verdict, "summary": summary, "findings": list(findings)}


def _f(severity, file="x.py"):
    return {"severity": severity, "file": file, "what": "w", "why": "y", "fix": "f"}


def test_guard_downgrades_fail_without_a_high_finding(anvil_review):
    """The reported bug: six non-high findings paired with `fail` produced a FAIL
    ledger row for code the reviewer's own summary praised."""
    out, advisory = anvil_review.apply_coherence_guard(
        _v("fail", [_f("medium"), _f("low")]))
    assert out["verdict"] == "concern"
    assert anvil_review.compute_passed(out["verdict"], None) == 1
    assert len(advisory) == 1 and "downgraded fail->concern" in advisory[0]


def test_guard_keeps_fail_when_a_high_finding_supports_it(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(_v("fail", [_f("high"), _f("low")]))
    assert out["verdict"] == "fail"
    assert advisory == []


def test_guard_severity_match_is_case_and_space_insensitive(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(
        _v("fail", [{"severity": " HIGH ", "file": "x.py"}]))
    assert out["verdict"] == "fail" and advisory == []


@pytest.mark.parametrize("label", ["critical", "blocker", "SEV-1", "hi"])
def test_guard_treats_an_unrecognized_severity_as_high(anvil_review, label):
    """A vocabulary drift upstream (prompt tweak, provider quirk) must not silently
    downgrade a genuine failure to passed=1 -- the outcome the guard exists to stop."""
    out, advisory = anvil_review.apply_coherence_guard(_v("fail", [_f(label)]))
    assert out["verdict"] == "fail"
    assert anvil_review.compute_passed(out["verdict"], None) == 0
    assert len(advisory) == 1
    assert "outside the high/medium/low schema" in advisory[0] and label.lower() in advisory[0]


def test_guard_upgrades_pass_carrying_an_unrecognized_severity(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(_v("pass", [_f("critical")]))
    assert out["verdict"] == "concern"
    assert any("upgraded pass->concern" in a for a in advisory)


def test_guard_does_not_treat_a_missing_severity_as_a_drifted_label(anvil_review):
    """Absent/empty severity is not a vocabulary drift; it must not prop up a `fail`."""
    out, advisory = anvil_review.apply_coherence_guard(
        _v("fail", [{"file": "x.py", "what": "w"}, {"severity": "  ", "file": "x.py"}]))
    assert out["verdict"] == "concern"
    assert not any("outside the high/medium/low schema" in a for a in advisory)


def test_guard_truncates_a_long_unrecognized_severity_list(anvil_review):
    findings = [_f(f"sev{i}") for i in range(anvil_review._MAX_ADVISORY_SEVERITIES + 2)]
    _, advisory = anvil_review.apply_coherence_guard(_v("fail", findings))
    note = next(a for a in advisory if "outside the high/medium/low schema" in a)
    assert "(+2 more)" in note


def test_guard_upgrades_pass_carrying_a_high_finding(anvil_review):
    """The silent inverse of the reported bug — a false clean verdict."""
    out, advisory = anvil_review.apply_coherence_guard(_v("pass", [_f("high")]))
    assert out["verdict"] == "concern"
    assert anvil_review.compute_passed(out["verdict"], None) == 1  # never manufactures a FAIL
    assert "upgraded pass->concern" in advisory[0]


def test_guard_only_annotates_pass_with_medium_findings(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(_v("pass", [_f("medium")]))
    assert out["verdict"] == "pass"
    assert len(advisory) == 1 and "medium-severity" in advisory[0]


def test_guard_leaves_a_clean_pass_alone(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(_v("pass"))
    assert out == _v("pass") and advisory == []


def test_guard_ignores_low_findings_under_pass(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(_v("pass", [_f("low")]))
    assert out["verdict"] == "pass" and advisory == []


def test_guard_flags_a_negative_verdict_with_no_findings(anvil_review):
    _, advisory = anvil_review.apply_coherence_guard(_v("concern"))
    assert any("empty findings array" in a for a in advisory)


def test_guard_reports_both_the_downgrade_and_the_empty_findings(anvil_review):
    out, advisory = anvil_review.apply_coherence_guard(_v("fail"))
    assert out["verdict"] == "concern"
    assert len(advisory) == 2


def test_guard_flags_findings_about_files_outside_the_diff(anvil_review):
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+line\n"
    _, advisory = anvil_review.apply_coherence_guard(
        _v("concern", [_f("low", "x.py:3"), _f("low", "elsewhere/other.py:9")]), diff)
    note = next(a for a in advisory if "does not touch" in a)
    assert "elsewhere/other.py:9" in note and "x.py:3" not in note


def test_guard_skips_the_off_diff_check_on_a_truncated_diff(anvil_review):
    """`diff` is only the first DIFF_HARD_LIMIT chars when truncated, so files whose
    header falls past the cutoff would be wrongly reported as untouched."""
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+line\n"
    _, advisory = anvil_review.apply_coherence_guard(
        _v("concern", [_f("low", "tail/only.py:9")]), diff, truncated=True)
    assert not any("does not touch" in a for a in advisory)
    # ...and it still fires when the reviewer saw the whole diff.
    _, advisory = anvil_review.apply_coherence_guard(
        _v("concern", [_f("low", "tail/only.py:9")]), diff, truncated=False)
    assert any("does not touch" in a for a in advisory)


def test_guard_does_not_flag_off_diff_files_when_no_paths_parsed(anvil_review):
    _, advisory = anvil_review.apply_coherence_guard(
        _v("concern", [_f("low", "anything.py")]), "not a diff at all")
    assert not any("does not touch" in a for a in advisory)


def test_guard_truncates_a_long_off_diff_file_list(anvil_review):
    diff = "diff --git a/x.py b/x.py\n"
    findings = [_f("low", f"far/f{i}.py") for i in range(anvil_review._MAX_ADVISORY_FILES + 3)]
    _, advisory = anvil_review.apply_coherence_guard(_v("concern", findings), diff)
    note = next(a for a in advisory if "does not touch" in a)
    assert "(+3 more)" in note


def test_guard_does_not_mutate_the_input_verdict(anvil_review):
    original = _v("fail", [_f("low")])
    anvil_review.apply_coherence_guard(original)
    assert original["verdict"] == "fail"


def test_guard_tolerates_malformed_findings(anvil_review):
    """Findings come straight from a model; normalize_verdict only checks the list type."""
    out, advisory = anvil_review.apply_coherence_guard(
        {"verdict": "fail", "summary": "s", "findings": ["a string", None, 42]})
    assert out["verdict"] == "concern"
    assert isinstance(advisory, list)


# --- SHARED_PROMPT ------------------------------------------------------------

def test_shared_prompt_forbids_reporting_bugs_the_diff_fixes(anvil_review):
    """The primary defence against a reviewer transcribing the diff's own changelog."""
    p = anvil_review.SHARED_PROMPT
    assert "AFTER this diff is applied" in p
    assert "Do NOT list bugs the diff fixes." in p
    assert "verdict must agree with your summary" in p


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
