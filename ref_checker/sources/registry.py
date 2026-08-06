"""Static registry of source modules and derived name lists.

Extracted so both ``runtime.py`` (circuit breaker) and ``check.py``
(orchestration) can reference the same source-name lists without either
depending on the other.
"""
from __future__ import annotations

from . import arxiv, crossref, dblp, github, openalex, osti, semanticscholar
from . import url as url_source
from .base import SourceContext

SCHOLARLY_SOURCES = [openalex, crossref, osti, dblp, semanticscholar, arxiv]
LIVENESS_SOURCES = [github, url_source]

ALL_SOURCE_NAMES = [s.SOURCE_NAME for s in SCHOLARLY_SOURCES + LIVENESS_SOURCES]
SCHOLARLY_SOURCE_NAMES = [s.SOURCE_NAME for s in SCHOLARLY_SOURCES]

# Single source of truth for per-source rate-limit defaults, derived from
# each module's own DEFAULT_DELAY. engine.py, runner.py, and cli/main.py's
# --delay-<source> argparse defaults all import this instead of maintaining
# their own copies of the same dict.
DEFAULT_DELAYS: dict[str, float] = {
    s.SOURCE_NAME: s.DEFAULT_DELAY for s in SCHOLARLY_SOURCES + LIVENESS_SOURCES
}


def build_all_contexts() -> dict[str, SourceContext]:
    """Build one :class:`SourceContext` per scholarly source, once.

    Called once per ``check_references()`` run (see ``runner.py``) so every
    reference in that run reuses the same session per source — the actual
    point of ``SourceContext``. Liveness sources (``github``, ``url``) don't
    have contexts yet (Part 2 of the SourceContext work; see ``PLAN.md``).
    """
    return {s.SOURCE_NAME: s.build_context() for s in SCHOLARLY_SOURCES}
