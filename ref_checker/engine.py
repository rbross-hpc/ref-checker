"""Single-reference lookup engine: assess one Reference against all sources.

Extracted from ``check.py`` (which re-exports ``lookup_reference`` for
backward compatibility with existing callers/tests) as part of splitting the
orchestration module into focused subsystems. No behavior change.
"""
from __future__ import annotations

from .extract import Reference
from .model import OutcomeKind, QueryKind
from .results import LookupResult, _Stats
from .runtime import SourceHealth, _RateLimiter, _Shutdown, _format_duration, _retry
from .sources import arxiv, github, url as url_source
from .sources.base import FN_BY_KIND as _FN_BY_KIND
from .sources.base import SourceContext
from .sources.registry import DEFAULT_DELAYS as _DEFAULT_DELAYS
from .sources.registry import SCHOLARLY_SOURCES as _SCHOLARLY_SOURCES

# What _ctx_for() needs from *contexts*: a mapping-like object supporting
# .get(name) -> SourceContext | None and item assignment. A plain
# dict[str, SourceContext] satisfies this (used by direct-call test paths
# and cli/main.py:run_lookup()); runner.py passes a
# sources.registry.ThreadLocalSourceContexts instead, which satisfies the
# same duck-typed interface but keys contexts per-thread rather than
# globally — see that class's docstring for why a flat dict is unsafe to
# share across concurrent worker threads.
_ContextsLike = dict


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
    contexts: "_ContextsLike | None" = None,
) -> LookupResult:
    """Run multi-reference lookup for a single reference.

    When *prior_result* is supplied its per_source entries seed the returned
    result; only sources named in *sources_to_query* are actually queried,
    and everything else is preserved from prior_result.

    *contexts* maps source name -> SourceContext (session + credentials).
    ``runner.py`` builds one such registry per ``check_references()`` run
    (a ``sources.registry.ThreadLocalSourceContexts``, not a plain dict —
    see its docstring) and threads it down here so every reference in the
    run reuses the same session per source *within the worker thread
    processing that reference*. When None (e.g. some direct-call test
    paths), a fresh context is built lazily per source on first use, with
    no reuse across separate ``lookup_reference()`` calls.
    """
    rl = rate_limiter if rate_limiter is not None else _RateLimiter(delays or _DEFAULT_DELAYS)
    health = health if health is not None else SourceHealth(stats=stats)
    contexts = contexts if contexts is not None else {}

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

    def _ctx_for(src) -> SourceContext:
        src_name = src.SOURCE_NAME
        ctx = contexts.get(src_name)
        if ctx is None:
            ctx = src.build_context()
            contexts[src_name] = ctx
        return ctx

    def call(src, queried_by: str, *args) -> tuple[dict | None, float | None]:
        src_name = src.SOURCE_NAME
        if health.is_disabled(src_name):
            result.record_source(src_name, OutcomeKind.DISABLED, queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        kind = QueryKind(queried_by)
        if kind not in src.SUPPORTED_QUERY_KINDS:
            return None, None
        fn = getattr(src, _FN_BY_KIND[kind])
        ctx = _ctx_for(src)
        rl.wait(src_name)
        # Re-check after rl.wait — another worker may have disabled the source
        # while we were waiting on the per-source rate limiter.
        if health.is_disabled(src_name):
            result.record_source(src_name, OutcomeKind.DISABLED, queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        if shutdown is not None and shutdown.requested():
            result.record_source(src_name, OutcomeKind.SKIPPED, queried_by=queried_by,
                                 note="aborted by user")
            return None, None
        if stats:
            stats.record_query(src_name, queried_by)
        errored = {"flag": False}

        def _on_exhausted() -> None:
            errored["flag"] = True

        out, cause, retry_after = _retry(
            lambda: fn(*args, ctx),
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
                result.record_source(src_name, OutcomeKind.RATE_LIMITED, queried_by=queried_by,
                                     note=note)
                reason = (
                    f"server requested Retry-After={retry_after:.0f}s "
                    f"(~{_format_duration(retry_after or 0.0)}) — quota exhausted"
                )
                health.disable(src_name, reason)
                return None, None
            rate_limited = cause == "rate_limit"
            note = "rate-limit retries exhausted" if rate_limited else "retries exhausted"
            result.record_source(
                src_name,
                OutcomeKind.RATE_LIMITED if rate_limited else OutcomeKind.ERROR,
                queried_by=queried_by,
                note=note,
            )
            # Rate-limit exhaustion advances the rate-limit counter (which
            # can independently disable the source at RATE_LIMIT_THRESHOLD);
            # real errors advance the regular error counter.
            health.record(src_name, "rate_limited" if rate_limited else "error")
            return None, None
        if out is None:
            # Shutdown-before-attempt path.
            return None, None

        summary, sim = out
        if summary is None:
            result.record_source(src_name, OutcomeKind.NOT_FOUND, queried_by=queried_by)
            health.record(src_name, "not_found")
            return None, None

        status = OutcomeKind.HIT_ID if queried_by in ("doi", "arxiv_id") else OutcomeKind.HIT_TITLE
        result.record_source(src_name, status, queried_by=queried_by,
                             score=sim, summary=summary)
        health.record(src_name, status)
        return summary, sim

    def call_liveness(src, urls: str, queried_by: str) -> tuple[dict | None, float | None]:
        src_name = src.SOURCE_NAME
        if health.is_disabled(src_name):
            result.record_source(src_name, OutcomeKind.DISABLED, queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        rl.wait(src_name)
        # Re-check after rl.wait — another worker may have disabled the source
        # while we were waiting on the per-source rate limiter.
        if health.is_disabled(src_name):
            result.record_source(src_name, OutcomeKind.DISABLED, queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        if shutdown is not None and shutdown.requested():
            result.record_source(src_name, OutcomeKind.SKIPPED, queried_by=queried_by,
                                 note="aborted by user")
            return None, None
        if stats:
            stats.record_query(src_name, queried_by)
        ctx = _ctx_for(src)
        errored = {"flag": False}

        def _on_exhausted() -> None:
            errored["flag"] = True

        try:
            out, cause, retry_after = _retry(
                lambda: src.check_url(urls, ctx),
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
                result.record_source(src_name, OutcomeKind.RATE_LIMITED, queried_by=queried_by,
                                     note=note)
                reason = (
                    f"server requested Retry-After={retry_after:.0f}s "
                    f"(~{_format_duration(retry_after or 0.0)}) — quota exhausted"
                )
                health.disable(src_name, reason)
                return None, None
            rate_limited = cause == "rate_limit"
            note = "rate-limit retries exhausted" if rate_limited else "retries exhausted"
            result.record_source(
                src_name,
                OutcomeKind.RATE_LIMITED if rate_limited else OutcomeKind.ERROR,
                queried_by=queried_by,
                note=note,
            )
            health.record(src_name, "rate_limited" if rate_limited else "error")
            return None, None
        if out is None:
            return None, None

        summary, sim, dead = out if len(out) == 3 else (*out, [])
        for d in dead:
            if d not in result.dead_urls:
                result.dead_urls.append(d)
        if summary is None:
            result.record_source(src_name, OutcomeKind.NOT_FOUND, queried_by=queried_by,
                                 note=(f"dead urls: {len(dead)}" if dead else None))
            health.record(src_name, "not_found")
            return None, None

        result.record_source(src_name, OutcomeKind.HIT_ID, queried_by=queried_by,
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
            e.get("status") == OutcomeKind.HIT_ID
            for e in result.per_source.values()
        )

    if ref.arxiv_id and not _id_confirmed() and _should_query(arxiv.SOURCE_NAME) and not _stopped():
        call(arxiv, "arxiv_id", ref.arxiv_id)
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
            call(src, "doi", ref.doi)

        if not _id_confirmed() and ref.arxiv_id and src_name != arxiv.SOURCE_NAME:
            call(src, "arxiv_id", ref.arxiv_id)

        if not _id_confirmed() and ref.title:
            # Only title-search if we don't already have a strong title hit ≥ 0.90
            best_title = 0.0
            for entry in result.per_source.values():
                if entry.get("status") == OutcomeKind.HIT_TITLE and entry.get("score") is not None:
                    if entry["score"] > best_title:
                        best_title = entry["score"]
            if best_title < 0.90:
                call(src, "title", ref.title)

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
