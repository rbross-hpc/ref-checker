"""Multi-reference orchestration: thread pool, resume/sidecar I/O, signal
handling, and end-of-run reporting.

Extracted from ``check.py`` (which re-exports ``check_references`` for
backward compatibility with existing callers/tests) as part of splitting the
orchestration module into focused subsystems. No behavior change.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .engine import lookup_reference
from .extract import Reference
from .format import format_result
from .planner import _plan_ref_work
from .results import LookupResult, _Stats
from .runtime import SourceHealth, _RateLimiter, _Shutdown, _format_duration
from . import sidecar as _sidecar
from .sources.registry import all_source_names as _all_source_names
from .sources.registry import default_delays as _default_delays
from .sources.registry import ThreadLocalSourceContexts as _ThreadLocalSourceContexts

_SS_UNAUTH_DELAY = 12.0

_ALL_DISABLED_SENTINEL = "all_scholarly_sources_disabled"


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
    effective_delays = dict(delays or _default_delays())
    # SS unauth tier is severely rate-limited. If no API key is configured,
    # spread requests further apart to reduce 429 pressure. Preserve
    # explicitly-zeroed delays (test fixtures) — only override when the
    # base delay is a normal positive value below _SS_UNAUTH_DELAY.
    if not os.environ.get("SEMANTICSCHOLAR_API_KEY", "").strip():
        current = effective_delays.get("semanticscholar", 8.0)
        if 0.0 < current < _SS_UNAUTH_DELAY:
            effective_delays["semanticscholar"] = _SS_UNAUTH_DELAY
    rl = _RateLimiter(effective_delays, shutdown=shutdown)
    # One SourceContext per source *per worker thread* for the whole run —
    # see ThreadLocalSourceContexts' docstring for why a single shared
    # SourceContext (and its requests.Session) is not safe to use
    # concurrently across worker threads. Every reference dispatched to a
    # given thread still reuses that thread's session per source, which is
    # what actually gets the connection-pooling benefit. All sessions built
    # across every thread during this run are closed in the finally: block
    # below, whether the run completes normally or is interrupted.
    contexts = _ThreadLocalSourceContexts()

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
            plan: set[str] | None = set(_all_source_names())
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
            contexts=contexts,
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
        # Deterministically close every session built by any worker thread
        # during this run (normal completion or interruption alike). Safe
        # here: the ThreadPoolExecutor's `with` block above has already
        # joined every worker thread by the time control reaches this
        # finally (context manager __exit__ waits for completion), so no
        # thread is still using a session concurrently with this close.
        contexts.close_all()

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
