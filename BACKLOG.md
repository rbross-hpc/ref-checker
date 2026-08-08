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

### Author/venue scoring investigated, not viable as a same-series disambiguator

The previous version of this item proposed author/venue agreement as the
next signal to push `same_author_similar`/`generic_titles` "ambiguous"
cases (see `tests/fixtures/matching_benchmark.json` /
[docs/matching.md](docs/matching.md#matching-quality-benchmark)) down
toward a clean reject. Pulling the real author/venue data behind the
`same_author_similar` category (from `wan_e3smv2.json`, `zfp_spectral.json`,
`klasky_5.json`) shows this doesn't hold up:

- Every case in that category is a same-author pair by construction, so
  author overlap is uniformly high across both the currently-ambiguous
  cases *and* the cases that already correctly reject today.
- The sharpest counterexample: `sameauthor-animation-arctic-vs-china`
  (identical authors, identical venue `Zenodo`, identical year) is two
  genuinely different papers (Arctic vs. southeast China) that the
  existing title-only scorer *already rejects correctly*. Treating
  author/venue agreement as a positive signal would risk **breaking**
  that correct rejection, not fixing anything.
- The two cases that actually land "ambiguous"
  (`sameauthor-aerosol-activation-parts`, `sameauthor-zfp-version-vs-faq`)
  have no author/venue signal distinguishing them from the
  already-correctly-rejected cases in the same category — what actually
  sets them apart is title *structure*: one title is a near-superset of
  the other (e.g. `"zfp version 1.0.1"` vs. `"zfp version 1.0.1: FAQ
  #20"`), which `SequenceMatcher` naturally scores very high on raw
  character overlap regardless of authorship.
- For `generic_titles`, the ambiguous cases are synthetic pairs with no
  author/venue fields in the benchmark schema at all (`ref_title`/
  `ref_year`/`cand_title`/`cand_year` only), so there's no real data to
  validate an author/venue signal against there either.

Not pursuing author/venue scoring as a fix for either category. A
narrower, untried alternative for the `same_author_similar` false-reject
risk: detect numbered/parted/versioned title-suffix patterns (`Part
I`/`II`, `V1`/`V2`, `: FAQ #N`, trailing numerals) as a distinct signal —
this is what actually separates the ambiguous cases from the correctly-
rejected ones in the real data, unlike author/venue. Not scoped or
committed to here; would need its own benchmark cases and design pass if
picked up.

### Abbreviation/alias handling for bare project names — investigated, narrow fix identified but not shipped

The benchmark corpus's `abbreviated` category found a real false-reject:
a bare acronym or software/project name (e.g. `"scikit-learn"`) scores far
below `min_match` against that project's full paper title
(`abbrev-scikit-bare-acronym`, score ~0.47), even though it's the same
work. The previous version of this item assumed fixing this would need an
alias/abbreviation table. Investigating the actual data shows that's an
overstatement for the one real case that motivates it, but a naive fix
carries the same false-confirm risk found in the author/venue
investigation above:

- A general "boost score when one title's words are fully contained in
  the other's" fix, tested against the full 41-case benchmark, flips 2
  cases into outright **false confirms**:
  `sameauthor-echam-ham-parts` (two different Part 1/2 papers) 0.684 →
  0.909, and `sameauthor-zfp-version-vs-faq` (an already-documented
  ambiguous gap, see above) 0.829 → 1.0. Same root cause as the
  author/venue finding: same-author/parted-series titles are frequently
  near-supersets of each other, so any generic "containment implies same
  paper" signal collides with that category.
- A **narrower, length-gated** version is empirically safe: require the
  *shorter* title to be small (≤ 3 normalized words — name/acronym-like,
  not just any short subtitle-truncated title) AND an exact ordered
  word-prefix of the longer title. Tested against the full benchmark,
  this only changes `abbrev-scikit-bare-acronym` (0.471 → 1.0, becoming a
  true confirm) — no other case's classification moves. Cross-checked
  against all 198 real titles across every `tests/fixtures/refs/*.json`
  fixture (not just the benchmark) for accidental prefix collisions
  between an unrelated short title and a longer one: found exactly one
  match, the real scikit-learn pair itself. No spurious hits found.
- This narrower fix does **not** address the benchmark's other 3
  `KNOWN FALSE REJECT` cases (`abbrev-bert-truncated-subtitle`,
  `preprint-persistence-augmented-subtitle`,
  `preprint-zfp-special-values-expanded`) — those are subtitle-*drop*
  cases where the shorter title is 7-8 words (not name-like), a
  structurally different and riskier problem: relaxing the length gate
  enough to catch them reintroduces the false-confirm risk on
  `sameauthor-zfp-version-vs-faq` (also a 5-word word-prefix relation).

Not implemented. If picked up: the length-gated word-prefix check above
(not an alias/abbreviation table — the fixture data doesn't demonstrate a
real need for one) is a scoped, validated starting point for the bare-name
case specifically. The subtitle-drop false-rejects remain a distinct,
harder, still-open gap with no validated fix.

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
