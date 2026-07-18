"""Tests for the check-lifecycle: circuit breaker, re-run planning, and shutdown.

All tests monkeypatch the source modules — no network calls are made.
"""
from __future__ import annotations

import json
import os
import signal
import threading

import pytest

from ref_checker import check as check_mod
from ref_checker import sidecar as sidecar_mod
from ref_checker.extract import Reference
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


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    """Zero out per-source delays so tests don't sleep."""
    monkeypatch.setattr(
        check_mod, "_DEFAULT_DELAYS",
        {k: 0.0 for k in check_mod._DEFAULT_DELAYS},
    )
    monkeypatch.setattr(check_mod, "_RETRY_BACKOFF", (0.0, 0.0, 0.0))


@pytest.fixture
def stub_sources(monkeypatch):
    """Replace every source function with a no-op returning (None, None).

    Tests can override individual functions to inject behavior.
    """
    from ref_checker.sources import (
        arxiv, crossref, dblp, github, openalex, osti, semanticscholar,
        url as url_source,
    )
    for src in (openalex, crossref, osti, dblp, semanticscholar, arxiv):
        for name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
            if hasattr(src, name):
                monkeypatch.setattr(src, name, lambda *a, **kw: (None, None))
    monkeypatch.setattr(github, "check_url", lambda *a, **kw: (None, None, []))
    monkeypatch.setattr(url_source, "check_url", lambda *a, **kw: (None, None, []))
    return {
        "openalex": openalex,
        "crossref": crossref,
        "osti": osti,
        "dblp": dblp,
        "semanticscholar": semanticscholar,
        "arxiv": arxiv,
        "github": github,
        "url": url_source,
    }


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class TestSourceHealth:
    def test_error_increments_counter(self):
        h = check_mod.SourceHealth(threshold=3)
        h.record("openalex", "error")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")
        h.record("openalex", "error")
        assert h.is_disabled("openalex")

    def test_hit_resets_counter(self):
        h = check_mod.SourceHealth(threshold=3)
        h.record("openalex", "error")
        h.record("openalex", "error")
        h.record("openalex", "hit_id")
        h.record("openalex", "error")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")

    def test_not_found_resets_counter(self):
        h = check_mod.SourceHealth(threshold=3)
        h.record("openalex", "error")
        h.record("openalex", "error")
        h.record("openalex", "not_found")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")

    def test_all_scholarly_disabled_detects_full_outage(self):
        h = check_mod.SourceHealth(threshold=1)
        for name in check_mod._SCHOLARLY_SOURCE_NAMES:
            h.record(name, "error")
        assert h.all_scholarly_disabled()


# --------------------------------------------------------------------------
# per_source population
# --------------------------------------------------------------------------


class TestPerSource:
    def test_records_hit_id_and_not_found(self, stub_sources):
        stub_sources["openalex"].get_by_doi = lambda doi: (_summary(doi=doi), 1.0)
        stub_sources["crossref"].get_by_doi = lambda doi: (None, None)

        ref = _ref(doi="10.1/test")
        result = check_mod.lookup_reference(ref, min_match=0.80)

        assert result.per_source["openalex"]["status"] == "hit_id"
        assert result.per_source["openalex"]["queried_by"] == ["doi"]
        assert result.per_source["openalex"]["summary"]["doi"] == "10.1/test"
        # crossref should not have been called — we got an ID hit first
        assert "crossref" not in result.per_source
        assert result.id_confirmed
        assert result.best_source == "openalex"

    def test_records_error_when_source_raises(self, stub_sources):
        def _boom(*a, **kw):
            raise RuntimeError("network down")
        stub_sources["openalex"].get_by_doi = _boom

        ref = _ref(doi="10.1/test")
        result = check_mod.lookup_reference(ref, min_match=0.80)

        assert result.per_source["openalex"]["status"] == "error"
        assert "openalex" in result.exhausted_sources

    def test_disabled_source_marked_and_skipped(self, stub_sources):
        def _boom(*a, **kw):
            raise RuntimeError("boom")
        stub_sources["openalex"].get_by_doi = _boom

        health = check_mod.SourceHealth(threshold=1)
        health._disabled.add("openalex")

        ref = _ref(doi="10.1/test")
        result = check_mod.lookup_reference(ref, min_match=0.80, health=health)

        assert result.per_source["openalex"]["status"] == "disabled"


# --------------------------------------------------------------------------
# _plan_ref_work
# --------------------------------------------------------------------------


class TestPlanRefWork:
    def test_fresh_ref_queries_all_sources(self):
        targets = check_mod._plan_ref_work(None, None, retry_closest=False, retry_errored=True)
        assert targets == set(check_mod._ALL_SOURCE_NAMES)

    def test_ok_ref_returns_none(self):
        r = LookupResult(id_confirmed=True, display_score=0.99, best_source="openalex")
        r.per_source["openalex"] = {"status": "hit_id", "queried_by": ["doi"],
                                    "score": 1.0, "summary": {}}
        assert check_mod._plan_ref_work(r, "OK", retry_closest=False, retry_errored=True) is None

    def test_closest_returns_none_by_default(self):
        r = LookupResult(display_score=0.85, best_source="crossref")
        r.per_source["crossref"] = {"status": "hit_title", "queried_by": ["title"],
                                    "score": 0.85, "summary": {}}
        assert check_mod._plan_ref_work(r, "CLOSEST", retry_closest=False, retry_errored=True) is None

    def test_closest_returns_untried_when_flagged(self):
        r = LookupResult(display_score=0.85)
        r.per_source["crossref"] = {"status": "hit_title", "queried_by": ["title"],
                                    "score": 0.85, "summary": {}}
        r.per_source["openalex"] = {"status": "not_found", "queried_by": ["title"],
                                    "score": None, "summary": None}
        targets = check_mod._plan_ref_work(r, "CLOSEST", retry_closest=True, retry_errored=True)
        # openalex was tried (not_found) so not in targets; crossref has a result;
        # everything else missing → in targets
        assert "openalex" not in targets
        assert "crossref" not in targets
        assert "dblp" in targets

    def test_no_match_retries_untried_and_errored(self):
        r = LookupResult(display_score=0.30)
        r.per_source["openalex"] = {"status": "hit_title", "queried_by": ["title"],
                                    "score": 0.30, "summary": {}}
        r.per_source["crossref"] = {"status": "error", "queried_by": ["doi"],
                                    "score": None, "summary": None}
        r.per_source["dblp"] = {"status": "not_found", "queried_by": ["title"],
                                "score": None, "summary": None}
        targets = check_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=True)
        assert "openalex" not in targets    # already got a title hit
        assert "crossref" in targets        # errored → retry
        assert "dblp" not in targets        # not_found is a real answer
        assert "semanticscholar" in targets # never tried
        assert "arxiv" in targets

    def test_errored_not_retried_when_flag_off(self):
        r = LookupResult(display_score=0.30)
        r.per_source["crossref"] = {"status": "error", "queried_by": ["doi"],
                                    "score": None, "summary": None}
        targets = check_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=False)
        assert "crossref" not in targets

    def test_disabled_always_retried(self):
        r = LookupResult(display_score=0.30)
        r.per_source["crossref"] = {"status": "disabled", "queried_by": [],
                                    "score": None, "summary": None,
                                    "note": "session circuit breaker"}
        targets = check_mod._plan_ref_work(r, "NO MATCH", retry_closest=False, retry_errored=False)
        assert "crossref" in targets


# --------------------------------------------------------------------------
# sources_to_query filtering
# --------------------------------------------------------------------------


class TestSourcesToQuery:
    def test_only_named_sources_are_queried(self, stub_sources, monkeypatch):
        calls = {"openalex": 0, "crossref": 0, "semanticscholar": 0}

        def make_stub(name):
            def _stub(*a, **kw):
                calls[name] += 1
                return None, None
            return _stub

        for name in calls:
            monkeypatch.setattr(stub_sources[name], "get_by_doi", make_stub(name))

        ref = _ref(doi="10.1/x")
        check_mod.lookup_reference(
            ref, min_match=0.80,
            sources_to_query={"crossref"},
        )

        assert calls["openalex"] == 0
        assert calls["crossref"] == 1
        assert calls["semanticscholar"] == 0

    def test_prior_per_source_preserved(self, stub_sources):
        prior = LookupResult()
        prior.per_source["openalex"] = {"status": "not_found", "queried_by": ["title"],
                                        "score": None, "summary": None}

        stub_sources["crossref"].search_by_title = lambda t: (_summary(title=t), 0.95)

        ref = _ref(title="A Paper")
        result = check_mod.lookup_reference(
            ref, min_match=0.80,
            sources_to_query={"crossref"},
            prior_result=prior,
        )
        assert result.per_source["openalex"]["status"] == "not_found"
        assert result.per_source["crossref"]["status"] == "hit_title"


# --------------------------------------------------------------------------
# End-to-end: check_references
# --------------------------------------------------------------------------


class TestCheckReferences:
    def test_completes_normally(self, stub_sources, tmp_path, capsys):
        stub_sources["openalex"].get_by_doi = lambda doi: (_summary(doi=doi), 1.0)
        refs = [_ref(index=1, doi="10.1/a"), _ref(index=2, doi="10.1/b")]
        sidecar = tmp_path / "results.json"

        reason = check_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf",
        )
        assert reason is None
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["schema_version"] == 2
        assert set(data["references"].keys()) == {"1", "2"}

    def test_all_sources_disabled_breaks_and_flushes(self, stub_sources, tmp_path):
        def _boom(*a, **kw):
            raise RuntimeError("service down")

        for name in check_mod._SCHOLARLY_SOURCE_NAMES:
            src = stub_sources[name]
            for fn_name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
                if hasattr(src, fn_name):
                    setattr(src, fn_name, _boom)

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sidecar = tmp_path / "results.json"

        reason = check_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf",
            source_error_threshold=1,
        )
        assert reason == "all_scholarly_sources_disabled"
        assert sidecar.exists()

    def test_smart_rerun_only_queries_missing_sources(self, stub_sources, tmp_path):
        refs = [_ref(index=1, doi="10.1/x", title="A Paper")]
        sidecar_path = tmp_path / "results.json"

        prior_result = LookupResult()
        prior_result.per_source["openalex"] = {
            "status": "hit_title", "queried_by": ["title"],
            "score": 0.30, "summary": _summary(title="Wrong Paper"),
        }
        prior_result.per_source["crossref"] = {
            "status": "not_found", "queried_by": ["doi", "title"],
            "score": None, "summary": None,
        }
        sidecar_mod.write(sidecar_path, "test.pdf", refs, {1: prior_result}, 0.80)

        calls = {name: 0 for name in check_mod._ALL_SOURCE_NAMES}

        def track(name, orig):
            def _wrapped(*a, **kw):
                calls[name] += 1
                return orig(*a, **kw) if orig else (None, None)
            return _wrapped

        stub_sources["openalex"].get_by_doi = track("openalex", None)
        stub_sources["crossref"].get_by_doi = track("crossref", None)
        stub_sources["dblp"].search_by_title = track(
            "dblp", lambda t: (_summary(title=t), 0.99),
        )
        stub_sources["semanticscholar"].get_by_doi = track("semanticscholar", None)
        stub_sources["arxiv"].get_by_doi = track("arxiv", None)

        reason = check_mod.check_references(
            refs, sidecar=sidecar_path, pdf_name="test.pdf",
            resume=True,
        )
        assert reason is None
        # openalex and crossref should NOT be re-queried (they had real prior entries)
        assert calls["openalex"] == 0
        assert calls["crossref"] == 0
        # dblp should have been queried
        assert calls["dblp"] >= 1

    def test_shutdown_via_signal_flushes_sidecar(self, stub_sources, tmp_path):
        """Simulate a SIGINT partway through a run and verify the sidecar is valid."""
        pytest.importorskip("threading")

        stub_sources["openalex"].get_by_doi = lambda doi: (_summary(doi=doi), 1.0)

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sidecar = tmp_path / "results.json"

        signalled = {"done": False}
        original_check_url = stub_sources["github"].check_url

        # Send SIGINT to ourselves during the 3rd ref's lookup by hooking one
        # of the stubbed calls. openalex.get_by_doi is called for each ref;
        # trigger on the 3rd call.
        call_count = {"n": 0}
        orig_get_by_doi = stub_sources["openalex"].get_by_doi

        def _maybe_signal(doi):
            call_count["n"] += 1
            if call_count["n"] == 3 and not signalled["done"]:
                signalled["done"] = True
                os.kill(os.getpid(), signal.SIGINT)
            return orig_get_by_doi(doi)

        stub_sources["openalex"].get_by_doi = _maybe_signal

        reason = check_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf",
        )
        assert reason == "keyboard_interrupt"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["schema_version"] == 2
        # We should have at least the first two refs saved (completed before signal).
        assert "1" in data["references"]
        assert "2" in data["references"]


# --------------------------------------------------------------------------
# recompute_best
# --------------------------------------------------------------------------


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
        assert "crossref" in r.exhausted_sources
