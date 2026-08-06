"""CrossRef source: DOI lookup and title search."""
from __future__ import annotations

import os
import re
from typing import Any

import requests

from ..model import QueryKind
from ..similarity import title_ratio
from ._http import raise_for_rate_limit

SOURCE_NAME = "crossref"
DEFAULT_DELAY = 2.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.TITLE})

_BASE = "https://api.crossref.org/works"


def _mailto() -> str:
    return os.environ.get("OPENALEX_MAILTO", "").strip()


def _user_agent() -> str:
    mailto = _mailto()
    if mailto:
        return f"ref-checker/0.1 (mailto:{mailto})"
    return "ref-checker/0.1"


def _polite_params(base: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = dict(base or {})
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto
    return params


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _user_agent()})
    return s


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


def get_by_doi(doi: str) -> tuple[dict | None, float | None]:
    norm = _normalize_doi(doi)
    if not norm:
        return None, None
    resp = _session().get(f"{_BASE}/{norm}", params=_polite_params(), timeout=30)
    if resp.status_code == 200:
        message = resp.json().get("message", {})
        return _summarize(message), 1.0
    if resp.status_code in (404, 410):
        return None, None
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return None, None


def search_by_title(
    title: str,
) -> tuple[dict | None, float | None]:
    params = _polite_params({
        "query.bibliographic": title,
        "rows": 5,
        "select": "DOI,title,author,published-print,published-online,container-title,type,URL",
    })
    resp = _session().get(_BASE, params=params, timeout=30)
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
