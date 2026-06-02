"""LookupResult dataclass and query-stats tracker."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field


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


@dataclass
class _Stats:
    queries: dict[str, int] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    exhausted: dict[str, int] = field(default_factory=dict)

    def record_query(self, source: str) -> None:
        self.queries[source] = self.queries.get(source, 0) + 1

    def record_retry(self, source: str) -> None:
        self.retries[source] = self.retries.get(source, 0) + 1

    def record_exhausted(self, source: str) -> None:
        self.exhausted[source] = self.exhausted.get(source, 0) + 1

    def print_summary(self) -> None:
        all_sources = sorted(set(list(self.queries) + list(self.retries) + list(self.exhausted)))
        if not all_sources:
            return
        print("[ref-checker] Query summary:", file=sys.stderr)
        for src in all_sources:
            q = self.queries.get(src, 0)
            r = self.retries.get(src, 0)
            e = self.exhausted.get(src, 0)
            retry_str = f", {r} retr{'y' if r == 1 else 'ies'}" if r else ""
            exhausted_str = f", {e} exhausted" if e else ""
            print(
                f"[ref-checker]   {src:20s} {q:3d} quer{'y' if q == 1 else 'ies'}"
                f"{retry_str}{exhausted_str}",
                file=sys.stderr,
            )
