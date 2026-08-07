# Multi-source lookup engine design

Covers `ref_checker/engine.py` (single-reference lookup), `runner.py`
(thread pool / resume / reporting), `runtime.py` (rate limiter, retry,
circuit breaker), `planner.py` (smart-rerun source selection), `results.py`,
and `sidecar.py`. For CLI flags and user-facing behavior (`--jobs`,
`--retry-*`, `--no-resume`, sidecar file locations), see
[README.md](../README.md#usage).

## Source priority

For each reference, sources are tried in this order:

1. **GitHub liveness** — if `ref.github_url` is set, HEAD-check immediately.
   GitHub-only references (repos, datasets) are almost never in scholarly
   databases; checking them first saves several seconds of fruitless API
   calls. Short-circuit on success.

2. **arXiv ID lookup** — if `ref.arxiv_id` is set, query arXiv directly
   (exact match, similarity = 1.0). Short-circuit on success.

3. **Scholarly loop** (skipped entirely for url-only refs):
   For each source in order — OpenAlex → CrossRef → OSTI → DBLP → Semantic
   Scholar → arXiv:
   - DOI lookup (if `ref.doi`)
   - arXiv-ID lookup via the source's DOI or native arXiv endpoint
     (if `ref.arxiv_id`, skipping arXiv itself which was already tried)
   - Title search (only if `result.best_similarity < 0.90` — skips
     expensive calls to Semantic Scholar and arXiv when OpenAlex/CrossRef
     already returned a good match)

4. **Generic URL liveness** — last resort, only if:
   - `result.best_similarity < min_match`, and
   - `ref.url` is set, and
   - `ref.github_url` is not set (GitHub already handled above)

## url-only gate

References with no DOI, no arXiv ID, no venue, and at least one URL skip the
entire scholarly loop. These are web pages, documentation, and tools not
indexed in any scholarly database. Going straight to URL liveness is faster
and avoids misleading "closest candidates."

## Year mismatch penalty

When a candidate is found via title search (not DOI/arXiv ID):
- If both `ref.year` and `candidate.year` are present and differ, subtract
  `0.10` from the similarity score (`results.apply_year_mismatch_penalty()`,
  shared with `format._osti_id_if_confident()` — see
  `results.STRONG_MATCH_THRESHOLD`).
- Record a `year_mismatch_note` for display.
- DOI/arXiv-ID hits are not penalized (the identifier is the identity proof);
  year mismatches are noted informally instead.

## Scoring and ID-hit annotation

The number shown in every status line is always **title similarity** —
`title_ratio(ref.title, candidate.title)` — with the following rules:

- **Identifier-confirmed hits** (DOI or arXiv lookup): raw title ratio, no
  year penalty. The identifier is proof of identity; year disagreement is
  surfaced as a Note only.
- **Title-search hits**: title ratio minus 0.10 if years differ, else raw
  title ratio.
- **Liveness-only hits** (GitHub, URL): displayed as `----` — no titles to
  compare.

If title similarity < 0.85 on an ID-confirmed hit, a `Note: DOI title: "..."`
line is shown, indicating the DOI may resolve to a differently-titled paper
(retitled preprint, DOI typo, etc.).

## Rate limiting and retries (`runtime.py`)

- Per-source minimum delay between consecutive calls, applied by a
  reservation-style `_RateLimiter` under a `threading.Lock`: `wait()`
  atomically computes the next available slot for a source and reserves it
  before sleeping, so under concurrency N threads calling the same source
  are still spaced exactly `delay` seconds apart. Default delays (see
  `sources/registry.py:DEFAULT_DELAYS`, derived from each source module's
  own `DEFAULT_DELAY` constant — see
  [source-adapter-contract.md](source-adapter-contract.md); overridable
  per-source via `--delay-<source>`): OpenAlex 2.0s, CrossRef 2.0s, OSTI
  2.0s, DBLP 1.0s, Semantic Scholar 8.0s, arXiv 3.0s, GitHub 1.0s, URL 1.0s.
- Per-call retry (`_retry` in `runtime.py`): up to 3 attempts with 5s / 10s /
  15s backoff on any exception (HTTP 429, 5xx, network timeout). 404 and 410
  are treated as confirmed misses (no retry). A `RateLimited` exception
  (distinct from a generic error) is tracked separately by the session
  circuit breaker and, when all retries against a source are exhausted
  because *every* attempt was rate-limited, is recorded as the
  `rate_limited` `OutcomeKind` in the sidecar rather than a generic `error`
  (see `model.py` and `BACKLOG.md` for how `rate_limited` and `error` are
  currently treated by retry planning).
- When all retries are exhausted for a source on a given reference, a
  `Note: retries exhausted for <source>` line is printed in the output for
  that reference, and the source's exhaustion count is included in the
  end-of-run query summary.

## Concurrency (`runner.py`)

- References are queried concurrently via a `ThreadPoolExecutor` (default 3
  workers; `-j N` / `--jobs N` to tune, `--jobs 1` for strictly sequential).
- Per-source polite-pool spacing is preserved via the reservation-style
  `_RateLimiter` described above regardless of worker count.
- `SourceHealth` (session circuit breaker) and `_Stats` are lock-protected
  for thread-safe mutation across worker threads.
- Formatted result blocks are buffered and emitted to stdout in
  citation-index order at end-of-run, so the report is deterministic
  regardless of completion order; progress and warnings stream to stderr
  live.
- On SIGINT the pool is shut down cleanly, waiting for in-flight references
  to finish before flushing the sidecar (`_Shutdown` in `runtime.py`).
- The rate limiter is shared across all references within a single
  `check_references()` call, enforcing inter-request delays correctly
  across reference boundaries. It does not persist across separate CLI
  invocations — repeated runs on different papers (or after `--no-resume`)
  repeat all source queries. The refs cache and results sidecar mitigate
  this for iterative work on the *same* paper, but there is no cross-paper
  API response cache (see `BACKLOG.md`).
- Similarly, `check_references()` builds a
  `sources/registry.py:ThreadLocalSourceContexts` once per run and threads
  it through `lookup_reference()` for every reference, so HTTP connections
  are pooled per source instead of each call opening a fresh one. See
  "Threading model for `SourceContext`" below for why this is
  thread-local rather than one flat dict shared across every worker
  thread. Like the rate limiter, pooled connections do not persist across
  separate CLI invocations.

### Threading model for `SourceContext`

`SourceContext` (session + credentials; see
[source-adapter-contract.md](source-adapter-contract.md)) is built **once
per source per worker thread**, not once per source for the whole run.
`sources/registry.py:ThreadLocalSourceContexts` is a `threading.local()`
-backed registry: calling `.get(name)` lazily builds a context for that
source the first time the *calling thread* asks for it, then returns the
same object on every subsequent call from that same thread — so every
reference dispatched to a given worker thread still reuses one session per
source (the actual point of `SourceContext`), it just no longer shares
that session with any *other* thread.

This exists because `requests.Session` is not documented as safe for
concurrent use: every request reads `session.cookies` to build the
outgoing `Cookie` header, and every response writes any `Set-Cookie` back
into it, unsynchronized at the `requests`-semantics level
(`requests.sessions.Session.send`). Two worker threads processing
different references and sharing one flat `SourceContext`/session (the
original design) could race on that cookie jar for any source whose
responses ever carry `Set-Cookie` — confirmed live for OSTI and
`github.com`, and plausible for the generic `url` liveness source, which
follows arbitrary user-supplied URLs. `ThreadLocalSourceContexts` closes
this by construction: `engine.py:_ctx_for()` is unaware of the
distinction — it just calls `.get(name)` on whatever `contexts` object it
was handed (a plain `dict` for direct-call test paths and
`cli/main.py:run_lookup()`'s single-context-per-invocation case, or a
`ThreadLocalSourceContexts` from `check_references()`), both of which
satisfy the same duck-typed `.get(name)` / `[name] = ...` interface.

Every session built by any thread during a run is tracked (under a lock)
so `check_references()` can close all of them deterministically —
`contexts.close_all()` runs in the `finally:` block after the
`ThreadPoolExecutor`'s `with` block has fully joined every worker thread
(`__exit__` calls `shutdown(wait=True)`), so no thread is still using a
session concurrently with the close. This runs on both normal completion
and interruption (Ctrl-C).

## Smart rerun (`planner.py`)

`_plan_ref_work` decides, per reference, which sources actually need to be
queried on a resume: only those missing from `per_source`, or previously
`disabled` / `skipped` / `error` / `rate_limited`. `disabled` and `skipped`
are always retried regardless of `--no-retry-errored`, since neither
represents a real attempt that concluded — `disabled` means the session
circuit breaker never let the call through, and `skipped` means the run was
interrupted (Ctrl-C) before the source's turn. `error` / `rate_limited` are
gated by `retry_errored` since those did make (and exhaust) real attempts.
This is what powers the default resume behavior described in
[README.md](../README.md#resuming-interrupted-or-incomplete-runs) and the
`--retry-all` / `--retry-closest` / `--no-retry-errored` flags.

## Persistent result model (`results.py`, `sidecar.py`)

`per_source` (keyed by source name) is the primitive that both the smart
rerun planner and the session circuit breaker's per-reference attribution
read from. Legacy `LookupResult` fields (`best_summary`, `display_score`,
`best_source`, `doi_found_in`, `arxiv_found_in`, `exhausted_sources`, ...)
are derived views recomputed by `LookupResult.recompute_best()` from
`per_source` — there is exactly one code path that decides the "winning"
result, rather than each lookup mutating final status independently.

`LookupResult.evidence` (a `model.EvidenceLevel`) is an additive,
finer-grained classification of what a lookup established
(`confirmed_identifier`, `strong_metadata_match`, `weak_or_ambiguous_match`,
`live_resource_only`, `not_found`, `incomplete`), computed alongside the
coarse `OK` / `CLOSEST` / `NO MATCH` status shown in the CLI. It exists
because a confirmed DOI and a merely-live URL both currently display as
`OK`, but are very different claims — see `BACKLOG.md` for the deferred
question of whether the coarse `status` itself should eventually be
renamed to reflect this distinction directly.

### Results sidecar schema

`schema_version` is currently `4` (v1–v3 are hard-rejected on load; a
WARNING is emitted when a recognized-but-outdated version is discarded). v4
extended `refs_hash` to cover every lookup-relevant reference field (title,
authors, year, doi, arxiv_id, venue, url, github_url), not just index+raw.

Each `references[i].result` carries a `per_source` map keyed by source name
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
`rate_limited` is distinct from `error`: it means every retry attempt
against that source hit `RateLimited`, as opposed to a non-rate-limit
exception. For `_plan_ref_work` smart-rerun and
`LookupResult.exhausted_sources` purposes the two are currently treated
identically (both retried under `retry_errored`, both count toward
"results may be incomplete") — the distinction is machine-visible but does
not yet change behavior.

### Inconclusive sources and `evidence`

A reference's aggregate `evidence` must never read as `not_found` unless
*every* applicable source actually returned a conclusive negative
(`not_found`). `LookupResult.recompute_best()` treats `skipped` (run
interrupted before that source's turn) and `disabled` (session circuit
breaker never let the call through) the same way it treats `error` /
`rate_limited`: as an inconclusive result that forces `evidence` to
`incomplete` rather than `not_found`, even when every *attempted* source
came back negative. This is a separate check from
`LookupResult.exhausted_sources`, which intentionally keeps its narrower,
user-facing meaning (`error` / `rate_limited` only — "we tried and the
attempt itself failed") since that list's contents are also surfaced
verbatim in CLI output; folding `skipped` / `disabled` into it would make
"exhausted sources: X" describe a source that was never actually attempted.
`skipped` and `disabled` therefore only affect the `evidence` computation,
not `exhausted_sources`.
</content>
