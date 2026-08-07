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

### Make SourceOutcome/typed per_source the real in-memory representation

`SourceOutcome` (`model.py`) is currently a decorative, unused-in-the-pipeline
typed accessor: `LookupResult.per_source` is `dict[str, dict]`, and every
production read/write (`record_source`, `recompute_best`, `planner.py`,
`format.py`, `sidecar.py`) goes through raw dicts end to end —
`LookupResult.source_outcome()` is never actually called anywhere except
its own tests. This means a typo'd status string, an unknown field, or an
invalid status/summary combination is not caught by the type system.

Next step: make `per_source: dict[str, SourceOutcome]` (plus a typed
`Candidate` for `best_summary`) the actual in-memory representation, and
restrict dict conversion to `sidecar.py`'s serialization boundary and
JSON-reporting code. This is expected to be behavior-preserving. Worth
doing before introducing mypy/pyright, since a type checker would
currently see through most of this code but lose all value the moment it
reaches a `dict[str, Any]`.

### Extend source Protocols to cover the full adapter contract

`ScholarlySource`/`LivenessSource` (`sources/base.py`) declare
`SOURCE_NAME`/`DEFAULT_DELAY`/`SUPPORTED_QUERY_KINDS`, but `build_context()`
— relied on by `registry.py` and `engine.py:_ctx_for()` for every source —
isn't part of either Protocol. The structural contract tests
(`test_source_contract.py`) also only check name/attribute presence
(`hasattr`, declared-kind-has-matching-function) and a couple of
scalar-value equalities (`DEFAULT_DELAYS[name] == DEFAULT_DELAY`) — not
function signatures, return types, or that every lookup function actually
accepts the mandatory trailing `ctx: SourceContext` parameter the
docstring in `base.py` claims is required.

Either add `build_context()` (and ideally the lookup/`check_url`
signatures) to the Protocols, or replace source modules with small adapter
objects implementing one complete Protocol — worth reconsidering given how
much behavior the source contract now carries.

### Stricter reference index validation

`extract.py`'s index parsing uses bare `int(...)` conversion (both in
`Reference.from_dict` and the loader's duplicate-detection path), which
silently accepts floats (`1.5` → `1`, truncated), booleans (`bool` is an
`int` subclass, so `True` → `1`), and non-positive values (`0`, negative
integers) — the only rejection path is a `TypeError`/`ValueError` from
non-numeric input, or an exact duplicate after conversion. Minor compared
to the identity-collision bug this loader already fixed (duplicate
detection is the property that actually matters for that), but easy to
tighten now: accept only a real positive integer, or optionally a string
containing only a positive base-10 integer.

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
