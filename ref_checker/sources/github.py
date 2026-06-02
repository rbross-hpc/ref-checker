"""GitHub URL liveness checker.

Checks whether GitHub URLs extracted from a reference are still live
(i.e., not 404). Treats a live URL as a 1.0-similarity match since the
URL itself is the identity of the referenced resource.
"""
from __future__ import annotations

from typing import Any

import requests

SOURCE_NAME = "github"

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
        "venue": "GitHub",
        "doi": None,
        "url": url,
        "external_id": None,
    }
