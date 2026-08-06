"""CrossRef source: DOI lookup and title search."""
from __future__ import annotations

import os
import re
from typing import Any

from ..model import QueryKind
from ..similarity import title_ratio
from ._http import build_session, raise_for_rate_limit
from .base import SourceContext

SOURCE_NAME = "crossref"
DEFAULT_DELAY = 2.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.TITLE})

_BASE = "https://api.crossref.org/works"


def build_context() -> SourceContext:
    """Build the CrossRef :class:`SourceContext` once per run.

    Shares ``OPENALEX_MAILTO`` with OpenAlex — same polite-pool convention.
    """
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    user_agent = f"ref-checker/0.1 (mailto:{mailto})" if mailto else "ref-checker/0.1"
    params = {"mailto": mailto} if mailto else None
    session = build_session(user_agent, params=params)
    return SourceContext(session=session)


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.lower() or None


def _summarize(entry: dict) -> dict[str, Any]:
    titles = entry.get("title") or []
    title = titles[0] if titles else None
    authors = []
    for a in entry.get("author") or []:
        given = a.get("given", "")
        family = a.get("family", "")
        name = f"{given} {family}".strip() if given else family
        if name:
            authors.append(name)
    year = None
    date_parts = (
        (entry.get("published-print") or entry.get("published-online") or {})
        .get("date-parts") or [[]]
    )
    if date_parts and date_parts[0]:
        year = date_parts[0][0]
    container = entry.get("container-title") or []
    venue = container[0] if container else None
    doi = _normalize_doi(entry.get("DOI"))
    url = f"https://doi.org/{doi}" if doi else entry.get("URL")
    return {
        "source": SOURCE_NAME,
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "url": url,
        "external_id": entry.get("DOI"),
    }


def get_by_doi(doi: str, ctx: SourceContext) -> tuple[dict | None, float | None]:
    norm = _normalize_doi(doi)
    if not norm:
        return None, None
    resp = ctx.session.get(f"{_BASE}/{norm}", timeout=30)
    if resp.status_code == 200:
        message = resp.json().get("message", {})
        return _summarize(message), 1.0
    if resp.status_code in (404, 410):
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None


def search_by_title(
    title: str, ctx: SourceContext,
) -> tuple[dict | None, float | None]:
    params = {
        "query.bibliographic": title,
        "rows": 5,
        "select": "DOI,title,author,published-print,published-online,container-title,type,URL",
    }
    resp = ctx.session.get(_BASE, params=params, timeout=30)
    if resp.status_code == 200:
        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None, None
        cands = []
        for item in items:
            titles = item.get("title") or []
            cand_title = titles[0] if titles else None
            sim = title_ratio(title, cand_title)
            cands.append((sim, item))
        best_sim, best_item = max(cands, key=lambda x: x[0])
        return _summarize(best_item), best_sim
    if resp.status_code == 404:
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None
