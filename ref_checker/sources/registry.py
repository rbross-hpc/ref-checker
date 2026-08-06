"""Static registry of source modules and derived name lists.

Extracted so both ``runtime.py`` (circuit breaker) and ``check.py``
(orchestration) can reference the same source-name lists without either
depending on the other.
"""
from __future__ import annotations

from . import arxiv, crossref, dblp, github, openalex, osti, semanticscholar
from . import url as url_source

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
