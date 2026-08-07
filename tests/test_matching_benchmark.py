"""Matching-quality benchmark: measure false-confirmation / false-rejection
rates of the title-search scoring path against a checked-in corpus.

Covers the full title-search path a real lookup uses to decide OK vs.
CLOSEST vs. NO MATCH: title_ratio() (similarity.py) -> ->
apply_year_mismatch_penalty() -> classification against the real
STRONG_MATCH_THRESHOLD and the CLI's default min_match (results.py /
engine.py). Deliberately excludes DOI/arXiv-ID-confirmed hits and liveness
hits -- those are identifier-proven, not title-matching decisions.

See tests/fixtures/README.md for corpus provenance and
docs/matching.md for how this fits into the broader scoring design.

Every case's `expected_classification` is the actual, current output of
the scorer, not an aspirational target -- this file is a checked-in
baseline. A change to similarity.py or results.py thresholds that shifts
any case's classification will fail here, forcing a conscious choice
(fix a real regression, or update the baseline with justification).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from ref_checker.results import STRONG_MATCH_THRESHOLD, apply_year_mismatch_penalty
from ref_checker.similarity import title_ratio

# Mirrors the CLI/engine default (cli/main.py --min-match, engine.py
# lookup_reference's min_match parameter). Intentionally duplicated here
# rather than imported, same as the other 2 literal occurrences already in
# the codebase -- there's no single shared constant for it today.
_MIN_MATCH = 0.80

_EXPECTED_CATEGORIES = {
    "exact",
    "abbreviated",
    "ocr_damage",
    "wrong_years",
    "preprint_vs_published",
    "same_author_similar",
    "generic_titles",
    "unresolvable",
}

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "matching_benchmark.json"


def _load_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def classify(score: float) -> str:
    """Reproduce the confirm/ambiguous/reject split real code applies.

    Mirrors results.py:recompute_best's use of STRONG_MATCH_THRESHOLD and
    min_match (see also sidecar.status_label, which turns this same split
    into the OK / CLOSEST / NO MATCH display labels).
    """
    if score >= STRONG_MATCH_THRESHOLD:
        return "confirm"
    if score >= _MIN_MATCH:
        return "ambiguous"
    return "reject"


def _score(case: dict) -> float:
    raw = title_ratio(case["ref_title"], case["cand_title"])
    return apply_year_mismatch_penalty(raw, case["ref_year"], case["cand_year"])


_CASES = _load_cases()


class TestFixtureIntegrity:
    def test_fixture_exists_and_nonempty(self):
        assert FIXTURE_PATH.is_file()
        assert _CASES

    def test_fixture_is_bare_json_array(self):
        raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, list)

    def test_no_duplicate_ids(self):
        ids = [c["id"] for c in _CASES]
        assert len(ids) == len(set(ids)), "duplicate case id in matching_benchmark.json"

    def test_all_backlog_categories_represented(self):
        seen = {c["category"] for c in _CASES}
        missing = _EXPECTED_CATEGORIES - seen
        assert not missing, f"categories with no cases: {missing}"
        unexpected = seen - _EXPECTED_CATEGORIES
        assert not unexpected, f"unrecognized categories: {unexpected}"

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
    def test_case_has_required_fields(self, case):
        for field in (
            "id", "category", "ref_title", "ref_year", "cand_title",
            "cand_year", "same_paper", "expected_classification", "note",
        ):
            assert field in case, f"{case.get('id')}: missing field {field!r}"
        assert case["expected_classification"] in ("confirm", "ambiguous", "reject")
        assert isinstance(case["same_paper"], bool)
        assert case["note"], f"{case['id']}: note must be non-empty"


class TestClassificationMatchesBaseline:
    """The main regression gate: pinpoints exactly which case flipped."""

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
    def test_case_classification_matches_baseline(self, case):
        score = _score(case)
        actual = classify(score)
        assert actual == case["expected_classification"], (
            f"{case['id']}: title_ratio/year-penalty score changed "
            f"(score={score:.3f}, was classified "
            f"{case['expected_classification']!r}, now {actual!r}). "
            "If this is an intentional similarity.py/results.py change, "
            "update this case's expected_classification (and, if the "
            "false-confirm/false-reject counts below no longer match, "
            "TestConfusionMatrixSummary's checked-in baseline too) with "
            "justification in the commit message."
        )


# Checked-in baseline for the aggregate false-confirmation / false-rejection
# counts, computed from the current fixture + current similarity.py /
# results.py behavior. See TestClassificationMatchesBaseline's docstring for
# what to do when this needs to change.
_EXPECTED_CONFUSION = {
    "true_confirm": 14,
    "false_confirm": 0,
    "true_reject": 12,
    "false_reject": 6,
    "ambiguous_same_paper": 4,
    "ambiguous_different_paper": 5,
}


def _confusion_bucket(case: dict) -> str:
    same = case["same_paper"]
    cls = case["expected_classification"]
    if cls == "confirm":
        return "true_confirm" if same else "false_confirm"
    if cls == "reject":
        return "true_reject" if not same else "false_reject"
    return "ambiguous_same_paper" if same else "ambiguous_different_paper"


class TestConfusionMatrixSummary:
    """Aggregate false-confirmation/false-rejection/ambiguous counts.

    This is the literal ask from BACKLOG.md: "Measure false confirmations,
    false rejections, and ambiguous cases before changing title/year
    thresholds or adding author/venue scoring." The per-category breakdown
    is printed (not asserted) for human inspection; the overall counts are
    asserted against a checked-in baseline as the one number worth
    watching over time.
    """

    def test_overall_confusion_matches_baseline(self):
        overall = Counter(_confusion_bucket(c) for c in _CASES)
        actual = {k: overall.get(k, 0) for k in _EXPECTED_CONFUSION}
        assert actual == _EXPECTED_CONFUSION, (
            f"Aggregate false-confirm/false-reject/ambiguous counts "
            f"changed: expected {_EXPECTED_CONFUSION}, got {actual}. "
            "See TestClassificationMatchesBaseline's docstring."
        )

    def test_no_outright_false_confirmations_today(self):
        """Document the current state explicitly, not just implicitly via
        the confusion-matrix baseline: no different-paper pair in this
        corpus scores >= STRONG_MATCH_THRESHOLD today. The risky
        same-author/generic-title collisions all land in 'ambiguous'
        rather than a hard false 'confirm' -- see same_author_similar and
        generic_titles cases' notes for the known gaps that keep this from
        being a stronger guarantee.
        """
        false_confirms = [
            c["id"] for c in _CASES
            if not c["same_paper"] and c["expected_classification"] == "confirm"
        ]
        assert false_confirms == []

    def test_per_category_breakdown(self, capsys):
        per_category: dict[str, Counter] = {}
        for c in _CASES:
            per_category.setdefault(c["category"], Counter())[_confusion_bucket(c)] += 1
        print("\nPer-category confusion breakdown:")
        for category in sorted(per_category):
            print(f"  {category}: {dict(per_category[category])}")
