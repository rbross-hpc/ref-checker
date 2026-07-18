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
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params) if params else {}))
        return self._response


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
    def test_openalex_get_by_doi_sends_mailto_when_set(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        session = _FakeSession(_FakeResponse(200, {
            "display_name": "T", "publication_year": 2020,
            "authorships": [], "primary_location": {}, "doi": None, "id": "x",
        }))
        monkeypatch.setattr(openalex, "_session", lambda: session)
        openalex.get_by_doi("10.1/x")
        assert session.calls
        _, params = session.calls[0]
        assert params.get("mailto") == "test@example.org"

    def test_openalex_get_by_doi_no_mailto_when_unset(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
        session = _FakeSession(_FakeResponse(200, {
            "display_name": "T", "publication_year": 2020,
            "authorships": [], "primary_location": {}, "doi": None, "id": "x",
        }))
        monkeypatch.setattr(openalex, "_session", lambda: session)
        openalex.get_by_doi("10.1/x")
        _, params = session.calls[0]
        assert "mailto" not in params

    def test_openalex_search_by_title_sends_mailto(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        session = _FakeSession(_FakeResponse(200, {"results": []}))
        monkeypatch.setattr(openalex, "_session", lambda: session)
        openalex.search_by_title("some title")
        _, params = session.calls[0]
        assert params.get("mailto") == "test@example.org"
        assert params.get("search") == "some title"

    def test_crossref_get_by_doi_sends_mailto(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        session = _FakeSession(_FakeResponse(200, {"message": {}}))
        monkeypatch.setattr(crossref, "_session", lambda: session)
        crossref.get_by_doi("10.1/x")
        _, params = session.calls[0]
        assert params.get("mailto") == "test@example.org"

    def test_crossref_search_by_title_sends_mailto(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
        session = _FakeSession(_FakeResponse(200, {"message": {"items": []}}))
        monkeypatch.setattr(crossref, "_session", lambda: session)
        crossref.search_by_title("some title")
        _, params = session.calls[0]
        assert params.get("mailto") == "test@example.org"

    def test_crossref_no_mailto_when_unset(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
        session = _FakeSession(_FakeResponse(200, {"message": {}}))
        monkeypatch.setattr(crossref, "_session", lambda: session)
        crossref.get_by_doi("10.1/x")
        _, params = session.calls[0]
        assert "mailto" not in params


# --------------------------------------------------------------------------
# 429 raises RateLimited per source
# --------------------------------------------------------------------------


class Test429RaisesRateLimited:
    def test_openalex_get_by_doi(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "4"}))
        monkeypatch.setattr(openalex, "_session", lambda: session)
        with pytest.raises(RateLimited) as ei:
            openalex.get_by_doi("10.1/x")
        assert ei.value.retry_after == pytest.approx(4.0)

    def test_openalex_search_by_title(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "2"}))
        monkeypatch.setattr(openalex, "_session", lambda: session)
        with pytest.raises(RateLimited) as ei:
            openalex.search_by_title("t")
        assert ei.value.retry_after == pytest.approx(2.0)

    def test_crossref_get_by_doi(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "3"}))
        monkeypatch.setattr(crossref, "_session", lambda: session)
        with pytest.raises(RateLimited) as ei:
            crossref.get_by_doi("10.1/x")
        assert ei.value.retry_after == pytest.approx(3.0)

    def test_crossref_search_by_title(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={}))
        monkeypatch.setattr(crossref, "_session", lambda: session)
        with pytest.raises(RateLimited):
            crossref.search_by_title("t")

    def test_osti_get_by_doi(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "5"}))
        monkeypatch.setattr(osti, "_session", lambda: session)
        with pytest.raises(RateLimited) as ei:
            osti.get_by_doi("10.1/x")
        assert ei.value.retry_after == pytest.approx(5.0)

    def test_osti_search_by_title(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429))
        monkeypatch.setattr(osti, "_session", lambda: session)
        with pytest.raises(RateLimited):
            osti.search_by_title("t")

    def test_semanticscholar_get_by_doi(self, monkeypatch):
        resp = _FakeResponse(429, headers={"Retry-After": "6"})

        def _fake_get(url, params=None, headers=None, timeout=None):
            return resp

        import requests as _rq
        monkeypatch.setattr(_rq, "get", _fake_get)
        with pytest.raises(RateLimited) as ei:
            semanticscholar.get_by_doi("10.1/x")
        assert ei.value.retry_after == pytest.approx(6.0)

    def test_semanticscholar_search_by_title(self, monkeypatch):
        resp = _FakeResponse(429, headers={})

        def _fake_get(url, params=None, headers=None, timeout=None):
            return resp

        import requests as _rq
        monkeypatch.setattr(_rq, "get", _fake_get)
        with pytest.raises(RateLimited):
            semanticscholar.search_by_title("t")

    def test_dblp_search_by_title(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "7"}))
        monkeypatch.setattr(dblp, "_session", lambda: session)
        with pytest.raises(RateLimited) as ei:
            dblp.search_by_title("t")
        assert ei.value.retry_after == pytest.approx(7.0)

    def test_arxiv_get_by_arxiv_id(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429, headers={"Retry-After": "8"}))
        monkeypatch.setattr(arxiv, "_session", lambda: session)
        with pytest.raises(RateLimited) as ei:
            arxiv.get_by_arxiv_id("2401.00001")
        assert ei.value.retry_after == pytest.approx(8.0)

    def test_arxiv_search_by_title(self, monkeypatch):
        session = _FakeSession(_FakeResponse(429))
        monkeypatch.setattr(arxiv, "_session", lambda: session)
        with pytest.raises(RateLimited):
            arxiv.search_by_title("t")
