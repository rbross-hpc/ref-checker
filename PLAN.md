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
