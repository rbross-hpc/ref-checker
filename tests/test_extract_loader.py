"""Tests for extract.load_references_from_list: the shared reference loader
used by both `check --refs-json` and `show`.
"""
import pytest

from ref_checker.extract import ReferenceLoadError, load_references_from_list


class TestAutoIndexing:
    def test_missing_indices_assigned_1_based(self):
        data = [
            {"title": "First"},
            {"title": "Second"},
            {"title": "Third"},
        ]
        refs = load_references_from_list(data)
        assert [r.index for r in refs] == [1, 2, 3]

    def test_explicit_indices_preserved(self):
        data = [
            {"index": 5, "title": "A"},
            {"index": 10, "title": "B"},
        ]
        refs = load_references_from_list(data)
        assert [r.index for r in refs] == [5, 10]

    def test_mixed_explicit_and_missing_indices(self):
        # Entry 1 has no index (auto-assigns to its 1-based position, 1).
        # Entry 2 explicitly claims index 2 (no collision).
        data = [
            {"title": "A"},
            {"index": 2, "title": "B"},
        ]
        refs = load_references_from_list(data)
        assert [r.index for r in refs] == [1, 2]

    def test_single_entry_no_index(self):
        data = [{"title": "Only One"}]
        refs = load_references_from_list(data)
        assert refs[0].index == 1


class TestDuplicateIndexRejection:
    def test_duplicate_explicit_indices_rejected(self):
        data = [
            {"index": 1, "title": "A"},
            {"index": 1, "title": "B"},
        ]
        with pytest.raises(ReferenceLoadError, match="duplicate"):
            load_references_from_list(data)

    def test_explicit_index_collides_with_auto_assigned(self):
        # Entry 1 has no index -> auto-assigns to position 1.
        # Entry 2 explicitly claims index 1 -> collision.
        data = [
            {"title": "A"},
            {"index": 1, "title": "B"},
        ]
        with pytest.raises(ReferenceLoadError, match="duplicate"):
            load_references_from_list(data)

    def test_invalid_index_type_strict_raises(self):
        data = [{"index": "not-a-number", "title": "A"}]
        with pytest.raises(ReferenceLoadError):
            load_references_from_list(data, strict=True)

    def test_invalid_index_type_permissive_skips(self, capsys):
        data = [
            {"index": "not-a-number", "title": "A"},
            {"title": "B"},
        ]
        refs = load_references_from_list(data, strict=False)
        assert len(refs) == 1
        assert refs[0].title == "B"
        err = capsys.readouterr().err
        assert "Warning" in err


class TestShapeValidation:
    def test_non_list_top_level_rejected(self):
        with pytest.raises(ReferenceLoadError):
            load_references_from_list({"references": []})

    def test_non_dict_entries_rejected(self):
        with pytest.raises(ReferenceLoadError):
            load_references_from_list(["not a dict"])

    def test_empty_list_returns_empty(self):
        assert load_references_from_list([]) == []


class TestStrictness:
    def test_strict_raises_on_malformed_entry(self):
        # A non-dict entry fails the top-level shape check before per-entry
        # parsing is even attempted, so use an entry whose per-field parsing
        # itself fails: Reference.from_dict tolerates most shapes, but a
        # non-integer explicit index is a reliable per-entry failure point.
        data = [{"index": [], "title": "A"}]
        with pytest.raises(ReferenceLoadError):
            load_references_from_list(data, strict=True)

    def test_permissive_skips_and_warns(self, capsys):
        data = [
            {"index": [], "title": "Bad"},
            {"title": "Good"},
        ]
        refs = load_references_from_list(data, strict=False)
        assert len(refs) == 1
        assert refs[0].title == "Good"
        err = capsys.readouterr().err
        assert "Warning" in err

    def test_strict_is_default(self):
        data = [{"index": [], "title": "Bad"}]
        with pytest.raises(ReferenceLoadError):
            load_references_from_list(data)


class TestFieldsPreserved:
    def test_all_fields_round_trip(self):
        data = [{
            "index": 1,
            "raw": "Full raw text",
            "title": "A Title",
            "authors": ["A. Author"],
            "year": 2020,
            "doi": "10.1/x",
            "arxiv_id": "2301.00001",
            "venue": "Some Venue",
            "url": "https://example.com",
        }]
        refs = load_references_from_list(data)
        ref = refs[0]
        assert ref.raw == "Full raw text"
        assert ref.title == "A Title"
        assert ref.authors == ["A. Author"]
        assert ref.year == 2020
        assert ref.doi == "10.1/x"
        assert ref.arxiv_id == "2301.00001"
        assert ref.venue == "Some Venue"
        assert ref.url == "https://example.com"
