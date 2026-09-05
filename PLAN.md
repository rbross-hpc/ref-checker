# ref-checker — Design overview

`ref-checker` is a Python CLI tool that verifies bibliographic references
against live scholarly databases. References can be provided as a **PDF**
(text extracted and parsed via LLM) or as a **JSON list** supplied directly,
with both paths producing identical lookup and reporting behaviour. Each
reference is checked against OpenAlex, CrossRef, OSTI, DBLP, Semantic
Scholar, and arXiv (optionally preceded by an institutional Ex Libris Primo
endpoint when configured via `PRIMO_BASE_URL`/`PRIMO_VID`/`PRIMO_INST`).
For references that are software repositories or web resources (with no
scholarly record), it performs URL liveness checks against GitHub and
general web URLs. Results are printed in citation order
with color-coded status indicators.

This file is a short index. See:

- **[README.md](README.md)** — installation, CLI usage, all flags,
  environment variables, output format examples. The single source of truth
  for user-facing behavior.
- **[BACKLOG.md](BACKLOG.md)** — deferred work items, not yet scheduled.
- **`docs/`** — design rationale for each subsystem, for anyone modifying
  the implementation:
  - [docs/extraction.md](docs/extraction.md) — PDF text extraction and
    LLM-based reference extraction (`pdf.py`, `extract.py`).
  - [docs/matching.md](docs/matching.md) — title similarity scoring
    (`similarity.py`).
  - [docs/lookup-engine.md](docs/lookup-engine.md) — multi-source lookup,
    rate limiting/retries, concurrency, resume, and the results sidecar
    (`engine.py`, `runner.py`, `runtime.py`, `planner.py`, `results.py`,
    `sidecar.py`).
  - [docs/source-adapter-contract.md](docs/source-adapter-contract.md) —
    the capability contract each source module declares
    (`sources/base.py`, `SUPPORTED_QUERY_KINDS`, `DEFAULT_DELAY`).
  - [docs/skills-subsystem.md](docs/skills-subsystem.md) — the bundled
    Agent Skill packaging design (`skills/`, `cli/skill.py`).

## Package layout

```
ref-checker/
├── pyproject.toml
├── LICENSE                        # BSD 3-Clause, Argonne National Laboratory
├── README.md
├── PLAN.md                        # this file
├── BACKLOG.md
├── docs/                          # design rationale, see links above
└── ref_checker/
    ├── __init__.py
    ├── __main__.py                # python -m ref_checker entry point
    ├── pdf.py                     # PDF → text (pypdf → pdfplumber fallback)
    ├── extract.py                 # heuristic narrowing + LLM extraction + refs cache
    ├── similarity.py              # Unicode-normalized title ratio
    ├── results.py                 # LookupResult dataclass + _Stats
    ├── model.py                   # QueryKind / OutcomeKind / EvidenceLevel / SourceOutcome
    ├── runtime.py                 # _Shutdown, SourceHealth, _RateLimiter, _retry
    ├── planner.py                 # _plan_ref_work: smart-rerun source selection
    ├── engine.py                  # lookup_reference: assess one reference
    ├── runner.py                  # check_references: thread pool, resume, reporting
    ├── check.py                   # backward-compat re-export shim over the above
    ├── format.py                  # output formatting (format_result, colors)
    ├── sidecar.py                 # results sidecar I/O and resume policy
    ├── cli/
    │   ├── __init__.py
    │   ├── main.py                # argparse subcommand dispatcher
    │   └── skill.py                # skill show / skill export subcommands
    ├── skills/
    │   └── reference-checking/
    │       ├── SKILL.md           # bundled Agent Skill (shipped as package data)
    │       └── references/
    │           └── schema.md      # single source of truth for the reference JSON schema
    └── sources/
        ├── __init__.py
        ├── base.py                # ScholarlySource / LivenessSource capability Protocols
        ├── registry.py            # static SCHOLARLY_SOURCES / ALL_SOURCE_NAMES / DEFAULT_DELAYS
        ├── _http.py               # shared Retry-After parsing helpers
        ├── primo.py               # optional institutional Primo source (first when configured)
        ├── openalex.py            # primary scholarly source
        ├── crossref.py            # secondary scholarly source
        ├── osti.py                # DOE OSTI (technical reports + DOE journal articles)
        ├── dblp.py                # tertiary scholarly source (CS conferences/journals)
        ├── semanticscholar.py     # quaternary scholarly source
        ├── arxiv.py               # quinary / preprint source
        ├── github.py              # GitHub URL liveness checker
        └── url.py                 # generic URL liveness fallback
```

## Shared `SourceContext` (session + credentials)

Source modules used to build a brand-new `requests.Session` on every
single HTTP call (`openalex.py`, `crossref.py`, `osti.py`, `dblp.py`,
`arxiv.py` each had a private `_session()` helper called fresh per
request), or use no session at all (`semanticscholar.py` called bare
`requests.get`). No connection-pooling benefit was realized anywhere. See
`docs/source-adapter-contract.md`'s "Shared SourceContext" section for the
landed shape, and the external assessment at
`../20260806-ref-checker-assessment.md` (§6, "HTTP behavior is shared
conceptually, but not structurally").

**Status: both parts done, plus Primo.** Part 1 (the 6 scholarly sources),
Part 2 (the 2 liveness sources, `github.py`/`url.py`), and a subsequent
optional Primo source (`primo.py`) have all landed — every source module has
`build_context()` and takes a mandatory trailing `ctx: SourceContext`.

**Scope decision**: `SourceContext` holds `session` + `credentials` only.
Rate limiting (`_RateLimiter`) and retry (`_retry`) stay exactly where they
are today — in `runtime.py`, applied by `engine.py`'s `call()`/
`call_liveness()` *around* the source call, not something source functions
invoke themselves. Folding limiter/retry into `SourceContext` (as the
assessment's illustrative sketch does) would blur an already-correct
separation of concerns, so that part of the sketch is deliberately not
followed literally.

**Lifecycle decision**: contexts are built **once per `check_references()`
run** (not per-reference, not per-call), so connection reuse actually
spans every reference in a run — the real point of this change. Built in
`runner.py`, threaded down through `engine.py:lookup_reference()` to
`call()`/`call_liveness()`, which pass the right context to whichever
source function they're calling.

This work is split into two parts:

### Part 1 — infrastructure + the 6 scholarly sources (done)

Scope: `openalex.py`, `crossref.py`, `osti.py`, `dblp.py`, `arxiv.py`, and
`semanticscholar.py`. Landed:

- `sources/base.py:SourceContext` — dataclass (`session:
  requests.Session`, `credentials: dict[str, str]`), next to the
  `ScholarlySource`/`LivenessSource` Protocols.
- `sources/_http.py:build_session(user_agent, params=None)` — replaced the
  5 near-duplicated `_session()` functions.
- `sources/registry.py:build_all_contexts()` — builds all 6 contexts once.
  OpenAlex/CrossRef set `session.params = {"mailto": ...}` when
  `OPENALEX_MAILTO` is set (`requests.Session.params` auto-merges with
  per-call params — this deleted both modules' `_polite_params()`
  helpers); OSTI/DBLP/arXiv are User-Agent only; Semantic Scholar reads
  `SEMANTICSCHOLAR_API_KEY` once into `credentials` instead of per-call
  via the old `_headers()`.
- Contexts threaded through the call chain: `runner.py:check_references()`
  builds `contexts: dict[str, SourceContext]` once and passes it to
  `engine.py:lookup_reference(..., contexts=contexts)`; `call()` looks up
  (or lazily builds, when no run-scoped dict was supplied) the context per
  source and passes it as the function's final arg.
  `cli/main.py:run_lookup()` builds one throwaway context per invocation.
- All 6 modules' public functions gained a mandatory trailing
  `ctx: SourceContext` parameter; dead `_session()`/`_headers()`/
  `_polite_params()`/`_mailto()` helpers removed.
- Test rewrites: `tests/test_rate_limit.py` (`TestPoliteMailto` rewritten
  to assert on `build_context()`'s session params directly, since mailto
  is now set once at context-build time rather than per-call;
  `Test429RaisesRateLimited`/`TestSemanticScholar403Hint` inject an
  explicit `SourceContext`), `tests/test_osti.py` (`make_session` fixture
  returns a bare session; call sites pass `_ctx(session)`),
  `tests/test_dblp.py` (`TestMirrorFailover`, same pattern).
  `tests/test_engine.py`/`test_runner.py` needed their `stub_sources`-based
  per-test lambda/def overrides updated to accept the new trailing `ctx`
  arg (their `stub_sources` fixture default itself needed no changes,
  being `lambda *a, **kw: ...`).
- New regression coverage: `TestSourceContextReuse` in `test_engine.py`
  (same context object reused across two calls to the same source within
  one `lookup_reference()`, and across separate `lookup_reference()` calls
  sharing one `contexts` dict) and `test_same_session_reused_across_references_in_one_run`
  in `test_runner.py` (proves `check_references()` end-to-end session
  reuse — the actual point of this change).
- Docs: `docs/source-adapter-contract.md` gained a "Shared SourceContext"
  section; `docs/lookup-engine.md`'s concurrency section notes session
  reuse. Semantic Scholar's "drop key on 403, retry unauthenticated"
  backlog item used exactly this: `ctx.credentials` is the session-scoped
  "key is bad" flag, mutated in place on first 403 (landed later on branch
  `fix/dblp-and-s2-source-fixes`).

### Part 2 — extend to liveness sources (`github.py`, `url.py`) (done)

Extended the same `SourceContext` to `check_url(urls, ctx)` on both
liveness sources, giving them connection pooling for the first time
(previously bare `requests.head` per call, no session at all). Landed:

- `github.py`/`url.py` each gained `build_context()` (User-Agent only, via
  the same `sources/_http.py:build_session()` used by Part 1) and
  `check_url` now takes a mandatory trailing `ctx: SourceContext`,
  replacing the per-call `requests.head(..., headers={"User-Agent": ...})`
  with `ctx.session.head(...)`.
- `sources/registry.py:build_all_contexts()` now covers
  `SCHOLARLY_SOURCES + LIVENESS_SOURCES` (was scholarly-only).
- `engine.py:call_liveness()` gained the same `_ctx_for(src)` lazy-or-shared
  context lookup that `call()` already had, and passes `ctx` as
  `check_url`'s final arg.
- Test rewrites: only one call site needed updating —
  `tests/test_engine.py`'s `TestEvidenceLevel::test_github_liveness_is_live_resource_only`
  override gained the trailing `ctx` param (confirming the plan's
  prediction that engine-level stubs are signature-agnostic and mostly
  didn't need changes).
- New regression coverage:
  `TestSourceContextReuse::test_liveness_source_reuses_context_across_references`
  (`test_engine.py`) and
  `test_same_session_reused_for_liveness_source_across_references`
  (`test_runner.py`), mirroring the Part 1 scholarly-source reuse tests.
- Manually verified against live network: `github.check_url()` and
  `url.check_url()` directly, plus a full `ref-checker check` run against
  a GitHub-URL reference.

### Part 3 — thread-local contexts, fixing shared-session concurrency (done)

Addresses issue #2 from the 2026-08-06 reassessment
(`../20260806-ref-checker-assessment-2.md`): Parts 1 and 2 built one
`SourceContext` per source, shared verbatim across every worker thread in
`check_references()`'s `ThreadPoolExecutor`. `requests.Session` is not
documented as safe for concurrent use — every request reads
`session.cookies` and every response writes `Set-Cookie` back into it,
unsynchronized across threads. Confirmed live (curling the actual target
APIs) that OSTI and `github.com` both set cookies today, so two worker
threads processing different references really could race on the same
session's cookie jar.

Landed:

- `sources/registry.py:ThreadLocalSourceContexts` — a `threading.local()`
  -backed registry. `.get(name)` lazily builds a context for that source
  the first time the *calling thread* asks, then returns the same object
  on every later call from that thread — reuse is now per-thread, not
  global. Tracks every context built by any thread (under a lock) so
  `.close_all()` can close every session deterministically at end of run.
  Satisfies the same `.get(name)` / `[name] = ...` duck-typed interface
  `engine.py:_ctx_for()` already used, so `engine.py` needed no logic
  change.
- `runner.py:check_references()` builds one `ThreadLocalSourceContexts`
  per run (replacing the flat dict from `build_all_contexts()`) and calls
  `contexts.close_all()` in the `finally:` block, after the
  `ThreadPoolExecutor`'s `with` block has fully joined every worker thread
  — safe on both normal completion and interruption.
  `sources/registry.py:build_all_contexts()` is kept as-is for direct-call
  test paths and `cli/main.py:run_lookup()`'s single-context-per-invocation
  case, where there's no concurrent thread sharing to worry about.
- New tests: `tests/test_registry.py` (unit tests for
  `ThreadLocalSourceContexts` directly — same-thread reuse, cross-thread
  isolation via real `threading.Thread`s, `close_all()` closes every
  session across threads) and `tests/test_runner.py`'s new
  `TestThreadLocalSourceContexts` class (end-to-end: same-thread reuse
  under `jobs=1`, cross-thread isolation under `jobs=3` — asserting no two
  distinct worker threads ever report the same session id — and
  deterministic session closure on both normal completion and an
  interrupted run).
- Docs: `docs/lookup-engine.md` gained a "Threading model for
  SourceContext" subsection explaining the cookie-jar race and the
  per-thread fix; `docs/source-adapter-contract.md`'s "Shared
  SourceContext" section updated to describe both context flavors.

## Testing

```bash
pytest tests/
```

Test files mirror the module layout above (e.g. `test_engine.py`,
`test_runner.py`, `test_runtime.py`, `test_planner.py`, `test_model.py`,
`test_results.py`, plus per-source and per-CLI-command test files). All
tests run offline — no network or LLM calls — see
[README.md](README.md#testing) for fixture provenance and how to run them.

## Dependencies

- `requests >= 2.31`
- `pypdf >= 4.0`
- `pdfplumber >= 0.10`
- `openai >= 1.0`
- Python >= 3.10

## License

BSD 3-Clause. Copyright (c) 2026, UChicago Argonne, LLC, Argonne National Laboratory.
</content>
