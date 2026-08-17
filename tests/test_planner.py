"""Tests for ref_checker.planner._plan_ref_work: smart-rerun source
selection given a reference's prior sidecar state.

Pure unit tests — no network, no stub_sources fixture needed.
"""
from __future__ import annotations

from ref_checker import planner as planner_mod
from ref_checker.results import LookupResult
from ref_checker.sources.registry import all_source_names


class TestPlanRefWork:
    def test_fresh_ref_queries_all_sources(self):
        targets = planner_mod._plan_ref_work(None, None, retry_closest=False, retry_errored=True)
        assert targets == set(all_source_names())

    def test_ok_ref_returns_none(self):
        r = LookupResult(id_confirmed=True, display_score=0.99, best_source="openalex")
        r.record_source("openalex", "hit_id", queried_by="doi", score=1.0, summary={})
        assert planner_mod._plan_ref_work(r, "OK", retry_closest=False, retry_errored=True) is None

    def test_closest_returns_none_by_default(self):
        r = LookupResult(display_score=0.85, best_source="crossref")
        r.record_source("crossref", "hit_title", queried_by="title", score=0.85, summary={})
        assert planner_mod._plan_ref_work(r, "CLOSEST", retry_closest=False, retry_errored=True) is None

    def test_closest_returns_untried_when_flagged(self):
        r = LookupResult(display_score=0.85)
        r.record_source("crossref", "hit_title", queried_by="title", score=0.85, summary={})
        r.record_source("openalex", "not_found", queried_by="title")
        targets = planner_mod._plan_ref_work(r, "CLOSEST", retry_closest=True, retry_errored=True)
        # openalex was tried (not_found) so not in targets; crossref has a result;
        # everything else missing → in targets
        assert "openalex" not in targets
        assert "crossref" not in targets
        assert "dblp" in targets

    def test_no_match_retries_untried_and_errored(self):
        r = LookupResult(display_score=0.30)
        r.record_source("openalex", "hit_title", queried_by="title", score=0.30, summary={})
        r.record_source("crossref", "error", queried_by="doi")
        r.record_source("dblp", "not_found", queried_by="title")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=True)
        assert "openalex" not in targets    # already got a title hit
        assert "crossref" in targets        # errored → retry
        assert "dblp" not in targets        # not_found is a real answer
        assert "semanticscholar" in targets # never tried
        assert "arxiv" in targets

    def test_errored_not_retried_when_flag_off(self):
        r = LookupResult(display_score=0.30)
        r.record_source("crossref", "error", queried_by="doi")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=False)
        assert "crossref" not in targets

    def test_disabled_always_retried(self):
        r = LookupResult(display_score=0.30)
        r.record_source("crossref", "disabled", note="session circuit breaker")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=False)
        assert "crossref" in targets

    def test_rate_limited_retried_like_error(self):
        r = LookupResult(display_score=0.30)
        r.record_source("crossref", "rate_limited", queried_by="doi")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=True)
        assert "crossref" in targets

    def test_rate_limited_not_retried_when_flag_off(self):
        r = LookupResult(display_score=0.30)
        r.record_source("crossref", "rate_limited", queried_by="doi")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=False)
        assert "crossref" not in targets

    def test_skipped_always_retried(self):
        r = LookupResult(display_score=0.30)
        r.record_source("crossref", "skipped", note="aborted by user")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=True)
        assert "crossref" in targets

    def test_skipped_retried_even_when_retry_errored_flag_off(self):
        """A skipped query was never actually attempted — it should be
        retried regardless of retry_errored, unlike error/rate_limited.
        """
        r = LookupResult(display_score=0.30)
        r.record_source("crossref", "skipped", note="aborted by user")
        targets = planner_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=False)
        assert "crossref" in targets
