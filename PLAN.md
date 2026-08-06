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

## Planned work: shared `SourceContext` (session + credentials)

Backlog item: source modules currently build a brand-new `requests.Session`
on every single HTTP call (`openalex.py`, `crossref.py`, `osti.py`,
`dblp.py`, `arxiv.py` each have a private `_session()` helper called fresh
per request), or use no session at all (`semanticscholar.py`, `github.py`,
`url.py` call bare `requests.get`/`requests.head`). No connection pooling
benefit is realized anywhere. See `docs/source-adapter-contract.md`'s
"Explicitly out of scope" section (to be updated once this lands) and the
external assessment at `../20260806-ref-checker-assessment.md` (§6, "HTTP
behavior is shared conceptually, but not structurally").

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

### Part 1 — infrastructure + the 6 scholarly sources

Scope: `openalex.py`, `crossref.py`, `osti.py`, `dblp.py`, `arxiv.py`
(all 5 already have a `_session()` to replace) plus `semanticscholar.py`
(no session today, but same `get_by_doi`/`get_by_arxiv_id`/
`search_by_title` function shape as the other 5, and already has a
`credentials`-shaped concept — `_headers()` reading
`SEMANTICSCHOLAR_API_KEY` — that maps directly onto
`SourceContext.credentials`). Liveness sources (`github.py`, `url.py`)
are deferred to Part 2 (different function shape: `check_url`, not
`get_by_doi`/`search_by_title`; no unit tests exercise `check_url`
directly today, only stubbed at the engine boundary; gaining a session
for the first time is a different flavor of change than "inject what
already exists").

Concrete steps:

1. `sources/base.py`: add `SourceContext` dataclass (`session:
   requests.Session`, `credentials: dict[str, str]`), next to the
   `ScholarlySource`/`LivenessSource` Protocols already there.
2. `sources/_http.py`: add `build_session(user_agent, params=None) ->
   requests.Session`, replacing the 5 near-duplicated `_session()`
   functions in `openalex.py`/`crossref.py`/`osti.py`/`dblp.py`/`arxiv.py`.
3. A context-construction helper (likely `sources/registry.py`) builds all
   6 contexts once:
   - OpenAlex/CrossRef: `User-Agent` + `session.params = {"mailto": ...}`
     if `OPENALEX_MAILTO` is set (`requests.Session.params` auto-merges
     with per-call params — this **deletes** both modules' duplicated
     `_polite_params()` helpers).
   - OSTI: `User-Agent` only (uses `OPENALEX_MAILTO` in its UA string
     today too — preserved as-is).
   - DBLP, arXiv: `User-Agent` only.
   - Semantic Scholar: session built via `build_session()` (new — it has
     no session today); `credentials={"SEMANTICSCHOLAR_API_KEY": ...}` if
     set, read once instead of per-call via `_headers()`.
4. Thread contexts through the call chain: `runner.py:check_references()`
   builds `contexts: dict[str, SourceContext]` once, passes to
   `engine.py:lookup_reference(..., contexts=contexts)`; `call()`/
   `call_liveness()` look up `contexts[src.SOURCE_NAME]` and pass it as
   the final arg to the source function. `cli/main.py:run_lookup()`
   (single-shot `ref-checker lookup <source>`) builds one throwaway
   context for just that source before dispatching.
5. Signature changes (6 modules): every public function gains a mandatory
   trailing `ctx: SourceContext` parameter — `get_by_doi(doi, ctx)`,
   `get_by_arxiv_id(arxiv_id, ctx)`, `search_by_title(title, ctx)`.
   Remove now-dead `_session()`/`_headers()`/`_polite_params()`/
   `_mailto()` helpers (keep `_normalize_doi` etc. — those aren't
   session-related).
6. Test rewrites (mechanical, ~35-40 call sites total):
   - `tests/test_rate_limit.py`: ~19 sites across `TestPoliteMailto` (7),
     `Test429RaisesRateLimited` (10: openalex×2, crossref×2, osti×2,
     semanticscholar×2, dblp×1, arxiv×2), `TestSemanticScholar403Hint`
     (2). Each `monkeypatch.setattr(src, "_session", lambda: session)` (or
     `monkeypatch.setattr(_rq, "get", _fake_get)` for semanticscholar)
     becomes an explicit `ctx = SourceContext(session=session,
     credentials={...})` passed into the call.
   - `tests/test_osti.py`: `make_session` fixture reworked to build/return
     a `SourceContext`; ~15 call sites in `TestGetByDoi`/`TestSearchByTitle`
     gain `ctx=` argument.
   - `tests/test_dblp.py`: `TestMirrorFailover` (2 tests), same pattern.
   - `tests/test_engine.py`/`test_runner.py`/`test_show.py`: **no changes
     needed** — their `stub_sources` fixture monkeypatches whole functions
     with `lambda *a, **kw: (...)`, signature-agnostic, absorbs the new
     parameter automatically.
   - `tests/test_sources.py`: unaffected (tests `_summarize` etc. directly,
     no session involved).
7. New regression coverage: assert OpenAlex/CrossRef sessions carry
   `mailto` when set (now via session-level params, not per-call);  assert
   one `SourceContext`'s session is reused across two calls to the same
   source within one `check_references()` run (proves reuse, not
   reconstruction — the actual point of this change).
8. Docs: update `docs/source-adapter-contract.md` (remove "Explicitly out
   of scope: Shared SourceContext" bullet, add a section on the final
   shape) and `docs/lookup-engine.md` ("Rate limiting and retries" section,
   note session reuse). `CHANGELOG.md` entry. Note in `BACKLOG.md` that
   Semantic Scholar's "drop key on 403, retry unauthenticated" backlog item
   is now easier since `ctx.credentials` is a natural place to mutate a
   session-scoped "key is bad" flag (not implementing that now, just
   noting the dependency is now in place).

### Part 2 — extend to liveness sources (`github.py`, `url.py`)

Follow-up, filed to `BACKLOG.md`. Extends the same `SourceContext` to
`check_url(urls, ctx)` on both liveness sources, giving them connection
pooling for the first time (currently bare `requests.head` per call, no
session at all). Smaller scope than Part 1: 2 modules, different function
shape (`check_url` returns a 3-tuple including dead-URL list, unlike the
2-tuple scholarly functions), no existing direct unit tests to rewrite
(only engine-level stubs, which are signature-agnostic and need no
changes).

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
