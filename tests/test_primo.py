"""Tests for ref_checker.sources.primo — offline (mocked HTTP).

The critical property tested first: every function is a safe no-op
(returns (None, None) and never touches the network) when the three
required env vars are not set.

Live tests are opt-in: they require WAKE_PRIMO_BASE_URL, WAKE_PRIMO_VID,
and WAKE_PRIMO_INST to be set in the environment, and borrow those values
into the PRIMO_* vars that ref-checker uses.  They are skipped
automatically when those vars are absent.
"""
from __future__ import annotations

import os
from unittest.mock import Mock

import pytest

from ref_checker.errors import RateLimited
from ref_checker.sources import primo
from ref_checker.sources.base import SourceContext

_ENV_VARS = ("PRIMO_BASE_URL", "PRIMO_VID", "PRIMO_INST", "PRIMO_SCOPE")


@pytest.fixture(autouse=True)
def _clear_primo_env(monkeypatch):
    """Every test starts fully unconfigured unless it sets vars explicitly."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def _configure(monkeypatch):
    """Set the three required PRIMO_* vars to known test values."""
    monkeypatch.setenv("PRIMO_BASE_URL", "https://example.primo.exlibrisgroup.com")
    monkeypatch.setenv("PRIMO_VID", "01EX_INST:01EX")
    monkeypatch.setenv("PRIMO_INST", "01EX_INST")


def _mock_response(status_code: int, json_body=None, headers=None):
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.raise_for_status = Mock()
    return resp


def _fake_session(status_code: int, json_body=None, headers=None):
    """Return a SourceContext whose session is a Mock returning one response."""
    resp = _mock_response(status_code, json_body, headers)
    session = Mock()
    session.get = Mock(return_value=resp)
    return SourceContext(session=session), session


def _doc(title, doi=None, authors=None, year=None, venue=None, record_id=None):
    """Build a minimal Primo PNX doc dict."""
    display: dict = {"title": [title]}
    if venue:
        display["ispartof"] = [venue]
    if year:
        display["creationdate"] = [str(year)]
    addata: dict = {}
    if doi:
        addata["doi"] = [doi]
    if authors:
        addata["au"] = list(authors)
    control: dict = {}
    if record_id:
        control["recordid"] = [record_id]
    return {"pnx": {"display": display, "addata": addata, "control": control}}


# ---------------------------------------------------------------------------
# Safety: unconfigured => never touches the network
# ---------------------------------------------------------------------------


class TestUnconfigured:
    def test_is_enabled_false(self):
        assert primo.is_enabled() is False

    def test_endpoint_none(self):
        assert primo._endpoint() is None

    def test_get_by_doi_noop(self):
        ctx, session = _fake_session(200, {"docs": []})
        result = primo.get_by_doi("10.1234/fake", ctx)
        session.get.assert_not_called()
        assert result == (None, None)

    def test_search_by_title_noop(self):
        ctx, session = _fake_session(200, {"docs": []})
        result = primo.search_by_title("Some Paper Title", ctx)
        session.get.assert_not_called()
        assert result == (None, None)

    def test_endpoint_requires_vid_and_inst(self, monkeypatch):
        monkeypatch.setenv("PRIMO_BASE_URL", "https://example.primo.exlibrisgroup.com")
        assert primo._endpoint() is None
        assert primo.is_enabled() is False

    def test_endpoint_requires_base_url(self, monkeypatch):
        monkeypatch.setenv("PRIMO_VID", "01EX_INST:01EX")
        monkeypatch.setenv("PRIMO_INST", "01EX_INST")
        assert primo._endpoint() is None
        assert primo.is_enabled() is False


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


class TestEndpointResolution:
    def test_resolves_all_three_vars(self, monkeypatch):
        _configure(monkeypatch)
        ep = primo._endpoint()
        assert ep is not None
        assert ep["base_url"] == "https://example.primo.exlibrisgroup.com"
        assert ep["vid"] == "01EX_INST:01EX"
        assert ep["inst"] == "01EX_INST"
        assert ep["scope"] == "MyInst_and_CI"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("PRIMO_BASE_URL", "https://example.primo.exlibrisgroup.com/")
        monkeypatch.setenv("PRIMO_VID", "01EX_INST:01EX")
        monkeypatch.setenv("PRIMO_INST", "01EX_INST")
        ep = primo._endpoint()
        assert ep["base_url"] == "https://example.primo.exlibrisgroup.com"

    def test_custom_scope(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setenv("PRIMO_SCOPE", "CustomScope")
        ep = primo._endpoint()
        assert ep["scope"] == "CustomScope"

    def test_is_enabled_true_when_configured(self, monkeypatch):
        _configure(monkeypatch)
        assert primo.is_enabled() is True


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_plain(self):
        assert primo._normalize_doi("10.1234/foo") == "10.1234/foo"

    def test_doi_org_prefix(self):
        assert primo._normalize_doi("https://doi.org/10.1234/foo") == "10.1234/foo"

    def test_dx_doi_org_prefix(self):
        assert primo._normalize_doi("https://dx.doi.org/10.1234/foo") == "10.1234/foo"

    def test_doi_colon_prefix(self):
        assert primo._normalize_doi("doi:10.1234/FOO") == "10.1234/foo"

    def test_uppercased_lowered(self):
        assert primo._normalize_doi("10.1234/ABC") == "10.1234/abc"

    def test_none(self):
        assert primo._normalize_doi(None) is None

    def test_empty(self):
        assert primo._normalize_doi("") is None


class TestCleanDescription:
    def test_strips_html(self):
        assert primo._clean_description("<p>Hello <b>world</b></p>") == "Hello world"

    def test_plain_text_unchanged(self):
        assert primo._clean_description("Plain text.") == "Plain text."

    def test_none(self):
        assert primo._clean_description(None) is None

    def test_empty(self):
        assert primo._clean_description("") is None


class TestSummarize:
    def test_all_fields(self):
        doc = _doc("A Great Paper", doi="10.1234/x", authors=["Smith, A.", "Jones, B."],
                   year=2023, venue="Journal of Things", record_id="ANL123")
        s = primo._summarize(doc)
        assert s["source"] == "primo"
        assert s["title"] == "A Great Paper"
        assert s["authors"] == ["Smith, A.", "Jones, B."]
        assert s["year"] == 2023
        assert s["venue"] == "Journal of Things"
        assert s["doi"] == "10.1234/x"
        assert s["url"] == "https://doi.org/10.1234/x"
        assert s["external_id"] == "ANL123"

    def test_no_doi_url_is_none(self):
        doc = _doc("No DOI Paper")
        s = primo._summarize(doc)
        assert s["doi"] is None
        assert s["url"] is None

    def test_doi_normalized(self):
        doc = _doc("X", doi="https://doi.org/10.1234/ABC")
        assert primo._summarize(doc)["doi"] == "10.1234/abc"

    def test_trailing_period_stripped_from_title(self):
        doc = _doc("A Title.")
        assert primo._summarize(doc)["title"] == "A Title"

    def test_year_extracted_from_creationdate(self):
        doc = _doc("X", year=2021)
        assert primo._summarize(doc)["year"] == 2021

    def test_missing_year_is_none(self):
        doc = _doc("X")
        assert primo._summarize(doc)["year"] is None

    def test_no_record_id_is_none(self):
        doc = _doc("X")
        assert primo._summarize(doc)["external_id"] is None


# ---------------------------------------------------------------------------
# get_by_doi (mocked HTTP, endpoint configured)
# ---------------------------------------------------------------------------


class TestGetByDoi:
    def test_happy_path(self, monkeypatch):
        _configure(monkeypatch)
        doc = _doc("A DOI Paper", doi="10.1234/real", record_id="REC1")
        ctx, session = _fake_session(200, {"docs": [doc]})
        summary, sim = primo.get_by_doi("10.1234/real", ctx)
        assert sim == 1.0
        assert summary["title"] == "A DOI Paper"
        assert summary["doi"] == "10.1234/real"
        call_kwargs = session.get.call_args[1]
        assert "any,contains,10.1234/real" in call_kwargs["params"]["q"]

    def test_doi_normalized_before_query(self, monkeypatch):
        _configure(monkeypatch)
        doc = _doc("X", doi="10.1234/abc")
        ctx, session = _fake_session(200, {"docs": [doc]})
        primo.get_by_doi("https://doi.org/10.1234/ABC", ctx)
        call_kwargs = session.get.call_args[1]
        assert "10.1234/abc" in call_kwargs["params"]["q"]

    def test_empty_doi_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, session = _fake_session(200, {"docs": []})
        assert primo.get_by_doi("", ctx) == (None, None)
        session.get.assert_not_called()

    def test_none_doi_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, session = _fake_session(200, {"docs": []})
        assert primo.get_by_doi(None, ctx) == (None, None)  # type: ignore[arg-type]
        session.get.assert_not_called()

    def test_empty_docs_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(200, {"docs": []})
        assert primo.get_by_doi("10.1234/x", ctx) == (None, None)

    def test_404_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(404)
        assert primo.get_by_doi("10.1234/x", ctx) == (None, None)

    def test_410_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(410)
        assert primo.get_by_doi("10.1234/x", ctx) == (None, None)

    def test_429_raises_rate_limited(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(429, headers={"Retry-After": "30"})
        with pytest.raises(RateLimited) as excinfo:
            primo.get_by_doi("10.1234/x", ctx)
        assert excinfo.value.retry_after == 30.0

    def test_500_raises(self, monkeypatch):
        _configure(monkeypatch)
        from requests import HTTPError
        resp = _mock_response(500)
        resp.raise_for_status.side_effect = HTTPError("500")
        session = Mock()
        session.get = Mock(return_value=resp)
        ctx = SourceContext(session=session)
        with pytest.raises(HTTPError):
            primo.get_by_doi("10.1234/x", ctx)


# ---------------------------------------------------------------------------
# search_by_title (mocked HTTP, endpoint configured)
# ---------------------------------------------------------------------------


class TestSearchByTitle:
    def test_happy_picks_best(self, monkeypatch):
        _configure(monkeypatch)
        docs = [
            _doc("Something Completely Different"),
            _doc("PVFS: A Parallel File System for Linux Clusters", doi="10.1234/pvfs"),
            _doc("Another Unrelated Paper"),
        ]
        ctx, _ = _fake_session(200, {"docs": docs})
        summary, sim = primo.search_by_title("PVFS: A Parallel File System for Linux Clusters", ctx)
        assert summary is not None
        assert summary["title"] == "PVFS: A Parallel File System for Linux Clusters"
        assert sim >= 0.99

    def test_low_similarity_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        docs = [_doc("Something Completely Unrelated")]
        ctx, _ = _fake_session(200, {"docs": docs})
        assert primo.search_by_title("Very Different Title Here", ctx) == (None, None)

    def test_empty_title_returns_none_without_query(self, monkeypatch):
        _configure(monkeypatch)
        ctx, session = _fake_session(200, {"docs": []})
        assert primo.search_by_title("", ctx) == (None, None)
        session.get.assert_not_called()

    def test_empty_docs_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(200, {"docs": []})
        assert primo.search_by_title("Any Title", ctx) == (None, None)

    def test_404_returns_none(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(404)
        assert primo.search_by_title("Any Title", ctx) == (None, None)

    def test_429_raises_rate_limited(self, monkeypatch):
        _configure(monkeypatch)
        ctx, _ = _fake_session(429, headers={"Retry-After": "60"})
        with pytest.raises(RateLimited):
            primo.search_by_title("Any Title", ctx)

    def test_query_uses_title_field(self, monkeypatch):
        _configure(monkeypatch)
        docs = [_doc("Exact Title Match")]
        ctx, session = _fake_session(200, {"docs": docs})
        primo.search_by_title("Exact Title Match", ctx)
        call_kwargs = session.get.call_args[1]
        assert call_kwargs["params"]["q"].startswith("title,contains,")


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_returns_source_context(self):
        ctx = primo.build_context()
        assert isinstance(ctx, SourceContext)
        assert ctx.session is not None

    def test_user_agent_with_mailto(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "test@example.com")
        ctx = primo.build_context()
        ua = ctx.session.headers.get("User-Agent", "")
        assert "mailto:test@example.com" in ua

    def test_user_agent_without_mailto(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
        ctx = primo.build_context()
        ua = ctx.session.headers.get("User-Agent", "")
        assert "ref-checker" in ua


# ---------------------------------------------------------------------------
# Live / integration tests — skipped unless WAKE_PRIMO_* env vars are set
# ---------------------------------------------------------------------------

_live = pytest.mark.skipif(
    not all(os.environ.get(f"WAKE_PRIMO_{k}") for k in ("BASE_URL", "VID", "INST")),
    reason="WAKE_PRIMO_BASE_URL / WAKE_PRIMO_VID / WAKE_PRIMO_INST not set",
)


@pytest.fixture()
def _live_primo_env(monkeypatch):
    """Map WAKE_PRIMO_* values into the PRIMO_* vars that ref-checker reads."""
    monkeypatch.setenv("PRIMO_BASE_URL", os.environ["WAKE_PRIMO_BASE_URL"])
    monkeypatch.setenv("PRIMO_VID", os.environ["WAKE_PRIMO_VID"])
    monkeypatch.setenv("PRIMO_INST", os.environ["WAKE_PRIMO_INST"])
    scope = os.environ.get("WAKE_PRIMO_SCOPE", "")
    if scope:
        monkeypatch.setenv("PRIMO_SCOPE", scope)


@_live
def test_live_is_enabled(_live_primo_env):
    assert primo.is_enabled() is True


@_live
def test_live_get_by_doi_known_paper(_live_primo_env):
    ctx = primo.build_context()
    summary, sim = primo.get_by_doi("10.1145/3458817.3476177", ctx)
    assert summary is not None, "Expected a hit for a known DOI"
    assert sim == 1.0
    assert summary["source"] == "primo"


@_live
def test_live_search_by_title_known_paper(_live_primo_env):
    ctx = primo.build_context()
    title = "PVFS: A Parallel File System for Linux Clusters"
    summary, sim = primo.search_by_title(title, ctx)
    assert summary is not None, f"Expected a hit for known title: {title!r}"
    assert sim >= 0.85
    assert summary["source"] == "primo"
