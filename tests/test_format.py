"""Tests for output formatting functions."""
import pytest
from ref_checker.extract import Reference
from ref_checker.results import LookupResult
from ref_checker.format import (
    _format_citation,
    _format_ref_header,
    _osti_id_if_confident,
    format_result,
)


def _ref(**kwargs):
    defaults = dict(index=1, raw="Smith, A Paper, 2020", title="A Paper",
                    authors=["Alice Smith"], year=2020, venue="ICDE")
    defaults.update(kwargs)
    return Reference(**defaults)


def _summary(doi="10.1/x", title="A Paper", year=2020, url="https://doi.org/10.1/x"):
    return {"doi": doi, "title": title, "year": year, "url": url,
            "authors": ["Alice Smith"], "venue": "ICDE"}


class TestFormatCitation:
    def test_full(self):
        s = _format_citation(["Alice Smith", "Bob Jones"], "A Paper", 2020, "ICDE")
        assert "Smith et al." in s
        assert '"A Paper"' in s
        assert "2020" in s
        assert "(ICDE)" in s

    def test_single_author(self):
        s = _format_citation(["Alice Smith"], "A Paper", 2020, None)
        assert "Smith," in s
        assert "et al." not in s

    def test_no_authors(self):
        s = _format_citation([], "A Paper", 2020, "ICDE")
        assert '"A Paper"' in s

    def test_no_title(self):
        s = _format_citation(["Smith"], None, 2020, "ICDE")
        assert "Smith" in s
        assert "None" not in s

    def test_empty_all(self):
        assert _format_citation([], None, None, None) == ""


class TestFormatRefHeader:
    def test_with_citation(self):
        ref = _ref()
        h = _format_ref_header(ref)
        assert h.startswith("[1]")
        assert "Smith" in h

    def test_fallback_to_raw(self):
        ref = Reference(index=3, raw="raw citation text", title=None)
        h = _format_ref_header(ref)
        assert "[3] raw citation text" == h


class TestFormatResult:
    MIN_MATCH = 0.80

    def test_id_confirmed_ok(self):
        ref = _ref()
        result = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex", best_summary=_summary(),
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "OK" in out
        assert "(0.99)" in out
        assert "doi:10.1/x" in out
        assert "[source: openalex]" in out

    def test_liveness_ok_shows_dash(self):
        ref = _ref(title=None, github_url="https://github.com/foo/bar")
        result = LookupResult(
            is_liveness=True, display_score=None,
            best_source="github",
            best_summary={"url": "https://github.com/foo/bar", "doi": None, "title": None},
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "OK" in out
        assert "(----)" in out

    def test_high_title_sim_ok(self):
        ref = _ref()
        result = LookupResult(
            display_score=0.93, best_source="crossref",
            best_summary=_summary(),
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "OK" in out
        assert "(0.93)" in out
        assert "CLOSEST" not in out

    def test_closest(self):
        ref = _ref()
        result = LookupResult(
            display_score=0.85, best_source="crossref",
            best_summary={"title": "A Somewhat Similar Paper", "year": 2020,
                          "url": "https://doi.org/10.1/y", "doi": None,
                          "authors": ["Bob Jones"], "venue": "VLDB"},
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "CLOSEST" in out
        assert "(0.85)" in out
        assert "Closest candidate" in out

    def test_no_match(self):
        ref = _ref()
        result = LookupResult(
            display_score=0.30, best_source="openalex",
            best_summary={"title": "Totally Different", "year": 2019,
                          "url": "https://x.com", "doi": None,
                          "authors": [], "venue": None},
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "NO MATCH" in out
        assert "(0.30)" in out

    def test_no_match_no_candidate(self):
        ref = _ref()
        result = LookupResult(display_score=0.0)
        out = format_result(ref, result, self.MIN_MATCH)
        assert "NO MATCH" in out
        assert "Closest candidate" not in out

    def test_year_mismatch_note_on_id_hit(self):
        ref = _ref(year=2020)
        result = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex", best_summary=_summary(year=2019),
            id_notes=["year mismatch (ref year=2020, match year=2019)"],
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "year mismatch" in out

    def test_year_mismatch_note_on_title_search(self):
        ref = _ref(year=2020)
        result = LookupResult(
            display_score=0.92, best_source="crossref",
            best_summary=_summary(year=2019),
            year_mismatch_note="ref year=2020, match year=2019",
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "year mismatch" in out

    def test_doi_not_found_shown_when_no_id(self):
        ref = _ref(doi="10.1/missing")
        result = LookupResult(
            display_score=0.40, best_source="crossref",
            doi_attempted="10.1/missing", doi_found_in=[],
            best_summary=None,
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "DOI not found" in out

    def test_doi_not_found_suppressed_on_id_confirmed(self):
        ref = _ref(doi="10.1/x")
        result = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex", best_summary=_summary(),
            doi_attempted="10.1/x", doi_found_in=[],
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "DOI not found" not in out

    def test_exhausted_sources_note(self):
        ref = _ref()
        result = LookupResult(
            display_score=0.40, best_source="openalex",
            best_summary=None,
            exhausted_sources=["semanticscholar", "arxiv"],
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "retries exhausted" in out
        assert "arxiv" in out
        assert "semanticscholar" in out

    def test_url_liveness_note(self):
        ref = _ref(url="https://example.com", title=None)
        result = LookupResult(
            is_liveness=True, display_score=None,
            best_source="url",
            best_summary={"url": "https://example.com", "doi": None, "title": None},
            url_liveness_check=True,
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "URL liveness check only" in out

    def test_dead_url_shown(self):
        ref = _ref()
        result = LookupResult(
            display_score=0.40,
            dead_urls=[("https://github.com/x/y", "HTTP 404")],
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "URL check failed" in out
        assert "HTTP 404" in out

    def test_doi_title_note_on_low_sim(self):
        ref = _ref(title="Hard Constraint Guided Flow Matching")
        result = LookupResult(
            id_confirmed=True, display_score=0.47,
            best_source="arxiv",
            best_summary={"doi": "10.48550/arXiv.2412.01786",
                          "title": "Gradient-Free Generation for Hard-Constrained Systems",
                          "url": None},
            id_notes=['DOI title: "Gradient-Free Generation for Hard-Constrained Systems"'],
        )
        out = format_result(ref, result, self.MIN_MATCH)
        assert "DOI title:" in out


# --------------------------------------------------------------------------
# --with-osti-id: _osti_id_if_confident helper
# --------------------------------------------------------------------------


def _osti_entry(status, score=None, ext_id="1234567", cand_year=None):
    return {
        "status": status,
        "queried_by": ["doi"] if status == "hit_id" else ["title"],
        "score": score,
        "summary": {
            "source": "osti",
            "title": "A DOE Report",
            "external_id": ext_id,
            "year": cand_year,
        },
        "note": None,
    }


class TestOstiIdIfConfident:
    def test_hit_id_returns_id(self):
        ref = _ref(year=2020)
        result = LookupResult(per_source={"osti": _osti_entry("hit_id")})
        assert _osti_id_if_confident(ref, result) == "1234567"

    def test_hit_title_high_score_returns_id(self):
        ref = _ref(year=2020)
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_title", score=0.95, cand_year=2020),
        })
        assert _osti_id_if_confident(ref, result) == "1234567"

    def test_hit_title_below_threshold_returns_none(self):
        ref = _ref(year=2020)
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_title", score=0.85, cand_year=2020),
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_hit_title_way_below_returns_none(self):
        ref = _ref(year=2020)
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_title", score=0.70, cand_year=2020),
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_year_penalty_pushes_below_threshold(self):
        ref = _ref(year=2020)
        # raw 0.95 - 0.10 year penalty = 0.85 → below 0.90 threshold
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_title", score=0.95, cand_year=2021),
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_year_penalty_still_above_threshold(self):
        ref = _ref(year=2020)
        # raw 1.00 - 0.10 = 0.90, exactly at threshold → shown
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_title", score=1.00, cand_year=2021),
        })
        assert _osti_id_if_confident(ref, result) == "1234567"

    def test_not_found_returns_none(self):
        ref = _ref()
        result = LookupResult(per_source={
            "osti": {"status": "not_found", "queried_by": ["title"],
                     "score": None, "summary": None, "note": None},
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_error_returns_none(self):
        ref = _ref()
        result = LookupResult(per_source={
            "osti": {"status": "error", "queried_by": ["title"],
                     "score": None, "summary": None, "note": "retries exhausted"},
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_disabled_returns_none(self):
        ref = _ref()
        result = LookupResult(per_source={
            "osti": {"status": "disabled", "queried_by": [],
                     "score": None, "summary": None,
                     "note": "session circuit breaker"},
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_missing_osti_entry_returns_none(self):
        ref = _ref()
        result = LookupResult(per_source={})
        assert _osti_id_if_confident(ref, result) is None

    def test_missing_external_id_returns_none(self):
        ref = _ref()
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_id", ext_id=None),
        })
        assert _osti_id_if_confident(ref, result) is None

    def test_hit_title_no_score_returns_none(self):
        ref = _ref()
        result = LookupResult(per_source={
            "osti": _osti_entry("hit_title", score=None),
        })
        assert _osti_id_if_confident(ref, result) is None


# --------------------------------------------------------------------------
# --with-osti-id: format_result integration
# --------------------------------------------------------------------------


class TestFormatResultWithOstiId:
    MIN_MATCH = 0.80

    def _ok_result_osti_wins(self):
        r = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="osti",
            best_summary={"doi": "10.2172/1234567", "title": "A DOE Report",
                          "url": "https://osti.gov/biblio/1234567",
                          "external_id": "1234567", "year": 2020},
            per_source={"osti": _osti_entry("hit_id")},
        )
        return r

    def _ok_result_other_source_wins_osti_hit_id(self):
        # OpenAlex won; OSTI also had a DOI hit
        r = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex",
            best_summary={"doi": "10.2172/1234567", "title": "A DOE Report",
                          "url": "https://openalex.org/W1", "year": 2020},
            per_source={
                "openalex": {"status": "hit_id", "queried_by": ["doi"],
                             "score": 1.0, "summary": {"doi": "10.2172/1234567"},
                             "note": None},
                "osti": _osti_entry("hit_id"),
            },
        )
        return r

    def test_no_flag_no_osti_suffix(self):
        ref = _ref()
        out = format_result(ref, self._ok_result_osti_wins(), self.MIN_MATCH)
        assert "OSTI:" not in out

    def test_flag_ok_line_osti_wins(self):
        ref = _ref()
        out = format_result(ref, self._ok_result_osti_wins(), self.MIN_MATCH,
                            with_osti_id=True)
        assert "(OSTI: 1234567)" in out

    def test_flag_ok_line_other_source_wins(self):
        ref = _ref()
        out = format_result(ref, self._ok_result_other_source_wins_osti_hit_id(),
                            self.MIN_MATCH, with_osti_id=True)
        # Even though OpenAlex won, OSTI ID is still shown since OSTI hit confidently
        assert "(OSTI: 1234567)" in out
        assert "[source: openalex]" in out

    def test_flag_closest_line_shows_osti(self):
        ref = _ref()
        r = LookupResult(
            display_score=0.85,
            best_source="crossref",
            best_summary={"doi": None, "title": "Something Close",
                          "url": "https://x.com", "authors": [], "year": 2020,
                          "venue": None},
            per_source={
                "crossref": {"status": "hit_title", "queried_by": ["title"],
                             "score": 0.85,
                             "summary": {"title": "Something Close"},
                             "note": None},
                "osti": _osti_entry("hit_id"),
            },
        )
        out = format_result(ref, r, self.MIN_MATCH, with_osti_id=True)
        assert "CLOSEST" in out
        assert "(OSTI: 1234567)" in out

    def test_flag_no_match_line_shows_osti(self):
        ref = _ref()
        r = LookupResult(
            display_score=0.30,
            best_source="openalex",
            best_summary={"title": "Wrong Paper", "url": "https://x.com",
                          "authors": [], "year": 2020, "venue": None},
            per_source={
                "openalex": {"status": "hit_title", "queried_by": ["title"],
                             "score": 0.30,
                             "summary": {"title": "Wrong Paper"},
                             "note": None},
                "osti": _osti_entry("hit_id"),
            },
        )
        out = format_result(ref, r, self.MIN_MATCH, with_osti_id=True)
        assert "NO MATCH" in out
        assert "(OSTI: 1234567)" in out

    def test_flag_no_osti_entry_no_suffix(self):
        ref = _ref()
        r = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex",
            best_summary={"doi": "10.1/x", "title": "A Paper", "url": None},
            per_source={
                "openalex": {"status": "hit_id", "queried_by": ["doi"],
                             "score": 1.0, "summary": {"doi": "10.1/x"},
                             "note": None},
            },
        )
        out = format_result(ref, r, self.MIN_MATCH, with_osti_id=True)
        assert "OSTI:" not in out

    def test_flag_low_confidence_title_no_suffix(self):
        ref = _ref(year=2020)
        r = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex",
            best_summary={"doi": "10.1/x", "title": "A Paper", "url": None},
            per_source={
                "openalex": {"status": "hit_id", "queried_by": ["doi"],
                             "score": 1.0, "summary": {"doi": "10.1/x"},
                             "note": None},
                "osti": _osti_entry("hit_title", score=0.85, cand_year=2020),
            },
        )
        out = format_result(ref, r, self.MIN_MATCH, with_osti_id=True)
        assert "OSTI:" not in out
