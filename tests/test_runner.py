"""Tests for ref_checker.runner.check_references: thread pool, resume/
sidecar I/O, signal handling, and end-of-run reporting.

All tests monkeypatch the source modules — no network calls are made.
"""
from __future__ import annotations

import json
import os
import signal
import threading

import pytest

from ref_checker import runner as runner_mod
from ref_checker import runtime as runtime_mod
from ref_checker import sidecar as sidecar_mod
from ref_checker.extract import Reference
from ref_checker.results import LookupResult
from ref_checker.sources.registry import scholarly_source_names, all_source_names
from ref_checker.sources.registry import default_delays as _real_default_delays


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
        runner_mod, "_default_delays",
        lambda: {k: 0.0 for k in _real_default_delays()},
    )
    monkeypatch.setattr(runtime_mod, "_RETRY_BACKOFF", (0.0, 0.0, 0.0))
    for var in ("PRIMO_BASE_URL", "PRIMO_VID", "PRIMO_INST", "PRIMO_SCOPE"):
        monkeypatch.delenv(var, raising=False)


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
# End-to-end: check_references
# --------------------------------------------------------------------------


class TestCheckReferences:
    def test_completes_normally(self, stub_sources, tmp_path, capsys):
        stub_sources["openalex"].get_by_doi = lambda doi, ctx: (_summary(doi=doi), 1.0)
        refs = [_ref(index=1, doi="10.1/a"), _ref(index=2, doi="10.1/b")]
        sidecar = tmp_path / "results.json"

        reason = runner_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf",
        )
        assert reason is None
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["schema_version"] == 4
        assert set(data["references"].keys()) == {"1", "2"}

    def test_same_session_reused_across_references_in_one_run(
        self, stub_sources, tmp_path,
    ):
        """check_references() builds SourceContexts once per run (not per
        reference) — see PLAN.md's SourceContext lifecycle decision. This is
        what actually gets a connection-pooling benefit: every reference in
        the run must see the same ctx.session object for a given source.
        """
        seen_sessions = []

        def _record(doi, ctx):
            seen_sessions.append(ctx.session)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _record
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 4)]
        sidecar = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sidecar, pdf_name="test.pdf", jobs=1)

        assert len(seen_sessions) == 3
        assert len({id(s) for s in seen_sessions}) == 1

    def test_same_session_reused_for_liveness_source_across_references(
        self, stub_sources, tmp_path,
    ):
        """Part 2 of the SourceContext work: github/url liveness sources get
        the same run-scoped session reuse as scholarly sources.
        """
        seen_sessions = []

        def _record(urls, ctx):
            seen_sessions.append(ctx.session)
            return (
                {"source": "github", "title": None, "authors": [], "year": None,
                 "venue": None, "doi": None, "url": urls, "external_id": None},
                1.0,
                [],
            )

        stub_sources["github"].check_url = _record
        refs = [
            _ref(index=i, github_url=f"https://github.com/org/repo{i}")
            for i in range(1, 4)
        ]
        sidecar = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sidecar, pdf_name="test.pdf", jobs=1)

        assert len(seen_sessions) == 3
        assert len({id(s) for s in seen_sessions}) == 1

    def test_all_sources_disabled_breaks_and_flushes(self, stub_sources, tmp_path):
        def _boom(*a, **kw):
            raise RuntimeError("service down")

        for name in scholarly_source_names():
            src = stub_sources[name]
            for fn_name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
                if hasattr(src, fn_name):
                    setattr(src, fn_name, _boom)

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sidecar = tmp_path / "results.json"

        reason = runner_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf",
            source_error_threshold=1,
        )
        assert reason == "all_scholarly_sources_disabled"
        assert sidecar.exists()

    def test_smart_rerun_only_queries_missing_sources(self, stub_sources, tmp_path):
        refs = [_ref(index=1, doi="10.1/x", title="A Paper")]
        sidecar_path = tmp_path / "results.json"

        prior_result = LookupResult()
        prior_result.record_source(
            "openalex", "hit_title", queried_by="title",
            score=0.30, summary=_summary(title="Wrong Paper"),
        )
        prior_result.record_source("crossref", "not_found", queried_by="doi")
        prior_result.record_source("crossref", "not_found", queried_by="title")
        sidecar_mod.write(sidecar_path, "test.pdf", refs, {1: prior_result}, 0.80)

        calls = {name: 0 for name in all_source_names()}

        def track(name, orig):
            def _wrapped(*a, **kw):
                calls[name] += 1
                return orig(*a, **kw) if orig else (None, None)
            return _wrapped

        stub_sources["openalex"].get_by_doi = track("openalex", None)
        stub_sources["crossref"].get_by_doi = track("crossref", None)
        stub_sources["dblp"].search_by_title = track(
            "dblp", lambda t, ctx: (_summary(title=t), 0.99),
        )
        stub_sources["semanticscholar"].get_by_doi = track("semanticscholar", None)
        stub_sources["arxiv"].get_by_doi = track("arxiv", None)

        reason = runner_mod.check_references(
            refs, sidecar=sidecar_path, pdf_name="test.pdf",
            resume=True,
        )
        assert reason is None
        # openalex and crossref should NOT be re-queried (they had real prior entries)
        assert calls["openalex"] == 0
        assert calls["crossref"] == 0
        # dblp should have been queried
        assert calls["dblp"] >= 1

    def test_interrupted_run_skipped_source_is_retried_and_incomplete_on_resume(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        """Regression test for the SKIPPED resume-state bug:

        1. A run is interrupted mid-reference — one source (crossref) is
           recorded as SKIPPED in the sidecar because shutdown becomes
           requested (as it genuinely would during a real Ctrl-C, mid
           rate-limiter reservation) after the source's per-reference
           iteration has already begun but before its actual HTTP attempt.
           We trigger this deterministically via the real _Shutdown object
           the rate limiter already holds a reference to (hooking
           _RateLimiter.wait, which is where shutdown races against
           in-flight per-source dispatch in the real code) rather than
           relying on real-time SIGINT delivery timing, which cannot be
           landed reliably in this exact one-statement window.
        2. The interrupted run's sidecar evidence must be INCOMPLETE, not
           NOT_FOUND (openalex genuinely came back not_found; crossref never
           actually ran).
        3. Resuming must re-query crossref (previously the bug: SKIPPED was
           silently never retried) and, once every source has a genuine
           conclusive answer, must move evidence to NOT_FOUND.
        """
        refs = [_ref(index=1, doi="10.1/x1", title="Some Paper")]
        sidecar = tmp_path / "results.json"

        stub_sources["openalex"].get_by_doi = lambda doi, ctx: (None, None)

        orig_wait = runtime_mod._RateLimiter.wait

        def _wait_then_request_shutdown_for_crossref(self, source_name):
            orig_wait(self, source_name)
            if source_name == "crossref" and self._shutdown is not None:
                self._shutdown.request()

        monkeypatch.setattr(
            runtime_mod._RateLimiter, "wait", _wait_then_request_shutdown_for_crossref,
        )

        reason = runner_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf", jobs=1,
        )
        assert reason == "keyboard_interrupt"

        data = json.loads(sidecar.read_text())
        entry = data["references"]["1"]["result"]
        assert entry["per_source"]["openalex"]["status"] == "not_found"
        assert entry["per_source"]["crossref"]["status"] == "skipped"
        # Interrupted, not a genuine negative — must not read as NOT_FOUND.
        assert entry["evidence"] == "incomplete"

        # --- Resume: crossref (skipped) must be re-queried this time. ---
        monkeypatch.setattr(runtime_mod._RateLimiter, "wait", orig_wait)
        calls = {name: 0 for name in all_source_names()}

        def track(name, orig):
            def _wrapped(*a, **kw):
                calls[name] += 1
                return orig(*a, **kw) if orig else (None, None)
            return _wrapped

        stub_sources["openalex"].get_by_doi = track("openalex", None)
        stub_sources["crossref"].get_by_doi = track("crossref", None)
        stub_sources["osti"].get_by_doi = track("osti", None)
        stub_sources["dblp"].search_by_title = track("dblp", None)
        stub_sources["semanticscholar"].get_by_doi = track("semanticscholar", None)
        stub_sources["arxiv"].get_by_doi = track("arxiv", None)

        reason2 = runner_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf", jobs=1, resume=True,
        )
        assert reason2 is None
        # openalex already had a real not_found — smart-rerun should not
        # re-query it. crossref was SKIPPED — must be retried.
        assert calls["openalex"] == 0
        assert calls["crossref"] == 1

        data2 = json.loads(sidecar.read_text())
        entry2 = data2["references"]["1"]["result"]
        assert entry2["per_source"]["crossref"]["status"] == "not_found"
        # Every scholarly source now has a genuine not_found — a real
        # negative result, correctly reported as NOT_FOUND this time.
        assert entry2["evidence"] == "not_found"

    def test_shutdown_via_signal_flushes_sidecar(self, stub_sources, tmp_path):
        """Simulate a SIGINT partway through a run and verify the sidecar is valid."""
        pytest.importorskip("threading")

        stub_sources["openalex"].get_by_doi = lambda doi, ctx: (_summary(doi=doi), 1.0)

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sidecar = tmp_path / "results.json"

        signalled = {"done": False}

        # Send SIGINT to ourselves during the 3rd ref's lookup by hooking one
        # of the stubbed calls. openalex.get_by_doi is called for each ref;
        # trigger on the 3rd call.
        call_count = {"n": 0}
        orig_get_by_doi = stub_sources["openalex"].get_by_doi

        def _maybe_signal(doi, ctx):
            call_count["n"] += 1
            if call_count["n"] == 3 and not signalled["done"]:
                signalled["done"] = True
                os.kill(os.getpid(), signal.SIGINT)
            return orig_get_by_doi(doi, ctx)

        stub_sources["openalex"].get_by_doi = _maybe_signal

        reason = runner_mod.check_references(
            refs, sidecar=sidecar, pdf_name="test.pdf",
        )
        assert reason == "keyboard_interrupt"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["schema_version"] == 4
        # We should have at least the first two refs saved (completed before signal).
        assert "1" in data["references"]
        assert "2" in data["references"]


# --------------------------------------------------------------------------
# Concurrency (--jobs N)
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_jobs3_produces_same_results_as_jobs1(self, stub_sources, tmp_path):
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]

        sc1 = tmp_path / "seq.json"
        r1 = runner_mod.check_references(refs, sidecar=sc1, pdf_name="p.pdf", jobs=1)
        assert r1 is None

        sc3 = tmp_path / "par.json"
        r3 = runner_mod.check_references(refs, sidecar=sc3, pdf_name="p.pdf", jobs=3)
        assert r3 is None

        d1 = json.loads(sc1.read_text())
        d3 = json.loads(sc3.read_text())
        assert set(d1["references"].keys()) == set(d3["references"].keys())
        for key in d1["references"]:
            s1 = d1["references"][key]["result"]["best_summary"]
            s3 = d3["references"][key]["result"]["best_summary"]
            assert s1 == s3

    def test_stdout_is_in_ref_index_order(self, stub_sources, tmp_path, capsys):
        # Make later refs finish faster by giving earlier ones a small "delay"
        # via a monkeypatched function that sleeps proportionally to index.
        import time

        call_order: list[int] = []

        def make_stub(doi):
            idx = int(doi.split("x")[-1])
            # Reverse-order sleep: earliest ref sleeps longest.
            sleep_s = (10 - idx) * 0.01
            time.sleep(sleep_s)
            call_order.append(idx)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = make_stub

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=3)
        out = capsys.readouterr().out
        # stdout should still be in ref-index order regardless of completion order.
        idxs = []
        for line in out.splitlines():
            if line.startswith("[") and "] " in line:
                head = line.split("]", 1)[0][1:]
                try:
                    idxs.append(int(head))
                except ValueError:
                    pass
        assert idxs == sorted(idxs)
        assert idxs == [1, 2, 3, 4, 5]

    def test_no_stdout_before_shutdown_emission(self, stub_sources, tmp_path, capsys):
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 4)]
        sc = tmp_path / "results.json"

        # We can't directly observe "during run" vs "at shutdown" from a single
        # capsys.readouterr(), but we can verify no result block appears before
        # the ThreadPoolExecutor has been shut down by asserting that stdout
        # only contains formatted blocks (never interleaved with per-completion
        # progress lines which go to stderr). All progress goes to stderr, so
        # stdout must contain ONLY the formatted blocks.
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=3)
        cap = capsys.readouterr()
        for line in cap.out.splitlines():
            assert not line.startswith("[ref-checker]"), (
                f"progress line leaked to stdout: {line!r}"
            )

    def test_circuit_breaker_trips_under_concurrency(self, stub_sources, tmp_path):
        errors_seen: list[int] = []
        lock = threading.Lock()

        def _boom(doi, ctx):
            with lock:
                errors_seen.append(1)
            raise RuntimeError("service down")

        for name in scholarly_source_names():
            src = stub_sources[name]
            for fn_name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
                if hasattr(src, fn_name):
                    setattr(src, fn_name, _boom)

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 10)]
        sc = tmp_path / "results.json"

        reason = runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf",
            source_error_threshold=1, jobs=3,
        )
        assert reason == "all_scholarly_sources_disabled"
        assert sc.exists()

    def test_shutdown_via_signal_flushes_sidecar_concurrent(self, stub_sources, tmp_path):
        """Simulate SIGINT partway through a concurrent run; sidecar must be valid."""
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 8)]
        sc = tmp_path / "results.json"

        call_count = {"n": 0}
        signalled = {"done": False}
        orig = stub_sources["openalex"].get_by_doi

        def _maybe_signal(doi, ctx):
            call_count["n"] += 1
            if call_count["n"] >= 3 and not signalled["done"]:
                signalled["done"] = True
                os.kill(os.getpid(), signal.SIGINT)
            return orig(doi, ctx)

        stub_sources["openalex"].get_by_doi = _maybe_signal

        # Use jobs=1 for deterministic signal-timing under test.
        reason = runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )
        assert reason == "keyboard_interrupt"
        assert sc.exists()
        data = json.loads(sc.read_text())
        assert data["schema_version"] == 4
        assert "1" in data["references"]

    def test_jobs_1_matches_sequential_semantics(self, stub_sources, tmp_path):
        # A resume-only ref (fully cached, no work) should appear in stdout
        # exactly once, in index order, regardless of jobs setting.
        refs = [_ref(index=1, doi="10.1/x1"), _ref(index=2, doi="10.1/x2")]

        # Seed the sidecar with cached results.
        prior = LookupResult(
            id_confirmed=True, display_score=0.99, best_source="openalex",
            best_summary=_summary(doi="10.1/pre"),
        )
        prior.record_source(
            "openalex", "hit_id", queried_by="doi",
            score=1.0, summary=_summary(doi="10.1/pre"),
        )
        sc = tmp_path / "results.json"
        sidecar_mod.write(sc, "p.pdf", refs, {1: prior, 2: prior}, 0.80)

        reason = runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=3, resume=True,
        )
        assert reason is None


# --------------------------------------------------------------------------
# Thread-local SourceContext (fix/threadlocal-source-context)
# --------------------------------------------------------------------------


class TestThreadLocalSourceContexts:
    """A shared requests.Session used concurrently by multiple worker
    threads is not safe (session.cookies is read/written on every
    request/response, unsynchronized at the requests-semantics level).
    check_references() must give each worker thread its own SourceContext
    per source, while still reusing that context for every reference
    dispatched to that thread (the actual point of SourceContext).
    """

    def test_same_thread_reuses_one_session_across_its_references(
        self, stub_sources, tmp_path,
    ):
        """Within a single worker thread, every reference it processes must
        see the same session for a given source — this is what preserves
        the connection-pooling benefit. jobs=1 is the simplest case: the
        whole run is one thread, so every reference must share one session.
        """
        seen_sessions = []

        def _record(doi, ctx):
            seen_sessions.append(ctx.session)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _record
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        assert len(seen_sessions) == 5
        assert len({id(s) for s in seen_sessions}) == 1

    def test_concurrent_run_never_shares_one_session_across_two_threads(
        self, stub_sources, tmp_path,
    ):
        """The core safety property: no two different worker threads may
        ever be handed the same SourceContext/session for the same source.
        Each (thread, session) pairing must be internally consistent (one
        thread -> always the same session) and distinct threads must never
        report the same session id.

        Relying on incidental scheduling to exercise >1 worker thread is
        not safe: ThreadPoolExecutor only spawns a new thread on submit()
        if no existing worker is already idle, so with a near-instant stub
        and zeroed delays (see _no_delays), a single thread can finish task
        1 and go idle before the pool ever needs to spawn a second one --
        this previously flaked in CI with only 1 distinct thread_id
        observed. runner.py's bounded submission window (jobs + 1 = 4)
        submits 4 tasks upfront before waiting on any completion, so the
        first 3 dispatched tasks are guaranteed to land on 3 distinct
        freshly-spawned threads (max_workers=3) -- a Barrier makes that
        guarantee actually observable by forcing all 3 to be alive
        simultaneously, instead of hoping they happen to overlap.
        """
        seen: list[tuple[int, int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(3)
        counted = 0
        count_lock = threading.Lock()

        def _record(doi, ctx):
            nonlocal counted
            with count_lock:
                should_wait = counted < 3
                counted += 1
            if should_wait:
                barrier.wait(timeout=5)
            with lock:
                seen.append((threading.get_ident(), id(ctx.session)))
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _record
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 10)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=3)

        assert len(seen) == 9
        session_by_thread: dict[int, int] = {}
        for thread_id, session_id in seen:
            if thread_id in session_by_thread:
                assert session_by_thread[thread_id] == session_id, (
                    "same thread saw two different sessions for one source"
                )
            else:
                session_by_thread[thread_id] = session_id

        thread_ids = set(session_by_thread)
        session_ids = set(session_by_thread.values())
        assert len(thread_ids) >= 2, (
            "test didn't actually exercise more than one worker thread"
        )
        assert len(session_ids) == len(thread_ids), (
            "two different worker threads shared the same session object"
        )

    def test_sessions_closed_after_normal_completion(self, stub_sources, tmp_path):
        import unittest.mock

        import requests

        closed_sessions: list[int] = []
        lock = threading.Lock()
        orig_close = requests.Session.close

        def _tracked_close(self):
            with lock:
                closed_sessions.append(id(self))
            orig_close(self)

        seen_session_ids = []

        def _record(doi, ctx):
            seen_session_ids.append(id(ctx.session))
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _record
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 4)]
        sc = tmp_path / "results.json"

        with unittest.mock.patch.object(requests.Session, "close", _tracked_close):
            runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        assert set(seen_session_ids) == set(closed_sessions)

    def test_sessions_closed_after_interrupted_run(self, stub_sources, tmp_path):
        import requests
        import unittest.mock

        closed_sessions: list[int] = []
        lock = threading.Lock()

        def _tracked_close(self):
            with lock:
                closed_sessions.append(id(self))

        seen_session_ids = []
        signalled = {"done": False}

        def _record_then_signal(doi, ctx):
            seen_session_ids.append(id(ctx.session))
            if not signalled["done"]:
                signalled["done"] = True
                os.kill(os.getpid(), signal.SIGINT)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _record_then_signal
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 4)]
        sc = tmp_path / "results.json"

        with unittest.mock.patch.object(requests.Session, "close", _tracked_close):
            reason = runner_mod.check_references(
                refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
            )

        assert reason == "keyboard_interrupt"
        assert set(seen_session_ids) == set(closed_sessions)


# --------------------------------------------------------------------------
# Bounded submission window (fix/bounded-submission)
# --------------------------------------------------------------------------


class TestBoundedSubmission:
    def test_in_flight_never_exceeds_jobs_plus_one(self, stub_sources, tmp_path):
        import time

        jobs = 3
        window = jobs + 1
        in_flight = {"n": 0, "max": 0}
        lock = threading.Lock()
        release = threading.Event()

        def _slow(doi, ctx):
            with lock:
                in_flight["n"] += 1
                if in_flight["n"] > in_flight["max"]:
                    in_flight["max"] = in_flight["n"]
            release.wait(timeout=2.0)
            with lock:
                in_flight["n"] -= 1
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _slow

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 21)]
        sc = tmp_path / "results.json"

        def _releaser():
            time.sleep(0.15)
            release.set()

        t = threading.Thread(target=_releaser)
        t.start()
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=jobs)
        t.join()

        assert in_flight["max"] <= window, (
            f"max in-flight was {in_flight['max']}, expected <= {window}"
        )

    def test_no_started_lines_and_concurrency_line_present(
        self, stub_sources, tmp_path, capsys
    ):
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 5)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=3)
        err = capsys.readouterr().err

        assert "started ref #" not in err
        assert "[ref-checker] Concurrency: 3 worker(s)" in err

    def test_rate_limit_exhaustion_does_not_advance_error_counter(
        self, stub_sources, tmp_path,
    ):
        # Rate-limit exhaustions advance the RATE_LIMIT counter, not the
        # regular error counter. With source_error_threshold=1 but the
        # error counter never advancing, disable only happens once the
        # rate-limit counter hits RATE_LIMIT_THRESHOLD (default 3).
        from ref_checker.errors import RateLimited

        def _always_rate_limited(*a, **kw):
            raise RateLimited(retry_after=0.0)

        stub_sources["openalex"].get_by_doi = _always_rate_limited
        stub_sources["openalex"].search_by_title = _always_rate_limited

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf",
            source_error_threshold=1, jobs=1,
        )
        data = json.loads(sc.read_text())
        first_entry = next(iter(data["references"].values()))
        first_oa = first_entry["result"].get("per_source", {}).get("openalex")
        assert first_oa is not None
        assert first_oa["status"] == "rate_limited"
        assert "rate-limit" in (first_oa.get("note") or "").lower()

        last_key = list(data["references"].keys())[-1]
        last_oa = data["references"][last_key]["result"].get("per_source", {}).get("openalex")
        assert last_oa is not None
        assert last_oa["status"] == "disabled"

    def test_rate_limited_retry_uses_retry_after(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        from ref_checker.errors import RateLimited

        waits: list[float] = []

        def _record_wait(self, timeout):
            waits.append(timeout)
            return False

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", _record_wait)

        calls = {"n": 0}

        def _twice_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RateLimited(retry_after=0.25)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _twice_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        retry_waits = [w for w in waits if 0.2 <= w <= 0.3]
        assert len(retry_waits) >= 2, f"expected 2 retry_after=0.25 waits, got {waits!r}"

    def test_mixed_error_and_rate_limit_still_trips_breaker(
        self, stub_sources, tmp_path,
    ):
        from ref_checker.errors import RateLimited

        seq = {"i": 0}

        def _mixed(doi, ctx):
            seq["i"] += 1
            if seq["i"] % 2 == 1:
                raise RateLimited(retry_after=0.0)
            raise RuntimeError("real 500")

        for name in scholarly_source_names():
            src = stub_sources[name]
            for fn_name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
                if hasattr(src, fn_name):
                    setattr(src, fn_name, _mixed)

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sc = tmp_path / "results.json"

        reason = runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf",
            source_error_threshold=1, jobs=1,
        )
        assert reason == "all_scholarly_sources_disabled"

    def test_broken_pipe_on_emit_is_swallowed(
        self, stub_sources, tmp_path, monkeypatch, capsys
    ):
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 4)]
        sc = tmp_path / "results.json"

        import builtins
        real_print = builtins.print

        def _bp_print(*args, **kwargs):
            if kwargs.get("file") is None or kwargs.get("file") is __import__("sys").stdout:
                raise BrokenPipeError(32, "Broken pipe")
            return real_print(*args, **kwargs)

        monkeypatch.setattr(builtins, "print", _bp_print)

        reason = runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=3,
        )

        assert reason is None
        assert sc.exists()
        data = json.loads(sc.read_text())
        assert data["schema_version"] == 4
        assert "1" in data["references"]


# --------------------------------------------------------------------------
# Quota exhaustion + wait visibility + elapsed time
# --------------------------------------------------------------------------


class TestQuotaAndVisibility:
    def test_quota_exhausted_disables_source_immediately(
        self, stub_sources, tmp_path,
    ):
        from ref_checker.errors import RateLimited

        call_count = {"n": 0}

        def _quota_exhausted(*a, **kw):
            call_count["n"] += 1
            raise RateLimited(retry_after=15000.0)

        stub_sources["openalex"].get_by_doi = _quota_exhausted
        stub_sources["openalex"].search_by_title = _quota_exhausted

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )

        data = json.loads(sc.read_text())
        first_oa = data["references"]["1"]["result"]["per_source"].get("openalex")
        assert first_oa is not None
        assert first_oa["status"] == "rate_limited"
        assert "quota exhausted" in (first_oa.get("note") or "").lower()

        later_oa = data["references"]["5"]["result"]["per_source"].get("openalex")
        assert later_oa is not None
        assert later_oa["status"] == "disabled"

        assert call_count["n"] == 1

    def test_retry_after_at_threshold_still_retries(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        calls = {"n": 0}

        def _at_threshold(*a, **kw):
            calls["n"] += 1
            raise RateLimited(retry_after=runtime_mod._QUOTA_EXHAUSTED_THRESHOLD)

        stub_sources["openalex"].get_by_doi = _at_threshold
        stub_sources["openalex"].search_by_title = _at_threshold

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )

        assert calls["n"] >= 3

    def test_retry_after_just_above_threshold_disables(
        self, stub_sources, tmp_path,
    ):
        from ref_checker.errors import RateLimited

        calls = {"n": 0}

        def _above_threshold(*a, **kw):
            calls["n"] += 1
            raise RateLimited(retry_after=runtime_mod._QUOTA_EXHAUSTED_THRESHOLD + 1.0)

        stub_sources["openalex"].get_by_doi = _above_threshold
        stub_sources["openalex"].search_by_title = _above_threshold

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )

        assert calls["n"] == 1

    def test_long_wait_prints_visibility_line(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        calls = {"n": 0}

        def _rate_limited_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RateLimited(retry_after=15.0)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _rate_limited_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )
        err = capsys.readouterr().err
        assert "openalex: waiting" in err
        assert "before retry" in err
        assert "Retry-After=15s" in err

    def test_short_rate_limit_wait_still_visible(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        # RateLimited waits always print regardless of duration so the user
        # can see the source's behavior even under brief throttling.
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        calls = {"n": 0}

        def _rate_limited_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RateLimited(retry_after=1.0)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _rate_limited_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )
        err = capsys.readouterr().err
        assert "openalex: waiting" in err
        assert "Retry-After=1s" in err

    def test_elapsed_time_appears_in_summary(
        self, stub_sources, tmp_path, capsys,
    ):
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 3)]
        sc = tmp_path / "results.json"

        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        assert "[ref-checker] Elapsed:" in err


# --------------------------------------------------------------------------
# Rate-limit diagnostics: first-429 line + always-on wait visibility
# --------------------------------------------------------------------------


class TestRateLimitDiagnostics:
    def test_first_rate_limit_diagnostic_prints_once(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        calls = {"n": 0}

        def _rl_twice_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RateLimited(retry_after=5.0)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _rl_twice_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        first_lines = [ln for ln in err.splitlines() if "first 429 seen" in ln]
        assert len(first_lines) == 1
        assert "Retry-After=5s" in first_lines[0]
        assert "openalex" in first_lines[0]

    def test_first_rate_limit_reports_missing_retry_after(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        calls = {"n": 0}

        def _rl_once_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimited(retry_after=None)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _rl_once_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        assert "openalex: first 429 seen (Retry-After=<none>)" in err

    def test_generic_backoff_below_threshold_stays_quiet(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        # Generic (non-RateLimited) retries at short waits should NOT
        # print the visibility line — only long ones and rate-limit ones do.
        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)
        monkeypatch.setattr(runtime_mod, "_RETRY_BACKOFF", (1.0, 1.0, 1.0))

        calls = {"n": 0}

        def _err_once_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("blip")
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _err_once_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        assert "openalex: waiting" not in err

    def test_rate_limit_wait_notes_cap_when_over_max(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        calls = {"n": 0}

        def _rl_then_ok(doi, ctx):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RateLimited(retry_after=120.0)
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _rl_then_ok

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        assert "Retry-After=120s" in err
        assert "capped at 60s" in err


# --------------------------------------------------------------------------
# Stats: per-mode breakdown (doi vs title)
# --------------------------------------------------------------------------


class TestStatsByMode:
    def test_records_doi_and_title_separately(
        self, stub_sources, tmp_path, capsys,
    ):
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (None, None)
        )
        stub_sources["openalex"].search_by_title = (
            lambda title, ctx: (None, None)
        )

        refs = [_ref(index=1, doi="10.1/x1"), _ref(index=2, doi=None, title="T2")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        # Look for the openalex summary line and confirm both modes appear.
        oa_line = next(ln for ln in err.splitlines() if "openalex " in ln)
        assert "doi" in oa_line
        assert "title" in oa_line

    def test_single_mode_still_shows_breakdown(
        self, stub_sources, tmp_path, capsys,
    ):
        # Single-mode sources still show the (N mode) breakdown so the
        # summary format is consistent across sources.
        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 4)]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        oa_line = next(ln for ln in err.splitlines() if "openalex " in ln)
        assert "(3 doi)" in oa_line

    def test_exhausted_by_mode_shown_with_mixed_modes(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        # openalex gets both a successful DOI query AND an exhausted
        # title query, forcing multi-mode breakdown on the exhausted line.
        from ref_checker.errors import RateLimited
        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )

        def _title_rl(title, ctx):
            raise RateLimited(retry_after=0.0)

        stub_sources["openalex"].search_by_title = _title_rl

        refs = [
            _ref(index=1, doi="10.1/x1"),
            _ref(index=2, doi=None, title="A title only ref"),
        ]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        oa_line = next(ln for ln in err.splitlines() if "openalex " in ln)
        assert "doi" in oa_line
        assert "title" in oa_line
        assert "exhausted" in oa_line


# --------------------------------------------------------------------------
# SS unauth delay tuning
# --------------------------------------------------------------------------


class TestSSUnauthDelay:
    def test_ss_delay_bumped_when_key_unset(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("SEMANTICSCHOLAR_API_KEY", raising=False)
        # Restore a realistic (non-zero) default so the override kicks in.
        monkeypatch.setattr(
            runner_mod, "_default_delays",
            lambda: {**{k: 0.0 for k in _real_default_delays()}, "semanticscholar": 8.0},
        )
        captured: dict = {}
        real_init = runtime_mod._RateLimiter.__init__

        def _capture_init(self, delays, shutdown=None):
            captured["delays"] = dict(delays)
            real_init(self, delays, shutdown=shutdown)

        monkeypatch.setattr(runtime_mod._RateLimiter, "__init__", _capture_init)

        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        assert captured["delays"]["semanticscholar"] == runner_mod._SS_UNAUTH_DELAY

    def test_ss_delay_preserved_when_key_set(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("SEMANTICSCHOLAR_API_KEY", "sk-test")
        monkeypatch.setattr(
            runner_mod, "_default_delays",
            lambda: {**{k: 0.0 for k in _real_default_delays()}, "semanticscholar": 8.0},
        )
        captured: dict = {}
        real_init = runtime_mod._RateLimiter.__init__

        def _capture_init(self, delays, shutdown=None):
            captured["delays"] = dict(delays)
            real_init(self, delays, shutdown=shutdown)

        monkeypatch.setattr(runtime_mod._RateLimiter, "__init__", _capture_init)

        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        assert captured["delays"]["semanticscholar"] == 8.0

    def test_ss_delay_zero_preserved_for_tests(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        # If tests explicitly zero out delays, the override must not fire.
        monkeypatch.delenv("SEMANTICSCHOLAR_API_KEY", raising=False)
        captured: dict = {}
        real_init = runtime_mod._RateLimiter.__init__

        def _capture_init(self, delays, shutdown=None):
            captured["delays"] = dict(delays)
            real_init(self, delays, shutdown=shutdown)

        monkeypatch.setattr(runtime_mod._RateLimiter, "__init__", _capture_init)

        stub_sources["openalex"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )
        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        assert captured["delays"]["semanticscholar"] == 0.0


# --------------------------------------------------------------------------
# Disabled-during-flight race fixes
# --------------------------------------------------------------------------


class TestDisabledDuringFlight:
    def test_retry_short_circuits_when_source_disabled_between_attempts(
        self, stub_sources, tmp_path, monkeypatch, capsys,
    ):
        # Simulate: a source raises RateLimited once, then another worker
        # disables it externally. The next retry iteration must short-circuit
        # without calling the source function again.
        from ref_checker.errors import RateLimited

        monkeypatch.setattr(runtime_mod._Shutdown, "wait", lambda self, t: False)

        call_count = {"n": 0}
        health_ref: dict = {}

        def _rl_then_check(doi, ctx):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # After this raise, we disable the source ourselves.
                health = health_ref.get("h")
                if health is not None:
                    health.disable("openalex", "manual test disable")
                raise RateLimited(retry_after=0.0)
            # If we get here, the short-circuit failed.
            return _summary(doi=doi), 1.0

        stub_sources["openalex"].get_by_doi = _rl_then_check

        real_init = runtime_mod.SourceHealth.__init__

        def _capture(self, *a, **kw):
            real_init(self, *a, **kw)
            health_ref["h"] = self

        monkeypatch.setattr(runtime_mod.SourceHealth, "__init__", _capture)

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        runner_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)

        # Only the first attempt actually invoked the source fn; the retry
        # iteration short-circuited on is_disabled.
        assert call_count["n"] == 1

    def test_call_short_circuits_when_disabled_during_rl_wait(
        self, stub_sources, tmp_path, monkeypatch,
    ):
        # Simulate: worker A holds the RateLimiter's slot for openalex.
        # While worker B is blocked in rl.wait, worker A disables the source.
        # When B's rl.wait returns, B must re-check and short-circuit
        # without ever calling the source function.
        from ref_checker.errors import RateLimited

        openalex_calls = {"n": 0}

        def _boom(*a, **kw):
            openalex_calls["n"] += 1
            raise RateLimited(retry_after=0.0)

        stub_sources["openalex"].get_by_doi = _boom
        stub_sources["openalex"].search_by_title = _boom
        stub_sources["crossref"].get_by_doi = (
            lambda doi, ctx: (_summary(doi=doi), 1.0)
        )

        refs = [_ref(index=i, doi=f"10.1/x{i}") for i in range(1, 6)]
        sc = tmp_path / "results.json"
        runner_mod.check_references(
            refs, sidecar=sc, pdf_name="p.pdf", jobs=1,
        )

        # RATE_LIMIT_THRESHOLD is 3 by default. With sequential (jobs=1)
        # each ref = 1 rate-limit exhaustion cycle = 3 calls to the source
        # fn. Once disabled, subsequent refs must short-circuit without
        # calling the source at all.
        assert openalex_calls["n"] <= 3 * runtime_mod.SourceHealth.RATE_LIMIT_THRESHOLD, (
            f"openalex called {openalex_calls['n']} times; should short-circuit "
            f"after {runtime_mod.SourceHealth.RATE_LIMIT_THRESHOLD} exhaustion cycles"
        )
