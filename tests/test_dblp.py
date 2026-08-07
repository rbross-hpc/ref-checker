"""Tests for DBLP source module (no network — fixture-based)."""
import pytest
from ref_checker.sources.dblp import _normalize_authors, _normalize_doi, _summarize


class TestNormalizeAuthors:
    def test_list_of_dicts(self):
        field = {"author": [{"@pid": "1", "text": "Alice Smith"},
                             {"@pid": "2", "text": "Bob Jones"}]}
        assert _normalize_authors(field) == ["Alice Smith", "Bob Jones"]

    def test_single_dict_not_list(self):
        field = {"author": {"@pid": "1", "text": "Alice Smith"}}
        assert _normalize_authors(field) == ["Alice Smith"]

    def test_disambig_digits_stripped(self):
        field = {"author": [{"text": "John Smith 0001"}, {"text": "Jane Doe 0002"}]}
        assert _normalize_authors(field) == ["John Smith", "Jane Doe"]

    def test_no_author_key(self):
        assert _normalize_authors({}) == []

    def test_none_input(self):
        assert _normalize_authors(None) == []

    def test_empty_names_skipped(self):
        field = {"author": [{"text": ""}, {"text": "Alice Smith"}]}
        assert _normalize_authors(field) == ["Alice Smith"]


class TestNormalizeDoi:
    def test_plain_doi(self):
        assert _normalize_doi("10.1145/3295500.3356169") == "10.1145/3295500.3356169"

    def test_doi_org_prefix(self):
        assert _normalize_doi("https://doi.org/10.1145/3295500") == "10.1145/3295500"

    def test_dx_doi_org_prefix(self):
        assert _normalize_doi("https://dx.doi.org/10.1145/3295500") == "10.1145/3295500"

    def test_doi_colon_prefix(self):
        assert _normalize_doi("doi:10.1145/3295500") == "10.1145/3295500"

    def test_uppercased_normalized(self):
        assert _normalize_doi("10.1145/ABC") == "10.1145/abc"

    def test_none_returns_none(self):
        assert _normalize_doi(None) is None

    def test_empty_returns_none(self):
        assert _normalize_doi("") is None


class TestSummarize:
    def _make_info(self, **kwargs):
        base = {
            "title": "MapReduce: simplified data processing on large clusters.",
            "year": "2008",
            "venue": "Commun. ACM",
            "doi": "10.1145/1327452.1327492",
            "url": "https://dblp.org/rec/journals/cacm/DeanG08",
            "key": "journals/cacm/DeanG08",
            "authors": {"author": [{"text": "Jeffrey Dean"}, {"text": "Sanjay Ghemawat"}]},
        }
        base.update(kwargs)
        return base

    def test_title_trailing_period_stripped(self):
        s = _summarize(self._make_info())
        assert s["title"] == "MapReduce: simplified data processing on large clusters"

    def test_year_as_int(self):
        s = _summarize(self._make_info())
        assert s["year"] == 2008

    def test_doi_normalized(self):
        s = _summarize(self._make_info())
        assert s["doi"] == "10.1145/1327452.1327492"

    def test_authors_extracted(self):
        s = _summarize(self._make_info())
        assert s["authors"] == ["Jeffrey Dean", "Sanjay Ghemawat"]

    def test_venue_present(self):
        s = _summarize(self._make_info())
        assert s["venue"] == "Commun. ACM"

    def test_source_name(self):
        s = _summarize(self._make_info())
        assert s["source"] == "dblp"

    def test_missing_doi_falls_back_to_url(self):
        s = _summarize(self._make_info(doi=None))
        assert s["doi"] is None
        assert s["url"] is not None

    def test_invalid_year_becomes_none(self):
        s = _summarize(self._make_info(year="not-a-year"))
        assert s["year"] is None

    def test_no_title_becomes_none(self):
        s = _summarize(self._make_info(title=None))
        assert s["title"] is None

    def test_empty_title_becomes_none(self):
        s = _summarize(self._make_info(title=""))
        assert s["title"] is None

    def test_punctuation_only_title_becomes_none(self):
        s = _summarize(self._make_info(title="..."))
        assert s["title"] is None


# --------------------------------------------------------------------------
# Mirror failover on RateLimited
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"HTTP {self.status_code}")


class TestMirrorFailover:
    def test_mirror_tried_on_rate_limit_from_primary(self, monkeypatch):
        from ref_checker.sources import dblp

        primary_url, mirror_url = dblp._BASES

        good_payload = {
            "result": {
                "hits": {
                    "hit": [
                        {"info": {"title": "MapReduce.", "year": "2008",
                                  "venue": "Commun. ACM",
                                  "doi": "10.1/x",
                                  "url": "https://dblp.org/x",
                                  "key": "k",
                                  "authors": {"author": [{"text": "A B"}]}}}
                    ]
                }
            }
        }
        calls: list[str] = []

        class _Session:
            headers: dict = {}

            def get(self, url, params=None, timeout=None):
                calls.append(url)
                if url == primary_url:
                    return _FakeResponse(429, headers={})
                return _FakeResponse(200, good_payload)

        from ref_checker.sources.base import SourceContext

        ctx = SourceContext(session=_Session())
        summary, sim = dblp.search_by_title("mapreduce", ctx)
        assert summary is not None
        assert summary["source"] == "dblp"
        assert calls == [primary_url, mirror_url]

    def test_both_mirrors_rate_limited_raises_rate_limited(self, monkeypatch):
        from ref_checker.sources import dblp
        from ref_checker.errors import RateLimited

        calls: list[str] = []

        class _Session:
            headers: dict = {}

            def get(self, url, params=None, timeout=None):
                calls.append(url)
                return _FakeResponse(429, headers={"Retry-After": "7"})

        from ref_checker.sources.base import SourceContext

        ctx = SourceContext(session=_Session())
        with pytest.raises(RateLimited) as ei:
            dblp.search_by_title("mapreduce", ctx)
        assert ei.value.retry_after == 7.0
        assert len(calls) == len(dblp._BASES)

    def test_both_mirrors_503_raises_rate_limited_with_retry_after(self, monkeypatch):
        from ref_checker.sources import dblp
        from ref_checker.errors import RateLimited

        calls: list[str] = []

        class _Session:
            headers: dict = {}

            def get(self, url, params=None, timeout=None):
                calls.append(url)
                return _FakeResponse(503, headers={"Retry-After": "12"})

        from ref_checker.sources.base import SourceContext

        ctx = SourceContext(session=_Session())
        with pytest.raises(RateLimited) as ei:
            dblp.search_by_title("mapreduce", ctx)
        assert ei.value.retry_after == 12.0
        assert len(calls) == len(dblp._BASES)

    def test_503_from_primary_still_fails_over(self, monkeypatch):
        from ref_checker.sources import dblp

        primary_url, mirror_url = dblp._BASES

        good_payload = {
            "result": {
                "hits": {
                    "hit": [
                        {"info": {"title": "Y.", "year": "2021",
                                  "venue": "V", "doi": "10.1/z",
                                  "url": "https://dblp.org/z",
                                  "key": "k",
                                  "authors": {"author": [{"text": "A B"}]}}}
                    ]
                }
            }
        }
        calls: list[str] = []

        class _Session:
            headers: dict = {}

            def get(self, url, params=None, timeout=None):
                calls.append(url)
                if url == primary_url:
                    return _FakeResponse(503, headers={})
                return _FakeResponse(200, good_payload)

        from ref_checker.sources.base import SourceContext

        ctx = SourceContext(session=_Session())
        summary, sim = dblp.search_by_title("y", ctx)
        assert summary is not None
        assert calls == [primary_url, mirror_url]

    def test_connection_error_on_primary_still_fails_over(self, monkeypatch):
        import requests
        from ref_checker.sources import dblp

        primary_url, mirror_url = dblp._BASES

        good_payload = {
            "result": {
                "hits": {
                    "hit": [
                        {"info": {"title": "X.", "year": "2020",
                                  "venue": "V", "doi": "10.1/y",
                                  "url": "https://dblp.org/y",
                                  "key": "k",
                                  "authors": {"author": [{"text": "A B"}]}}}
                    ]
                }
            }
        }
        calls: list[str] = []

        class _Session:
            headers: dict = {}

            def get(self, url, params=None, timeout=None):
                calls.append(url)
                if url == primary_url:
                    raise requests.exceptions.ConnectionError("boom")
                return _FakeResponse(200, good_payload)

        from ref_checker.sources.base import SourceContext

        ctx = SourceContext(session=_Session())
        summary, sim = dblp.search_by_title("x", ctx)
        assert summary is not None
        assert calls == [primary_url, mirror_url]
