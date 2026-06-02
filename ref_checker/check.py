"""Multi-source reference lookup driver."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

from .extract import Reference
from .format import format_result
from .results import LookupResult, _Stats
from .similarity import title_ratio
from . import sidecar as _sidecar
from .sources import arxiv, crossref, dblp, github, openalex, semanticscholar, url as url_source

_SCHOLARLY_SOURCES = [openalex, crossref, dblp, semanticscholar, arxiv]

_DEFAULT_DELAYS: dict[str, float] = {
    "openalex": 2.0,
    "crossref": 2.0,
    "dblp": 1.0,
    "semanticscholar": 8.0,
    "arxiv": 3.0,
    "github": 1.0,
    "url": 1.0,
}

_RETRY_BACKOFF = (5.0, 10.0, 15.0)
_YEAR_MISMATCH_PENALTY = 0.10


def _retry(
    fn,
    tries: int = 3,
    stats: _Stats | None = None,
    source: str = "",
    on_exhausted: Callable[[], None] | None = None,
) -> tuple[dict | None, float | None]:
    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < tries - 1:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                if stats and source:
                    stats.record_retry(source)
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


class _RateLimiter:
    def __init__(self, delays: dict[str, float]) -> None:
        self._delays = delays
        self._last: dict[str, float] = {}

    def wait(self, source_name: str) -> None:
        delay = self._delays.get(source_name, 1.0)
        last = self._last.get(source_name)
        if last is not None:
            elapsed = time.monotonic() - last
            remaining = delay - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def mark(self, source_name: str) -> None:
        self._last[source_name] = time.monotonic()


def _consider(
    ref: Reference,
    result: LookupResult,
    summary: dict | None,
    sim: float | None,
    source_name: str,
) -> None:
    """Record a title-search hit. Applies year penalty to the display score."""
    if summary is None or sim is None:
        return

    ref_year = ref.year
    cand_year = summary.get("year")
    year_note: str | None = None

    if ref_year and cand_year and ref_year != cand_year:
        sim = max(0.0, sim - _YEAR_MISMATCH_PENALTY)
        year_note = f"ref year={ref_year}, match year={cand_year}"

    current = result.display_score if result.display_score is not None else 0.0
    if sim > current:
        result.display_score = sim
        result.best_summary = summary
        result.best_source = source_name
        result.year_mismatch_note = year_note


def _consider_id_hit(
    ref: Reference,
    result: LookupResult,
    summary: dict,
    source_name: str,
) -> None:
    """Record an identifier-based (DOI/arXiv) or liveness hit.

    Display score = title_ratio(ref.title, candidate.title) with no year
    penalty — the identifier is proof of identity; year disagreement is
    surfaced as a Note only.

    For liveness-only sources (GitHub, URL) display_score is set to None
    since there is no meaningful title to compare against.
    """
    liveness_only = source_name in (github.SOURCE_NAME, url_source.SOURCE_NAME)

    result.best_summary = summary
    result.best_source = source_name
    result.id_confirmed = True
    result.year_mismatch_note = None
    result.id_notes = []

    if liveness_only:
        result.display_score = None
        result.is_liveness = True
    else:
        cand_title = summary.get("title")
        if ref.title and cand_title:
            t_sim = title_ratio(ref.title, cand_title)
            result.display_score = t_sim
            if t_sim < 0.85:
                result.id_notes.append(f"DOI title: \"{cand_title}\"")
        else:
            result.display_score = None

        ref_year = ref.year
        cand_year = summary.get("year")
        if ref_year and cand_year and ref_year != cand_year:
            result.id_notes.append(f"year mismatch (ref year={ref_year}, match year={cand_year})")
            result.year_mismatch_note = f"ref year={ref_year}, match year={cand_year}"


def lookup_reference(
    ref: Reference,
    delays: dict[str, float] | None = None,
    min_match: float = 0.80,
    stats: _Stats | None = None,
    rate_limiter: _RateLimiter | None = None,
) -> LookupResult:
    """Run multi-source lookup for a single reference."""
    rl = rate_limiter if rate_limiter is not None else _RateLimiter(delays or _DEFAULT_DELAYS)
    result = LookupResult(
        doi_attempted=ref.doi,
        arxiv_attempted=ref.arxiv_id,
    )

    def call(src, fn_name: str, *args):
        fn = getattr(src, fn_name, None)
        if fn is None:
            return None, None
        rl.wait(src.SOURCE_NAME)
        if stats:
            stats.record_query(src.SOURCE_NAME)
        out = _retry(
            lambda: fn(*args),
            stats=stats,
            source=src.SOURCE_NAME,
            on_exhausted=lambda: result.exhausted_sources.append(src.SOURCE_NAME),
        )
        rl.mark(src.SOURCE_NAME)
        return out

    def call_liveness(src, urls: str):
        fn = src.check_url
        rl.wait(src.SOURCE_NAME)
        if stats:
            stats.record_query(src.SOURCE_NAME)
        try:
            out = _retry(
                lambda: fn(urls),
                stats=stats,
                source=src.SOURCE_NAME,
                on_exhausted=lambda: result.exhausted_sources.append(src.SOURCE_NAME),
            )
        except Exception:
            out = (None, None, [])
        rl.mark(src.SOURCE_NAME)
        summary, sim, dead = out if len(out) == 3 else (*out, [])
        result.dead_urls.extend(dead)
        return summary, sim

    # --- GitHub first (if the ref has a GitHub URL) ---
    if ref.github_url:
        summary, sim = call_liveness(github, ref.github_url)
        if summary:
            _consider_id_hit(ref, result, summary, github.SOURCE_NAME)
            return result

    # --- arXiv ID lookup (before scholarly loop) ---
    if ref.arxiv_id and not result.id_confirmed:
        summary, sim = call(arxiv, "get_by_arxiv_id", ref.arxiv_id)
        if summary:
            result.arxiv_found_in.append(arxiv.SOURCE_NAME)
            _consider_id_hit(ref, result, summary, arxiv.SOURCE_NAME)
            return result

    # --- Scholarly sources loop (OA → CR → DBLP → SS → arXiv) ---
    # Skip entirely when the reference is clearly a non-paper resource:
    # no DOI, no arXiv ID, no venue, and has a URL.
    url_only = (
        not ref.doi
        and not ref.arxiv_id
        and not ref.venue
        and (ref.url or ref.github_url)
    )

    for src in (_SCHOLARLY_SOURCES if not url_only else []):
        if result.id_confirmed:
            break

        if ref.doi:
            summary, sim = call(src, "get_by_doi", ref.doi)
            if summary:
                result.doi_found_in.append(src.SOURCE_NAME)
                _consider_id_hit(ref, result, summary, src.SOURCE_NAME)

        if not result.id_confirmed and ref.arxiv_id and src.SOURCE_NAME != arxiv.SOURCE_NAME:
            summary, sim = call(src, "get_by_arxiv_id", ref.arxiv_id)
            if summary:
                if src.SOURCE_NAME not in result.arxiv_found_in:
                    result.arxiv_found_in.append(src.SOURCE_NAME)
                _consider_id_hit(ref, result, summary, src.SOURCE_NAME)

        current_score = result.display_score if result.display_score is not None else 0.0
        if not result.id_confirmed and current_score < 0.90 and ref.title:
            summary, sim = call(src, "search_by_title", ref.title)
            _consider(ref, result, summary, sim, src.SOURCE_NAME)

    # --- Generic URL liveness fallback (last resort, no scholarly match) ---
    current_score = result.display_score if result.display_score is not None else 0.0
    if current_score < min_match and ref.url and not ref.github_url:
        summary, sim = call_liveness(url_source, ref.url)
        if summary:
            _consider_id_hit(ref, result, summary, url_source.SOURCE_NAME)
            result.url_liveness_check = True

    return result


def check_references(
    refs: list[Reference],
    delays: dict[str, float] | None = None,
    min_match: float = 0.80,
    sidecar: Path | None = None,
    resume: bool = False,
    retry_all: bool = False,
    retry_closest: bool = False,
    pdf_name: str = "",
) -> None:
    """Look up every reference and print a human-readable summary to stdout."""
    stats = _Stats()
    rl = _RateLimiter(delays or _DEFAULT_DELAYS)
    all_results: dict[int, LookupResult] = {}
    prior: dict[int, dict] = {}
    cached_count = 0

    if sidecar is not None and resume and sidecar.exists():
        prior, hash_ok = _sidecar.load(sidecar, refs)
        if not hash_ok:
            print(
                "[ref-checker] WARNING: sidecar refs_hash mismatch — "
                "references may have changed; ignoring sidecar and running fresh.",
                file=sys.stderr,
            )
            prior = {}

    total = len(refs)
    for i, ref in enumerate(refs, start=1):
        prior_entry = prior.get(ref.index)
        prior_result = prior_entry.get("result") if prior_entry is not None else None
        use_cached = (
            not retry_all
            and prior_result is not None
            and not _sidecar.needs_retry(prior_result, retry_closest)
        )

        if use_cached:
            result = _sidecar.result_from_dict(prior_result)
            cached_count += 1
            print(
                f"[ref-checker] checking {i}/{total} (cached): {ref.title or ref.raw[:60]!r}",
                file=sys.stderr,
            )
        else:
            print(
                f"[ref-checker] checking {i}/{total}: {ref.title or ref.raw[:60]!r}",
                file=sys.stderr,
            )
            result = lookup_reference(
                ref, delays=delays, min_match=min_match, stats=stats, rate_limiter=rl
            )

        all_results[ref.index] = result
        print(format_result(ref, result, min_match))
        print()

        if sidecar is not None:
            _sidecar.write(sidecar, pdf_name, refs, all_results, min_match)

    print("[ref-checker]", file=sys.stderr)
    if cached_count:
        print(f"[ref-checker] Resumed: {cached_count} ref(s) loaded from sidecar", file=sys.stderr)
    stats.print_summary()
