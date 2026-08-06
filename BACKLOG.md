# ref-checker backlog

Deferred work items — not scheduled, captured so they aren't lost.

## Documentation

### PLAN.md sidecar section refresh

`PLAN.md` lines 362–380 describe the results sidecar but predate the
error-handling / smart-re-run work. Update to reflect current reality:

- `schema_version` is now `4` (v1, v2, and v3 are hard-rejected on load; a
  WARNING is emitted when a recognized-but-outdated version is discarded).
  v4 extended `refs_hash` to cover every lookup-relevant reference field
  (title, authors, year, doi, arxiv_id, venue, url, github_url), not just
  index+raw.
- Each `references[i].result` carries a `per_source` map keyed by source name
  (`openalex`, `crossref`, `osti`, `dblp`, `semanticscholar`, `arxiv`,
  `github`, `url`). Entry shape:
  ```
  {
    "status": "hit_id" | "hit_title" | "not_found" | "error" | "rate_limited"
             | "disabled" | "skipped",
    "queried_by": ["doi" | "arxiv_id" | "title" | "url", ...],
    "score":   float | null,
    "summary": <source-summary dict> | null,
    "note":    str | null
  }
  ```
  `status` values are backed by `ref_checker.model.OutcomeKind` (a `str`
  Enum — sidecar JSON is unaffected, it's still plain strings on disk).
  `rate_limited` is distinct from `error` as of schema v4's model work: it
  means every retry attempt against that source hit `RateLimited`, as
  opposed to a non-rate-limit exception. For `_plan_ref_work` smart-rerun
  and `LookupResult.exhausted_sources` purposes the two are currently
  treated identically (both retried under `retry_errored`, both count
  toward "results may be incomplete") — the distinction is machine-visible
  but does not yet change behavior.
- The `per_source` map is the primitive that powers `_plan_ref_work` (smart
  re-run: retry only sources that are missing / `disabled` / `error` /
  `rate_limited`) and the session circuit breaker's per-ref attribution.
- Legacy `LookupResult` fields (`best_summary`, `display_score`, `best_source`,
  `doi_found_in`, `arxiv_found_in`, `exhausted_sources`, ...) are derived
  views recomputed by `LookupResult.recompute_best()` from `per_source`.
- `LookupResult.evidence` (a `ref_checker.model.EvidenceLevel`) is an
  additive, finer-grained classification computed alongside the coarse
  `status` (`OK`/`CLOSEST`/`NO MATCH`): `confirmed_identifier`,
  `strong_metadata_match`, `weak_or_ambiguous_match`, `live_resource_only`,
  `not_found`, or `incomplete`. It distinguishes claims that `status`
  collapses together — e.g. a confirmed DOI and a merely-live URL both
  currently display as `OK`, but have different `evidence` values.

### Consider renaming the OK/CLOSEST/NO MATCH display status

The coarse `status` field (`OK` / `CLOSEST` / `NO MATCH`, computed by
`sidecar.status_label()`) collapses several distinct claims into `OK`:
a confirmed DOI/arXiv-ID match, a strong (>= 0.90) title-search match, and
a bare URL-liveness check with no bibliographic record at all. The new
additive `LookupResult.evidence` field (see above) now carries this finer
distinction without changing `status`, `_plan_ref_work`, or `needs_retry`
— deliberately, so it could ship without a second sidecar schema bump
right after v4's index/hash fixes.

Once `evidence` has been in the field for a while and any external
scripts/dashboards built against the current `status` strings have had a
chance to migrate to `evidence`, consider whether `status` itself should
be renamed to something like `CONFIRMED` / `AMBIGUOUS` / `UNRESOLVED` (or
similar) to stop conflating "verified identifier" with "URL merely
responded" under the same `OK` label. This is a more disruptive change
than adding `evidence` was — it touches the CLI's terminal output
directly — so it deserves its own design pass rather than piggybacking on
this work.

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
