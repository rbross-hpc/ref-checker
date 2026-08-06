"""Single-reference lookup engine: assess one Reference against all sources.

Extracted from ``check.py`` (which re-exports ``lookup_reference`` for
backward compatibility with existing callers/tests) as part of splitting the
orchestration module into focused subsystems. No behavior change.
"""
from __future__ import annotations

from .extract import Reference
from .model import OutcomeKind
from .results import LookupResult, _Stats
from .runtime import SourceHealth, _RateLimiter, _Shutdown, _format_duration, _retry
from .sources import arxiv, github, url as url_source
from .sources.registry import SCHOLARLY_SOURCES as _SCHOLARLY_SOURCES

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
            result.record_source(src_name, OutcomeKind.DISABLED, queried_by=queried_by,
                                 note="session circuit breaker")
            return None, None
        fn = getattr(src, fn_name, None)
        if fn is None:
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
                if entry.get("status") == OutcomeKind.HIT_TITLE and entry.get("score") is not None:
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
