"""Tests for ref_checker.results.LookupResult.recompute_best().

Pure unit tests against LookupResult directly — no network, no
check_references/lookup_reference involved.
"""
from __future__ import annotations

import pytest

from ref_checker.extract import Reference
from ref_checker.model import EvidenceLevel
from ref_checker.results import LookupResult


def _ref(index=1, title="A Paper", year=2020, doi=None, arxiv_id=None,
         venue=None, url=None, github_url=None, raw=None):
    return Reference(
        index=index,
        raw=raw if raw is not None else f"ref-{index}",
        title=title,
        year=year,
        doi=doi,
        arxiv_id=arxiv_id,
        venue=venue,
        url=url,
        github_url=github_url,
    )


def _summary(title="A Paper", year=2020, doi="10.1/x"):
    return {
        "source": "test",
        "title": title,
        "authors": ["A. Author"],
        "year": year,
        "venue": "Venue",
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else None,
        "external_id": doi,
    }


class TestRecomputeBest:
    def test_id_hit_wins_over_title_hit(self):
        ref = _ref(title="A Paper", year=2020)
        r = LookupResult()
        r.record_source("crossref", "hit_title", queried_by="title",
                        score=0.95, summary=_summary(title="A Paper", year=2020))
        r.record_source("openalex", "hit_id", queried_by="doi",
                        score=1.0, summary=_summary(title="A Paper", doi="10.1/x"))
        r.recompute_best(ref, 0.80)
        assert r.id_confirmed
        assert r.best_source == "openalex"

    def test_year_mismatch_note(self):
        ref = _ref(title="A Paper", year=2020)
        r = LookupResult()
        r.record_source("openalex", "hit_id", queried_by="doi",
                        score=1.0, summary=_summary(title="A Paper", year=2021))
        r.recompute_best(ref, 0.80)
        assert r.id_confirmed
        assert r.year_mismatch_note == "ref year=2020, match year=2021"
        assert any("year mismatch" in n for n in r.id_notes)

    def test_title_hit_year_penalty_applied(self):
        ref = _ref(title="A Paper", year=2020)
        r = LookupResult()
        r.record_source("crossref", "hit_title", queried_by="title",
                        score=0.90, summary=_summary(title="A Paper", year=2019))
        r.recompute_best(ref, 0.80)
        # 0.90 - 0.10 penalty = 0.80
        assert r.display_score == pytest.approx(0.80)
        assert r.year_mismatch_note is not None

    def test_no_hits_leaves_fields_none(self):
        ref = _ref()
        r = LookupResult()
        r.record_source("openalex", "not_found", queried_by="title")
        r.record_source("crossref", "error", queried_by="doi")
        r.recompute_best(ref, 0.80)
        assert r.best_source is None
        assert r.display_score is None

    def test_all_not_found_is_not_found(self):
        """A genuine conclusive negative from every source is NOT_FOUND."""
        ref = _ref()
        r = LookupResult()
        r.record_source("openalex", "not_found", queried_by="title")
        r.record_source("crossref", "not_found", queried_by="title")
        r.recompute_best(ref, 0.80)
        assert r.evidence == EvidenceLevel.NOT_FOUND

    def test_all_skipped_is_incomplete_not_not_found(self):
        """An interrupted run (every source skipped) must not be reported
        as a confident NOT_FOUND — the checks were never actually made.
        """
        ref = _ref()
        r = LookupResult()
        r.record_source("openalex", "skipped", queried_by=None, note="aborted by user")
        r.record_source("crossref", "skipped", queried_by=None, note="aborted by user")
        r.recompute_best(ref, 0.80)
        assert r.evidence == EvidenceLevel.INCOMPLETE

    def test_all_disabled_is_incomplete_not_not_found(self):
        """Sources tripped by the circuit breaker were never conclusively
        checked either — must not read as NOT_FOUND.
        """
        ref = _ref()
        r = LookupResult()
        r.record_source("openalex", "disabled", queried_by=None,
                        note="session circuit breaker")
        r.record_source("crossref", "disabled", queried_by=None,
                        note="session circuit breaker")
        r.recompute_best(ref, 0.80)
        assert r.evidence == EvidenceLevel.INCOMPLETE

    def test_mixed_not_found_and_skipped_is_incomplete(self):
        """Even one skipped/disabled source among otherwise-conclusive
        not_found results means the aggregate is not a genuine negative.
        """
        ref = _ref()
        r = LookupResult()
        r.record_source("openalex", "not_found", queried_by="title")
        r.record_source("crossref", "skipped", queried_by=None,
                        note="aborted by user")
        r.recompute_best(ref, 0.80)
        assert r.evidence == EvidenceLevel.INCOMPLETE

    def test_skipped_does_not_affect_exhausted_sources_list(self):
        """exhausted_sources keeps its existing error/rate_limited-only
        meaning and display text — skipped/disabled sources must not be
        added to it, only to the separate INCOMPLETE evidence signal.
        """
        ref = _ref()
        r = LookupResult()
        r.record_source("openalex", "skipped", queried_by=None,
                        note="aborted by user")
        r.record_source("crossref", "error", queried_by="doi")
        r.recompute_best(ref, 0.80)
        assert r.exhausted_sources == ["crossref"]
        assert r.evidence == EvidenceLevel.INCOMPLETE
