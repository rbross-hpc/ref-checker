"""Multi-source reference lookup driver."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .errors import RateLimited
from .extract import Reference
from .format import format_result
from .results import LookupResult, _Stats
from . import sidecar as _sidecar
from .sources import arxiv, crossref, dblp, github, openalex, osti, semanticscholar, url as url_source

_SCHOLARLY_SOURCES = [openalex, crossref, osti, dblp, semanticscholar, arxiv]
_LIVENESS_SOURCES = [github, url_source]
_ALL_SOURCE_NAMES = [s.SOURCE_NAME for s in _SCHOLARLY_SOURCES + _LIVENESS_SOURCES]
_SCHOLARLY_SOURCE_NAMES = [s.SOURCE_NAME for s in _SCHOLARLY_SOURCES]

_DEFAULT_DELAYS: dict[str, float] = {
    "openalex": 2.0,
    "crossref": 2.0,
    "osti": 2.0,
    "dblp": 1.0,
    "semanticscholar": 8.0,
    "arxiv": 3.0,
    "github": 1.0,
    "url": 1.0,
}

_RETRY_BACKOFF = (5.0, 10.0, 15.0)

_MAX_RETRY_AFTER = 60.0

_QUOTA_EXHAUSTED_THRESHOLD = 300.0

_WAIT_VISIBILITY_THRESHOLD = 10.0

_SS_UNAUTH_DELAY = 12.0

_ALL_DISABLED_SENTINEL = "all_scholarly_sources_disabled"


def _format_duration(secs: float) -> str:
    """Adaptive human-friendly duration: 12.3s / 3m 42s / 4h 11m."""
    if secs < 0:
        secs = 0.0
    if secs < 60.0:
        return f"{secs:.1f}s"
    if secs < 3600.0:
        m = int(secs // 60)
        s = int(secs - m * 60)
        return f"{m}m {s:02d}s"
    h = int(secs // 3600)
    m = int((secs - h * 3600) // 60)
    return f"{h}h {m:02d}m"


class _Shutdown:
    """Cooperative shutdown coordinator with two-stage Ctrl-C support.

    Stage 1: first request — sets the event; callers observe .requested()
             and stop after their current unit of work.
    Stage 2: second request — the SIGINT handler restores the previous
             handler and raises KeyboardInterrupt from wherever we are.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._stage = 0

    def request(self) -> int:
        self._stage += 1
        self._event.set()
        return self._stage

    def requested(self) -> bool:
        return self._event.is_set()

    def stage(self) -> int:
        return self._stage

    def wait(self, timeout: float) -> bool:
        """Sleep up to *timeout* seconds; return True if shutdown requested."""
        if timeout <= 0:
            return self._event.is_set()
        return self._event.wait(timeout)


class SourceHealth:
    """Session-scoped circuit breaker for scholarly sources.

    Two independent counters per source:

    - Regular error counter: incremented on ``error``, tripped at
      ``THRESHOLD`` consecutive errors. Reset by any ``hit_*`` /
      ``not_found`` / ``rate_limited`` outcome (real errors are more
      definitive than transient rate limits).
    - Rate-limit counter: incremented on ``rate_limited`` (i.e., a full
      retry cycle where every attempt was a :class:`~ref_checker.errors.RateLimited`),
      tripped at ``RATE_LIMIT_THRESHOLD`` consecutive rate-limit exhaustions.
      Reset by any ``hit_*`` / ``not_found`` outcome.

    Also tracks whether we've already logged the first-429 diagnostic for
    a source in this session (see :meth:`should_log_first_rate_limit`).
    """

    THRESHOLD = 3
    RATE_LIMIT_THRESHOLD = 3

    def __init__(
        self,
        threshold: int = THRESHOLD,
        rate_limit_threshold: int = RATE_LIMIT_THRESHOLD,
        stats: _Stats | None = None,
    ) -> None:
        self._threshold = threshold
        self._rate_limit_threshold = rate_limit_threshold
        self._consecutive: dict[str, int] = {}
        self._consecutive_rate_limit: dict[str, int] = {}
        self._disabled: set[str] = set()
        self._first_rate_limit_logged: set[str] = set()
        self._stats = stats
        self._lock = threading.Lock()

    def record(self, source: str, status: str) -> None:
        newly_disabled = False
        disable_reason = ""
        with self._lock:
            if status in ("hit_id", "hit_title", "not_found"):
                self._consecutive[source] = 0
                self._consecutive_rate_limit[source] = 0
                return
            if status == "rate_limited":
                self._consecutive[source] = 0
                self._consecutive_rate_limit[source] = (
                    self._consecutive_rate_limit.get(source, 0) + 1
                )
                if (
                    self._consecutive_rate_limit[source] >= self._rate_limit_threshold
                    and source not in self._disabled
                ):
                    self._disabled.add(source)
                    newly_disabled = True
                    disable_reason = (
                        f"{self._rate_limit_threshold} consecutive rate-limit "
                        f"exhaustions (source appears systematically throttled)"
                    )
                if not newly_disabled:
                    return
            elif status == "error":
                self._consecutive_rate_limit[source] = 0
                self._consecutive[source] = self._consecutive.get(source, 0) + 1
                if (
                    self._consecutive[source] >= self._threshold
                    and source not in self._disabled
                ):
                    self._disabled.add(source)
                    newly_disabled = True
                    disable_reason = f"{self._threshold} consecutive errors"
                if not newly_disabled:
                    return
        if newly_disabled:
            if self._stats is not None:
                self._stats.record_disabled(source, disable_reason)
            print(
                f"[ref-checker] source '{source}' disabled: {disable_reason}",
                file=sys.stderr,
            )

    def should_log_first_rate_limit(self, source: str) -> bool:
        """Return True the first time called per source; False thereafter."""
        with self._lock:
            if source in self._first_rate_limit_logged:
                return False
            self._first_rate_limit_logged.add(source)
            return True

    def is_disabled(self, source: str) -> bool:
        with self._lock:
            return source in self._disabled

    def disable(self, source: str, reason: str) -> None:
        """Force-disable *source* immediately, bypassing the 3-strike counter.

        Idempotent — no-op if already disabled. Used for definitive signals
        like a server-issued long Retry-After indicating quota exhaustion.
        """
        newly_disabled = False
        with self._lock:
            if source not in self._disabled:
                self._disabled.add(source)
                newly_disabled = True
        if newly_disabled:
            if self._stats is not None:
                self._stats.record_disabled(source, reason)
            print(
                f"[ref-checker] source '{source}' disabled: {reason}",
                file=sys.stderr,
            )

    def all_scholarly_disabled(self) -> bool:
        with self._lock:
            return all(name in self._disabled for name in _SCHOLARLY_SOURCE_NAMES)


class _RateLimiter:
    """Reservation-style per-source rate limiter.

    Under contention (multiple worker threads querying the same source), wait()
    atomically computes the next available slot and reserves it under a lock
    before sleeping. This guarantees strict per-source spacing regardless of
    concurrency: N threads calling OpenAlex will be spaced exactly `delay`
    seconds apart, deterministically.
    """

    def __init__(self, delays: dict[str, float], shutdown: _Shutdown | None = None) -> None:
        self._delays = delays
        self._last: dict[str, float] = {}
        self._shutdown = shutdown
        self._lock = threading.Lock()

    def wait(self, source_name: str) -> None:
        with self._lock:
            delay = self._delays.get(source_name, 1.0)
            last = self._last.get(source_name)
            now = time.monotonic()
            if last is None:
                # First call to this source — no wait, reserve now.
                self._last[source_name] = now
                sleep_for = 0.0
            else:
                next_slot = max(now, last + delay)
                self._last[source_name] = next_slot
                sleep_for = next_slot - now
        if sleep_for <= 0:
            return
        if self._shutdown is not None:
            self._shutdown.wait(sleep_for)
        else:
            time.sleep(sleep_for)

    def mark(self, source_name: str) -> None:
        # No-op: reservation is done in wait(). Kept for API compatibility.
        pass


def _retry(
    fn,
    tries: int = 3,
    stats: _Stats | None = None,
    source: str = "",
    mode: str = "",
    on_exhausted: Callable[[], None] | None = None,
    shutdown: _Shutdown | None = None,
    health: "SourceHealth | None" = None,
):
    """Call *fn* with retries; return (result_or_None, cause, retry_after).

    ``mode`` is passed to the stats recorder so per-source query/retry/
    exhaustion counts can be broken down by lookup mode
    (``"doi"`` / ``"arxiv_id"`` / ``"title"`` / ``"url"``).

    ``health`` (when supplied) is used to emit a one-time ``first 429 seen``
    diagnostic per source in the session.

    ``result_or_None`` is whatever *fn* returned on success (a tuple, per the
    source contract), or ``None`` on exhaustion / shutdown.

    ``cause`` is one of:
      - ``None`` on success or shutdown-before-attempt.
      - ``"error"`` on exhaustion where at least one attempt failed with a
        non-rate-limit exception.
      - ``"rate_limit"`` on exhaustion where *every* failed attempt was a
        :class:`RateLimited`. Callers count this toward the rate-limit
        counter of the circuit breaker (via ``health.record(src, "rate_limited")``).
      - ``"quota_exhausted"`` when a single :class:`RateLimited` carried a
        ``retry_after`` larger than :data:`_QUOTA_EXHAUSTED_THRESHOLD`. No
        further attempts are made — the caller should immediately disable
        the source for the session.

    ``retry_after`` is the server-supplied Retry-After value (seconds) that
    triggered ``"quota_exhausted"``; ``None`` otherwise.

    ``RateLimited.retry_after`` (when present, capped at ``_MAX_RETRY_AFTER``)
    supersedes the default ``_RETRY_BACKOFF`` schedule for in-threshold waits.
    """
    last_exc: Exception | None = None
    all_rate_limited = True
    any_attempted = False

    def _do_wait(wait: float, reason: str, is_rate_limit: bool) -> bool:
        """Sleep for *wait* seconds; return True if shutdown requested.

        Rate-limit waits always print (however brief); generic-backoff waits
        only print when >= _WAIT_VISIBILITY_THRESHOLD.
        """
        if is_rate_limit or wait >= _WAIT_VISIBILITY_THRESHOLD:
            print(
                f"[ref-checker] {source}: waiting {_format_duration(wait)} "
                f"before retry ({reason})",
                file=sys.stderr,
            )
        if shutdown is not None:
            return shutdown.wait(wait)
        time.sleep(wait)
        return False

    def _log_first_rate_limit(exc: RateLimited) -> None:
        if health is None or not source:
            return
        if not health.should_log_first_rate_limit(source):
            return
        if exc.retry_after is None:
            ra = "<none>"
        else:
            ra = f"{exc.retry_after:.0f}s"
        print(
            f"[ref-checker] {source}: first 429 seen (Retry-After={ra})",
            file=sys.stderr,
        )

    for attempt in range(tries):
        if shutdown is not None and shutdown.requested():
            return None, None, None
        if health is not None and source and health.is_disabled(source):
            return None, None, None
        any_attempted = True
        try:
            return fn(), None, None
        except RateLimited as exc:
            last_exc = exc
            _log_first_rate_limit(exc)
            if (
                exc.retry_after is not None
                and exc.retry_after > _QUOTA_EXHAUSTED_THRESHOLD
            ):
                if stats and source:
                    stats.record_exhausted(source, mode)
                if on_exhausted:
                    on_exhausted()
                print(
                    f"[ref-checker] WARNING: {source} reports quota exhausted "
                    f"(Retry-After={exc.retry_after:.0f}s, "
                    f"~{_format_duration(exc.retry_after)}) — abandoning source",
                    file=sys.stderr,
                )
                return None, "quota_exhausted", exc.retry_after
            if attempt < tries - 1:
                if exc.retry_after is not None:
                    raw = exc.retry_after
                    wait = min(raw, _MAX_RETRY_AFTER)
                    if raw > _MAX_RETRY_AFTER:
                        reason = (
                            f"Retry-After={raw:.0f}s, "
                            f"capped at {_MAX_RETRY_AFTER:.0f}s"
                        )
                    else:
                        reason = f"Retry-After={raw:.0f}s"
                else:
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    reason = "Retry-After=<none>, backoff"
                if stats and source:
                    stats.record_retry(source, mode)
                if _do_wait(wait, reason, is_rate_limit=True):
                    return None, None, None
        except Exception as exc:
            last_exc = exc
            all_rate_limited = False
            if attempt < tries - 1:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                if stats and source:
                    stats.record_retry(source, mode)
                if _do_wait(wait, "backoff", is_rate_limit=False):
                    return None, None, None
    if not any_attempted:
        return None, None, None
    if stats and source:
        stats.record_exhausted(source, mode)
    if on_exhausted:
        on_exhausted()
    cause = "rate_limit" if all_rate_limited else "error"
    kind = "rate-limit retries" if cause == "rate_limit" else "retries"
    print(
        f"[ref-checker] WARNING: all {tries} {kind} exhausted for {source}"
        + (f": {last_exc}" if last_exc else ""),
        file=sys.stderr,
    )
    return None, cause, None


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
        return set(_ALL_SOURCE_NAMES)

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
    for src in _ALL_SOURCE_NAMES:
        entry = prior_result.per_source.get(src)
        if entry is None:
            targets.add(src)
            continue
        st = entry.get("status")
        if st == "disabled":
            targets.add(src)
        elif st == "error" and retry_errored:
            targets.add(src)
    return targets


def lookup_reference(
    ref: Reference,
    delays: dict[str, float] | None = None,
    min_match: float = 0.80,
    stats: _Stats | None = None,
    rate_limiter: _RateLimiter | None = None,
    health: SourceHealth | None = None,
    shutdown: _Shutdown | None = None,
    sources_to_query: set[str] | None = None,
    prior_result: LookupResult | None = None,
) -> LookupResult:
    """Run multi-source lookup for a single reference.

    When *prior_result* is supplied its per_source entries seed the returned
    result; only sources named in *sources_to_query* are actually queried,
    and everything else is preserved from prior_result.
    """
    rl = rate_limiter if rate_limiter is not None else _RateLimiter(delays or _DEFAULT_DELAYS)
    health = health if health is not None else SourceHealth(stats=stats)

    if prior_result is not None:
        result = LookupResult(
            doi_attempted=ref.doi,
            arxiv_attempted=ref.arxiv_id,
            per_source=dict(prior_result.per_source),
            dead_urls=list(prior_result.dead_urls),
        )
    else:
        result = LookupResult(
            doi_attempted=ref.doi,
            arxiv_attempted=ref.arxiv_id,
        )

    def _should_query(src_name: str) -> bool:
        if sources_to_query is None:
            return True
        return src_name in sources_to_query

    def call(src, fn_name: str, queried_by: str, *args) -> tuple[dict | None, float | None]:
        src_name = src.SOURCE_NAME
        if health.is_disabled(src_name):
            result.record_source(src_name, "disabled", queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        fn = getattr(src, fn_name, None)
        if fn is None:
            return None, None
        rl.wait(src_name)
        # Re-check after rl.wait — another worker may have disabled the source
        # while we were waiting on the per-source rate limiter.
        if health.is_disabled(src_name):
            result.record_source(src_name, "disabled", queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        if shutdown is not None and shutdown.requested():
            result.record_source(src_name, "skipped", queried_by=queried_by,
                                 note="aborted by user")
            return None, None
        if stats:
            stats.record_query(src_name, queried_by)
        errored = {"flag": False}

        def _on_exhausted() -> None:
            errored["flag"] = True

        out, cause, retry_after = _retry(
            lambda: fn(*args),
            stats=stats,
            source=src_name,
            mode=queried_by,
            on_exhausted=_on_exhausted,
            shutdown=shutdown,
            health=health,
        )
        rl.mark(src_name)
        if errored["flag"]:
            if cause == "quota_exhausted":
                note = (
                    f"quota exhausted (Retry-After={retry_after:.0f}s, "
                    f"~{_format_duration(retry_after or 0.0)})"
                )
                result.record_source(src_name, "error", queried_by=queried_by,
                                     note=note)
                reason = (
                    f"server requested Retry-After={retry_after:.0f}s "
                    f"(~{_format_duration(retry_after or 0.0)}) — quota exhausted"
                )
                health.disable(src_name, reason)
                return None, None
            note = ("rate-limit retries exhausted"
                    if cause == "rate_limit" else "retries exhausted")
            result.record_source(src_name, "error", queried_by=queried_by,
                                 note=note)
            # Rate-limit exhaustion advances the rate-limit counter (which
            # can independently disable the source at RATE_LIMIT_THRESHOLD);
            # real errors advance the regular error counter.
            health.record(src_name, "rate_limited" if cause == "rate_limit" else "error")
            return None, None
        if out is None:
            # Shutdown-before-attempt path.
            return None, None

        summary, sim = out
        if summary is None:
            result.record_source(src_name, "not_found", queried_by=queried_by)
            health.record(src_name, "not_found")
            return None, None

        status = "hit_id" if queried_by in ("doi", "arxiv_id") else "hit_title"
        result.record_source(src_name, status, queried_by=queried_by,
                             score=sim, summary=summary)
        health.record(src_name, status)
        return summary, sim

    def call_liveness(src, urls: str, queried_by: str) -> tuple[dict | None, float | None]:
        src_name = src.SOURCE_NAME
        if health.is_disabled(src_name):
            result.record_source(src_name, "disabled", queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        rl.wait(src_name)
        # Re-check after rl.wait — another worker may have disabled the source
        # while we were waiting on the per-source rate limiter.
        if health.is_disabled(src_name):
            result.record_source(src_name, "disabled", queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        if shutdown is not None and shutdown.requested():
            result.record_source(src_name, "skipped", queried_by=queried_by,
                                 note="aborted by user")
            return None, None
        if stats:
            stats.record_query(src_name, queried_by)
        errored = {"flag": False}

        def _on_exhausted() -> None:
            errored["flag"] = True

        try:
            out, cause, retry_after = _retry(
                lambda: src.check_url(urls),
                stats=stats,
                source=src_name,
                mode=queried_by,
                on_exhausted=_on_exhausted,
                shutdown=shutdown,
                health=health,
            )
        except Exception:
            out, cause, retry_after = None, "error", None
        rl.mark(src_name)
        if errored["flag"]:
            if cause == "quota_exhausted":
                note = (
                    f"quota exhausted (Retry-After={retry_after:.0f}s, "
                    f"~{_format_duration(retry_after or 0.0)})"
                )
                result.record_source(src_name, "error", queried_by=queried_by,
                                     note=note)
                reason = (
                    f"server requested Retry-After={retry_after:.0f}s "
                    f"(~{_format_duration(retry_after or 0.0)}) — quota exhausted"
                )
                health.disable(src_name, reason)
                return None, None
            note = ("rate-limit retries exhausted"
                    if cause == "rate_limit" else "retries exhausted")
            result.record_source(src_name, "error", queried_by=queried_by,
                                 note=note)
            health.record(src_name, "rate_limited" if cause == "rate_limit" else "error")
            return None, None
        if out is None:
            return None, None

        summary, sim, dead = out if len(out) == 3 else (*out, [])
        for d in dead:
            if d not in result.dead_urls:
                result.dead_urls.append(d)
        if summary is None:
            result.record_source(src_name, "not_found", queried_by=queried_by,
                                 note=(f"dead urls: {len(dead)}" if dead else None))
            health.record(src_name, "not_found")
            return None, None

        result.record_source(src_name, "hit_id", queried_by=queried_by,
                             score=1.0, summary=summary)
        health.record(src_name, "hit_id")
        return summary, sim

    def _stopped() -> bool:
        return shutdown is not None and shutdown.requested()

    # --- GitHub liveness first (if the ref has a GitHub URL) ---
    if ref.github_url and _should_query(github.SOURCE_NAME) and not _stopped():
        summary, _ = call_liveness(github, ref.github_url, "url")
        if summary:
            result.recompute_best(ref, min_match)
            return result

    # --- arXiv ID lookup (before scholarly loop) ---
    def _id_confirmed() -> bool:
        return any(
            e.get("status") == "hit_id"
            for e in result.per_source.values()
        )

    if ref.arxiv_id and not _id_confirmed() and _should_query(arxiv.SOURCE_NAME) and not _stopped():
        call(arxiv, "get_by_arxiv_id", "arxiv_id", ref.arxiv_id)
        if _id_confirmed():
            result.recompute_best(ref, min_match)
            return result

    # --- Scholarly loop: skip entirely for pure URL/dataset refs ---
    url_only = (
        not ref.doi
        and not ref.arxiv_id
        and not ref.venue
        and (ref.url or ref.github_url)
    )

    scholarly_iter = _SCHOLARLY_SOURCES if not url_only else []

    for src in scholarly_iter:
        if _id_confirmed():
            break
        if _stopped():
            break
        src_name = src.SOURCE_NAME
        if not _should_query(src_name):
            continue

        if ref.doi:
            call(src, "get_by_doi", "doi", ref.doi)

        if not _id_confirmed() and ref.arxiv_id and src_name != arxiv.SOURCE_NAME:
            call(src, "get_by_arxiv_id", "arxiv_id", ref.arxiv_id)

        if not _id_confirmed() and ref.title:
            # Only title-search if we don't already have a strong title hit ≥ 0.90
            best_title = 0.0
            for entry in result.per_source.values():
                if entry.get("status") == "hit_title" and entry.get("score") is not None:
                    if entry["score"] > best_title:
                        best_title = entry["score"]
            if best_title < 0.90:
                call(src, "search_by_title", "title", ref.title)

    # --- Generic URL liveness fallback ---
    result.recompute_best(ref, min_match)
    current_score = result.display_score if result.display_score is not None else 0.0
    if (
        not result.id_confirmed
        and current_score < min_match
        and ref.url
        and not ref.github_url
        and _should_query(url_source.SOURCE_NAME)
        and not _stopped()
    ):
        call_liveness(url_source, ref.url, "url")

    result.recompute_best(ref, min_match)
    return result


def check_references(
    refs: list[Reference],
    delays: dict[str, float] | None = None,
    min_match: float = 0.80,
    sidecar: Path | None = None,
    resume: bool = False,
    retry_all: bool = False,
    retry_closest: bool = False,
    retry_errored: bool = True,
    source_error_threshold: int = SourceHealth.THRESHOLD,
    pdf_name: str = "",
    with_osti_id: bool = False,
    jobs: int = 3,
) -> str | None:
    """Look up every reference and print a human-readable summary.

    When *jobs* > 1, refs are queried concurrently via a thread pool while
    per-source rate limits are preserved via strict reservation. Formatted
    result blocks are buffered and emitted to stdout in ref-index order at
    end-of-run so the report is deterministic regardless of completion order.
    Progress and warnings stream to stderr live.

    Returns:
      - None on normal completion
      - "keyboard_interrupt" if shutdown was requested via signal
      - "all_scholarly_sources_disabled" if the circuit breaker tripped everything
    """
    if jobs < 1:
        jobs = 1

    t0 = time.monotonic()
    stats = _Stats()
    shutdown = _Shutdown()
    health = SourceHealth(threshold=source_error_threshold, stats=stats)
    effective_delays = dict(delays or _DEFAULT_DELAYS)
    # SS unauth tier is severely rate-limited. If no API key is configured,
    # spread requests further apart to reduce 429 pressure. Preserve
    # explicitly-zeroed delays (test fixtures) — only override when the
    # base delay is a normal positive value below _SS_UNAUTH_DELAY.
    if not os.environ.get("SEMANTICSCHOLAR_API_KEY", "").strip():
        current = effective_delays.get("semanticscholar", 8.0)
        if 0.0 < current < _SS_UNAUTH_DELAY:
            effective_delays["semanticscholar"] = _SS_UNAUTH_DELAY
    rl = _RateLimiter(effective_delays, shutdown=shutdown)

    all_results: dict[int, LookupResult] = {}
    prior_entries: dict[int, dict] = {}
    cached_count = 0
    reason: str | None = None

    if sidecar is not None and resume and sidecar.exists():
        prior_entries, hash_ok = _sidecar.load(sidecar, refs)
        if not hash_ok:
            print(
                "[ref-checker] WARNING: sidecar refs_hash mismatch or version — "
                "references may have changed; ignoring sidecar and running fresh.",
                file=sys.stderr,
            )
            prior_entries = {}

    # Seed all_results with prior data so a mid-run flush preserves everything.
    for idx, entry in prior_entries.items():
        prior_result_dict = entry.get("result")
        if prior_result_dict is not None:
            all_results[idx] = _sidecar.result_from_dict(prior_result_dict)

    # Plan phase: partition refs into cached / no-untried / to-query.
    plans: dict[int, set[str] | None] = {}
    to_query: list[Reference] = []
    for ref in refs:
        prior_entry = prior_entries.get(ref.index)
        prior_result_dict = prior_entry.get("result") if prior_entry is not None else None
        prior_result = all_results.get(ref.index)
        prior_status = prior_result_dict.get("status") if prior_result_dict else None

        if retry_all:
            plan: set[str] | None = set(_ALL_SOURCE_NAMES)
        else:
            plan = _plan_ref_work(
                prior_result, prior_status,
                retry_closest=retry_closest,
                retry_errored=retry_errored,
            )

        plans[ref.index] = plan

        if plan is None:
            # Fully satisfied — nothing to do.
            cached_count += 1
        elif plan == set() and prior_result is not None:
            # Not satisfied, but no untried sources — keep prior.
            cached_count += 1
        else:
            to_query.append(ref)

    prev_sigint = signal.getsignal(signal.SIGINT)

    def _sigint_handler(signum, frame):
        stage = shutdown.request()
        if stage == 1:
            print(
                "[ref-checker] Shutting down — finishing in-flight refs and flushing "
                "sidecar. Press Ctrl-C again to abort immediately.",
                file=sys.stderr,
            )
        else:
            signal.signal(signal.SIGINT, prev_sigint)
            raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, OSError):
        # Not on main thread — signals unavailable. Continue without handler.
        prev_sigint = None

    total = len(refs)
    sidecar_lock = threading.Lock()

    def _write_sidecar() -> None:
        if sidecar is None:
            return
        with sidecar_lock:
            try:
                _sidecar.write(sidecar, pdf_name, refs, all_results, min_match)
            except Exception as exc:
                print(
                    f"[ref-checker] WARNING: sidecar write failed: {exc}",
                    file=sys.stderr,
                )

    def _worker(ref: Reference) -> tuple[Reference, LookupResult, float]:
        started = time.monotonic()
        prior_result = all_results.get(ref.index)
        result = lookup_reference(
            ref,
            delays=delays,
            min_match=min_match,
            stats=stats,
            rate_limiter=rl,
            health=health,
            shutdown=shutdown,
            sources_to_query=plans[ref.index],
            prior_result=prior_result,
        )
        elapsed = time.monotonic() - started
        return ref, result, elapsed

    print(
        f"[ref-checker] Planning: {cached_count} cached, {len(to_query)} to query "
        f"(jobs={jobs})",
        file=sys.stderr,
    )
    print(
        f"[ref-checker] Concurrency: {jobs} worker(s)",
        file=sys.stderr,
    )

    try:
        if jobs == 1 or len(to_query) <= 1:
            # Sequential path — behaviorally identical to pre-concurrency for
            # jobs=1 and tests that pin jobs for determinism.
            for i, ref in enumerate(to_query, start=1):
                if shutdown.requested():
                    reason = "keyboard_interrupt"
                    break
                try:
                    _, result, elapsed = _worker(ref)
                except KeyboardInterrupt:
                    reason = "keyboard_interrupt"
                    break
                all_results[ref.index] = result
                print(
                    f"[ref-checker] completed ref #{ref.index} "
                    f"({i}/{len(to_query)}, {elapsed:.1f}s)",
                    file=sys.stderr,
                )
                _write_sidecar()
                if health.all_scholarly_disabled():
                    print(
                        "[ref-checker] All scholarly sources are disabled — cannot "
                        "continue. Flushing sidecar and exiting.",
                        file=sys.stderr,
                    )
                    reason = _ALL_DISABLED_SENTINEL
                    break
                if shutdown.requested():
                    reason = "keyboard_interrupt"
                    break
        else:
            # Concurrent path with bounded submission window of jobs+1.
            # Submit only a small window of refs upfront, then submit exactly
            # one more each time a future completes. This keeps the log
            # readable (started/completed lines interleave naturally), avoids
            # queueing hundreds of futures that would need to be cancelled on
            # SIGINT, and lets the circuit breaker actually prevent doomed
            # queries from being dispatched.
            window = jobs + 1
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures: dict = {}
                submitted = 0
                total_q = len(to_query)

                def _submit_next() -> None:
                    nonlocal submitted
                    if submitted >= total_q:
                        return
                    if shutdown.requested():
                        return
                    ref = to_query[submitted]
                    submitted += 1
                    fut = pool.submit(_worker, ref)
                    futures[fut] = ref

                for _ in range(min(window, total_q)):
                    _submit_next()

                completed = 0
                try:
                    while futures:
                        done_iter = as_completed(list(futures))
                        try:
                            fut = next(done_iter)
                        except StopIteration:
                            break
                        if shutdown.requested():
                            reason = "keyboard_interrupt"
                            for f in list(futures):
                                if not f.running() and not f.done():
                                    f.cancel()
                            break
                        try:
                            ref, result, elapsed = fut.result()
                        except KeyboardInterrupt:
                            reason = "keyboard_interrupt"
                            break
                        except Exception as exc:
                            ref = futures[fut]
                            print(
                                f"[ref-checker] WARNING: ref #{ref.index} lookup crashed: {exc}",
                                file=sys.stderr,
                            )
                            result = LookupResult(
                                doi_attempted=ref.doi,
                                arxiv_attempted=ref.arxiv_id,
                            )
                            elapsed = 0.0
                        all_results[ref.index] = result
                        completed += 1
                        futures.pop(fut, None)
                        print(
                            f"[ref-checker] completed ref #{ref.index} "
                            f"({completed}/{total_q}, {elapsed:.1f}s)",
                            file=sys.stderr,
                        )
                        _write_sidecar()
                        if health.all_scholarly_disabled():
                            print(
                                "[ref-checker] All scholarly sources are disabled — "
                                "cannot continue. Cancelling pending refs, waiting "
                                "for in-flight to finish, then flushing sidecar.",
                                file=sys.stderr,
                            )
                            reason = _ALL_DISABLED_SENTINEL
                            for f in list(futures):
                                if not f.running() and not f.done():
                                    f.cancel()
                            break
                        _submit_next()
                finally:
                    # Drain any in-flight futures that raced past the break so
                    # their results also make it into all_results.
                    for fut, ref in list(futures.items()):
                        if fut.done() and ref.index not in all_results:
                            try:
                                _, result, _ = fut.result()
                                all_results[ref.index] = result
                            except Exception:
                                pass
    except KeyboardInterrupt:
        reason = "keyboard_interrupt"
    finally:
        if prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, prev_sigint)
            except (ValueError, OSError):
                pass
        # Final sidecar flush (belt-and-suspenders).
        _write_sidecar()

        # Emit phase: print every ref in index order, once. Swallow
        # BrokenPipeError so downstream pipes (`| tee`, `| head`) closing
        # mid-emit doesn't produce noisy tracebacks.
        try:
            for ref in refs:
                result = all_results.get(ref.index)
                if result is None:
                    continue
                print(format_result(ref, result, min_match, with_osti_id=with_osti_id))
                print()
        except BrokenPipeError:
            pass

        # Belt-and-suspenders: flush stdout now and silence any late atexit
        # flush by redirecting to /dev/null on BrokenPipeError.
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            try:
                sys.stdout = open(os.devnull, "w")
            except Exception:
                pass

        print("[ref-checker]", file=sys.stderr)
        if cached_count:
            print(
                f"[ref-checker] Resumed: {cached_count} ref(s) loaded from sidecar",
                file=sys.stderr,
            )
        print(
            f"[ref-checker] Elapsed: {_format_duration(time.monotonic() - t0)}",
            file=sys.stderr,
        )
        stats.print_summary()
        if reason == "keyboard_interrupt":
            print("[ref-checker] Interrupted — partial results saved.", file=sys.stderr)

    return reason
