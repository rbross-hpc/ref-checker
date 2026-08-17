"""Runtime registry of source modules and derived name lists.

Exported as **functions** rather than module-level constants so that
optional sources (currently: Primo) are evaluated at call time against the
live environment, not at import time.  This means:

- Tests can control which sources appear simply by setting or clearing the
  relevant env vars — no monkeypatching of module-level lists required.
- ``conftest.py``'s ``load_dotenv()`` runs before any test executes, so the
  right set of sources is seen by every call site without ordering surprises.
- Future optional sources follow the same pattern: add an ``is_enabled()``
  guard inside ``scholarly_sources()`` and nothing else needs to change.

``LIVENESS_SOURCES`` is unconditional today (both ``github`` and ``url`` are
always active), but is exposed as a function for API symmetry.

``ThreadLocalSourceContexts`` is unchanged in behaviour; its internals call
``sources_by_name()`` at access time rather than at construction time.
"""
from __future__ import annotations

import threading
from typing import Any

from . import arxiv, crossref, dblp, github, openalex, osti, primo, semanticscholar
from . import url as url_source
from .base import SourceContext

_ALWAYS_SCHOLARLY = [openalex, crossref, osti, dblp, semanticscholar, arxiv]
_LIVENESS = [github, url_source]


def scholarly_sources() -> list[Any]:
    """Return the ordered list of scholarly source modules for this run.

    Primo is prepended when ``primo.is_enabled()`` returns True (i.e. all
    three of ``PRIMO_BASE_URL``, ``PRIMO_VID``, and ``PRIMO_INST`` are set).
    Evaluated fresh on every call — no caching.
    """
    if primo.is_enabled():
        return [primo] + _ALWAYS_SCHOLARLY
    return list(_ALWAYS_SCHOLARLY)


def liveness_sources() -> list[Any]:
    """Return the list of liveness source modules."""
    return list(_LIVENESS)


def all_sources() -> list[Any]:
    """Return scholarly + liveness sources for this run."""
    return scholarly_sources() + liveness_sources()


def scholarly_source_names() -> list[str]:
    return [s.SOURCE_NAME for s in scholarly_sources()]


def all_source_names() -> list[str]:
    return [s.SOURCE_NAME for s in all_sources()]


def default_delays() -> dict[str, float]:
    """Return per-source default delays derived from each module's DEFAULT_DELAY."""
    return {s.SOURCE_NAME: s.DEFAULT_DELAY for s in all_sources()}


def sources_by_name() -> dict[str, Any]:
    return {s.SOURCE_NAME: s for s in all_sources()}


def build_all_contexts() -> dict[str, SourceContext]:
    """Build one :class:`SourceContext` per source (scholarly and liveness), once.

    Kept for direct/lookup-subcommand callers that don't need thread
    isolation (see ``cli/main.py:run_lookup()``, which builds a single
    throwaway context per invocation and never shares it across threads).
    ``check_references()`` uses :class:`ThreadLocalSourceContexts` instead —
    see its docstring for why a flat dict is unsafe across worker threads.
    """
    return {s.SOURCE_NAME: s.build_context() for s in all_sources()}


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
            src = sources_by_name().get(source_name)
            if src is None:
                return None
            ctx = src.build_context()
            by_name[source_name] = ctx
            with self._built_lock:
                self._built.append(ctx)
        return ctx

    def __setitem__(self, source_name: str, ctx: SourceContext) -> None:
        by_name = self._thread_dict()
        if source_name not in by_name:
            with self._built_lock:
                self._built.append(ctx)
        by_name[source_name] = ctx

    def close_all(self) -> None:
        """Close every session built by any thread during this run."""
        with self._built_lock:
            built = list(self._built)
        for ctx in built:
            try:
                ctx.session.close()
            except Exception:
                pass
