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
