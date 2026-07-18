"""Tests for OSTI source module (no network — monkeypatched)."""
from __future__ import annotations

import pytest

from ref_checker.sources import osti
from ref_checker.sources.osti import (
    _extract_url,
    _extract_year,
    _normalize_doi,
    _parse_authors,
    _summarize,
)


# --------------------------------------------------------------------------
# Fake requests plumbing
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

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
        self.calls.append((url, params or {}))
        return self._response


@pytest.fixture
def make_session(monkeypatch):
    def _factory(status_code, payload):
        session = _FakeSession(_FakeResponse(status_code, payload))
        monkeypatch.setattr(osti, "_session", lambda: session)
        return session
    return _factory


# --------------------------------------------------------------------------
# _normalize_doi
# --------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_plain(self):
        assert _normalize_doi("10.2172/1234567") == "10.2172/1234567"

    def test_doi_org_prefix(self):
        assert _normalize_doi("https://doi.org/10.2172/1234567") == "10.2172/1234567"

    def test_dx_doi_org_prefix(self):
        assert _normalize_doi("https://dx.doi.org/10.2172/1234567") == "10.2172/1234567"

    def test_doi_colon_prefix(self):
        assert _normalize_doi("doi: 10.2172/1234567") == "10.2172/1234567"

    def test_uppercased_lowered(self):
        assert _normalize_doi("10.2172/ABCDEF") == "10.2172/abcdef"

    def test_none(self):
        assert _normalize_doi(None) is None

    def test_empty(self):
        assert _normalize_doi("") is None


# --------------------------------------------------------------------------
# _parse_authors
# --------------------------------------------------------------------------


class TestParseAuthors:
    def test_dict_with_name(self):
        raw = [{"name": "Alice Smith"}, {"name": "Bob Jones"}]
        assert _parse_authors(raw) == ["Alice Smith", "Bob Jones"]

    def test_dict_with_first_last(self):
        raw = [{"first_name": "Alice", "last_name": "Smith"},
               {"first_name": "Bob", "last_name": "Jones"}]
        assert _parse_authors(raw) == ["Alice Smith", "Bob Jones"]

    def test_dict_name_preferred_over_first_last(self):
        raw = [{"name": "Alice Q. Smith", "first_name": "Alice", "last_name": "Smith"}]
        assert _parse_authors(raw) == ["Alice Q. Smith"]

    def test_bare_string_strips_affiliation(self):
        raw = ["Doe, J. [ANL]"]
        assert _parse_authors(raw) == ["Doe, J."]

    def test_bare_string_no_affiliation(self):
        raw = ["Doe, John"]
        assert _parse_authors(raw) == ["Doe, John"]

    def test_empty_names_skipped(self):
        raw = [{"name": ""}, {"first_name": "", "last_name": ""}, "", {"name": "Alice"}]
        assert _parse_authors(raw) == ["Alice"]

    def test_non_list_returns_empty(self):
        assert _parse_authors(None) == []
        assert _parse_authors("foo") == []
        assert _parse_authors({"name": "Alice"}) == []

    def test_mixed_forms(self):
        raw = [
            {"name": "Alice Smith"},
            "Doe, J. [ORNL]",
            {"first_name": "Bob", "last_name": "Jones"},
        ]
        assert _parse_authors(raw) == ["Alice Smith", "Doe, J.", "Bob Jones"]


# --------------------------------------------------------------------------
# _extract_year
# --------------------------------------------------------------------------


class TestExtractYear:
    def test_valid(self):
        assert _extract_year("2024-03-15") == 2024

    def test_year_only(self):
        assert _extract_year("2024") == 2024

    def test_none(self):
        assert _extract_year(None) is None

    def test_empty(self):
        assert _extract_year("") is None

    def test_too_short(self):
        assert _extract_year("202") is None

    def test_non_numeric_prefix(self):
        assert _extract_year("abcd-01-01") is None

    def test_non_string(self):
        assert _extract_year(2024) is None


# --------------------------------------------------------------------------
# _extract_url
# --------------------------------------------------------------------------


class TestExtractUrl:
    def test_citation_link_found(self):
        links = [
            {"rel": "fulltext", "href": "https://osti.gov/fulltext/x"},
            {"rel": "citation", "href": "https://osti.gov/biblio/1234567"},
        ]
        assert _extract_url(links) == "https://osti.gov/biblio/1234567"

    def test_no_citation_link(self):
        links = [{"rel": "fulltext", "href": "https://osti.gov/fulltext/x"}]
        assert _extract_url(links) is None

    def test_empty_list(self):
        assert _extract_url([]) is None

    def test_non_list(self):
        assert _extract_url(None) is None


# --------------------------------------------------------------------------
# _summarize
# --------------------------------------------------------------------------


class TestSummarize:
    def _record(self, **overrides):
        base = {
            "osti_id": 1234567,
            "doi": "10.2172/1234567",
            "title": "A DOE Report",
            "authors": [{"first_name": "Alice", "last_name": "Smith"}],
            "publication_date": "2024-03-15",
            "journal_name": "Journal of Reports",
            "publisher": "Argonne National Laboratory",
            "links": [{"rel": "citation", "href": "https://osti.gov/biblio/1234567"}],
        }
        base.update(overrides)
        return base

    def test_all_fields(self):
        s = _summarize(self._record())
        assert s["source"] == "osti"
        assert s["title"] == "A DOE Report"
        assert s["authors"] == ["Alice Smith"]
        assert s["year"] == 2024
        assert s["venue"] == "Journal of Reports"
        assert s["doi"] == "10.2172/1234567"
        assert s["url"] == "https://osti.gov/biblio/1234567"
        assert s["external_id"] == "1234567"

    def test_journal_name_preferred_over_publisher(self):
        s = _summarize(self._record(journal_name="J. Preferred", publisher="Pub"))
        assert s["venue"] == "J. Preferred"

    def test_publisher_fallback_when_no_journal(self):
        s = _summarize(self._record(journal_name=None))
        assert s["venue"] == "Argonne National Laboratory"

    def test_both_venue_missing(self):
        s = _summarize(self._record(journal_name=None, publisher=None))
        assert s["venue"] is None

    def test_url_falls_back_to_doi(self):
        s = _summarize(self._record(links=[]))
        assert s["url"] == "https://doi.org/10.2172/1234567"

    def test_url_none_when_no_doi_and_no_citation(self):
        s = _summarize(self._record(links=[], doi=None))
        assert s["url"] is None

    def test_external_id_stringified(self):
        s = _summarize(self._record(osti_id=99))
        assert s["external_id"] == "99"

    def test_external_id_none_when_missing(self):
        s = _summarize(self._record(osti_id=None))
        assert s["external_id"] is None

    def test_missing_publication_date(self):
        s = _summarize(self._record(publication_date=None))
        assert s["year"] is None

    def test_doi_normalized(self):
        s = _summarize(self._record(doi="https://doi.org/10.2172/ABC"))
        assert s["doi"] == "10.2172/abc"


# --------------------------------------------------------------------------
# get_by_doi
# --------------------------------------------------------------------------


class TestGetByDoi:
    def test_happy(self, make_session):
        payload = [{
            "osti_id": 42,
            "doi": "10.2172/42",
            "title": "Report",
            "authors": [{"name": "A"}],
            "publication_date": "2024",
            "journal_name": None,
            "publisher": "ANL",
            "links": [{"rel": "citation", "href": "https://osti.gov/biblio/42"}],
        }]
        session = make_session(200, payload)
        summary, sim = osti.get_by_doi("10.2172/42")
        assert sim == 1.0
        assert summary["external_id"] == "42"
        assert session.calls[0][1] == {"doi": "10.2172/42"}

    def test_doi_normalized_before_query(self, make_session):
        session = make_session(200, [])
        osti.get_by_doi("https://doi.org/10.2172/ABC")
        assert session.calls[0][1] == {"doi": "10.2172/abc"}

    def test_empty_doi_returns_none(self, make_session):
        make_session(200, [])
        assert osti.get_by_doi("") == (None, None)

    def test_none_doi_returns_none(self, make_session):
        make_session(200, [])
        assert osti.get_by_doi(None) == (None, None)

    def test_404_returns_none(self, make_session):
        make_session(404, None)
        assert osti.get_by_doi("10.2172/42") == (None, None)

    def test_410_returns_none(self, make_session):
        make_session(410, None)
        assert osti.get_by_doi("10.2172/42") == (None, None)

    def test_empty_list_returns_none(self, make_session):
        make_session(200, [])
        assert osti.get_by_doi("10.2172/42") == (None, None)

    def test_non_list_returns_none(self, make_session):
        make_session(200, {"error": "bad"})
        assert osti.get_by_doi("10.2172/42") == (None, None)

    def test_500_raises(self, make_session):
        from requests import HTTPError
        make_session(500, None)
        with pytest.raises(HTTPError):
            osti.get_by_doi("10.2172/42")


# --------------------------------------------------------------------------
# search_by_title
# --------------------------------------------------------------------------


class TestSearchByTitle:
    def test_happy_picks_best(self, make_session):
        payload = [
            {"osti_id": 1, "title": "Something Unrelated",
             "publication_date": "2020", "authors": [], "links": []},
            {"osti_id": 2, "title": "A DOE Report on X",
             "publication_date": "2024", "authors": [], "links": []},
            {"osti_id": 3, "title": "A Different Report",
             "publication_date": "2022", "authors": [], "links": []},
        ]
        make_session(200, payload)
        summary, sim = osti.search_by_title("A DOE Report on X")
        assert summary["external_id"] == "2"
        assert sim == pytest.approx(1.0)

    def test_empty_list_returns_none(self, make_session):
        make_session(200, [])
        assert osti.search_by_title("anything") == (None, None)

    def test_non_list_returns_none(self, make_session):
        make_session(200, {"error": "bad"})
        assert osti.search_by_title("anything") == (None, None)

    def test_404_returns_none(self, make_session):
        make_session(404, None)
        assert osti.search_by_title("anything") == (None, None)

    def test_500_raises(self, make_session):
        from requests import HTTPError
        make_session(500, None)
        with pytest.raises(HTTPError):
            osti.search_by_title("anything")
