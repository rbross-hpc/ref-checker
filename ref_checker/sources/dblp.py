"""DBLP source: title search via the DBLP JSON API."""
from __future__ import annotations

import re
from typing import Any

import requests

from ..errors import RateLimited
from ..model import QueryKind
from ..similarity import title_ratio
from ._http import build_session, parse_retry_after, raise_for_rate_limit, user_agent
from .base import SourceContext

SOURCE_NAME = "dblp"
DEFAULT_DELAY = 1.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.TITLE})

_BASES = [
    "https://dblp.org/search/publ/api",
    "https://dblp.uni-trier.de/search/publ/api",
]
def build_context() -> SourceContext:
    """Build the DBLP :class:`SourceContext` once per run. User-Agent only."""
    return SourceContext(session=build_session(user_agent()))


def _normalize_authors(authors_field: Any) -> list[str]:
    if not authors_field:
        return []
    raw = authors_field.get("author")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    names = []
    for a in raw:
        t = a.get("text") or "" if isinstance(a, dict) else str(a)
        t = re.sub(r"\s+\d{4}$", "", t).strip()
        if t:
            names.append(t)
    return names


def _normalize_doi(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    return s.lower() or None


def _summarize(info: dict) -> dict[str, Any]:
    title = (info.get("title") or "").rstrip(".").strip() or None
    year_raw = info.get("year")
    try:
        year = int(year_raw) if year_raw else None
    except (TypeError, ValueError):
        year = None
    venue = info.get("venue") or None
    doi = _normalize_doi(info.get("doi"))
    url = info.get("url") or info.get("ee")
    if doi and not url:
        url = f"https://doi.org/{doi}"
    return {
        "source": SOURCE_NAME,
        "title": title,
        "authors": _normalize_authors(info.get("authors")),
        "year": year,
        "venue": venue,
        "doi": doi,
        "url": url,
        "external_id": info.get("key") or info.get("url"),
    }


def search_by_title(
    title: str, ctx: SourceContext,
) -> tuple[dict | None, float | None]:
    params: dict[str, Any] = {"q": title, "format": "json", "h": 5}
    last_exc: Exception | None = None
    for base in _BASES:
        try:
            resp = ctx.session.get(base, params=params, timeout=30)
            if resp.status_code in (404, 410):
                return None, None
            if resp.status_code == 503:
                retry_after = parse_retry_after(resp)
                last_exc = RateLimited(
                    retry_after=retry_after,
                    message=f"503 Service Unavailable from {base}"
                    + (
                        f" (Retry-After={retry_after}s)"
                        if retry_after is not None
                        else ""
                    ),
                )
                continue
            raise_for_rate_limit(resp, SOURCE_NAME)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                return None, None
            hits_container = (data.get("result") or {}).get("hits") or {}
            raw_hits = hits_container.get("hit")
            if not raw_hits:
                return None, None
            if isinstance(raw_hits, dict):
                raw_hits = [raw_hits]
            cands = []
            for h in raw_hits:
                info = h.get("info") or {}
                summary = _summarize(info)
                sim = title_ratio(title, summary["title"])
                cands.append((sim, summary))
            best_sim, best_summary = max(cands, key=lambda x: x[0])
            return best_summary, best_sim
        except (requests.exceptions.ConnectionError, RateLimited) as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return None, None
