"""Multi-source reference lookup driver."""
from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

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

_ALL_DISABLED_SENTINEL = "all_scholarly_sources_disabled"


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

    A source is disabled after THRESHOLD consecutive `error` outcomes across
    the session. Any `hit_*` or `not_found` outcome resets the counter.
    """

    THRESHOLD = 3

    def __init__(self, threshold: int = THRESHOLD, stats: _Stats | None = None) -> None:
        self._threshold = threshold
        self._consecutive: dict[str, int] = {}
        self._disabled: set[str] = set()
        self._stats = stats

    def record(self, source: str, status: str) -> None:
        if status in ("hit_id", "hit_title", "not_found"):
            self._consecutive[source] = 0
            return
        if status == "error":
            self._consecutive[source] = self._consecutive.get(source, 0) + 1
            if (
                self._consecutive[source] >= self._threshold
                and source not in self._disabled
            ):
                self._disabled.add(source)
                reason = f"{self._threshold} consecutive errors"
                if self._stats is not None:
                    self._stats.record_disabled(source, reason)
                print(
                    f"[ref-checker] source '{source}' disabled for remainder of "
                    f"session after {self._threshold} consecutive errors",
                    file=sys.stderr,
                )

    def is_disabled(self, source: str) -> bool:
        return source in self._disabled

    def all_scholarly_disabled(self) -> bool:
        return all(name in self._disabled for name in _SCHOLARLY_SOURCE_NAMES)


class _RateLimiter:
    def __init__(self, delays: dict[str, float], shutdown: _Shutdown | None = None) -> None:
        self._delays = delays
        self._last: dict[str, float] = {}
        self._shutdown = shutdown

    def wait(self, source_name: str) -> None:
        delay = self._delays.get(source_name, 1.0)
        last = self._last.get(source_name)
        if last is None:
            return
        elapsed = time.monotonic() - last
        remaining = delay - elapsed
        if remaining <= 0:
            return
        if self._shutdown is not None:
            self._shutdown.wait(remaining)
        else:
            time.sleep(remaining)

    def mark(self, source_name: str) -> None:
        self._last[source_name] = time.monotonic()


def _retry(
    fn,
    tries: int = 3,
    stats: _Stats | None = None,
    source: str = "",
    on_exhausted: Callable[[], None] | None = None,
    shutdown: _Shutdown | None = None,
) -> tuple[dict | None, float | None] | tuple[dict | None, float | None, list]:
    last_exc: Exception | None = None
    for attempt in range(tries):
        if shutdown is not None and shutdown.requested():
            return None, None
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < tries - 1:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                if stats and source:
                    stats.record_retry(source)
                if shutdown is not None:
                    if shutdown.wait(wait):
                        return None, None
                else:
                    time.sleep(wait)
    if stats and source:
        stats.record_exhausted(source)
    if on_exhausted:
        on_exhausted()
    print(
        f"[ref-checker] WARNING: all {tries} retries exhausted for {source}"
        + (f": {last_exc}" if last_exc else ""),
        file=sys.stderr,
    )
    return None, None


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
        if shutdown is not None and shutdown.requested():
            result.record_source(src_name, "skipped", queried_by=queried_by,
                                 note="aborted by user")
            return None, None
        if stats:
            stats.record_query(src_name)
        errored = {"flag": False}

        def _on_exhausted() -> None:
            errored["flag"] = True

        out = _retry(
            lambda: fn(*args),
            stats=stats,
            source=src_name,
            on_exhausted=_on_exhausted,
            shutdown=shutdown,
        )
        rl.mark(src_name)
        if errored["flag"]:
            result.record_source(src_name, "error", queried_by=queried_by,
                                 note="retries exhausted")
            health.record(src_name, "error")
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
        if shutdown is not None and shutdown.requested():
            result.record_source(src_name, "skipped", queried_by=queried_by,
                                 note="aborted by user")
            return None, None
        if stats:
            stats.record_query(src_name)
        errored = {"flag": False}

        def _on_exhausted() -> None:
            errored["flag"] = True

        try:
            out = _retry(
                lambda: src.check_url(urls),
                stats=stats,
                source=src_name,
                on_exhausted=_on_exhausted,
                shutdown=shutdown,
            )
        except Exception:
            out = (None, None, [])
        rl.mark(src_name)
        if errored["flag"]:
            result.record_source(src_name, "error", queried_by=queried_by,
                                 note="retries exhausted")
            health.record(src_name, "error")
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
) -> str | None:
    """Look up every reference and print a human-readable summary.

    Returns:
      - None on normal completion
      - "keyboard_interrupt" if shutdown was requested via signal
      - "all_scholarly_sources_disabled" if the circuit breaker tripped everything
    """
    stats = _Stats()
    shutdown = _Shutdown()
    health = SourceHealth(threshold=source_error_threshold, stats=stats)
    rl = _RateLimiter(delays or _DEFAULT_DELAYS, shutdown=shutdown)

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

    prev_sigint = signal.getsignal(signal.SIGINT)

    def _sigint_handler(signum, frame):
        stage = shutdown.request()
        if stage == 1:
            print(
                "[ref-checker] Shutting down — finishing current request and flushing "
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
    try:
        for i, ref in enumerate(refs, start=1):
            if shutdown.requested():
                reason = "keyboard_interrupt"
                break

            prior_entry = prior_entries.get(ref.index)
            prior_result_dict = prior_entry.get("result") if prior_entry is not None else None
            prior_result = all_results.get(ref.index)
            prior_status = prior_result_dict.get("status") if prior_result_dict else None

            if retry_all:
                targets: set[str] | None = set(_ALL_SOURCE_NAMES)
            else:
                targets = _plan_ref_work(
                    prior_result, prior_status,
                    retry_closest=retry_closest,
                    retry_errored=retry_errored,
                )

            if targets is None:
                # Fully satisfied — replay from sidecar.
                result = prior_result
                cached_count += 1
                print(
                    f"[ref-checker] checking {i}/{total} (cached): "
                    f"{ref.title or ref.raw[:60]!r}",
                    file=sys.stderr,
                )
            elif targets == set() and prior_result is not None:
                # Not satisfied, but no untried sources — keep prior.
                result = prior_result
                cached_count += 1
                print(
                    f"[ref-checker] checking {i}/{total}: "
                    f"{ref.title or ref.raw[:60]!r} — "
                    f"no untried sources; keeping prior result",
                    file=sys.stderr,
                )
            else:
                mode = "resuming" if prior_result is not None else "fresh"
                print(
                    f"[ref-checker] checking {i}/{total} ({mode}): "
                    f"{ref.title or ref.raw[:60]!r}",
                    file=sys.stderr,
                )
                result = lookup_reference(
                    ref,
                    delays=delays,
                    min_match=min_match,
                    stats=stats,
                    rate_limiter=rl,
                    health=health,
                    shutdown=shutdown,
                    sources_to_query=targets,
                    prior_result=prior_result,
                )

            all_results[ref.index] = result
            print(format_result(ref, result, min_match, with_osti_id=with_osti_id))
            print()

            if sidecar is not None:
                try:
                    _sidecar.write(sidecar, pdf_name, refs, all_results, min_match)
                except Exception as exc:
                    print(
                        f"[ref-checker] WARNING: sidecar write failed: {exc}",
                        file=sys.stderr,
                    )

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
    except KeyboardInterrupt:
        reason = "keyboard_interrupt"
    finally:
        if prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, prev_sigint)
            except (ValueError, OSError):
                pass
        if sidecar is not None:
            try:
                _sidecar.write(sidecar, pdf_name, refs, all_results, min_match)
            except Exception as exc:
                print(
                    f"[ref-checker] WARNING: final sidecar write failed: {exc}",
                    file=sys.stderr,
                )
        print("[ref-checker]", file=sys.stderr)
        if cached_count:
            print(
                f"[ref-checker] Resumed: {cached_count} ref(s) loaded from sidecar",
                file=sys.stderr,
            )
        stats.print_summary()
        if reason == "keyboard_interrupt":
            print("[ref-checker] Interrupted — partial results saved.", file=sys.stderr)

    return reason
