"""Multi-source reference lookup driver."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .extract import Reference
from .format import format_result
from .planner import _plan_ref_work
from .results import LookupResult, _Stats
from .runtime import (
    SourceHealth,
    _QUOTA_EXHAUSTED_THRESHOLD,
    _RateLimiter,
    _Shutdown,
    _format_duration,
    _retry,
)
from . import sidecar as _sidecar
from .sources import arxiv, github, url as url_source
from .sources.registry import ALL_SOURCE_NAMES as _ALL_SOURCE_NAMES
from .sources.registry import SCHOLARLY_SOURCES as _SCHOLARLY_SOURCES
from .sources.registry import SCHOLARLY_SOURCE_NAMES as _SCHOLARLY_SOURCE_NAMES

__all__ = [
    "SourceHealth",
    "check_references",
    "lookup_reference",
    # Re-exported for backward compatibility: existing callers/tests reach
    # into these as check.<name> even though they now live in runtime.py,
    # planner.py, or sources/registry.py.
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

_SS_UNAUTH_DELAY = 12.0

_ALL_DISABLED_SENTINEL = "all_scholarly_sources_disabled"


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
        if sidecar is not None:
            print(
                f"[ref-checker] Re-emit results anytime with: "
                f"ref-checker show {sidecar}",
                file=sys.stderr,
            )

    return reason
