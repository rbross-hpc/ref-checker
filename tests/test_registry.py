"""Unit tests for ref_checker.sources.registry.ThreadLocalSourceContexts.

Pure unit tests against the class directly, isolated from
check_references()/lookup_reference() (see test_runner.py's
TestThreadLocalSourceContexts for the end-to-end integration coverage).
"""
from __future__ import annotations

import threading

from ref_checker.sources.base import SourceContext
from ref_checker.sources.registry import (
    all_source_names,
    ThreadLocalSourceContexts,
)


class TestThreadLocalSourceContexts:
    def test_get_returns_a_source_context(self):
        contexts = ThreadLocalSourceContexts()
        ctx = contexts.get("openalex")
        assert isinstance(ctx, SourceContext)

    def test_get_unknown_source_returns_none(self):
        contexts = ThreadLocalSourceContexts()
        assert contexts.get("not-a-real-source") is None

    def test_same_thread_gets_same_context_on_repeated_calls(self):
        contexts = ThreadLocalSourceContexts()
        first = contexts.get("openalex")
        second = contexts.get("openalex")
        assert first is second

    def test_different_sources_get_different_contexts(self):
        contexts = ThreadLocalSourceContexts()
        openalex_ctx = contexts.get("openalex")
        crossref_ctx = contexts.get("crossref")
        assert openalex_ctx is not crossref_ctx
        assert openalex_ctx.session is not crossref_ctx.session

    def test_different_threads_get_different_contexts_for_same_source(self):
        # Threads are cheap and do almost no work here (just a dict lookup),
        # so a thread can fully terminate -- and have its native OS thread
        # id recycled -- before another thread in this same batch even
        # starts. threading.get_ident() is therefore not a safe uniqueness
        # key for short-lived threads (see git history: this previously
        # kept results in a dict keyed by get_ident(), which flaked in CI
        # with fewer than 4 entries whenever two threads' lifetimes didn't
        # overlap and the OS reused an id). Collect into a plain list
        # instead, and use a Barrier to force all 4 threads to be alive and
        # calling contexts.get() concurrently, which is what this test
        # actually means to exercise.
        contexts = ThreadLocalSourceContexts()
        collected: list[SourceContext] = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def _worker():
            barrier.wait()
            ctx = contexts.get("openalex")
            with lock:
                collected.append(ctx)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(collected) == 4
        session_ids = {id(ctx.session) for ctx in collected}
        assert len(session_ids) == 4

    def test_close_all_closes_every_built_session_across_threads(self):
        contexts = ThreadLocalSourceContexts()
        built: list[SourceContext] = []
        lock = threading.Lock()

        def _worker():
            ctx = contexts.get("openalex")
            with lock:
                built.append(ctx)

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        closed = []
        for ctx in built:
            orig_close = ctx.session.close

            def _tracked(orig=orig_close, s=ctx.session):
                closed.append(id(s))
                orig()

            ctx.session.close = _tracked

        contexts.close_all()

        assert set(closed) == {id(ctx.session) for ctx in built}

    def test_close_all_with_no_contexts_built_is_a_no_op(self):
        contexts = ThreadLocalSourceContexts()
        contexts.close_all()  # must not raise

    def test_setitem_matches_get_duck_type_used_by_engine(self):
        """engine.py:_ctx_for() does contexts.get(name); if None,
        build_context() then contexts[name] = ctx. get() never returns
        None for a known source name on this class, so this path is dead
        in practice, but must still work for duck-type parity with the
        plain dict this class replaces.
        """
        contexts = ThreadLocalSourceContexts()
        from ref_checker.sources import openalex
        ctx = openalex.build_context()
        contexts["openalex"] = ctx
        assert contexts.get("openalex") is ctx

    def test_all_source_names_resolvable(self):
        contexts = ThreadLocalSourceContexts()
        for name in all_source_names():
            ctx = contexts.get(name)
            assert ctx is not None, f"{name} did not resolve to a context"
