"""Static registry of source modules and derived name lists.

Extracted so both ``runtime.py`` (circuit breaker) and ``check.py``
(orchestration) can reference the same source-name lists without either
depending on the other.

Primo is conditionally included: it is an opt-in institutional source that
requires ``PRIMO_BASE_URL``, ``PRIMO_VID``, and ``PRIMO_INST`` to be set.
When those env vars are present ``primo.is_enabled()`` returns True and the
source is prepended to ``SCHOLARLY_SOURCES`` (tried first, before OpenAlex).
When unconfigured, it is absent from every derived list and from the CLI.
"""
from __future__ import annotations

import threading

from . import arxiv, crossref, dblp, github, openalex, osti, primo, semanticscholar
from . import url as url_source
from .base import SourceContext

_PRIMO_SOURCES = [primo] if primo.is_enabled() else []
SCHOLARLY_SOURCES = _PRIMO_SOURCES + [openalex, crossref, osti, dblp, semanticscholar, arxiv]
LIVENESS_SOURCES = [github, url_source]
_ALL_SOURCES = SCHOLARLY_SOURCES + LIVENESS_SOURCES

ALL_SOURCE_NAMES = [s.SOURCE_NAME for s in _ALL_SOURCES]
SCHOLARLY_SOURCE_NAMES = [s.SOURCE_NAME for s in SCHOLARLY_SOURCES]

# Single source of truth for per-source rate-limit defaults, derived from
# each module's own DEFAULT_DELAY. engine.py, runner.py, and cli/main.py's
# --delay-<source> argparse defaults all import this instead of maintaining
# their own copies of the same dict.
DEFAULT_DELAYS: dict[str, float] = {s.SOURCE_NAME: s.DEFAULT_DELAY for s in _ALL_SOURCES}

_SOURCES_BY_NAME = {s.SOURCE_NAME: s for s in _ALL_SOURCES}


def build_all_contexts() -> dict[str, SourceContext]:
    """Build one :class:`SourceContext` per source (scholarly and liveness), once.

    Kept for direct/lookup-subcommand callers that don't need thread
    isolation (see ``cli/main.py:run_lookup()``, which builds a single
    throwaway context per invocation and never shares it across threads).
    ``check_references()`` uses :class:`ThreadLocalSourceContexts` instead —
    see its docstring for why a flat dict is unsafe across worker threads.
    """
    return {s.SOURCE_NAME: s.build_context() for s in _ALL_SOURCES}


class ThreadLocalSourceContexts:
    """A ``contexts`` registry, one :class:`SourceContext` per source **per
    thread**, for safe use across a :class:`concurrent.futures.ThreadPoolExecutor`.

    ``requests.Session`` is not documented as thread-safe: every request
    reads ``session.cookies`` to build the outgoing ``Cookie`` header and
    every response writes any ``Set-Cookie`` back into it
    (``requests.sessions.Session.send``), unsynchronized at the
    ``requests``-semantics level. ``check_references()`` previously built
    one flat ``dict[str, SourceContext]`` shared verbatim across every
    worker thread (see git history) — for any source whose responses can
    carry ``Set-Cookie`` (confirmed live for OSTI and github.com; also
    possible for the generic ``url`` liveness source, which follows
    arbitrary user-supplied URLs), two threads processing different
    references could race on the same session's cookie jar.

    Exposes the same duck-typed ``get(name)`` interface
    ``engine.py:_ctx_for()`` already expects from a plain dict, so no
    change is needed there — only *what* gets passed as ``contexts=``
    changes. Within one thread, calling ``get(name)`` repeatedly (across
    every reference dispatched to that thread) returns the *same*
    ``SourceContext`` — connection pooling is preserved per-thread, just no
    longer shared *across* threads.

    Every built session is tracked (under a lock, since multiple worker
    threads build their own contexts concurrently) so the run can close
    all of them deterministically at the end via :meth:`close_all`,
    regardless of how many threads ended up building a context for a given
    source.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._built_lock = threading.Lock()
        self._built: list[SourceContext] = []

    def _thread_dict(self) -> dict[str, SourceContext]:
        by_name = getattr(self._local, "contexts", None)
        if by_name is None:
            by_name = {}
            self._local.contexts = by_name
        return by_name

    def get(self, source_name: str) -> SourceContext | None:
        by_name = self._thread_dict()
        ctx = by_name.get(source_name)
        if ctx is None:
            src = _SOURCES_BY_NAME.get(source_name)
            if src is None:
                return None
            ctx = src.build_context()
            by_name[source_name] = ctx
            with self._built_lock:
                self._built.append(ctx)
        return ctx

    def __setitem__(self, source_name: str, ctx: SourceContext) -> None:
        # engine.py:_ctx_for() falls back to this after a build_context()
        # call when get() returned None — dead in practice for this class
        # (get() always builds and returns a context for a known source
        # name), but implemented for full duck-type parity with the plain
        # dict this class replaces.
        by_name = self._thread_dict()
        if source_name not in by_name:
            with self._built_lock:
                self._built.append(ctx)
        by_name[source_name] = ctx

    def close_all(self) -> None:
        """Close every session built by any thread during this run.

        Safe to call once, after every worker thread has finished (i.e.
        from the main thread after the pool has been shut down / joined) —
        session objects themselves are not used concurrently with this
        call at that point.
        """
        with self._built_lock:
            built = list(self._built)
        for ctx in built:
            try:
                ctx.session.close()
            except Exception:
                pass
