"""Tests for ref_checker.engine.lookup_reference: assess one reference
against all sources.

All tests monkeypatch the source modules — no network calls are made.
"""
from __future__ import annotations

import pytest

from ref_checker import engine as engine_mod
from ref_checker import runtime as runtime_mod
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
    """Zero out per-source delays and retry backoff so tests don't sleep."""
    monkeypatch.setattr(
        engine_mod, "_DEFAULT_DELAYS",
        {k: 0.0 for k in engine_mod._DEFAULT_DELAYS},
    )
    monkeypatch.setattr(runtime_mod, "_RETRY_BACKOFF", (0.0, 0.0, 0.0))


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
# per_source population
# --------------------------------------------------------------------------


class TestPerSource:
    def test_records_hit_id_and_not_found(self, stub_sources):
        stub_sources["openalex"].get_by_doi = lambda doi, ctx: (_summary(doi=doi), 1.0)
        stub_sources["crossref"].get_by_doi = lambda doi, ctx: (None, None)

        ref = _ref(doi="10.1/test")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

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
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.per_source["openalex"]["status"] == "error"
        assert "openalex" in result.exhausted_sources

    def test_records_rate_limited_when_source_exhausts_on_rate_limit(self, stub_sources):
        from ref_checker.errors import RateLimited

        def _always_rate_limited(*a, **kw):
            raise RateLimited(retry_after=0.0)
        stub_sources["openalex"].get_by_doi = _always_rate_limited
        stub_sources["openalex"].search_by_title = _always_rate_limited

        ref = _ref(doi="10.1/test")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.per_source["openalex"]["status"] == "rate_limited"
        # rate_limited counts toward exhausted_sources exactly like error —
        # both mean "we did not get real information", just different cause.
        assert "openalex" in result.exhausted_sources

    def test_disabled_source_marked_and_skipped(self, stub_sources):
        def _boom(*a, **kw):
            raise RuntimeError("boom")
        stub_sources["openalex"].get_by_doi = _boom

        health = engine_mod.SourceHealth(threshold=1)
        health._disabled.add("openalex")

        ref = _ref(doi="10.1/test")
        result = engine_mod.lookup_reference(ref, min_match=0.80, health=health)

        assert result.per_source["openalex"]["status"] == "disabled"


# --------------------------------------------------------------------------
# EvidenceLevel computation (additive alongside OK/CLOSEST/NO MATCH status)
# --------------------------------------------------------------------------


class TestEvidenceLevel:
    def test_doi_hit_is_confirmed_identifier(self, stub_sources):
        from ref_checker.model import EvidenceLevel

        stub_sources["openalex"].get_by_doi = lambda doi, ctx: (_summary(doi=doi), 1.0)

        ref = _ref(doi="10.1/test")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.evidence == EvidenceLevel.CONFIRMED_IDENTIFIER

    def test_github_liveness_is_live_resource_only(self, stub_sources):
        from ref_checker.model import EvidenceLevel

        stub_sources["github"].check_url = lambda url: (
            {"source": "github", "title": None, "authors": [], "year": None,
             "venue": None, "doi": None, "url": url, "external_id": None},
            1.0,
            [],
        )

        ref = _ref(github_url="https://github.com/org/repo")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.evidence == EvidenceLevel.LIVE_RESOURCE_ONLY

    def test_strong_title_match_is_strong_metadata_match(self, stub_sources):
        from ref_checker.model import EvidenceLevel

        stub_sources["openalex"].search_by_title = lambda title, ctx: (_summary(doi=None), 0.95)

        ref = _ref(title="A Paper")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.evidence == EvidenceLevel.STRONG_METADATA_MATCH

    def test_weak_title_match_is_weak_or_ambiguous(self, stub_sources):
        from ref_checker.model import EvidenceLevel

        stub_sources["openalex"].search_by_title = lambda title, ctx: (_summary(doi=None), 0.82)

        ref = _ref(title="A Paper")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.evidence == EvidenceLevel.WEAK_OR_AMBIGUOUS_MATCH

    def test_no_match_at_all_is_not_found(self, stub_sources):
        from ref_checker.model import EvidenceLevel

        ref = _ref(title="Totally Unmatched Paper")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.evidence == EvidenceLevel.NOT_FOUND

    def test_exhausted_sources_with_no_match_is_incomplete(self, stub_sources):
        from ref_checker.model import EvidenceLevel

        def _boom(*a, **kw):
            raise RuntimeError("network down")
        stub_sources["openalex"].get_by_doi = _boom
        stub_sources["openalex"].search_by_title = _boom

        ref = _ref(doi="10.1/test", title="Some Title")
        result = engine_mod.lookup_reference(ref, min_match=0.80)

        assert result.evidence == EvidenceLevel.INCOMPLETE


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
        engine_mod.lookup_reference(
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

        stub_sources["crossref"].search_by_title = lambda t, ctx: (_summary(title=t), 0.95)

        ref = _ref(title="A Paper")
        result = engine_mod.lookup_reference(
            ref, min_match=0.80,
            sources_to_query={"crossref"},
            prior_result=prior,
        )
        assert result.per_source["openalex"]["status"] == "not_found"
        assert result.per_source["crossref"]["status"] == "hit_title"


# --------------------------------------------------------------------------
# SourceContext reuse (session pooling)
# --------------------------------------------------------------------------


class TestSourceContextReuse:
    def test_same_context_object_passed_across_calls_in_one_run(self, stub_sources):
        """A single lookup_reference() call may query the same source more
        than once (e.g. DOI then title fallback) — it must reuse one ctx,
        not build a fresh one per call.
        """
        seen_ctxs = []

        def _record(doi, ctx):
            seen_ctxs.append(ctx)
            return None, None

        stub_sources["openalex"].get_by_doi = _record
        stub_sources["openalex"].search_by_title = _record

        ref = _ref(doi="10.1/test", title="A Paper")
        engine_mod.lookup_reference(ref, min_match=0.80)

        assert len(seen_ctxs) == 2
        assert seen_ctxs[0] is seen_ctxs[1]

    def test_contexts_dict_reused_across_references_in_one_run(self, stub_sources):
        """This is what runner.py relies on: build contexts once, pass the
        same dict into lookup_reference() for every reference in the run,
        so every reference reuses the same per-source session.
        """
        seen_ctxs = []

        def _record(doi, ctx):
            seen_ctxs.append(ctx)
            return None, None

        stub_sources["openalex"].get_by_doi = _record

        contexts: dict = {}
        ref1 = _ref(index=1, doi="10.1/a")
        ref2 = _ref(index=2, doi="10.1/b")
        engine_mod.lookup_reference(ref1, min_match=0.80, contexts=contexts)
        engine_mod.lookup_reference(ref2, min_match=0.80, contexts=contexts)

        assert len(seen_ctxs) == 2
        assert seen_ctxs[0] is seen_ctxs[1]

    def test_without_shared_contexts_dict_each_call_builds_its_own(self, stub_sources):
        """Direct-call test paths that don't pass contexts= (contexts=None,
        the default) still work — but don't get cross-call reuse, since
        each lookup_reference() call starts its own empty contexts dict.
        """
        seen_ctxs = []

        def _record(doi, ctx):
            seen_ctxs.append(ctx)
            return None, None

        stub_sources["openalex"].get_by_doi = _record

        ref1 = _ref(index=1, doi="10.1/a")
        ref2 = _ref(index=2, doi="10.1/b")
        engine_mod.lookup_reference(ref1, min_match=0.80)
        engine_mod.lookup_reference(ref2, min_match=0.80)

        assert len(seen_ctxs) == 2
        assert seen_ctxs[0] is not seen_ctxs[1]
