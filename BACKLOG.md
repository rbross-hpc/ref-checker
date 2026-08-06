# ref-checker backlog

Deferred work items — not scheduled, captured so they aren't lost.

## Documentation

### Consider renaming the OK/CLOSEST/NO MATCH display status

The coarse `status` field (`OK` / `CLOSEST` / `NO MATCH`, computed by
`sidecar.status_label()`) collapses several distinct claims into `OK`:
a confirmed DOI/arXiv-ID match, a strong (>= 0.90) title-search match, and
a bare URL-liveness check with no bibliographic record at all. The new
additive `LookupResult.evidence` field (see
[docs/lookup-engine.md](docs/lookup-engine.md#persistent-result-model-resultspy-sidecarpy))
now carries this finer distinction without changing `status`,
`_plan_ref_work`, or `needs_retry` — deliberately, so it could ship without
a second sidecar schema bump
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

## Matching quality

### Checked-in matching-quality benchmark corpus

The current title score (`similarity.py`) is a reasonable baseline, but no
checked-in corpus measures it. Build a small benchmark covering: exact
citations, abbreviated titles, OCR damage, wrong years, preprint vs.
published versions, similar papers by the same authors, generic titles that
should not auto-match, and intentionally unresolvable references. Measure
false confirmations, false rejections, and ambiguous cases before changing
title/year thresholds or adding author/venue scoring. See
[docs/matching.md](docs/matching.md).

### arXiv title search recall

`sources/arxiv.py` uses `ti:"<title>"` for title search, which requires a
fairly exact match. A looser `all:` query would improve recall for titles
with PDF-extraction artifacts that survive the repair pass in
`extract.py`, at the cost of potentially noisier candidates.

## Performance

### Cross-run API response cache

Individual API responses are not cached across runs. Re-running on a
different paper (or after `--no-resume`) repeats all source queries. The
refs cache and results sidecar mitigate this for iterative work on the
*same* paper (see [docs/lookup-engine.md](docs/lookup-engine.md#concurrency-runnerpy))
but don't share across papers. A persistent cache (likely SQLite, keyed by
source + query mode + normalized query + cache age) would let a second
paper citing the same DOI skip re-querying it. Worth doing once the source
adapter interface (below, or a future `Protocol`) makes it natural to wrap
adapter calls in a caching layer.

## Performance (continued)

### Extend SourceContext (session pooling) to liveness sources

Part 1 of the `SourceContext` work (see `PLAN.md`'s "Planned work" section)
covers the 6 scholarly sources (`openalex`, `crossref`, `osti`, `dblp`,
`arxiv`, `semanticscholar`). `github.py` and `url.py` are liveness sources
with a different function shape (`check_url(urls, ctx)`, returning a
3-tuple including a dead-URL list, vs. the 2-tuple scholarly functions) and
currently use bare `requests.head` per call with no session at all —
extending `SourceContext` to them would give them connection pooling for
the first time. Deferred to a follow-up because it's a distinct-enough
shape of change from "inject a session that already existed via a private
`_session()` helper," and because no unit tests exercise `check_url`
directly today (only engine-level stubs, which are signature-agnostic and
would need no changes either way).

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
