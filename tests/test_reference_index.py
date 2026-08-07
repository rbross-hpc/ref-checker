"""Tests for Reference.from_dict's index validation and the LLM-output
index pre-resolution helper (extract._resolve_llm_indices).

See tests/test_extract_loader.py for the equivalent coverage of
load_references_from_list's index handling.
"""
from __future__ import annotations

import pytest

from ref_checker.extract import Reference, _resolve_llm_indices


class TestReferenceFromDictIndexRequired:
    def test_missing_index_raises(self):
        with pytest.raises(ValueError, match="index"):
            Reference.from_dict({"title": "No index"})

    @pytest.mark.parametrize("bad_index", [1.5, 2.0, True, False, 0, -1, -100, "1", None, []])
    def test_invalid_index_raises(self, bad_index):
        with pytest.raises(ValueError):
            Reference.from_dict({"index": bad_index, "title": "Bad"})

    def test_valid_positive_int_index_accepted(self):
        ref = Reference.from_dict({"index": 1, "title": "Good"})
        assert ref.index == 1


class TestResolveLlmIndices:
    """_resolve_llm_indices falls back to 1-based list position on any
    missing, invalid, or duplicate LLM-supplied index -- LLM output is
    untrusted and a bad index shouldn't fail the whole extraction (unlike
    load_references_from_list's strict-mode ReferenceLoadError).
    """

    def test_missing_index_falls_back_to_position(self):
        entries = [{"title": "A"}, {"title": "B"}]
        resolved = _resolve_llm_indices(entries)
        assert [e["index"] for e in resolved] == [1, 2]

    def test_valid_explicit_index_preserved(self):
        entries = [{"index": 5, "title": "A"}, {"index": 10, "title": "B"}]
        resolved = _resolve_llm_indices(entries)
        assert [e["index"] for e in resolved] == [5, 10]

    @pytest.mark.parametrize("bad_index", [1.5, True, 0, -1, "1"])
    def test_invalid_index_falls_back_to_position(self, bad_index):
        entries = [{"index": bad_index, "title": "A"}]
        resolved = _resolve_llm_indices(entries)
        assert resolved[0]["index"] == 1

    def test_duplicate_explicit_index_falls_back_to_position(self):
        # Both entries claim index 1; the second falls back to its own
        # 1-based position (2) instead of colliding or raising.
        entries = [{"index": 1, "title": "A"}, {"index": 1, "title": "B"}]
        resolved = _resolve_llm_indices(entries)
        assert [e["index"] for e in resolved] == [1, 2]

    def test_explicit_index_collides_with_earlier_fallback_position(self):
        # Entry 1 has no index -> falls back to position 1.
        # Entry 2 explicitly claims index 1 -> collision -> falls back to
        # its own position, 2.
        entries = [{"title": "A"}, {"index": 1, "title": "B"}]
        resolved = _resolve_llm_indices(entries)
        assert [e["index"] for e in resolved] == [1, 2]

    def test_other_fields_preserved(self):
        entries = [{"index": 3, "title": "A", "doi": "10.1/x"}]
        resolved = _resolve_llm_indices(entries)
        assert resolved[0]["title"] == "A"
        assert resolved[0]["doi"] == "10.1/x"

    def test_from_dict_succeeds_on_resolved_entries(self):
        entries = [{"title": "A"}, {"index": "bad", "title": "B"}]
        refs = [Reference.from_dict(r) for r in _resolve_llm_indices(entries)]
        assert [r.index for r in refs] == [1, 2]
        assert [r.title for r in refs] == ["A", "B"]
