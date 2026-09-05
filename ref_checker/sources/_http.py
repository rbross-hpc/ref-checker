"""Shared HTTP helpers for source modules."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .. import __version__
from ..errors import RateLimited


def user_agent(mailto: str | None = None) -> str:
    """Build a versioned User-Agent without setuptools-scm's local segment."""
    version = __version__.split("+", 1)[0]
    return f"ref-checker/{version} (mailto:{mailto})" if mailto else f"ref-checker/{version}"


def build_session(
    user_agent: str, params: dict[str, Any] | None = None
) -> requests.Session:
    """Build a ``requests.Session`` with a fixed User-Agent and, optionally,
    session-level query params (e.g. a polite-pool ``mailto``) that
    ``requests`` automatically merges into every per-call ``params=`` dict.

    Replaces the near-duplicated ``_session()`` helpers that used to live in
    each source module.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})
    if params:
        s.params = dict(params)
    return s


def parse_retry_after(resp: Any) -> float | None:
    """Parse the ``Retry-After`` header of a response.

    Supports integer-seconds and HTTP-date formats. Returns None if the
    header is missing or unparseable. Negative values are clamped to 0.
    """
    if resp is None:
        return None
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        secs = float(raw)
        return max(0.0, secs)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


def raise_for_rate_limit(resp: Any, source_name: str) -> None:
    """If *resp* is a 429, raise :class:`RateLimited` with parsed Retry-After.

    Callers should invoke this immediately before ``resp.raise_for_status()``
    so 429 responses are surfaced as ``RateLimited`` (which the outer retry
    loop handles specially) rather than generic ``HTTPError``.
    """
    status = getattr(resp, "status_code", None)
    if status != 429:
        return
    retry_after = parse_retry_after(resp)
    raise RateLimited(
        retry_after=retry_after,
        message=f"429 Too Many Requests from {source_name}"
        + (f" (Retry-After={retry_after}s)" if retry_after is not None else ""),
    )
