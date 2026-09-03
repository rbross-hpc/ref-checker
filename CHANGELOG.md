# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

### Changed

### Fixed

- **LLM extraction now streams the Chat Completions request**
  (`ref_checker/extract.py`'s `_call_llm`): Argo rejects long-running
  non-streaming requests with HTTP 500 ("Streaming is required for
  operations that may take longer than 10 minutes"), regardless of prompt
  size or actual output length — every extraction against a Claude model
  served through Argo failed identically, wiping out the refs cache.
  `_call_llm` now requests `stream=True` and reassembles the streamed
  delta chunks into the same JSON string a non-streaming call would have
  returned.

- **LLM extraction now recovers from markdown-fenced or prose-prefixed
  JSON output**: despite requesting `response_format={"type":
  "json_object"}`, some models served through Argo (observed with GPT-4o,
  GPT-4.1, and Claude Sonnet 4.6) ignore the request and wrap their JSON
  response in a ` ```json ` code fence, or occasionally prefix it with
  prose. `_call_llm` previously called `json.loads()` directly and failed
  outright on either. It now strips a leading/trailing code fence and,
  failing that, recovers the first balanced `{...}` object from the
  response before parsing.

## [0.3.0] - 2026-08-17

### Added

- **Ex Libris Primo source** (`ref_checker/sources/primo.py`): opt-in
  institutional discovery-layer source. Queries the Primo PNX REST API for
  DOI lookup and title search. Enabled by setting `PRIMO_BASE_URL`,
  `PRIMO_VID`, and `PRIMO_INST`; completely inert (safe no-op) when unset.
  When configured, runs first in the scholarly lookup chain (before OpenAlex)
  and is available via `ref-checker lookup primo`. All four `PRIMO_*` vars
  shown in the credential summary at startup.

- **`.env` file support** (`python-dotenv`): the CLI now loads a `.env` file
  from the working directory at startup (`override=False` — real env vars
  always win). `.env` is gitignored. Allows setting `PRIMO_*`,
  `OPENAI_API_KEY`, etc. without manual `export`.

- **Checked-in matching-quality benchmark corpus**
  (`tests/fixtures/matching_benchmark.json` +
  `tests/test_matching_benchmark.py`): 41 hand-curated `(ref_title,
  ref_year, cand_title, cand_year)` pairs across the 8 categories called
  for in `BACKLOG.md` — exact, abbreviated, OCR damage, wrong years,
  preprint vs. published, similar papers by the same authors, generic
  titles, and intentionally unresolvable references — each with a ground-
  truth `same_paper` label and a checked-in `expected_classification`
  baseline. Exercises the real title-search scoring path end to end
  (`similarity.py:title_ratio` → `results.py:apply_year_mismatch_penalty`
  → classification against `STRONG_MATCH_THRESHOLD`/`min_match`), not a
  reimplementation of it, so the corpus can't silently drift from
  production behavior. Most `same_author_similar` cases and part of
  `generic_titles`/`abbreviated` are **real** near-collision titles mined
  from this repo's own `tests/fixtures/refs/*.json` fixtures (e.g. a
  numbered paper series, Part I/II companions, LaTeX-brace-mangled
  BibTeX titles from the sibling `../annual-report` repo) rather than
  hypothetical constructions; the anchor `preprint_vs_published` case
  (`ChatVis`) is a verified real preprint-retitled-at-publication case
  from that same sibling repo's extracted-publication metadata.
  `TestClassificationMatchesBaseline` asserts per-case classification
  against the baseline (pinpoints exactly which case flipped);
  `TestConfusionMatrixSummary` asserts the aggregate false-confirm/
  false-reject/ambiguous counts and confirms no different-paper pair in
  the current corpus scores high enough to be an outright false
  confirmation today. Several cases are annotated `KNOWN FALSE REJECT`
  (no alias table for bare project names/acronyms) or `KNOWN ACCEPTED
  GAP` (no author/venue scoring to disambiguate same-series/generic
  titles) — measurement-only per the backlog item's scope; fixing either
  gap is a new, separate `BACKLOG.md` item. `docs/matching.md`'s "Known
  limitations" section rewritten around the corpus's actual findings.

### Investigated (not implemented)

- **Author/venue scoring for `same_author_similar`/`generic_titles`
  disambiguation**: the backlog item proposed author/venue agreement as
  the next signal to push these benchmark categories' "ambiguous" cases
  toward a clean reject. Pulling the real author/venue data behind the
  `same_author_similar` category (`wan_e3smv2.json`, `zfp_spectral.json`,
  `klasky_5.json`) shows this doesn't hold: every case in that category is
  a same-author pair by construction, so author/venue overlap is
  uniformly high across both the ambiguous cases and the cases that
  already correctly reject today. The sharpest counterexample is
  `sameauthor-animation-arctic-vs-china` — identical authors, identical
  venue (`Zenodo`), identical year, and two genuinely different papers
  (Arctic vs. southeast China) that the existing title-only scorer already
  rejects correctly; treating author/venue agreement as a positive signal
  risks breaking that correct rejection rather than fixing an ambiguous
  one. The two cases that do land "ambiguous" differ from the correctly-
  rejected cases by title *structure* (one title is a near-superset of the
  other), not authorship. Not implemented; `BACKLOG.md`'s item rewritten
  to record this finding and note an untried, narrower alternative
  (numbered/parted/versioned title-suffix detection) instead.
- **Abbreviation/alias handling for bare project names**: the backlog item
  assumed the benchmark's real `abbrev-scikit-bare-acronym` false-reject
  (`"scikit-learn"` vs. its full paper title, score ~0.47) would need an
  alias/abbreviation table. Investigation found that overstated for the
  one real case that motivates it — it's a literal word-prefix relation,
  not an unrelated alias — but a general "containment implies same paper"
  fix carries the same false-confirm risk found in the author/venue
  investigation above: tested against the full benchmark, it flips
  `sameauthor-echam-ham-parts` (0.684 → 0.909) and
  `sameauthor-zfp-version-vs-faq` (0.829 → 1.0) into false confirms, since
  same-author/parted-series titles are frequently near-supersets of each
  other. A narrower fix — gate to titles where the shorter side is ≤ 3
  normalized words (name/acronym-like) and an exact ordered word-prefix of
  the longer side — was validated as safe: only `abbrev-scikit-bare-acronym`
  changes classification in the benchmark, and cross-checking against all
  198 real titles across `tests/fixtures/refs/*.json` found no accidental
  prefix collisions beyond the real scikit-learn pair. Not implemented;
  the benchmark's other 3 `KNOWN FALSE REJECT` cases are subtitle-drop
  cases (shorter side 7-8 words), a distinct, harder gap this narrower fix
  would not address. `BACKLOG.md`'s item rewritten to record the finding
  and the validated-but-unshipped narrower fix.

### Fixed

- **Flaky `test_runner.py::TestThreadLocalSourceContexts::test_concurrent_run_never_shares_one_session_across_two_threads`**
  (test-only, no production code change): the test hoped `jobs=3` with 9
  quick no-op stubbed references would incidentally exercise more than
  one real worker thread, then asserted on it. That's not guaranteed —
  `ThreadPoolExecutor` only spawns a new thread on `submit()` if no
  existing worker is already idle, so with zeroed rate-limit delays and a
  near-instant stub, a single thread could finish task 1 and go idle
  before the pool ever needed a second one, tripping the test's own
  `"test didn't actually exercise more than one worker thread"` guard.
  Fixed by forcing the first 3 calls into the stub to block on a
  `threading.Barrier(3)` — `runner.py`'s bounded submission window
  (`jobs + 1`) already guarantees the first 3 dispatched tasks land on 3
  distinct freshly-spawned threads, so the barrier makes that existing
  guarantee observable instead of relying on incidental scheduling.
  Stress-tested 50x locally with no failures.
  `ThreadLocalSourceContexts` itself was already correct (same
  conclusion reached when fixing the sibling flaky
  `test_registry.py::test_different_threads_get_different_contexts_for_same_source`,
  below) — only the test harness was buggy.

### Changed

- **`ScholarlySource`/`LivenessSource` Protocols (`sources/base.py`) now
  include `build_context()`**, which every source module already
  implemented and which `registry.py`/`engine.py:_ctx_for()` already
  called unconditionally for every source — it just wasn't part of either
  Protocol before. The three scholarly lookup functions
  (`get_by_doi`/`get_by_arxiv_id`/`search_by_title`) remain deliberately
  outside `ScholarlySource` (a `Protocol` can't express "required only if
  `SUPPORTED_QUERY_KINDS` says so" — DBLP is title-only, CrossRef/OSTI
  have no arXiv lookup). `engine.py`'s previously-untyped `src` parameters
  (`_ctx_for`, `call`, `call_liveness`) are now annotated
  `ScholarlySource | LivenessSource` (documentation value only — no
  static type checker runs in CI, see `docs/source-adapter-contract.md`).
  Cosmetic: `osti.py`/`dblp.py`'s `search_by_title` signatures now have
  the same trailing comma as the other 4 scholarly sources.
- **`test_source_contract.py` gained real signature checks**: a
  `runtime_checkable` `Protocol`'s `isinstance()` check only verifies that
  an attribute/method of the right *name* exists, never its signature —
  so nothing previously caught a source module whose lookup/`check_url`
  function didn't actually accept a trailing, positional
  `ctx: SourceContext` parameter, or whose `build_context()` took
  unexpected arguments. New `TestSourceFunctionSignatures` class uses
  `inspect.signature()` against every function each source module
  actually implements (driven by `SUPPORTED_QUERY_KINDS`/`FN_BY_KIND` for
  scholarly sources) to check this directly. 16 new tests; behavior
  unchanged (no production dispatch logic touched).

### Fixed

- **`Reference` index parsing silently accepted invalid values**:
  `Reference.from_dict` and `load_references_from_list`'s explicit-index
  path both used bare `int(...)` conversion, which silently truncated
  floats (`1.5` → `1`), coerced booleans (`bool` is an `int` subclass, so
  `True` → `1`), and accepted non-positive values (`0`, negative
  integers) — despite `schema.md` documenting `index` as a positive,
  1-based integer. `from_dict` also defaulted a missing `index` to `0`
  rather than requiring one. Both now go through a shared
  `_validate_index()` helper (native `int`, not `bool`, `>= 1`) and raise
  `ValueError` on anything else — subject to `load_references_from_list`'s
  existing `strict`/permissive branching and duplicate-index rejection,
  unchanged. `Reference.from_dict` now *requires* a valid, pre-resolved
  `index` already present in its input dict; each caller resolves one
  appropriately for its own trust level:
  - `load_references_from_list` already pre-resolved an index per entry
    (explicit or 1-based position) before calling `from_dict`; unchanged
    except for the stricter check.
  - `_call_llm` (LLM-extracted references, untrusted input) gains a new
    `_resolve_llm_indices()` pre-pass mirroring the loader's 1-based
    fallback, but — unlike the loader's strict mode — an invalid or
    duplicate LLM-supplied index falls back to that entry's list position
    rather than raising, so an LLM index quirk doesn't fail the whole
    extraction and trigger a retry.
  - `cli/show.py`'s sidecar display now passes the sidecar's own
    (already-validated) outer index key into `from_dict`, rather than
    trusting the nested `ref` dict's own `"index"` field, which a
    hand-edited sidecar could omit or leave inconsistent with the outer
    key.
  - `load_refs_cache` needed no code change: its existing
    `try/except Exception: return None, "corrupt"` around
    `Reference.from_dict` now correctly treats a cache with a
    missing/invalid per-entry index as `"corrupt"` instead of silently
    accepting it as `"valid"` with a coerced-to-`0` index.
  40 new tests added (float/bool/zero/negative index rejection at both
  `from_dict` and the loader; `_resolve_llm_indices` fallback behavior;
  sidecar outer-key-authoritative display; corrupted-refs-cache
  detection). No existing fixture data needed changes — every committed
  fixture already used valid positive sequential indices.

- **Flaky `test_registry.py::test_different_threads_get_different_contexts_for_same_source`**
  (test-only, no production code change): the test keyed a results dict by
  `threading.get_ident()`, but native OS thread ids can be recycled once a
  thread terminates — with 4 threads each doing almost no work, one thread
  could fully finish (and have its id reused) before another in the same
  batch even started, so two `_worker()` invocations would collide on the
  same dict key and `len(results)` would come back below 4. Seen flaking
  in CI on Python 3.10/3.12. Fixed by collecting into a plain list instead
  of a dict keyed by thread id, and adding a `threading.Barrier(4)` so all
  4 threads are provably alive and calling `contexts.get()` concurrently —
  what the test actually means to exercise. Stress-tested 50x locally with
  no failures. `ThreadLocalSourceContexts` itself
  (`sources/registry.py`) was already correct; only the test harness was
  buggy.

### Added

- **Shared `SourceContext` (session pooling) for liveness sources**:
  `github` and `url` each gain a `build_context() -> SourceContext`
  function, and `check_url` now takes a mandatory trailing
  `ctx: SourceContext` parameter, replacing bare `requests.head(...)` per
  call with `ctx.session.head(...)`. Completes Part 2 of the
  `SourceContext` work (Part 1 covered the 6 scholarly sources, above).
  `sources/registry.py:build_all_contexts()` now builds contexts for all 8
  sources; `engine.py:call_liveness()` uses the same context lookup as
  `call()`. See `docs/source-adapter-contract.md`'s "Shared SourceContext"
  section and `PLAN.md`.
- **Shared `SourceContext` (session pooling) for scholarly sources**:
  `openalex`, `crossref`, `osti`, `dblp`, `semanticscholar`, and `arxiv`
  each gain a `build_context() -> SourceContext` function, and every
  lookup function (`get_by_doi`, `get_by_arxiv_id`, `search_by_title`) now
  takes a mandatory trailing `ctx: SourceContext` parameter. `SourceContext`
  is a small dataclass holding just `session: requests.Session` and
  `credentials: dict[str, str]` — rate limiting and retry stay exactly
  where they were (`runtime.py`/`engine.py`), a deliberate departure from
  the literal shape suggested by the external assessment. `runner.py`
  builds one context per scholarly source, once per `check_references()`
  run (via `sources/registry.py:build_all_contexts()`), and threads it
  through `engine.py:lookup_reference()` for every reference — so a whole
  run reuses one `requests.Session` (and its connection pool) per source,
  instead of every HTTP call opening a fresh one via the old per-call
  `_session()` helpers. `cli/main.py`'s single-shot `ref-checker lookup
  <source>` builds one throwaway context before dispatching. Semantic
  Scholar's API key, previously read fresh from the environment on every
  call via `_headers()`, is now read once into `ctx.credentials` at
  context-build time. See `docs/source-adapter-contract.md`'s "Shared
  SourceContext" section and `PLAN.md`. Liveness sources (`github`, `url`)
  are follow-up work — see `BACKLOG.md`.
- `ref_checker.sources.base`: `ScholarlySource` / `LivenessSource`
  `typing.Protocol` definitions formalizing the source adapter contract,
  plus a shared `FN_BY_KIND` mapping (`QueryKind` -> conventional function
  name). Every source module now declares `DEFAULT_DELAY` and (for
  scholarly sources) `SUPPORTED_QUERY_KINDS`, replacing implicit
  `getattr(src, fn_name, None) is None` capability discovery in
  `engine.py` with an explicit check. See
  `docs/source-adapter-contract.md`.
- `tests/test_source_contract.py`: structural regression tests asserting
  every registered source module satisfies its Protocol and that
  `SUPPORTED_QUERY_KINDS` never drifts out of sync with the functions a
  module actually implements.

### Fixed

- **`SourceContext` (and its `requests.Session`) was shared unsynchronized
  across worker threads**: `check_references()` previously built one
  `SourceContext` per source and passed the identical object to every
  `ThreadPoolExecutor` worker, so multiple threads processing different
  references could call the same source's session concurrently.
  `requests.Session` is not documented as thread-safe — every request
  reads `session.cookies` and every response writes `Set-Cookie` back into
  it, unsynchronized across threads; confirmed live that OSTI and
  `github.com` both set cookies today, so this was a real (not merely
  theoretical) cross-reference cookie-jar race for at least those two
  sources. Fixed via `sources.registry.ThreadLocalSourceContexts`, a
  `threading.local()`-backed registry giving each worker thread its own
  `SourceContext` per source while still reusing that thread's session
  across every reference it processes. Every session built by any thread
  is closed deterministically in `check_references()`'s `finally:` block,
  after the thread pool has fully joined, on both normal completion and
  interruption. See `docs/lookup-engine.md`'s "Threading model for
  SourceContext" section.
- **Interrupted runs could be mistaken for completed negative results**:
  `_plan_ref_work` (the resume/smart-rerun planner) now retries sources
  left in `skipped` status by a previous interrupted run (Ctrl-C), not just
  `disabled` / `error` / `rate_limited`. Previously a source skipped due to
  shutdown was silently never re-queried on `--resume`. Separately,
  `LookupResult.recompute_best()` now classifies a reference's `evidence`
  as `incomplete` (rather than `not_found`) whenever any applicable source
  is `skipped` or `disabled`, even if every source that *did* run came back
  negative — a reference is only reported as a genuine `not_found` when
  every applicable source reached a conclusive negative result.
  `exhausted_sources` (and its CLI display text) keeps its existing
  `error`/`rate_limited`-only meaning; the new check is additive. See
  `docs/lookup-engine.md`'s "Inconclusive sources and evidence" section.
- **Duplicated rate-limit defaults**: the per-source delay defaults were
  hardcoded identically in three places (`engine.py`, `runner.py`, and
  eight `--delay-<source>` argparse defaults in `cli/main.py`). Now
  derived once as `sources.registry.DEFAULT_DELAYS` from each source
  module's own `DEFAULT_DELAY`, and imported everywhere else. No
  behavior change.
- **Duplicated `lookup` subcommand capability logic**: `cli/main.py`'s
  `lookup` subcommand independently re-encoded which flags each source
  accepts (hardcoded exclusion tuples in the parser) and which function to
  call for a given source/argument combination (a per-source `if/elif`
  chain in `run_lookup`). Both now derive from each source's
  `SUPPORTED_QUERY_KINDS`. No CLI-facing change — same flags accepted per
  source, same dispatch preference order (arXiv ID before DOI for the
  `arxiv` source; DOI before arXiv ID before title elsewhere).
- **Duplicated status-bucket policy**: `format.format_result()` independently
  reimplemented the `OK`/`CLOSEST`/`NO MATCH` threshold logic already
  computed by `sidecar.status_label()`. Both now agree by construction —
  `format_result()` calls `status_label()` directly instead of re-deriving
  the bucket from `display_score`/`STRONG_MATCH_THRESHOLD`/`min_match`
  comparisons. No output change; a regression test
  (`TestFormatResultMatchesStatusLabel`) guards against the two
  implementations silently diverging again.
- **DBLP ignored `Retry-After` on 503**: a 503 from a DBLP mirror was
  previously wrapped in a bare `requests.HTTPError` and only used to
  decide "try the next mirror" — any `Retry-After` hint on that response
  was discarded. Now parsed via `_http.parse_retry_after` and carried as
  `RateLimited(retry_after=...)`, same as the existing 429 path, so if
  both mirrors are unavailable the outer retry loop honors DBLP's
  requested backoff instead of falling back to the default schedule.
  Still tries the next mirror first when one mirror 503s; unchanged when
  either mirror responds successfully.
- **Semantic Scholar 403 with a bad API key no longer aborts the source
  for the rest of the run**: previously, any 403 (including one caused by
  an invalid/revoked `SEMANTICSCHOLAR_API_KEY`) raised a generic
  `HTTPError` with a hint to check the key, and every subsequent call
  hit the same 403 again for the rest of the run. Now, on the first 403
  where a key is set in `ctx.credentials`, `get_by_doi`/`get_by_arxiv_id`/
  `search_by_title` print one `WARNING` to stderr, clear the key in
  `ctx.credentials` (so `_headers()` omits `x-api-key` on every later
  call in that thread too — Semantic Scholar's public tier still works,
  just with stricter rate limits), and retry the current request once
  unauthenticated. If no key was set (or the retry also 403s), falls
  through to the existing generic hint error unchanged. Since
  `SourceContext` is thread-local (see the `ThreadLocalSourceContexts`
  entry above), each worker thread discovers and drops a bad key
  independently on its own first 403.

### Changed

- **`SourceOutcome` is now the real in-memory representation of
  `LookupResult.per_source`**, not a decorative, unused-in-the-pipeline
  typed accessor: `per_source: dict[str, SourceOutcome]` (was `dict[str,
  dict]`). `record_source()` now builds/mutates `SourceOutcome` instances
  (same precedence/merge semantics as before — status precedence,
  best-score-wins, deduped `queried_by` append, note-overwrite rules,
  unchanged); `recompute_best()`, `engine.py`, `planner.py`, and
  `format.py`'s `_osti_id_if_confident()` read via attribute access
  (`entry.outcome`, `entry.summary`, ...) instead of `dict.get(...)`.
  `SourceOutcome` gained a `to_dict()` (paired with the existing
  `from_dict()`, matching `Reference`'s house style); dict conversion is
  now restricted to `sidecar.py`'s serialization boundary
  (`result_to_dict`/`result_from_dict`) — the on-disk sidecar JSON shape
  is unchanged (`SourceOutcome.to_dict()` emits the same plain
  string/list/dict shape as before, not enum reprs).
  `LookupResult.source_outcome()` is now a thin `per_source.get(...)`
  wrapper since entries already are `SourceOutcome`. Behavior-preserving;
  no CLI or sidecar-format change. `summary`/`best_summary` remain
  untyped `dict | None` for now — a typed `Candidate` is a separate,
  larger follow-on (touches every source adapter module), tracked in
  `BACKLOG.md`.

## [0.2.0] - 2026-08-06

### Added

- CI: GitHub Actions workflow running `ruff check` and `pytest` on Python
  3.10–3.13 for every push and pull request.
- `ruff` added as a dev dependency, with a lint configuration in
  `pyproject.toml`.
- `extract.load_references_from_list()`: a single shared loader used by both
  `check --refs-json` and `show`, so the two commands interpret the same
  bare refs JSON identically. Missing `index` fields are auto-assigned from
  the entry's 1-based position (matching citation-style display); duplicate
  explicit indices are rejected with a clear error.
- `ref_checker.model`: a typed domain model layer. `QueryKind` and
  `OutcomeKind` are `str`-subclassed enums backing the existing
  `per_source[name]["status"]`/`["queried_by"]` string values (sidecar JSON
  is byte-compatible — no schema change from this alone). `EvidenceLevel`
  is a new, additive, finer-grained classification of what a lookup
  established (`confirmed_identifier`, `strong_metadata_match`,
  `weak_or_ambiguous_match`, `live_resource_only`, `not_found`,
  `incomplete`), stored as `LookupResult.evidence` / sidecar
  `result.evidence` alongside the existing coarse `OK`/`CLOSEST`/`NO MATCH`
  status — it distinguishes claims that status collapses together, e.g. a
  confirmed DOI vs. a merely-live URL both currently display as `OK`.
  `SourceOutcome` is an optional typed accessor over one
  `per_source[name]` entry, available via `LookupResult.source_outcome()`.
- A `rate_limited` `OutcomeKind`, now actually written into
  `per_source[name]["status"]` when a source's retries are exhausted
  because every attempt was rate-limited (previously such attempts were
  indistinguishable from a generic `error` in the stored per-source
  outcome, even though the session-level circuit breaker already tracked
  the distinction internally). Currently ranked and handled identically to
  `error` for retry-planning and `exhausted_sources` purposes — see
  `BACKLOG.md` for the rationale.

### Changed

- Bumped package version from `0.1.0` to `0.2.0` (no tagged release
  previously existed for `0.1.0`; the README's install example already
  referenced `v0.2.0`).
- `SIDECAR_SCHEMA_VERSION` bumped `3` → `4`. Sidecars written by v3 (and all
  earlier versions) are hard-rejected on load with a WARNING, exactly like
  the v1/v2 rejection already in place — no silent partial-upgrade.
- **`check.py` split into `engine.py` and `runner.py`.** `check.py` had
  accumulated two large, only-loosely-coupled responsibilities:
  `lookup_reference` (assess one reference against all sources) and
  `check_references` (thread pool, resume/sidecar I/O, signal handling,
  end-of-run reporting). These are now `engine.py` and `runner.py`
  respectively; `check.py` is a thin backward-compatible re-export shim
  (~50 lines) so existing callers (`cli/main.py`) and any code reaching
  into `check.<name>` keep working unchanged. No behavior change.
- Earlier, `runtime.py` (`_Shutdown`, `SourceHealth`, `_RateLimiter`,
  `_retry`), `planner.py` (`_plan_ref_work`), and `sources/registry.py`
  (static source-module lists) were extracted from `check.py` in the same
  spirit — the whole orchestration module has been progressively split
  into focused subsystems across this release.
- **Tests reorganized to match**: the 1496-line `test_check_lifecycle.py`
  (15 test classes) has been split into five focused files —
  `test_runtime.py` (circuit breaker, duration formatting, rate limiter),
  `test_planner.py` (smart-rerun source selection), `test_results.py`
  (`LookupResult.recompute_best()` — a pre-existing misplacement, fixed
  opportunistically), `test_engine.py` (`lookup_reference`), and
  `test_runner.py` (`check_references` end-to-end). Same 78 tests,
  redistributed rather than rewritten; a misplaced `_RateLimiter` unit
  test was moved out of a `TestConcurrency` class it didn't belong in.
  Fixture-scoped delay/backoff monkeypatches (`_DEFAULT_DELAYS`,
  `_RETRY_BACKOFF`) now target the module that actually reads them
  (`engine_mod`/`runner_mod`/`runtime_mod`) rather than the `check_mod`
  re-export — the same fix pattern used when `runtime.py` was first
  extracted.

### Fixed

- Lint cleanup: removed unused imports and local variables, renamed
  ambiguous single-letter variable names, removed no-op `f`-string
  prefixes. No behavior change; full test suite (361 tests) remains green.
- **Sidecar identity bug**: `check --refs-json` previously passed entries
  straight to `Reference.from_dict()`, which defaults a missing `index` to
  `0` — so multiple ref entries without an explicit `index` could silently
  collide on index `0`, corrupting in-memory dicts, the sidecar, and output.
  `show` already auto-assigned indices correctly; `check` now matches it.
- **Sidecar hash under-specification**: `refs_hash()` previously hashed only
  `index` + `raw`, so editing a structured field (title, DOI, year, authors,
  etc.) without touching `raw` left a stale sidecar looking valid. The hash
  now covers every lookup-relevant field. This changes the hash output for
  all existing sidecars — see the `SIDECAR_SCHEMA_VERSION` bump above.
- **Duplicated OSTI confidence policy**: `format._osti_id_if_confident()`
  independently reimplemented the year-mismatch-penalty and
  strong-match-threshold logic already used by
  `LookupResult.recompute_best()`. Both now call a single shared
  `results.apply_year_mismatch_penalty()` helper and reference the same
  `results.STRONG_MATCH_THRESHOLD` constant, so the two policies cannot
  silently drift apart. (Note: OSTI's confidence check still evaluates
  OSTI's own per-source score, not the overall `LookupResult.evidence` —
  those answer different questions when another source has a stronger
  match than OSTI.)

## [0.1.0] - historical

Everything prior to this changelog's creation. Highlights (see `git log`
for full history):

- Multi-source reference lookup against OpenAlex, CrossRef, OSTI, DBLP,
  Semantic Scholar, and arXiv.
- GitHub and generic URL liveness checking for non-scholarly references.
- PDF extraction pipeline (`pypdf`/`pdfplumber` with LLM-based reference
  extraction) with a content-addressed extraction cache.
- Resumable runs via a versioned JSON results sidecar, with smart
  re-run planning (only retry sources that are missing, disabled, or
  errored).
- Per-source rate limiting, retry/backoff, `Retry-After` handling, and a
  session-scoped circuit breaker for systematically failing sources.
- Bounded thread-pool concurrency (`-j`/`--jobs`) with deterministic,
  citation-ordered reporting regardless of completion order.
- `ref-checker show` subcommand for re-rendering a sidecar or bare refs
  JSON without re-querying any source.
- Bundled Agent Skill (`ref-checker skill`) for reference checking.
