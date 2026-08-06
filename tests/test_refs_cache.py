"""Tests for the per-paper reference extraction cache."""
import json
from pathlib import Path

from ref_checker.extract import Reference, write_refs_cache, load_refs_cache


def _make_refs():
    return [
        Reference(index=1, raw="First ref", title="A Paper", authors=["Smith"], year=2020),
        Reference(index=2, raw="Second ref", title="B Paper", authors=["Jones"], year=2019),
    ]


def _make_pdf(tmp_path: Path, content: bytes = b"fake pdf content") -> Path:
    p = tmp_path / "paper.pdf"
    p.write_bytes(content)
    return p


class TestWriteRefsCache:
    def test_writes_valid_json(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        refs = _make_refs()
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, refs, {"model": "test", "tail_pages": 5})
        data = json.loads(cache.read_text())
        assert data["schema_version"] == 1
        assert "pdf_sha256" in data
        assert len(data["references"]) == 2

    def test_contains_pdf_name(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        refs = _make_refs()
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, refs, {})
        data = json.loads(cache.read_text())
        assert data["pdf"] == "paper.pdf"

    def test_extractor_meta_stored(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, _make_refs(), {"model": "GPT-5.4", "tail_pages": 5})
        data = json.loads(cache.read_text())
        assert data["extractor"]["model"] == "GPT-5.4"
        assert data["extractor"]["tail_pages"] == 5

    def test_atomic_no_tmp_leftover(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, _make_refs(), {})
        assert cache.exists()
        assert not (tmp_path / "paper.tmp").exists()


class TestLoadRefsCache:
    def test_valid_load(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        refs = _make_refs()
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, refs, {})
        loaded, reason = load_refs_cache(cache, pdf)
        assert reason == "valid"
        assert len(loaded) == 2
        assert loaded[0].title == "A Paper"
        assert loaded[1].title == "B Paper"

    def test_missing_returns_missing(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        cache = tmp_path / "nonexistent.refs.json"
        loaded, reason = load_refs_cache(cache, pdf)
        assert loaded is None
        assert reason == "missing"

    def test_corrupt_json_returns_corrupt(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        cache = tmp_path / "paper.refs.json"
        cache.write_text("not json{{{")
        loaded, reason = load_refs_cache(cache, pdf)
        assert loaded is None
        assert reason == "corrupt"

    def test_bare_list_returns_schema_mismatch(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        cache = tmp_path / "paper.refs.json"
        cache.write_text(json.dumps([{"index": 1, "raw": "x", "title": "y"}]))
        loaded, reason = load_refs_cache(cache, pdf)
        assert loaded is None
        assert reason == "schema_mismatch"

    def test_wrong_schema_version_returns_schema_mismatch(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        cache = tmp_path / "paper.refs.json"
        cache.write_text(json.dumps({"schema_version": 99, "pdf_sha256": "x", "references": []}))
        loaded, reason = load_refs_cache(cache, pdf)
        assert loaded is None
        assert reason == "schema_mismatch"

    def test_hash_mismatch_on_changed_pdf(self, tmp_path):
        pdf = _make_pdf(tmp_path, b"original content")
        refs = _make_refs()
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, refs, {})
        pdf.write_bytes(b"different content")
        loaded, reason = load_refs_cache(cache, pdf)
        assert loaded is None
        assert reason == "hash_mismatch"

    def test_roundtrip_preserves_fields(self, tmp_path):
        pdf = _make_pdf(tmp_path)
        refs = _make_refs()
        cache = tmp_path / "paper.refs.json"
        write_refs_cache(cache, pdf, refs, {})
        loaded, _ = load_refs_cache(cache, pdf)
        assert loaded[0].index == 1
        assert loaded[0].raw == "First ref"
        assert loaded[0].authors == ["Smith"]
        assert loaded[0].year == 2020
        assert loaded[1].index == 2
