"""Generic URL liveness checker — last-resort fallback for web-only references.

Used when a reference has no DOI, no arXiv ID, no GitHub URL, and no
scholarly source returned a good match. Checks whether a URL extracted
from the reference text is still live.
"""
from __future__ import annotations

from typing import Any

import requests

SOURCE_NAME = "url"
DEFAULT_DELAY = 1.0

_USER_AGENT = "ref-checker/0.1"


def check_url(urls: str) -> tuple[dict | None, float | None, list[tuple[str, str]]]:
    """HEAD-check each space-separated URL in *urls*.

    Returns (summary, 1.0, []) on the first live URL.
    Returns (None, None, [(url, reason), ...]) when all URLs are dead/failed.
    Raises on transient errors so the driver's retry loop can handle them.
    """
    dead: list[tuple[str, str]] = []
    for url in urls.split():
        try:
            resp = requests.head(
                url,
                allow_redirects=True,
                timeout=15,
                headers={"User-Agent": _USER_AGENT},
            )
        except requests.RequestException as exc:
            raise exc

        if resp.status_code in (200, 301, 302):
            final_url = resp.url if resp.url else url
            return _summarize(final_url), 1.0, []

        if resp.status_code in (404, 410):
            dead.append((url, f"HTTP {resp.status_code}"))
            continue

        resp.raise_for_status()

    return None, None, dead


def _summarize(url: str) -> dict[str, Any]:
    return {
        "source": SOURCE_NAME,
        "title": None,
        "authors": [],
        "year": None,
        "venue": "Web",
        "doi": None,
        "url": url,
        "external_id": None,
    }
