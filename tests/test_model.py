"""Tests for the typed domain model: QueryKind, OutcomeKind, EvidenceLevel,
SourceOutcome.
"""
from ref_checker.model import EvidenceLevel, OutcomeKind, QueryKind, SourceOutcome
from ref_checker.results import LookupResult


class TestStrEnumCompatibility:
    """OutcomeKind/QueryKind must compare, hash, and serialize like plain
    strings so existing sidecar JSON and string-literal comparisons keep
    working during incremental migration.
    """

    def test_outcome_kind_equals_plain_string(self):
        assert OutcomeKind.HIT_ID == "hit_id"
        assert OutcomeKind.RATE_LIMITED == "rate_limited"

    def test_outcome_kind_hashes_like_plain_string(self):
        d = {OutcomeKind.HIT_ID: "value"}
        assert d.get("hit_id") == "value"

    def test_outcome_kind_dict_lookup_with_plain_string_key(self):
        d = {"hit_id": 5}
        assert d.get(OutcomeKind.HIT_ID) == 5

    def test_query_kind_equals_plain_string(self):
        assert QueryKind.DOI == "doi"
        assert QueryKind.ARXIV_ID == "arxiv_id"

    def test_outcome_kind_json_serializes_as_plain_string(self):
        import json
        assert json.dumps(OutcomeKind.HIT_ID) == '"hit_id"'


class TestSourceOutcomeFromDict:
    def test_basic_fields(self):
        entry = {
            "status": "hit_id",
            "queried_by": ["doi"],
            "score": 1.0,
            "summary": {"title": "X"},
            "note": None,
        }
        so = SourceOutcome.from_dict("openalex", entry)
        assert so.source == "openalex"
        assert so.outcome == OutcomeKind.HIT_ID
        assert so.queried_by == [QueryKind.DOI]
        assert so.score == 1.0
        assert so.summary == {"title": "X"}
        assert so.note is None

    def test_multiple_queried_by(self):
        entry = {"status": "not_found", "queried_by": ["doi", "title"],
                  "score": None, "summary": None, "note": None}
        so = SourceOutcome.from_dict("crossref", entry)
        assert so.queried_by == [QueryKind.DOI, QueryKind.TITLE]

    def test_empty_queried_by(self):
        entry = {"status": "disabled", "queried_by": [],
                  "score": None, "summary": None, "note": "circuit breaker"}
        so = SourceOutcome.from_dict("arxiv", entry)
        assert so.queried_by == []
        assert so.note == "circuit breaker"


class TestEvidenceLevelValues:
    def test_all_members_are_str(self):
        for member in EvidenceLevel:
            assert isinstance(member.value, str)

    def test_json_serializes_as_plain_string(self):
        import json
        assert json.dumps(EvidenceLevel.CONFIRMED_IDENTIFIER) == '"confirmed_identifier"'


class TestLookupResultSourceOutcome:
    def test_returns_typed_outcome_for_known_source(self):
        r = LookupResult(per_source={
            "openalex": {"status": "hit_id", "queried_by": ["doi"],
                         "score": 1.0, "summary": {"title": "X"}, "note": None},
        })
        so = r.source_outcome("openalex")
        assert so is not None
        assert so.source == "openalex"
        assert so.outcome == OutcomeKind.HIT_ID
        assert so.queried_by == [QueryKind.DOI]
        assert so.score == 1.0

    def test_returns_none_for_unqueried_source(self):
        r = LookupResult(per_source={})
        assert r.source_outcome("openalex") is None

    def test_does_not_mutate_per_source(self):
        r = LookupResult(per_source={
            "openalex": {"status": "hit_id", "queried_by": ["doi"],
                         "score": 1.0, "summary": {"title": "X"}, "note": None},
        })
        original = dict(r.per_source)
        r.source_outcome("openalex")
        assert r.per_source == original
