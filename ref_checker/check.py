"""Backward-compatible re-export shim.

``check.py`` used to contain the entire orchestration pipeline. It has been
split into focused modules:

- ``engine.py``  — ``lookup_reference``: assess one reference against all
  sources.
- ``runner.py``  — ``check_references``: thread pool, resume/sidecar I/O,
  signal handling, end-of-run reporting.
- ``runtime.py`` — ``_Shutdown``, ``SourceHealth``, ``_RateLimiter``,
  ``_retry``: shared runtime primitives.
- ``planner.py`` — ``_plan_ref_work``: smart-rerun source selection.
- ``sources/registry.py`` — static source-module lists and derived
  name lists.

This module re-exports every name that used to live here so existing
callers (``cli/main.py``) and the test suite's extensive ``check.<name>``
usage keep working unchanged.
"""
from __future__ import annotations

from .engine import lookup_reference
from .planner import _plan_ref_work
from .runner import check_references
from .runtime import (
    SourceHealth,
    _QUOTA_EXHAUSTED_THRESHOLD,
    _RateLimiter,
    _Shutdown,
    _format_duration,
    _retry,
)
from .sources.registry import ALL_SOURCE_NAMES as _ALL_SOURCE_NAMES
from .sources.registry import SCHOLARLY_SOURCES as _SCHOLARLY_SOURCES
from .sources.registry import SCHOLARLY_SOURCE_NAMES as _SCHOLARLY_SOURCE_NAMES

__all__ = [
    "SourceHealth",
    "check_references",
    "lookup_reference",
    # Re-exported for backward compatibility: existing callers/tests reach
    # into these as check.<name> even though they now live in engine.py,
    # runner.py, runtime.py, planner.py, or sources/registry.py.
    "_ALL_SOURCE_NAMES",
    "_QUOTA_EXHAUSTED_THRESHOLD",
    "_RateLimiter",
    "_SCHOLARLY_SOURCE_NAMES",
    "_SCHOLARLY_SOURCES",
    "_Shutdown",
    "_format_duration",
    "_plan_ref_work",
    "_retry",
]
