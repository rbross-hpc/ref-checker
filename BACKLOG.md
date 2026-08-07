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

## Architecture / type safety

### Typed `Candidate` for `best_summary`/`SourceOutcome.summary`

`LookupResult.per_source` is now `dict[str, SourceOutcome]` (done — see
`CHANGELOG.md`), but `SourceOutcome.summary` and `LookupResult.best_summary`
are still untyped `dict | None`: the provider-summary shape produced by
every source adapter (`title`, `authors`, `year`, `venue`, `doi`, `url`,
`external_id`, `source`). Introducing a typed `Candidate` dataclass for
this was deliberately deferred out of the `SourceOutcome` work — it has a
materially bigger blast radius, touching every source adapter module's
`_summarize()`-equivalent function (`openalex.py`, `crossref.py`,
`osti.py`, `dblp.py`, `semanticscholar.py`, `arxiv.py`, `github.py`,
`url.py`), not just the 6 files `SourceOutcome` touched. Worth doing as a
follow-on once there's appetite for that wider a diff; `SourceOutcome`
itself needs no further change to accommodate it (`summary: Candidate |
None` is a drop-in type swap).

## Matching quality

### Author/venue scoring for same-series and generic-title disambiguation

`tests/fixtures/matching_benchmark.json` (see
[docs/matching.md](docs/matching.md#matching-quality-benchmark)) quantifies
a real gap: papers in a numbered series or Part I/II pair by the same
authors (e.g. real cases mined from `wan_e3smv2.json`/`zfp_spectral.json`),
and unrelated papers with generically similar phrasing (e.g. "A Survey of
X Techniques for Y"), can score in the "ambiguous" band on title alone —
8 of the corpus's 41 checked-in cases (`same_author_similar` +
`generic_titles`, tagged `KNOWN ACCEPTED GAP`). No outright false
*confirmations* occur in the current corpus, but author/venue agreement is
the natural next signal to add to push these down toward a clean reject.
Any such change should be validated against the benchmark corpus (extend
it with more cases as needed) rather than by intuition, and should update
the corpus's checked-in baseline classifications/confusion-matrix counts
where they intentionally change.

### Abbreviation/alias handling for bare project names

The benchmark corpus's `abbreviated` category found a real false-reject:
a bare acronym or software/project name (e.g. `"scikit-learn"`) scores far
below `min_match` against that project's full paper title, even though
it's the same work. Fixing this would need an alias/abbreviation table or
a different scoring approach for short, name-like titles — out of scope
for the benchmark-building work itself.

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
