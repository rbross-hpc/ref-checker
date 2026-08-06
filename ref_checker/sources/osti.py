"""OSTI source: DOI lookup and title search via the OSTI records API.

OSTI (https://www.osti.gov) is the U.S. Department of Energy's repository of
DOE-funded technical reports, journal articles, conference papers, patents,
theses, and datasets. It is the canonical source for DOE lab publications
(ANL, ORNL, LBNL, LLNL, ...) — many of which are technical reports that never
appear in CrossRef or DBLP.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..model import QueryKind
from ..similarity import title_ratio
from ._http import build_session, raise_for_rate_limit
from .base import SourceContext

SOURCE_NAME = "osti"
DEFAULT_DELAY = 2.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.TITLE})

_BASE = "https://www.osti.gov/api/v1/records"


def build_context() -> SourceContext:
    """Build the OSTI :class:`SourceContext` once per run.

    User-Agent only (no session-level params) — OSTI uses ``OPENALEX_MAILTO``
    in its UA string like OpenAlex/CrossRef, but doesn't accept a ``mailto``
    query param.
    """
    mailto = os.environ.get("OPENALEX_MAILTO", "")
    user_agent = f"ref-checker/0.1 (mailto:{mailto})" if mailto else "ref-checker/0.1"
    return SourceContext(session=build_session(user_agent))


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.lower() or None


def _parse_authors(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for a in raw:
        if isinstance(a, dict):
            name = a.get("name")
            if not name:
                first = (a.get("first_name") or "").strip()
                last = (a.get("last_name") or "").strip()
                name = f"{first} {last}".strip()
        elif isinstance(a, str):
            name = a.split("[")[0].strip().rstrip(",").strip()
        else:
            name = ""
        if name:
            out.append(name)
    return out


def _extract_year(publication_date: Any) -> int | None:
    if not isinstance(publication_date, str) or len(publication_date) < 4:
        return None
    head = publication_date[:4]
    try:
        return int(head)
    except ValueError:
        return None


def _extract_url(links: Any) -> str | None:
    if not isinstance(links, list):
        return None
    for lnk in links:
        if isinstance(lnk, dict) and lnk.get("rel") == "citation":
            href = lnk.get("href")
            if href:
                return href
    return None


def _summarize(record: dict) -> dict[str, Any]:
    doi = _normalize_doi(record.get("doi"))
    url = _extract_url(record.get("links"))
    if not url and doi:
        url = f"https://doi.org/{doi}"
    venue = record.get("journal_name") or record.get("publisher") or None
    osti_id = record.get("osti_id")
    return {
        "source": SOURCE_NAME,
        "title": record.get("title"),
        "authors": _parse_authors(record.get("authors")),
        "year": _extract_year(record.get("publication_date")),
        "venue": venue,
        "doi": doi,
        "url": url,
        "external_id": str(osti_id) if osti_id is not None else None,
    }


def get_by_doi(doi: str, ctx: SourceContext) -> tuple[dict | None, float | None]:
    norm = _normalize_doi(doi)
    if not norm:
        return None, None
    resp = ctx.session.get(_BASE, params={"doi": norm}, timeout=30)
    if resp.status_code == 200:
        records = resp.json()
        if isinstance(records, list) and records:
            return _summarize(records[0]), 1.0
        return None, None
    if resp.status_code in (404, 410):
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None


def search_by_title(
    title: str, ctx: SourceContext
) -> tuple[dict | None, float | None]:
    resp = ctx.session.get(_BASE, params={"title": title}, timeout=30)
    if resp.status_code == 200:
        records = resp.json()
        if not isinstance(records, list) or not records:
            return None, None
        cands = [
            (title_ratio(title, r.get("title")), r)
            for r in records
        ]
        best_sim, best_record = max(cands, key=lambda x: x[0])
        return _summarize(best_record), best_sim
    if resp.status_code in (404, 410):
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None
