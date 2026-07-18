"""Shared exception types for ref-checker."""
from __future__ import annotations


class RateLimited(Exception):
    """Raised by a source when the upstream API returns HTTP 429.

    ``retry_after`` is the number of seconds the server asked us to wait
    before retrying (from the ``Retry-After`` header), or ``None`` if the
    header was missing or unparseable. The outer retry loop honors this
    value in preference to the default backoff schedule.
    """

    def __init__(self, retry_after: float | None = None, message: str = "") -> None:
        super().__init__(message or f"rate limited (retry_after={retry_after})")
        self.retry_after = retry_after
