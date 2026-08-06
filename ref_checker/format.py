"""Output formatting for ref-checker results."""
from __future__ import annotations

import os
import sys

from .extract import Reference
from .results import LookupResult

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""
_GREEN  = "\033[32m" if _USE_COLOR else ""
_RED    = "\033[31m" if _USE_COLOR else ""
_ORANGE = "\033[33m" if _USE_COLOR else ""
_RESET  = "\033[0m"  if _USE_COLOR else ""

_OSTI_CONFIDENT_TITLE_THRESHOLD = 0.90
_OSTI_YEAR_PENALTY = 0.10


def _osti_id_if_confident(ref: Reference, result: LookupResult) -> str | None:
    """Return the OSTI external_id if the OSTI per-source entry is confident.

    Confident means:
      - status == "hit_id" (DOI match), OR
      - status == "hit_title" AND post-year-penalty score >= 0.90.
    """
    entry = result.per_source.get("osti") if result.per_source else None
    if not entry:
        return None
    summary = entry.get("summary") or {}
    ext_id = summary.get("external_id")
    if not ext_id:
        return None
    status = entry.get("status")
    if status == "hit_id":
        return str(ext_id)
    if status == "hit_title":
        score = entry.get("score")
        if score is None:
            return None
        cand_year = summary.get("year")
        if ref.year and cand_year and ref.year != cand_year:
            score = max(0.0, score - _OSTI_YEAR_PENALTY)
        if score >= _OSTI_CONFIDENT_TITLE_THRESHOLD:
            return str(ext_id)
    return None


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


def _id_str(summary: dict) -> str:
    if summary.get("doi"):
        return f"doi:{summary['doi']}"
    return summary.get("url") or ""


def format_result(
    ref: Reference,
    result: LookupResult,
    min_match: float,
    with_osti_id: bool = False,
) -> str:
    lines = [_format_ref_header(ref)]
    s = result.best_summary
    score = result.display_score
    src_tag = f"  [source: {result.best_source}]" if result.best_source else ""

    osti_suffix = ""
    if with_osti_id:
        osti_id = _osti_id_if_confident(ref, result)
        if osti_id:
            osti_suffix = f"  (OSTI: {osti_id})"

    score_str = f"({score:.2f})" if score is not None else "(----)"
    effective = score if score is not None else 1.0

    if s and (result.id_confirmed or result.is_liveness):
        lines.append(f"    {_GREEN}OK{_RESET} {score_str}  {_id_str(s)}{src_tag}{osti_suffix}")
        if result.url_liveness_check:
            lines.append(f"    {_ORANGE}Note:{_RESET} URL liveness check only — no bibliographic record found")
        for note in result.id_notes:
            lines.append(f"    {_ORANGE}Note:{_RESET} {note}")

    elif s and effective >= 0.90:
        lines.append(f"    {_GREEN}OK{_RESET} {score_str}  {_id_str(s)}{src_tag}{osti_suffix}")
        if result.year_mismatch_note:
            lines.append(f"    {_ORANGE}Note:{_RESET} year mismatch ({result.year_mismatch_note})")

    elif s and effective >= min_match:
        lines.append(f"    {_ORANGE}CLOSEST{_RESET} {score_str}{src_tag}{osti_suffix}")
        lines.append("        Closest candidate across services:")
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
        lines.append(f"    {_RED}NO MATCH{_RESET} {score_str}{src_tag}{osti_suffix}")
        if s:
            lines.append("        Closest candidate across services:")
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
