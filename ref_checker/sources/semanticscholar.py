"""Semantic Scholar source: DOI lookup, arXiv-ID lookup, and title search."""
from __future__ import annotations

import os
import re
from typing import Any

import requests

from ..model import QueryKind
from ..similarity import title_ratio
from ._http import raise_for_rate_limit

SOURCE_NAME = "semanticscholar"
DEFAULT_DELAY = 8.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.ARXIV_ID, QueryKind.TITLE})

_BASE = "https://api.semanticscholar.org/graph/v1/paper"
_FIELDS = "title,authors,year,venue,externalIds"


def _headers() -> dict[str, str]:
    h: dict[str, str] = {}
    api_key = os.environ.get("SEMANTICSCHOLAR_API_KEY", "")
    if api_key:
        h["x-api-key"] = api_key
    return h


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.lower() or None


def _summarize(entry: dict) -> dict[str, Any]:
    ext = entry.get("externalIds") or {}
    doi = _normalize_doi(ext.get("DOI"))
    arxiv_id = ext.get("ArXiv")
    authors = [a.get("name", "") for a in entry.get("authors") or []]
    authors = [a for a in authors if a]
    url = None
    if doi:
        url = f"https://doi.org/{doi}"
    elif arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
    else:
        paper_id = entry.get("paperId", "")
        if paper_id:
            url = f"https://www.semanticscholar.org/paper/{paper_id}"
    return {
        "source": SOURCE_NAME,
        "title": entry.get("title"),
        "authors": authors,
        "year": entry.get("year"),
        "venue": entry.get("venue") or None,
        "doi": doi,
        "url": url,
        "external_id": entry.get("paperId"),
    }


def _get_paper(paper_id_str: str) -> tuple[dict | None, float | None]:
    """Fetch a paper by its Semantic Scholar paper ID string (e.g. 'DOI:10.x/y')."""
    resp = requests.get(
        f"{_BASE}/{paper_id_str}",
        params={"fields": _FIELDS},
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code == 200:
        entry = resp.json()
        if entry:
            return _summarize(entry), 1.0
    if resp.status_code in (404, 410):
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    if resp.status_code == 403:
        from requests import HTTPError
        raise HTTPError(
            "403 Forbidden from Semantic Scholar — check SEMANTICSCHOLAR_API_KEY "
            "(auth failure, not rate limit)"
        )
    resp.raise_for_status()
    return None, None


def get_by_doi(doi: str) -> tuple[dict | None, float | None]:
    norm = _normalize_doi(doi)
    if not norm:
        return None, None
    return _get_paper(f"DOI:{norm}")


def get_by_arxiv_id(arxiv_id: str) -> tuple[dict | None, float | None]:
    bare = re.sub(r"v\d+$", "", arxiv_id.strip())
    return _get_paper(f"arXiv:{bare}")


def search_by_title(
    title: str,
) -> tuple[dict | None, float | None]:
    params: dict[str, Any] = {
        "query": title,
        "limit": 5,
        "fields": _FIELDS,
    }
    resp = requests.get(
        f"{_BASE}/search",
        params=params,
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code == 200:
        data = resp.json().get("data") or []
        if not data:
            return None, None
        cands = [
            (title_ratio(title, entry.get("title")), entry)
            for entry in data
        ]
        best_sim, best_entry = max(cands, key=lambda x: x[0])
        return _summarize(best_entry), best_sim
    if resp.status_code == 404:
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    if resp.status_code == 403:
        from requests import HTTPError
        raise HTTPError(
            "403 Forbidden from Semantic Scholar — check SEMANTICSCHOLAR_API_KEY "
            "(auth failure, not rate limit)"
        )
    resp.raise_for_status()
    return None, None
