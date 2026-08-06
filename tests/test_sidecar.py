"""Tests for the results sidecar module."""
import json
import pytest

from ref_checker.extract import Reference
from ref_checker.results import LookupResult
from ref_checker import sidecar


def _ref(index=1, raw="ref text", title="A Paper", year=2020):
    return Reference(index=index, raw=raw, title=title, year=year)


def _ok_result():
    return LookupResult(
        id_confirmed=True,
        display_score=0.99,
        best_source="openalex",
        best_summary={"doi": "10.1/test", "title": "A Paper", "url": None},
        doi_found_in=["openalex"],
    )


def _no_match_result():
    return LookupResult(
        display_score=0.42,
        best_source="crossref",
        best_summary={"title": "Something Else", "url": "https://x.com"},
    )


def _exhausted_result():
    return LookupResult(
        id_confirmed=True,
        display_score=0.95,
        best_source="openalex",
        best_summary={"doi": "10.1/x", "title": "A Paper", "url": None},
        exhausted_sources=["semanticscholar"],
    )


class TestRefsHash:
    def test_deterministic(self):
        refs = [_ref(1, "raw1"), _ref(2, "raw2")]
        assert sidecar.refs_hash(refs) == sidecar.refs_hash(refs)

    def test_order_independent(self):
        refs_ab = [_ref(1, "raw1"), _ref(2, "raw2")]
        refs_ba = [_ref(2, "raw2"), _ref(1, "raw1")]
        assert sidecar.refs_hash(refs_ab) == sidecar.refs_hash(refs_ba)

    def test_changes_with_content(self):
        refs_a = [_ref(1, "raw1")]
        refs_b = [_ref(1, "raw2")]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_with_extra_ref(self):
        refs_a = [_ref(1, "raw1")]
        refs_b = [_ref(1, "raw1"), _ref(2, "raw2")]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_when_title_differs_but_raw_same(self):
        refs_a = [Reference(index=1, raw="same raw", title="Original Title")]
        refs_b = [Reference(index=1, raw="same raw", title="Corrected Title")]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_when_doi_differs_but_raw_same(self):
        refs_a = [Reference(index=1, raw="same raw", title="T", doi="10.1/a")]
        refs_b = [Reference(index=1, raw="same raw", title="T", doi="10.1/b")]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_when_year_differs_but_raw_same(self):
        refs_a = [Reference(index=1, raw="same raw", title="T", year=2020)]
        refs_b = [Reference(index=1, raw="same raw", title="T", year=2021)]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_when_authors_differ_but_raw_same(self):
        refs_a = [Reference(index=1, raw="same raw", title="T", authors=["A. One"])]
        refs_b = [Reference(index=1, raw="same raw", title="T", authors=["B. Two"])]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_when_arxiv_id_differs_but_raw_same(self):
        refs_a = [Reference(index=1, raw="same raw", title="T", arxiv_id="2301.00001")]
        refs_b = [Reference(index=1, raw="same raw", title="T", arxiv_id="2301.00002")]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_changes_when_url_differs_but_raw_same(self):
        refs_a = [Reference(index=1, raw="same raw", title="T", url="https://a.example")]
        refs_b = [Reference(index=1, raw="same raw", title="T", url="https://b.example")]
        assert sidecar.refs_hash(refs_a) != sidecar.refs_hash(refs_b)

    def test_unchanged_when_nothing_differs(self):
        refs_a = [Reference(index=1, raw="raw", title="T", year=2020, doi="10.1/x")]
        refs_b = [Reference(index=1, raw="raw", title="T", year=2020, doi="10.1/x")]
        assert sidecar.refs_hash(refs_a) == sidecar.refs_hash(refs_b)


class TestStatusLabel:
    def test_id_confirmed_is_ok(self):
        r = LookupResult(id_confirmed=True, display_score=0.5)
        assert sidecar.status_label(r, 0.80) == "OK"

    def test_liveness_is_ok(self):
        r = LookupResult(is_liveness=True, display_score=None)
        assert sidecar.status_label(r, 0.80) == "OK"

    def test_high_score_is_ok(self):
        r = LookupResult(display_score=0.95)
        assert sidecar.status_label(r, 0.80) == "OK"

    def test_mid_score_is_closest(self):
        r = LookupResult(display_score=0.85)
        assert sidecar.status_label(r, 0.80) == "CLOSEST"

    def test_low_score_is_no_match(self):
        r = LookupResult(display_score=0.50)
        assert sidecar.status_label(r, 0.80) == "NO MATCH"

    def test_none_score_is_no_match(self):
        r = LookupResult(display_score=None)
        assert sidecar.status_label(r, 0.80) == "NO MATCH"


class TestResultRoundtrip:
    def test_ok_result_roundtrip(self):
        r = _ok_result()
        d = sidecar.result_to_dict(r, 0.80)
        r2 = sidecar.result_from_dict(d)
        assert r2.id_confirmed == r.id_confirmed
        assert r2.display_score == r.display_score
        assert r2.best_source == r.best_source
        assert r2.doi_found_in == r.doi_found_in

    def test_dead_urls_roundtrip(self):
        r = LookupResult(dead_urls=[("https://example.com", "HTTP 404")])
        d = sidecar.result_to_dict(r, 0.80)
        r2 = sidecar.result_from_dict(d)
        assert r2.dead_urls == [("https://example.com", "HTTP 404")]

    def test_status_in_dict(self):
        d = sidecar.result_to_dict(_ok_result(), 0.80)
        assert d["status"] == "OK"

    def test_no_match_status(self):
        d = sidecar.result_to_dict(_no_match_result(), 0.80)
        assert d["status"] == "NO MATCH"

    def test_per_source_roundtrip(self):
        r = LookupResult()
        r.per_source = {
            "openalex": {"status": "hit_id", "queried_by": ["doi"],
                         "score": 1.0, "summary": {"title": "X"}, "note": None},
            "crossref": {"status": "not_found", "queried_by": ["doi", "title"],
                         "score": None, "summary": None, "note": None},
            "dblp":     {"status": "error", "queried_by": ["title"],
                         "score": None, "summary": None,
                         "note": "retries exhausted"},
            "arxiv":    {"status": "disabled", "queried_by": [],
                         "score": None, "summary": None,
                         "note": "session circuit breaker"},
        }
        d = sidecar.result_to_dict(r, 0.80)
        r2 = sidecar.result_from_dict(d)
        assert r2.per_source == r.per_source

    def test_evidence_roundtrip(self):
        from ref_checker.model import EvidenceLevel

        r = _ok_result()
        r.evidence = EvidenceLevel.CONFIRMED_IDENTIFIER
        d = sidecar.result_to_dict(r, 0.80)
        assert d["evidence"] == "confirmed_identifier"
        r2 = sidecar.result_from_dict(d)
        assert r2.evidence == EvidenceLevel.CONFIRMED_IDENTIFIER

    def test_evidence_none_roundtrips_as_none(self):
        r = LookupResult()
        d = sidecar.result_to_dict(r, 0.80)
        assert d["evidence"] is None
        r2 = sidecar.result_from_dict(d)
        assert r2.evidence is None

    def test_missing_evidence_key_loads_as_none(self):
        """A sidecar written before this field existed (or hand-edited to
        omit it) should still load cleanly with evidence=None."""
        d = sidecar.result_to_dict(_ok_result(), 0.80)
        del d["evidence"]
        r2 = sidecar.result_from_dict(d)
        assert r2.evidence is None


class TestNeedsRetry:
    def test_ok_no_retry(self):
        d = sidecar.result_to_dict(_ok_result(), 0.80)
        assert sidecar.needs_retry(d, False) is False

    def test_no_match_retry(self):
        d = sidecar.result_to_dict(_no_match_result(), 0.80)
        assert sidecar.needs_retry(d, False) is True

    def test_exhausted_retried(self):
        d = sidecar.result_to_dict(_exhausted_result(), 0.80)
        assert sidecar.needs_retry(d, False) is True

    def test_dead_url_retried(self):
        r = LookupResult(
            id_confirmed=True, display_score=0.99,
            best_source="openalex",
            best_summary={"doi": "10.1/x", "url": None},
            dead_urls=[("https://github.com/x/y", "HTTP 404")],
        )
        d = sidecar.result_to_dict(r, 0.80)
        assert sidecar.needs_retry(d, False) is True

    def test_closest_not_retried_by_default(self):
        r = LookupResult(display_score=0.85, best_source="crossref",
                         best_summary={"title": "x", "url": "http://x.com"})
        d = sidecar.result_to_dict(r, 0.80)
        assert sidecar.needs_retry(d, retry_closest=False) is False

    def test_closest_retried_when_flag_set(self):
        r = LookupResult(display_score=0.85, best_source="crossref",
                         best_summary={"title": "x", "url": "http://x.com"})
        d = sidecar.result_to_dict(r, 0.80)
        assert sidecar.needs_retry(d, retry_closest=True) is True

    def test_unknown_status_retried(self):
        assert sidecar.needs_retry({"status": "BOGUS"}, False) is True


class TestWriteLoad:
    def test_write_and_load_valid(self, tmp_path):
        refs = [_ref(1), _ref(2, raw="ref2", title="B Paper")]
        results = {1: _ok_result(), 2: _no_match_result()}
        path = tmp_path / "test.results.json"
        sidecar.write(path, "test.pdf", refs, results, 0.80)
        entries, hash_ok = sidecar.load(path, refs)
        assert hash_ok is True
        assert 1 in entries
        assert 2 in entries
        assert entries[1]["result"]["status"] == "OK"
        assert entries[2]["result"]["status"] == "NO MATCH"

    def test_hash_mismatch_on_different_refs(self, tmp_path):
        refs = [_ref(1)]
        results = {1: _ok_result()}
        path = tmp_path / "test.results.json"
        sidecar.write(path, "test.pdf", refs, results, 0.80)
        other_refs = [_ref(1, raw="completely different raw text")]
        entries, hash_ok = sidecar.load(path, other_refs)
        assert hash_ok is False

    def test_missing_file_returns_empty(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        entries, hash_ok = sidecar.load(path, [_ref(1)])
        assert entries == {}
        assert hash_ok is False

    def test_corrupt_json_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json{{{{")
        entries, hash_ok = sidecar.load(path, [_ref(1)])
        assert entries == {}
        assert hash_ok is False

    def test_wrong_schema_version_returns_empty(self, tmp_path):
        path = tmp_path / "old.json"
        path.write_text(json.dumps({"schema_version": 99, "refs_hash": "x", "references": {}}))
        entries, hash_ok = sidecar.load(path, [_ref(1)])
        assert entries == {}
        assert hash_ok is False

    @pytest.mark.parametrize("old_version", [1, 2, 3])
    def test_outdated_schema_version_is_rejected(self, tmp_path, old_version):
        """Outdated schema versions must be hard-rejected (no upgrade)."""
        path = tmp_path / f"v{old_version}.json"
        path.write_text(json.dumps({
            "schema_version": old_version,
            "refs_hash": "x",
            "references": {"1": {"ref": {}, "result": {"status": "OK"}}},
        }))
        entries, hash_ok = sidecar.load(path, [_ref(1)])
        assert entries == {}
        assert hash_ok is False

    @pytest.mark.parametrize("old_version", [1, 2, 3])
    def test_outdated_schema_version_emits_warning(self, tmp_path, capsys, old_version):
        """Outdated but recognized schema versions emit a WARNING on load."""
        path = tmp_path / f"v{old_version}.json"
        path.write_text(json.dumps({
            "schema_version": old_version,
            "refs_hash": "x",
            "references": {},
        }))
        sidecar.load(path, [_ref(1)])
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert f"v{old_version}" in err
        assert str(path) in err

    def test_unknown_schema_version_is_silent(self, tmp_path, capsys):
        """Unrecognized schema versions are rejected silently (could be garbage)."""
        path = tmp_path / "unknown.json"
        path.write_text(json.dumps({
            "schema_version": 99,
            "refs_hash": "x",
            "references": {},
        }))
        sidecar.load(path, [_ref(1)])
        err = capsys.readouterr().err
        assert "WARNING" not in err

    def test_atomic_write_no_tmp_leftover(self, tmp_path):
        refs = [_ref(1)]
        path = tmp_path / "test.results.json"
        sidecar.write(path, "test.pdf", refs, {1: _ok_result()}, 0.80)
        assert path.exists()
        assert not (tmp_path / "test.tmp").exists()
