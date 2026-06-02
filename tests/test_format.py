"""Tests for output formatting functions."""
import pytest
from ref_checker.extract import Reference
from ref_checker.results import LookupResult
from ref_checker.format import format_result, _format_citation, _format_ref_header


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
