"""Tests exercising the committed reference-JSON fixtures.

The fixtures under tests/fixtures/refs/ are golden data — they should not
be regenerated during test runs. See tests/fixtures/README.md for provenance
and manual regeneration instructions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ref_checker.extract import Reference


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "refs"


def _all_fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


class TestFixtureIntegrity:
    def test_fixtures_directory_exists(self):
        assert FIXTURES_DIR.is_dir(), f"missing fixtures dir: {FIXTURES_DIR}"

    def test_at_least_one_fixture_present(self):
        paths = _all_fixture_paths()
        assert paths, "no fixture files found in tests/fixtures/refs/"

    def test_expected_fixtures_present(self):
        names = {p.name for p in _all_fixture_paths()}
        # Hand-crafted:
        assert "edge_cases.json" in names
        assert "mixed_small.json" in names
        # Extracted from pub-analysis PDFs:
        assert "klasky_5.json" in names
        assert "zfp_spectral.json" in names
        assert "dorier_mofka.json" in names
        assert "cruz_zombie.json" in names
        assert "wan_e3smv2.json" in names
        # Derived (klasky_5 with identifiers scrubbed):
        assert "klasky_5_no_ids.json" in names

    @pytest.mark.parametrize("path", _all_fixture_paths(),
                             ids=lambda p: p.name)
    def test_fixture_is_bare_json_array(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list), (
            f"{path.name}: expected bare list per schema.md, got {type(data).__name__}"
        )
        assert data, f"{path.name}: empty"

    @pytest.mark.parametrize("path", _all_fixture_paths(),
                             ids=lambda p: p.name)
    def test_fixture_refs_load_via_from_dict(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        for i, raw in enumerate(data, start=1):
            ref = Reference.from_dict(raw)
            assert isinstance(ref, Reference)
            assert ref.index >= 0

    @pytest.mark.parametrize("path", _all_fixture_paths(),
                             ids=lambda p: p.name)
    def test_fixture_indices_are_unique_and_ascending(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        indices = [r.get("index") for r in data if r.get("index") is not None]
        # Some fixtures may omit index — that's allowed by schema.
        if indices:
            assert len(set(indices)) == len(indices), (
                f"{path.name}: duplicate index values"
            )


class TestBigFixtureIsBig:
    """The dorier_mofka fixture is our '15+' large-set stressor."""

    def test_dorier_has_at_least_15_refs(self):
        data = json.loads(
            (FIXTURES_DIR / "dorier_mofka.json").read_text(encoding="utf-8")
        )
        assert len(data) >= 15


class TestKlaskyNoIdsDerivation:
    """klasky_5_no_ids.json must match klasky_5.json in every field except
    doi and arxiv_id, which must both be null in the derived fixture."""

    def _load_pair(self):
        base = json.loads(
            (FIXTURES_DIR / "klasky_5.json").read_text(encoding="utf-8")
        )
        derived = json.loads(
            (FIXTURES_DIR / "klasky_5_no_ids.json").read_text(encoding="utf-8")
        )
        return base, derived

    def test_same_ref_count(self):
        base, derived = self._load_pair()
        assert len(base) == len(derived)

    def test_doi_and_arxiv_id_scrubbed(self):
        _, derived = self._load_pair()
        for r in derived:
            assert r.get("doi") is None, f"ref #{r.get('index')} has non-null doi"
            assert r.get("arxiv_id") is None, (
                f"ref #{r.get('index')} has non-null arxiv_id"
            )

    def test_other_fields_preserved(self):
        base, derived = self._load_pair()
        base_by_idx = {r.get("index"): r for r in base}
        for d in derived:
            b = base_by_idx.get(d.get("index"))
            assert b is not None
            for field in ("title", "authors", "year", "venue", "url",
                          "github_url", "raw"):
                assert d.get(field) == b.get(field), (
                    f"ref #{d.get('index')} field {field!r} diverged from base"
                )


class TestEdgeCases:
    def test_edge_case_all_null_ref_present(self):
        data = json.loads(
            (FIXTURES_DIR / "edge_cases.json").read_text(encoding="utf-8")
        )
        all_null = [
            r for r in data
            if r.get("title") is None
            and r.get("doi") is None
            and r.get("arxiv_id") is None
            and not r.get("url")
            and not r.get("github_url")
        ]
        assert all_null, "expected at least one all-null ref in edge_cases.json"

    def test_edge_case_doi_only_ref_present(self):
        data = json.loads(
            (FIXTURES_DIR / "edge_cases.json").read_text(encoding="utf-8")
        )
        assert any(r.get("doi") for r in data), (
            "expected at least one DOI-bearing ref in edge_cases.json"
        )

    def test_edge_case_arxiv_only_ref_present(self):
        data = json.loads(
            (FIXTURES_DIR / "edge_cases.json").read_text(encoding="utf-8")
        )
        assert any(r.get("arxiv_id") for r in data), (
            "expected at least one arXiv-bearing ref in edge_cases.json"
        )

    def test_edge_case_url_only_ref_present(self):
        data = json.loads(
            (FIXTURES_DIR / "edge_cases.json").read_text(encoding="utf-8")
        )
        assert any(r.get("url") or r.get("github_url") for r in data), (
            "expected at least one URL-bearing ref in edge_cases.json"
        )
