"""Ex Libris Primo discovery-layer source: DOI lookup and title search.

Primo aggregates publisher metadata (Elsevier, Springer, IEEE, ACM, ...) behind
a single institutional discovery endpoint. Its public (unauthenticated) REST API
returns full bibliographic records — including abstracts — for records the
library's Primo Central Index has harvested, which frequently covers publishers
that OpenAlex's abstract reconstruction misses entirely.

This module deliberately ships no default endpoint. Primo is an *institutional*
service; the endpoint is specific to whichever library the caller has access to
(e.g. Argonne's ``anl.primo.exlibrisgroup.com``) and must never be hard-coded,
or every ref-checker install would silently query one institution's system.
Every function here is a safe no-op (returns ``(None, None)``) unless all three
required env vars are set — see ``_endpoint()``.

Configuration (all optional; unset = feature inactive):

  ``PRIMO_BASE_URL``  -- e.g. ``https://anl.primo.exlibrisgroup.com``
  ``PRIMO_VID``       -- e.g. ``01ANL_INST:01ANL``
  ``PRIMO_INST``      -- e.g. ``01ANL_INST``
  ``PRIMO_SCOPE``     -- e.g. ``MyInst_and_CI`` (default when unset)

The source is enabled if and only if all three of ``PRIMO_BASE_URL``,
``PRIMO_VID``, and ``PRIMO_INST`` are set. ``is_enabled()`` returns False
otherwise, and the registry skips this source entirely.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..model import QueryKind
from ..similarity import title_ratio
from ._http import build_session, raise_for_rate_limit, user_agent
from .base import SourceContext

SOURCE_NAME = "primo"
DEFAULT_DELAY = 1.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.TITLE})

_DEFAULT_SCOPE = "MyInst_and_CI"
_DEFAULT_TAB = "Everything"
_DEFAULT_TITLE_SIMILARITY_THRESHOLD = 0.85


def _endpoint() -> dict[str, str] | None:
    """Resolve the Primo endpoint from environment variables.

    Returns None (feature inactive) if any of the three required vars is
    missing — the safe default for any ref-checker install that has not
    explicitly configured an institutional Primo endpoint.
    """
    base_url = os.environ.get("PRIMO_BASE_URL", "").strip()
    vid = os.environ.get("PRIMO_VID", "").strip()
    inst = os.environ.get("PRIMO_INST", "").strip()
    if not base_url or not vid or not inst:
        return None
    scope = os.environ.get("PRIMO_SCOPE", "").strip() or _DEFAULT_SCOPE
    return {
        "base_url": base_url.rstrip("/"),
        "vid": vid,
        "inst": inst,
        "scope": scope,
    }


def is_enabled() -> bool:
    """Return True iff all required Primo env vars are set."""
    return _endpoint() is not None


def build_context() -> SourceContext:
    """Build the Primo :class:`SourceContext` once per run.

    Uses ``OPENALEX_MAILTO`` in the User-Agent string (same polite-pool
    convention as CrossRef/OSTI) if set.
    """
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    return SourceContext(session=build_session(user_agent(mailto or None)))


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.lower() or None


def _clean_description(desc: str | None) -> str | None:
    """Strip light HTML artifacts that Primo abstracts occasionally carry."""
    if not desc:
        return None
    text = re.sub(r"<[^>]+>", "", desc).strip()
    return text or None


def _first_field(disp: dict[str, Any], key: str) -> str | None:
    val = disp.get(key)
    if isinstance(val, list) and val:
        return val[0]
    return None


def _parse_creators(addata: dict[str, Any]) -> list[str]:
    """Extract author names from PNX addata (au / addau fields)."""
    names: list[str] = []
    for key in ("au", "addau"):
        raw = addata.get(key)
        if isinstance(raw, list):
            names.extend(n for n in raw if isinstance(n, str) and n.strip())
    return names or []


def _extract_year(disp: dict[str, Any], addata: dict[str, Any]) -> int | None:
    """Try display.creationdate, then addata.date."""
    for val in (disp.get("creationdate"), addata.get("date")):
        if not val:
            continue
        if isinstance(val, list):
            val = val[0] if val else None
        if not val:
            continue
        m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(val))
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _summarize(doc: dict[str, Any]) -> dict[str, Any]:
    """Map a Primo PNX doc dict to ref-checker's canonical summary shape."""
    pnx = doc.get("pnx", {})
    disp = pnx.get("display", {})
    addata = pnx.get("addata", {})
    control = pnx.get("control", {})

    title_raw = _first_field(disp, "title")
    title = title_raw.rstrip(".").strip() if title_raw else None

    doi_list = addata.get("doi")
    doi_raw = doi_list[0] if isinstance(doi_list, list) and doi_list else None
    doi = _normalize_doi(doi_raw)

    url = f"https://doi.org/{doi}" if doi else None

    venue_raw = _first_field(disp, "ispartof") or _first_field(disp, "publisher")
    venue = venue_raw.strip() if venue_raw else None

    record_id_list = control.get("recordid")
    record_id = (
        record_id_list[0]
        if isinstance(record_id_list, list) and record_id_list
        else record_id_list if isinstance(record_id_list, str) else None
    )

    return {
        "source": SOURCE_NAME,
        "title": title,
        "authors": _parse_creators(addata),
        "year": _extract_year(disp, addata),
        "venue": venue,
        "doi": doi,
        "url": url,
        "external_id": record_id,
    }


def _query(q: str, *, field: str = "any", limit: int = 3, ctx: SourceContext) -> list[dict[str, Any]]:
    """Issue a raw PNX search and return the list of doc records (may be empty).

    Raises :class:`~ref_checker.errors.RateLimited` on 429; returns ``[]``
    for any other non-200 / no-endpoint case (best-effort — Primo is never
    a required lookup).
    """
    endpoint = _endpoint()
    if endpoint is None:
        return []

    url = f"{endpoint['base_url']}/primaws/rest/pub/pnxs"
    params = {
        "blendFacetsSeparately": "false",
        "getMore": "0",
        "inst": endpoint["inst"],
        "lang": "en",
        "limit": str(limit),
        "mode": "advanced",
        "offset": "0",
        "pcAvailability": "true",
        "q": f"{field},contains,{q}",
        "scope": endpoint["scope"],
        "skipDelivery": "Y",
        "sort": "rank",
        "tab": _DEFAULT_TAB,
        "vid": endpoint["vid"],
    }
    resp = ctx.session.get(url, params=params, timeout=20)
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            return []
        return data.get("docs", []) or []
    if resp.status_code in (404, 410):
        return []
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    return []


def get_by_doi(doi: str, ctx: SourceContext) -> tuple[dict | None, float | None]:
    """Return a bibliographic summary for *doi* from Primo, or ``(None, None)``.

    Raises :class:`~ref_checker.errors.RateLimited` on 429 so the engine
    can back off. Returns ``(None, None)`` for no-endpoint / no-match /
    other non-fatal failures.
    """
    norm = _normalize_doi(doi)
    if not norm:
        return None, None
    docs = _query(norm, field="any", limit=1, ctx=ctx)
    if not docs:
        return None, None
    return _summarize(docs[0]), 1.0


def search_by_title(title: str, ctx: SourceContext) -> tuple[dict | None, float | None]:
    """Return the best-matching Primo record for *title*, or ``(None, None)``.

    Fetches up to 5 candidates and picks the one with the highest title
    similarity, returning ``(None, None)`` if no candidate clears
    ``_DEFAULT_TITLE_SIMILARITY_THRESHOLD``.
    """
    if not title:
        return None, None
    docs = _query(title, field="title", limit=5, ctx=ctx)
    if not docs:
        return None, None

    best_summary: dict | None = None
    best_sim = 0.0
    for doc in docs:
        summary = _summarize(doc)
        sim = title_ratio(title, summary.get("title"))
        if sim > best_sim:
            best_sim = sim
            best_summary = summary

    if best_summary is not None and best_sim >= _DEFAULT_TITLE_SIMILARITY_THRESHOLD:
        return best_summary, best_sim
    return None, None
