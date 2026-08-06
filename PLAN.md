# ref-checker — Design overview

`ref-checker` is a Python CLI tool that verifies bibliographic references
against live scholarly databases. References can be provided as a **PDF**
(text extracted and parsed via LLM) or as a **JSON list** supplied directly,
with both paths producing identical lookup and reporting behaviour. Each
reference is checked against OpenAlex, CrossRef, OSTI, DBLP, Semantic
Scholar, and arXiv. For references that are software repositories or web
resources (with no scholarly record), it performs URL liveness checks
against GitHub and general web URLs. Results are printed in citation order
with color-coded status indicators.

This file is a short index. See:

- **[README.md](README.md)** — installation, CLI usage, all flags,
  environment variables, output format examples. The single source of truth
  for user-facing behavior.
- **[CHANGELOG.md](CHANGELOG.md)** — what has actually shipped, release by
  release.
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
├── CHANGELOG.md
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

**Status: Part 1 done** (the 6 scholarly sources). **Part 2 pending**
(the 2 liveness sources, `github.py`/`url.py` — see `BACKLOG.md`).

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
  reuse; `CHANGELOG.md` entry added. Semantic Scholar's "drop key on 403,
  retry unauthenticated" backlog item is now easier since
  `ctx.credentials` is a natural place to mutate a session-scoped "key is
  bad" flag (not implemented yet, just noting the dependency is in place).

### Part 2 — extend to liveness sources (`github.py`, `url.py`) (pending)

Filed to `BACKLOG.md`. Extends the same `SourceContext` to
`check_url(urls, ctx)` on both liveness sources, giving them connection
pooling for the first time (currently bare `requests.head` per call, no
session at all). Smaller scope than Part 1: 2 modules, different function
shape (`check_url` returns a 3-tuple including dead-URL list, unlike the
2-tuple scholarly functions), no existing direct unit tests to rewrite
(only engine-level stubs, which are signature-agnostic and need no
changes). `engine.py:call_liveness()` will need the same lazy-or-shared
context lookup that `call()` already has.

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
