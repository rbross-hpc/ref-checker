# ref-checker backlog

Deferred work items — not scheduled, captured so they aren't lost.

## Documentation

### PLAN.md sidecar section refresh

`PLAN.md` lines 362–380 describe the results sidecar but predate the
error-handling / smart-re-run work. Update to reflect current reality:

- `schema_version` is now `3` (v1 and v2 are hard-rejected on load; a
  WARNING is emitted when a recognized-but-outdated version is discarded).
- Each `references[i].result` carries a `per_source` map keyed by source name
  (`openalex`, `crossref`, `osti`, `dblp`, `semanticscholar`, `arxiv`,
  `github`, `url`). Entry shape:
  ```
  {
    "status": "hit_id" | "hit_title" | "not_found" | "error" | "disabled" | "skipped",
    "queried_by": ["doi" | "arxiv_id" | "title" | "url", ...],
    "score":   float | null,
    "summary": <source-summary dict> | null,
    "note":    str | null
  }
  ```
- The `per_source` map is the primitive that powers `_plan_ref_work` (smart
  re-run: retry only sources that are missing / `disabled` / `error`) and the
  session circuit breaker's per-ref attribution.
- Legacy `LookupResult` fields (`best_summary`, `display_score`, `best_source`,
  `doi_found_in`, `arxiv_found_in`, `exhausted_sources`, ...) are derived
  views recomputed by `LookupResult.recompute_best()` from `per_source`.

### Optional: `references/sidecar-schema.md`

Standalone doc describing the results sidecar as a consumable format, parallel
to the existing `references/schema.md` (which covers the input refs JSON).
Only worth doing if programmatic sidecar consumers outside `ref-checker`
itself become a thing.

## Sources

### DBLP `Retry-After` handling on 503

`ref_checker/sources/dblp.py` currently treats 503 as "try the next mirror"
and moves on. DBLP occasionally returns 503 with a `Retry-After` header when
it wants us to back off explicitly. We could parse `Retry-After` (via the
existing `_http.parse_retry_after` helper) and, when present, raise
`RateLimited(retry_after=...)` so the outer retry loop waits the requested
amount before hitting the mirror. Low priority: the current mirror-failover
path already handles the common case.

### Per-source `--delay-<src>` CLI flags

Currently only `--delay-osti` exists (from the OSTI merge). Generalize to
one flag per scholarly source: `--delay-openalex`, `--delay-crossref`,
`--delay-osti`, `--delay-dblp`, `--delay-arxiv`, `--delay-semanticscholar`.
Each optional; each overrides the corresponding entry in `_DEFAULT_DELAYS`.
Wire through `check_references(delays=...)`. Useful when a specific source
is being unusually generous or stingy on a given day and the user wants to
tune without editing code.

### Semantic Scholar: drop API key on 403 and retry unauthenticated

When Semantic Scholar returns 403, the current fix (added alongside the
quota-exhaustion work) surfaces a hint pointing at `SEMANTICSCHOLAR_API_KEY`
in the error message. Better: on the first 403, emit a WARNING that the key
appears invalid/revoked/unauthorized, drop the `x-api-key` header for the
remainder of the session, and retry the current request unauthenticated
(Semantic Scholar's public tier has stricter rate limits but still works).
Requires a session-scoped mutable flag in `sources/semanticscholar.py` (or
threading a `SourceHealth`-like handle through), plus a test that the second
call omits the header once the flag is tripped.
