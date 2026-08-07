"""Resume / smart-rerun planning: decide which sources to (re)query for a
reference given its prior sidecar state.

Originally extracted from ``check.py`` (which re-exports ``_plan_ref_work``
for backward compatibility with existing callers/tests) as part of splitting
the orchestration module into focused subsystems.
"""
from __future__ import annotations

from .model import OutcomeKind
from .results import LookupResult
from .sources.registry import ALL_SOURCE_NAMES


def _plan_ref_work(
    prior_result: LookupResult | None,
    prior_status: str | None,
    retry_closest: bool,
    retry_errored: bool,
) -> set[str] | None:
    """Return the set of source names to (re)query for this ref.

    Returns:
      - None when the ref is fully satisfied — replay from sidecar, no work.
      - A set (possibly empty) of source names to query. An empty set means
        the ref is not satisfied but no untried sources remain — the driver
        will keep the prior result and log accordingly.
    """
    if prior_result is None or prior_status is None:
        return set(ALL_SOURCE_NAMES)

    if prior_status == "OK":
        if prior_result.exhausted_sources:
            pass
        elif prior_result.dead_urls:
            pass
        else:
            return None

    if prior_status == "CLOSEST" and not retry_closest:
        return None

    targets: set[str] = set()
    for src in ALL_SOURCE_NAMES:
        entry = prior_result.per_source.get(src)
        if entry is None:
            targets.add(src)
            continue
        st = entry.outcome
        if st == OutcomeKind.DISABLED:
            targets.add(src)
        elif st == OutcomeKind.SKIPPED:
            # A skipped source was never actually attempted (e.g. the run
            # was interrupted before its turn) — always retry regardless of
            # retry_errored, since there is no "already tried and failed"
            # signal to respect that flag for.
            targets.add(src)
        elif st in (OutcomeKind.ERROR, OutcomeKind.RATE_LIMITED) and retry_errored:
            targets.add(src)
    return targets
