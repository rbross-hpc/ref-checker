"""Formal capability contract for source modules.

Source modules (``ref_checker/sources/*.py``) are plain modules, not
classes — these ``Protocol`` definitions exist for documentation and
one-time structural validation (see ``tests/test_source_contract.py``),
not for runtime dispatch. ``engine.py`` still calls source functions by
their conventional names (``get_by_doi``, ``get_by_arxiv_id``,
``search_by_title``, ``check_url``); what changes is that a source's
supported query kinds are now declared explicitly via
``SUPPORTED_QUERY_KINDS`` instead of discovered implicitly via
``getattr(module, name, None) is None``.

Every scholarly source module must declare:

- ``SOURCE_NAME``: the string used throughout ``per_source``, the sidecar,
  rate-limit config, and CLI flags.
- ``DEFAULT_DELAY``: the default per-call rate-limit delay in seconds
  (see ``ref_checker.sources.registry.DEFAULT_DELAYS``, which is derived
  from this field rather than hardcoded separately).
- ``SUPPORTED_QUERY_KINDS``: which of ``QueryKind.DOI`` /
  ``QueryKind.ARXIV_ID`` / ``QueryKind.TITLE`` the module implements a
  corresponding ``get_by_doi`` / ``get_by_arxiv_id`` / ``search_by_title``
  function for. Not every scholarly source supports every kind (e.g. DBLP
  is title-only), so this is plain metadata rather than a method the
  Protocol itself requires — a ``Protocol`` can't cleanly express
  "optionally present."

Liveness sources (``github``, ``url``) implement ``check_url`` instead and
have no ``SUPPORTED_QUERY_KINDS``.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model import QueryKind

__all__ = ["ScholarlySource", "LivenessSource", "FN_BY_KIND"]


@runtime_checkable
class ScholarlySource(Protocol):
    SOURCE_NAME: str
    DEFAULT_DELAY: float
    SUPPORTED_QUERY_KINDS: frozenset[QueryKind]


@runtime_checkable
class LivenessSource(Protocol):
    SOURCE_NAME: str
    DEFAULT_DELAY: float

    def check_url(
        self, urls: str
    ) -> tuple[dict | None, float | None, list[tuple[str, str]]]: ...


# The conventional function name each QueryKind maps to on a ScholarlySource.
# Single source of truth for engine.py's dispatch and cli/main.py's `lookup`
# subcommand, so the two can't independently drift on this mapping.
FN_BY_KIND: dict[QueryKind, str] = {
    QueryKind.DOI: "get_by_doi",
    QueryKind.ARXIV_ID: "get_by_arxiv_id",
    QueryKind.TITLE: "search_by_title",
}
