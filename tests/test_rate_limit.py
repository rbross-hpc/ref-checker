"""Tests for RateLimited handling, polite-pool params, and Retry-After parsing.

No network — all sessions monkeypatched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from ref_checker.errors import RateLimited
from ref_checker.sources import arxiv, crossref, dblp, openalex, osti, semanticscholar
from ref_checker.sources._http import parse_retry_after, raise_for_rate_limit
from ref_checker.sources.base import SourceContext


# --------------------------------------------------------------------------
# Fake plumbing
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    """Mimics ``requests.Session``'s auto-merge of session-level ``params``
    (set once, e.g. by ``build_context()``) into every per-call ``params=``.
    """

    def __init__(self, response: _FakeResponse, params: dict | None = None):
        self._response = response
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}
        self.params: dict = dict(params or {})

    def get(self, url, params=None, headers=None, timeout=None):
        merged = dict(self.params)
        if params:
            merged.update(params)
        self.calls.append((url, merged))
        return self._response


def _ctx(session, credentials=None) -> SourceContext:
    return SourceContext(session=session, credentials=credentials or {})


# --------------------------------------------------------------------------
# parse_retry_after
# --------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_integer_seconds(self):
        resp = _FakeResponse(429, headers={"Retry-After": "7"})
        assert parse_retry_after(resp) == pytest.approx(7.0)

    def test_float_seconds(self):
        resp = _FakeResponse(429, headers={"Retry-After": "2.5"})
        assert parse_retry_after(resp) == pytest.approx(2.5)

    def test_http_date(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=15)
        resp = _FakeResponse(
            429, headers={"Retry-After": format_datetime(future, usegmt=True)}
        )
        val = parse_retry_after(resp)
        assert val is not None
        assert 10.0 <= val <= 20.0

    def test_missing_header(self):
        resp = _FakeResponse(429, headers={})
        assert parse_retry_after(resp) is None

    def test_unparseable_header(self):
        resp = _FakeResponse(429, headers={"Retry-After": "nonsense"})
        assert parse_retry_after(resp) is None

    def test_empty_header(self):
        resp = _FakeResponse(429, headers={"Retry-After": "   "})
        assert parse_retry_after(resp) is None

    def test_negative_clamped_to_zero(self):
        resp = _FakeResponse(429, headers={"Retry-After": "-5"})
        assert parse_retry_after(resp) == 0.0

    def test_case_insensitive(self):
        resp = _FakeResponse(429, headers={"retry-after": "3"})
        assert parse_retry_after(resp) == pytest.approx(3.0)

    def test_none_response(self):
        assert parse_retry_after(None) is None


# --------------------------------------------------------------------------
# raise_for_rate_limit
# --------------------------------------------------------------------------


class TestRaiseForRateLimit:
    def test_200_is_noop(self):
        resp = _FakeResponse(200)
        raise_for_rate_limit(resp, "openalex")  # should not raise

    def test_404_is_noop(self):
        resp = _FakeResponse(404)
        raise_for_rate_limit(resp, "openalex")

    def test_500_is_noop(self):
        resp = _FakeResponse(500)
        raise_for_rate_limit(resp, "openalex")

    def test_429_raises_with_retry_after(self):
        resp = _FakeResponse(429, headers={"Retry-After": "5"})
        with pytest.raises(RateLimited) as ei:
            raise_for_rate_limit(resp, "openalex")
        assert ei.value.retry_after == pytest.approx(5.0)

    def test_429_raises_with_none_when_header_missing(self):
        resp = _FakeResponse(429, headers={})
        with pytest.raises(RateLimited) as ei:
            raise_for_rate_limit(resp, "crossref")
        assert ei.value.retry_after is None


# --------------------------------------------------------------------------
# Polite-pool mailto query param
# --------------------------------------------------------------------------


class TestPoliteMailto:
    """``mailto`` is now set once at context-build time as a session-level
    param (``build_session(..., params={"mailto": ...})``), not re-added on
    every call — so these tests exercise ``build_context()`` directly and
    confirm ``requests`` auto-merges the session-level param into a real
    per-call request.
    """

    def test_openalex_get_by_doi_sends_mailto_when_set(self, monkeypatch):
        import requests as _rq

        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        ctx = openalex.build_context()
        assert ctx.session.params.get("mailto") == "test@example.org"
        req = ctx.session.prepare_request(
            _rq.Request("GET", "https://api.openalex.org/works/doi:10.1/x")
        )
        assert req.url is not None
        assert "mailto=test%40example.org" in req.url

    def test_openalex_get_by_doi_no_mailto_when_unset(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
        ctx = openalex.build_context()
        assert "mailto" not in ctx.session.params

    def test_openalex_search_by_title_sends_mailto(self, monkeypatch):
        import requests as _rq

        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        ctx = openalex.build_context()
        req = ctx.session.prepare_request(
            _rq.Request(
                "GET", "https://api.openalex.org/works", params={"search": "some title"}
            )
        )
        assert req.url is not None
        assert "mailto=test%40example.org" in req.url
        assert "search=some" in req.url

    def test_crossref_get_by_doi_sends_mailto(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        ctx = crossref.build_context()
        assert ctx.session.params.get("mailto") == "test@example.org"

    def test_crossref_search_by_title_sends_mailto(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        ctx = crossref.build_context()
        assert ctx.session.params.get("mailto") == "test@example.org"

    def test_crossref_no_mailto_when_unset(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
        ctx = crossref.build_context()
        assert "mailto" not in ctx.session.params


# --------------------------------------------------------------------------
# 429 raises RateLimited per source
# --------------------------------------------------------------------------


class Test429RaisesRateLimited:
    def test_openalex_get_by_doi(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "4"}))
        with pytest.raises(RateLimited) as ei:
            openalex.get_by_doi("10.1/x", _ctx(session))
        assert ei.value.retry_after == pytest.approx(4.0)

    def test_openalex_search_by_title(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "2"}))
        with pytest.raises(RateLimited) as ei:
            openalex.search_by_title("t", _ctx(session))
        assert ei.value.retry_after == pytest.approx(2.0)

    def test_crossref_get_by_doi(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "3"}))
        with pytest.raises(RateLimited) as ei:
            crossref.get_by_doi("10.1/x", _ctx(session))
        assert ei.value.retry_after == pytest.approx(3.0)

    def test_crossref_search_by_title(self):
        session = _FakeSession(_FakeResponse(429, headers={}))
        with pytest.raises(RateLimited):
            crossref.search_by_title("t", _ctx(session))

    def test_osti_get_by_doi(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "5"}))
        with pytest.raises(RateLimited) as ei:
            osti.get_by_doi("10.1/x", _ctx(session))
        assert ei.value.retry_after == pytest.approx(5.0)

    def test_osti_search_by_title(self):
        session = _FakeSession(_FakeResponse(429))
        with pytest.raises(RateLimited):
            osti.search_by_title("t", _ctx(session))

    def test_semanticscholar_get_by_doi(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "6"}))
        with pytest.raises(RateLimited) as ei:
            semanticscholar.get_by_doi("10.1/x", _ctx(session))
        assert ei.value.retry_after == pytest.approx(6.0)

    def test_semanticscholar_search_by_title(self):
        session = _FakeSession(_FakeResponse(429, headers={}))
        with pytest.raises(RateLimited):
            semanticscholar.search_by_title("t", _ctx(session))

    def test_dblp_search_by_title(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "7"}))
        with pytest.raises(RateLimited) as ei:
            dblp.search_by_title("t", _ctx(session))
        assert ei.value.retry_after == pytest.approx(7.0)

    def test_arxiv_get_by_arxiv_id(self):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "8"}))
        with pytest.raises(RateLimited) as ei:
            arxiv.get_by_arxiv_id("2401.00001", _ctx(session))
        assert ei.value.retry_after == pytest.approx(8.0)

    def test_arxiv_search_by_title(self):
        session = _FakeSession(_FakeResponse(429))
        with pytest.raises(RateLimited):
            arxiv.search_by_title("t", _ctx(session))


# --------------------------------------------------------------------------
# Semantic Scholar 403 hint
# --------------------------------------------------------------------------


class TestSemanticScholar403Hint:
    def test_get_by_doi_403_includes_hint(self):
        session = _FakeSession(_FakeResponse(403, headers={}))
        from requests import HTTPError
        with pytest.raises(HTTPError) as ei:
            semanticscholar.get_by_doi("10.1/x", _ctx(session))
        msg = str(ei.value)
        assert "403" in msg
        assert "SEMANTICSCHOLAR_API_KEY" in msg

    def test_search_by_title_403_includes_hint(self):
        session = _FakeSession(_FakeResponse(403, headers={}))
        from requests import HTTPError
        with pytest.raises(HTTPError) as ei:
            semanticscholar.search_by_title("t", _ctx(session))
        msg = str(ei.value)
        assert "403" in msg
        assert "SEMANTICSCHOLAR_API_KEY" in msg
