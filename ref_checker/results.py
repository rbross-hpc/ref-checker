"""LookupResult dataclass and query-stats tracker."""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field

from .model import EvidenceLevel, OutcomeKind, QueryKind, SourceOutcome
from .similarity import title_ratio

_YEAR_MISMATCH_PENALTY = 0.10

# Shared with format._osti_id_if_confident: the score a title-search hit
# must clear (after any year-mismatch penalty) to count as a confident
# match rather than merely a "closest candidate". Single source of truth
# so the two call sites (overall best-match scoring here, and OSTI's
# per-source confidence check in format.py) can't silently drift apart.
STRONG_MATCH_THRESHOLD = 0.90

_LIVENESS_SOURCES = ("github", "url")
_ID_MODES = ("doi", "arxiv_id", "url")


def apply_year_mismatch_penalty(
    score: float,
    ref_year: int | None,
    cand_year: int | None,
) -> float:
    """Subtract _YEAR_MISMATCH_PENALTY from *score* if both years are known
    and differ; otherwise return *score* unchanged. Floored at 0.0.
    """
    if ref_year and cand_year and ref_year != cand_year:
        return max(0.0, score - _YEAR_MISMATCH_PENALTY)
    return score


# When multiple lookup modes are tried against the same source, keep the
# most-informative status. Higher number wins. rate_limited ranks alongside
# error: both mean "we did not get real information from this attempt",
# just with a different cause.
_STATUS_PRECEDENCE = {
    OutcomeKind.HIT_ID:       5,
    OutcomeKind.HIT_TITLE:    4,
    OutcomeKind.ERROR:        3,
    OutcomeKind.RATE_LIMITED: 3,
    OutcomeKind.NOT_FOUND:    2,
    OutcomeKind.DISABLED:     1,
    OutcomeKind.SKIPPED:      0,
}


@dataclass
class LookupResult:
    best_summary: dict | None = None
    display_score: float | None = None    # title_ratio for ID hits (no year penalty);
                                          # title_ratio - year_penalty for title-search hits;
                                          # None for liveness-only hits
    best_source: str | None = None
    id_confirmed: bool = False            # True when a DOI or arXiv ID lookup succeeded
    is_liveness: bool = False             # True when result is GitHub/URL liveness only
    doi_attempted: str | None = None
    doi_found_in: list[str] = field(default_factory=list)
    arxiv_attempted: str | None = None
    arxiv_found_in: list[str] = field(default_factory=list)
    year_mismatch_note: str | None = None
    id_notes: list[str] = field(default_factory=list)
    dead_urls: list[tuple[str, str]] = field(default_factory=list)
    exhausted_sources: list[str] = field(default_factory=list)
    url_liveness_check: bool = False
    per_source: dict[str, SourceOutcome] = field(default_factory=dict)
    evidence: EvidenceLevel | None = None  # additive; see recompute_best

    def source_outcome(self, source: str) -> SourceOutcome | None:
        """Return per_source[source], or None if never queried.

        Thin convenience wrapper kept for API stability — per_source
        entries are already SourceOutcome instances.
        """
        return self.per_source.get(source)

    def record_source(
        self,
        source: str,
        status: OutcomeKind | str,
        *,
        queried_by: str | None = None,
        score: float | None = None,
        summary: dict | None = None,
        note: str | None = None,
    ) -> None:
        """Merge a per-source outcome into per_source[source].

        - queried_by is appended (deduped) to entry.queried_by.
        - score/summary are stored only if the new score is strictly better
          than what's already there (or nothing is there yet).
        - status is kept per _STATUS_PRECEDENCE (higher wins). This means
          an error from one lookup mode is not masked by a later not_found
          from a different mode against the same source.
        - note overwrites when non-None.
        """
        status = OutcomeKind(status) if not isinstance(status, OutcomeKind) else status
        entry = self.per_source.get(source)
        note_may_overwrite = True
        if entry is None:
            entry = SourceOutcome(
                source=source,
                outcome=status,
                queried_by=[],
                score=None,
                summary=None,
                note=None,
            )
            self.per_source[source] = entry
        else:
            old_rank = _STATUS_PRECEDENCE.get(entry.outcome, -1)
            new_rank = _STATUS_PRECEDENCE.get(status, -1)
            if new_rank > old_rank:
                entry.outcome = status
            elif new_rank < old_rank:
                # Do not let a lower-precedence status clobber the note
                # that explained the higher-precedence status.
                note_may_overwrite = False

        if queried_by:
            qk = QueryKind(queried_by)
            if qk not in entry.queried_by:
                entry.queried_by.append(qk)
        if score is not None:
            prior = entry.score
            if prior is None or score > prior:
                entry.score = score
                entry.summary = summary
        elif summary is not None and entry.summary is None:
            entry.summary = summary
        if note is not None and note_may_overwrite:
            entry.note = note

    def recompute_best(self, ref, min_match: float) -> None:
        """Re-derive best_summary / display_score / best_source and friends from per_source.

        This is the single point of truth for turning per-source outcomes into
        the flat "winning result" fields the formatter and status_label consume.
        """
        self.best_summary = None
        self.best_source = None
        self.display_score = None
        self.id_confirmed = False
        self.is_liveness = False
        self.year_mismatch_note = None
        self.id_notes = []
        self.doi_found_in = []
        self.arxiv_found_in = []
        self.exhausted_sources = []
        self.url_liveness_check = False
        self.evidence = None

        has_inconclusive_source = False
        for src, entry in self.per_source.items():
            status = entry.outcome
            if status in (OutcomeKind.ERROR, OutcomeKind.RATE_LIMITED):
                if src not in self.exhausted_sources:
                    self.exhausted_sources.append(src)
            if status in (
                OutcomeKind.ERROR, OutcomeKind.RATE_LIMITED,
                OutcomeKind.SKIPPED, OutcomeKind.DISABLED,
            ):
                has_inconclusive_source = True
            if status == OutcomeKind.HIT_ID:
                qby = entry.queried_by or []
                if "doi" in qby and src not in self.doi_found_in:
                    self.doi_found_in.append(src)
                if "arxiv_id" in qby and src not in self.arxiv_found_in:
                    self.arxiv_found_in.append(src)

        id_hits = [
            (src, entry) for src, entry in self.per_source.items()
            if entry.outcome == OutcomeKind.HIT_ID and entry.summary
        ]
        if id_hits:
            id_hits.sort(key=lambda kv: (kv[0] in _LIVENESS_SOURCES, kv[0]))
            best_src, best_entry = id_hits[0]
            summary = best_entry.summary
            self.best_summary = summary
            self.best_source = best_src
            self.id_confirmed = True

            if best_src in _LIVENESS_SOURCES:
                self.is_liveness = True
                self.display_score = None
                if best_src == "url":
                    self.url_liveness_check = True
                self.evidence = EvidenceLevel.LIVE_RESOURCE_ONLY
            else:
                cand_title = summary.get("title") if summary else None
                if ref.title and cand_title:
                    t_sim = title_ratio(ref.title, cand_title)
                    self.display_score = t_sim
                    if t_sim < 0.85:
                        self.id_notes.append(f'DOI title: "{cand_title}"')
                else:
                    self.display_score = None

                ref_year = ref.year
                cand_year = summary.get("year") if summary else None
                if ref_year and cand_year and ref_year != cand_year:
                    self.id_notes.append(
                        f"year mismatch (ref year={ref_year}, match year={cand_year})"
                    )
                    self.year_mismatch_note = (
                        f"ref year={ref_year}, match year={cand_year}"
                    )
                self.evidence = EvidenceLevel.CONFIRMED_IDENTIFIER
            return

        title_hits = [
            (src, entry) for src, entry in self.per_source.items()
            if entry.outcome == OutcomeKind.HIT_TITLE and entry.summary is not None
        ]
        best_score = -1.0
        best_src = None
        best_summary = None
        best_year_note = None
        for src, entry in title_hits:
            summary = entry.summary
            raw_score = entry.score
            if raw_score is None:
                continue
            ref_year = ref.year
            cand_year = summary.get("year")
            score = apply_year_mismatch_penalty(raw_score, ref_year, cand_year)
            year_note = (
                f"ref year={ref_year}, match year={cand_year}"
                if score != raw_score else None
            )
            if score > best_score:
                best_score = score
                best_src = src
                best_summary = summary
                best_year_note = year_note

        if best_src is not None:
            self.best_source = best_src
            self.best_summary = best_summary
            self.display_score = best_score
            self.year_mismatch_note = best_year_note

        if best_score >= STRONG_MATCH_THRESHOLD:
            self.evidence = EvidenceLevel.STRONG_METADATA_MATCH
        elif best_score >= min_match:
            self.evidence = EvidenceLevel.WEAK_OR_AMBIGUOUS_MATCH
        elif self.exhausted_sources or self.dead_urls or has_inconclusive_source:
            # exhausted_sources/dead_urls cover error/rate-limit/dead-URL
            # cases (and are also surfaced verbatim in CLI output);
            # has_inconclusive_source additionally catches skipped
            # (interrupted run) and disabled (circuit breaker) sources,
            # which are not genuine negative evidence and must not be
            # reported as a confident NOT_FOUND.
            self.evidence = EvidenceLevel.INCOMPLETE
        else:
            self.evidence = EvidenceLevel.NOT_FOUND


_MODE_ORDER = ("doi", "arxiv_id", "title", "url")


@dataclass
class _Stats:
    queries: dict[str, int] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    exhausted: dict[str, int] = field(default_factory=dict)
    disabled: dict[str, str] = field(default_factory=dict)
    queries_by_mode: dict[str, dict[str, int]] = field(default_factory=dict)
    retries_by_mode: dict[str, dict[str, int]] = field(default_factory=dict)
    exhausted_by_mode: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def _bump(self, table: dict[str, dict[str, int]], source: str, mode: str) -> None:
        if not mode:
            return
        inner = table.setdefault(source, {})
        inner[mode] = inner.get(mode, 0) + 1

    def record_query(self, source: str, mode: str = "") -> None:
        with self._lock:
            self.queries[source] = self.queries.get(source, 0) + 1
            self._bump(self.queries_by_mode, source, mode)

    def record_retry(self, source: str, mode: str = "") -> None:
        with self._lock:
            self.retries[source] = self.retries.get(source, 0) + 1
            self._bump(self.retries_by_mode, source, mode)

    def record_exhausted(self, source: str, mode: str = "") -> None:
        with self._lock:
            self.exhausted[source] = self.exhausted.get(source, 0) + 1
            self._bump(self.exhausted_by_mode, source, mode)

    def record_disabled(self, source: str, reason: str) -> None:
        with self._lock:
            self.disabled[source] = reason

    @staticmethod
    def _format_mode_breakdown(by_mode: dict[str, int] | None) -> str:
        if not by_mode:
            return ""
        used = [m for m in _MODE_ORDER if by_mode.get(m)]
        for m in by_mode:
            if m not in _MODE_ORDER and by_mode[m]:
                used.append(m)
        if not used:
            return ""
        parts = [f"{by_mode[m]} {m}" for m in used]
        return " (" + ", ".join(parts) + ")"

    def print_summary(self) -> None:
        all_sources = sorted(set(list(self.queries) + list(self.retries) + list(self.exhausted)))
        if all_sources:
            print("[ref-checker] Query summary:", file=sys.stderr)
            for src in all_sources:
                q = self.queries.get(src, 0)
                r = self.retries.get(src, 0)
                e = self.exhausted.get(src, 0)
                q_break = self._format_mode_breakdown(self.queries_by_mode.get(src))
                r_break = self._format_mode_breakdown(self.retries_by_mode.get(src))
                e_break = self._format_mode_breakdown(self.exhausted_by_mode.get(src))
                retry_str = (
                    f", {r} retr{'y' if r == 1 else 'ies'}{r_break}" if r else ""
                )
                exhausted_str = f", {e} exhausted{e_break}" if e else ""
                print(
                    f"[ref-checker]   {src:20s} {q:3d} quer{'y' if q == 1 else 'ies'}"
                    f"{q_break}{retry_str}{exhausted_str}",
                    file=sys.stderr,
                )
        if self.disabled:
            srcs = ", ".join(sorted(self.disabled))
            print(f"[ref-checker] Disabled this session: {srcs}", file=sys.stderr)
