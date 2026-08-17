"""Shared runtime primitives: shutdown coordination, source-health circuit
breaker, per-source rate limiting, and retry/backoff.

Extracted from ``check.py`` (which re-exports these names for backward
compatibility with existing callers/tests) as the first step in splitting
the orchestration module into focused subsystems. No behavior change.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from .errors import RateLimited
from .results import _Stats
from .sources.registry import scholarly_source_names

_RETRY_BACKOFF = (5.0, 10.0, 15.0)

_MAX_RETRY_AFTER = 60.0

_QUOTA_EXHAUSTED_THRESHOLD = 300.0

_WAIT_VISIBILITY_THRESHOLD = 10.0


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
            return all(name in self._disabled for name in scholarly_source_names())


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
