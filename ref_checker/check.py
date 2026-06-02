"""Multi-source reference lookup driver and output formatter."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""
_GREEN  = "\033[32m" if _USE_COLOR else ""
_RED    = "\033[31m" if _USE_COLOR else ""
_ORANGE = "\033[33m" if _USE_COLOR else ""
_RESET  = "\033[0m"  if _USE_COLOR else ""

from .extract import Reference
from .similarity import title_ratio
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


@dataclass
class LookupResult:
    best_summary: dict | None = None
    display_score: float | None = None    # title_ratio for ID hits (no year penalty);
                                          # title_ratio - year_penalty for title-search hits;
                                          # None for liveness-only hits
    best_source: str | None = None
    id_confirmed: bool = False            # True when a DOI or arXiv ID lookup succeeded
    is_liveness: bool = False             # True when result is GitHub/URL liveness only
    doi_attempted: str | None = None
    doi_found_in: list[str] = field(default_factory=list)
    arxiv_attempted: str | None = None
    arxiv_found_in: list[str] = field(default_factory=list)
    year_mismatch_note: str | None = None
    id_notes: list[str] = field(default_factory=list)
    dead_urls: list[tuple[str, str]] = field(default_factory=list)
    exhausted_sources: list[str] = field(default_factory=list)
    url_liveness_check: bool = False


@dataclass
class _Stats:
    queries: dict[str, int] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    exhausted: dict[str, int] = field(default_factory=dict)

    def record_query(self, source: str) -> None:
        self.queries[source] = self.queries.get(source, 0) + 1

    def record_retry(self, source: str) -> None:
        self.retries[source] = self.retries.get(source, 0) + 1

    def record_exhausted(self, source: str) -> None:
        self.exhausted[source] = self.exhausted.get(source, 0) + 1

    def print_summary(self) -> None:
        all_sources = sorted(set(list(self.queries) + list(self.retries) + list(self.exhausted)))
        if not all_sources:
            return
        print("[ref-checker] Query summary:", file=sys.stderr)
        for src in all_sources:
            q = self.queries.get(src, 0)
            r = self.retries.get(src, 0)
            e = self.exhausted.get(src, 0)
            retry_str = f", {r} retr{'y' if r == 1 else 'ies'}" if r else ""
            exhausted_str = f", {e} exhausted" if e else ""
            print(f"[ref-checker]   {src:20s} {q:3d} quer{'y' if q == 1 else 'ies'}{retry_str}{exhausted_str}", file=sys.stderr)


def _retry(
    fn,
    tries: int = 3,
    stats: _Stats | None = None,
    source: str = "",
    on_exhausted: "Callable[[], None] | None" = None,
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
                result.id_notes.append(
                    f"DOI title: \"{cand_title}\""
                )
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


def _format_citation(
    authors: list[str],
    title: str | None,
    year: int | None,
    venue: str | None,
) -> str:
    parts = []
    if authors:
        first = authors[0]
        last = first.split()[-1] if first else first
        suffix = " et al." if len(authors) > 1 else ""
        parts.append(f"{last}{suffix}")
    if title:
        parts.append(f'"{title}"')
    if year:
        parts.append(str(year))
    if venue:
        parts.append(f"({venue})")
    return ", ".join(parts)


def _format_ref_header(ref: Reference) -> str:
    citation = _format_citation(ref.authors, ref.title, ref.year, ref.venue)
    return f"[{ref.index}] {citation}" if citation else f"[{ref.index}] {ref.raw}"


def _format_result(ref: Reference, result: LookupResult, min_match: float) -> str:
    lines = [_format_ref_header(ref)]
    s = result.best_summary
    score = result.display_score
    src_tag = f"  [source: {result.best_source}]" if result.best_source else ""

    score_str = f"({score:.2f})" if score is not None else "(----)"

    # Identifier string: prefer doi:, fall back to url
    def id_str(summary: dict) -> str:
        if summary.get("doi"):
            return f"doi:{summary['doi']}"
        return summary.get("url") or ""

    # Effective numeric score for tier decisions (liveness = 1.0 for threshold purposes)
    effective = score if score is not None else 1.0

    if s and (result.id_confirmed or result.is_liveness):
        lines.append(f"    {_GREEN}OK{_RESET} {score_str}  {id_str(s)}{src_tag}")
        if result.url_liveness_check:
            lines.append(f"    {_ORANGE}Note:{_RESET} URL liveness check only — no bibliographic record found")
        for note in result.id_notes:
            lines.append(f"    {_ORANGE}Note:{_RESET} {note}")

    elif s and effective >= 0.90:
        lines.append(f"    {_GREEN}OK{_RESET} {score_str}  {id_str(s)}{src_tag}")
        if result.year_mismatch_note:
            lines.append(f"    {_ORANGE}Note:{_RESET} year mismatch ({result.year_mismatch_note})")

    elif s and effective >= min_match:
        lines.append(f"    {_ORANGE}CLOSEST{_RESET} {score_str}{src_tag}")
        lines.append(f"        Closest candidate across services:")
        citation = _format_citation(
            s.get("authors") or [],
            s.get("title"),
            s.get("year"),
            s.get("venue"),
        )
        if citation:
            lines.append(f"        {citation}")
        if s.get("url"):
            lines.append(f"        {s['url']}")
        if result.year_mismatch_note:
            lines.append(f"    {_ORANGE}Note:{_RESET} year mismatch ({result.year_mismatch_note})")

    else:
        lines.append(f"    {_RED}NO MATCH{_RESET} {score_str}{src_tag}")
        if s:
            lines.append(f"        Closest candidate across services:")
            citation = _format_citation(
                s.get("authors") or [],
                s.get("title"),
                s.get("year"),
                s.get("venue"),
            )
            if citation:
                lines.append(f"        {citation}")
            if s.get("url"):
                lines.append(f"        {s['url']}")

    if not result.id_confirmed and not result.is_liveness:
        if result.doi_attempted and not result.doi_found_in:
            lines.append(f"    DOI not found in any source: {result.doi_attempted}")
        if result.arxiv_attempted and not result.arxiv_found_in:
            lines.append(f"    arXiv ID not found in any source: {result.arxiv_attempted}")
    for dead_url, reason in result.dead_urls:
        lines.append(f"    URL check failed ({reason}): {dead_url}")
    if result.exhausted_sources:
        srcs = ", ".join(sorted(set(result.exhausted_sources)))
        lines.append(f"    {_ORANGE}Note:{_RESET} retries exhausted for {srcs} — results may be incomplete")

    return "\n".join(lines)


_SIDECAR_SCHEMA_VERSION = 1


def _refs_hash(refs: list[Reference]) -> str:
    raw = "\n".join(str(r.index) + r.raw for r in sorted(refs, key=lambda r: r.index))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _status_label(result: LookupResult, min_match: float) -> str:
    if result.is_liveness or result.id_confirmed:
        return "OK"
    score = result.display_score if result.display_score is not None else 0.0
    if score >= 0.90:
        return "OK"
    if score >= min_match:
        return "CLOSEST"
    return "NO MATCH"


def _result_to_dict(result: LookupResult, min_match: float) -> dict:
    return {
        "status": _status_label(result, min_match),
        "display_score": result.display_score,
        "best_source": result.best_source,
        "id_confirmed": result.id_confirmed,
        "is_liveness": result.is_liveness,
        "best_summary": result.best_summary,
        "doi_attempted": result.doi_attempted,
        "doi_found_in": result.doi_found_in,
        "arxiv_attempted": result.arxiv_attempted,
        "arxiv_found_in": result.arxiv_found_in,
        "year_mismatch_note": result.year_mismatch_note,
        "id_notes": result.id_notes,
        "dead_urls": [list(t) for t in result.dead_urls],
        "exhausted_sources": result.exhausted_sources,
        "url_liveness_check": result.url_liveness_check,
    }


def _result_from_dict(d: dict) -> LookupResult:
    return LookupResult(
        best_summary=d.get("best_summary"),
        display_score=d.get("display_score"),
        best_source=d.get("best_source"),
        id_confirmed=d.get("id_confirmed", False),
        is_liveness=d.get("is_liveness", False),
        doi_attempted=d.get("doi_attempted"),
        doi_found_in=d.get("doi_found_in") or [],
        arxiv_attempted=d.get("arxiv_attempted"),
        arxiv_found_in=d.get("arxiv_found_in") or [],
        year_mismatch_note=d.get("year_mismatch_note"),
        id_notes=d.get("id_notes") or [],
        dead_urls=[tuple(t) for t in (d.get("dead_urls") or [])],
        exhausted_sources=d.get("exhausted_sources") or [],
        url_liveness_check=d.get("url_liveness_check", False),
    )


def _needs_retry(entry: dict, retry_closest: bool) -> bool:
    status = entry.get("status", "NO MATCH")
    if status not in ("OK", "CLOSEST", "NO MATCH"):
        return True
    if status == "NO MATCH":
        return True
    if status == "CLOSEST" and retry_closest:
        return True
    if entry.get("exhausted_sources"):
        return True
    if entry.get("dead_urls"):
        return True
    return False


def _load_sidecar(path: Path, refs: list[Reference]) -> tuple[dict, bool]:
    """Load sidecar JSON. Returns (entries_by_index, hash_ok)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, False
    if data.get("schema_version") != _SIDECAR_SCHEMA_VERSION:
        return {}, False
    stored_hash = data.get("refs_hash")
    current_hash = _refs_hash(refs)
    hash_ok = stored_hash == current_hash
    entries = {int(k): v for k, v in (data.get("references") or {}).items()}
    return entries, hash_ok


def _write_sidecar(
    path: Path,
    pdf_name: str,
    refs: list[Reference],
    all_results: dict[int, LookupResult],
    min_match: float,
) -> None:
    data = {
        "schema_version": _SIDECAR_SCHEMA_VERSION,
        "pdf": pdf_name,
        "refs_hash": _refs_hash(refs),
        "references": {
            str(ref.index): {
                "ref": ref.to_dict(),
                "result": _result_to_dict(all_results[ref.index], min_match),
            }
            for ref in refs
            if ref.index in all_results
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
        prior, hash_ok = _load_sidecar(sidecar, refs)
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
        use_cached = (
            not retry_all
            and prior_entry is not None
            and not _needs_retry(prior_entry, retry_closest)
        )

        if use_cached:
            result = _result_from_dict(prior_entry["result"])
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
        print(_format_result(ref, result, min_match))
        print()

        if sidecar is not None:
            _write_sidecar(sidecar, pdf_name, refs, all_results, min_match)

    print("[ref-checker]", file=sys.stderr)
    if cached_count:
        print(f"[ref-checker] Resumed: {cached_count} ref(s) loaded from sidecar", file=sys.stderr)
    stats.print_summary()
