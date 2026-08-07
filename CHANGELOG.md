# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
