"""OpenAlex source: DOI lookup, arXiv-ID lookup, and title search."""
from __future__ import annotations

import os
import re
import warnings
from typing import Any

from ..model import QueryKind
from ..similarity import title_ratio
from ._http import build_session, raise_for_rate_limit, user_agent
from .base import SourceContext

SOURCE_NAME = "openalex"
DEFAULT_DELAY = 2.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.ARXIV_ID, QueryKind.TITLE})

_BASE = "https://api.openalex.org/works"
_WARNED_MAILTO = False


def build_context() -> SourceContext:
    """Build the OpenAlex :class:`SourceContext` once per run.

    Session carries a fixed User-Agent and (if ``OPENALEX_MAILTO`` is set) a
    session-level ``mailto`` param that ``requests`` auto-merges into every
    per-call ``params=`` dict — replacing the old per-call ``_polite_params``.
    """
    global _WARNED_MAILTO
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if not mailto:
        if not _WARNED_MAILTO:
            warnings.warn(
                "[ref-checker] OPENALEX_MAILTO is not set. "
                "OpenAlex requests will use an anonymous User-Agent. "
                "Set OPENALEX_MAILTO to your email for polite API access.",
                stacklevel=3,
            )
            _WARNED_MAILTO = True
    params = {"mailto": mailto} if mailto else None
    session = build_session(user_agent(mailto or None), params=params)
    return SourceContext(session=session)


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.lower() or None


def _reconstruct_abstract(inv_index: dict | None) -> str | None:
    if not inv_index:
        return None
    words: dict[int, str] = {}
    for word, positions in inv_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words)) or None


def _summarize(work: dict) -> dict[str, Any]:
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    doi = _normalize_doi(work.get("doi"))
    authors = [
        a.get("author", {}).get("display_name") or ""
        for a in work.get("authorships", [])
    ]
    authors = [a for a in authors if a]
    return {
        "source": SOURCE_NAME,
        "title": work.get("display_name"),
        "authors": authors,
        "year": work.get("publication_year"),
        "venue": source.get("display_name"),
        "doi": doi,
        "url": loc.get("landing_page_url") or work.get("id"),
        "external_id": work.get("id"),
    }


def get_by_doi(doi: str, ctx: SourceContext) -> tuple[dict | None, float | None]:
    norm = _normalize_doi(doi)
    if not norm:
        return None, None
    resp = ctx.session.get(f"{_BASE}/doi:{norm}", timeout=30)
    if resp.status_code == 200:
        return _summarize(resp.json()), 1.0
    if resp.status_code in (404, 410):
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None


def get_by_arxiv_id(
    arxiv_id: str, ctx: SourceContext
) -> tuple[dict | None, float | None]:
    bare = re.sub(r"v\d+$", "", arxiv_id.strip())
    return get_by_doi(f"10.48550/arXiv.{bare}", ctx)


def search_by_title(
    title: str, ctx: SourceContext,
) -> tuple[dict | None, float | None]:
    resp = ctx.session.get(_BASE, params={"search": title, "per-page": 5}, timeout=30)
    if resp.status_code == 200:
        results = resp.json().get("results", [])
        if not results:
            return None, None
        cands = [
            (title_ratio(title, w.get("display_name")), w)
            for w in results
        ]
        best_sim, best_work = max(cands, key=lambda x: x[0])
        return _summarize(best_work), best_sim
    if resp.status_code == 404:
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None
