"""Typed domain model for per-source query modes, outcomes, and evidence
strength.

``QueryKind`` and ``OutcomeKind`` are ``str``-subclassed enums whose values
match the plain strings already used throughout ``per_source`` entries and
the sidecar JSON (``"doi"``, ``"hit_id"``, ``"not_found"``, ...). Because a
``str`` subclass compares equal to, hashes equal to, and JSON-serializes
identically to a plain string with the same value, existing sidecar files
and any code still comparing against raw string literals continue to work
unchanged — this is purely an incremental typing layer, not a schema or
behavior change.

``EvidenceLevel`` is new: a finer-grained, additive classification of what a
lookup actually established (confirmed identifier vs. strong metadata match
vs. live-resource-only vs. ...), computed alongside the existing coarse
OK/CLOSEST/NO MATCH status rather than replacing it.

``SourceOutcome`` is the actual in-memory representation of one
``per_source[name]`` entry (``LookupResult.per_source: dict[str,
SourceOutcome]``) — not just a decorative read-only accessor. Dict
conversion is restricted to the sidecar's JSON serialization boundary
(``sidecar.py``'s ``result_to_dict``/``result_from_dict``, via
``SourceOutcome.to_dict``/``from_dict``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class QueryKind(str, Enum):
    """How a source was queried for a given attempt."""

    DOI = "doi"
    ARXIV_ID = "arxiv_id"
    TITLE = "title"
    URL = "url"


class OutcomeKind(str, Enum):
    """The result of one query attempt against one source.

    Stored verbatim as ``per_source[name]["status"]`` in the sidecar.
    """

    HIT_ID = "hit_id"
    HIT_TITLE = "hit_title"
    NOT_FOUND = "not_found"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"
    SKIPPED = "skipped"


class EvidenceLevel(str, Enum):
    """A finer-grained classification of what a lookup established.

    Additive alongside the existing OK/CLOSEST/NO MATCH display status
    (see ``sidecar.status_label``) rather than a replacement for it — it
    distinguishes claims that status collapses together, e.g. a confirmed
    DOI vs. a merely-live URL both currently display as "OK".
    """

    CONFIRMED_IDENTIFIER = "confirmed_identifier"
    STRONG_METADATA_MATCH = "strong_metadata_match"
    WEAK_OR_AMBIGUOUS_MATCH = "weak_or_ambiguous_match"
    LIVE_RESOURCE_ONLY = "live_resource_only"
    NOT_FOUND = "not_found"
    INCOMPLETE = "incomplete"


@dataclass
class SourceOutcome:
    """One source's accumulated outcome for one reference.

    Mutable — ``LookupResult.record_source()`` merges new query attempts
    into an existing entry in place (status precedence, best-score-wins,
    deduped ``queried_by`` append; see its docstring). ``summary`` stays an
    untyped ``dict | None`` for now (the provider-summary shape produced
    by every source adapter) — introducing a typed ``Candidate`` for it is
    a separate, larger follow-on (see BACKLOG.md).
    """

    source: str
    outcome: OutcomeKind
    queried_by: list[QueryKind]
    score: float | None
    summary: dict | None
    note: str | None

    @classmethod
    def from_dict(cls, source: str, entry: dict) -> "SourceOutcome":
        return cls(
            source=source,
            outcome=OutcomeKind(entry.get("status")),
            queried_by=[QueryKind(q) for q in (entry.get("queried_by") or [])],
            score=entry.get("score"),
            summary=entry.get("summary"),
            note=entry.get("note"),
        )

    def to_dict(self) -> dict:
        return {
            "status": self.outcome.value,
            "queried_by": [q.value for q in self.queried_by],
            "score": self.score,
            "summary": self.summary,
            "note": self.note,
        }
