# ref-checker backlog

Deferred work items — not scheduled, captured so they aren't lost.

## Documentation

### PLAN.md sidecar section refresh

`PLAN.md` lines 362–380 describe the results sidecar but predate the
error-handling / smart-re-run work. Update to reflect current reality:

- `schema_version` is now `2` (v1 is hard-rejected on load).
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
