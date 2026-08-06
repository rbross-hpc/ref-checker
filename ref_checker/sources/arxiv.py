"""arXiv source: ID lookup and title search via the arXiv Atom API."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import requests

from ..similarity import title_ratio
from ._http import raise_for_rate_limit

SOURCE_NAME = "arxiv"

_BASE = "https://export.arxiv.org/api/query"
_NS = {"atom": "http://www.w3.org/2005/Atom"}
_USER_AGENT = "ref-checker/0.1"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT})
    return s


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    title = (entry.findtext("atom:title", "", _NS) or "").strip().replace("\n", " ")
    published = entry.findtext("atom:published", "", _NS) or ""
    entry_id = entry.findtext("atom:id", "", _NS) or ""
    authors = [
        a.findtext("atom:name", "", _NS) or ""
        for a in entry.findall("atom:author", _NS)
    ]
    authors = [a for a in authors if a]
    year = int(published[:4]) if len(published) >= 4 and published[:4].isdigit() else None
    arxiv_id_match = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?)", entry_id)
    bare_id = arxiv_id_match.group(1) if arxiv_id_match else None
    if bare_id:
        bare_id = re.sub(r"v\d+$", "", bare_id)
    doi = f"10.48550/arXiv.{bare_id}" if bare_id else None
    url = entry_id or (f"https://arxiv.org/abs/{bare_id}" if bare_id else None)
    return {
        "source": SOURCE_NAME,
        "title": title or None,
        "authors": authors,
        "year": year,
        "venue": "arXiv",
        "doi": doi,
        "url": url,
        "external_id": bare_id,
    }


def get_by_doi(doi: str) -> tuple[dict | None, float | None]:
    doi = doi.strip()
    m = re.search(r"arXiv\.(\d{4}\.\d{4,5})", doi, re.IGNORECASE)
    if m:
        return get_by_arxiv_id(m.group(1))
    return None, None


def get_by_arxiv_id(arxiv_id: str) -> tuple[dict | None, float | None]:
    bare = re.sub(r"v\d+$", "", arxiv_id.strip())
    resp = _session().get(
        _BASE,
        params={"id_list": bare, "max_results": 1},
        timeout=30,
    )
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None, None
    entry = root.find("atom:entry", _NS)
    if entry is None:
        return None, None
    return _parse_entry(entry), 1.0


def search_by_title(
    title: str,
) -> tuple[dict | None, float | None]:
    search_query = f'ti:"{title}"'
    resp = _session().get(
        _BASE,
        params={"search_query": search_query, "max_results": 5},
        timeout=30,
    )
    raise_for_rate_limit(resp, SOURCE_NAME)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None, None
    entries = root.findall("atom:entry", _NS)
    if not entries:
        return None, None
    cands = []
    for entry in entries:
        parsed = _parse_entry(entry)
        sim = title_ratio(title, parsed["title"])
        cands.append((sim, parsed))
    best_sim, best_parsed = max(cands, key=lambda x: x[0])
    return best_parsed, best_sim
