# ref-checker — Design and Implementation Plan

## Overview

`ref-checker` is a Python CLI tool that verifies bibliographic references
against live scholarly databases. References can be provided as a **PDF** (text
extracted and parsed via LLM) or as a **JSON list** supplied directly, with
both paths producing identical lookup and reporting behaviour. Each reference is
checked against OpenAlex, CrossRef, OSTI, DBLP, Semantic Scholar, and arXiv. For
references that are software repositories or web resources (with no scholarly
record), it performs URL liveness checks against GitHub and general web URLs.
Results are printed in citation order with color-coded status indicators.

---

## Package layout

```
ref-checker/
├── pyproject.toml
├── LICENSE                        # BSD 3-Clause, Argonne National Laboratory
├── README.md
├── PLAN.md                        # this file
└── ref_checker/
    ├── __init__.py
    ├── __main__.py                # python -m ref_checker entry point
    ├── pdf.py                     # PDF → text (pypdf → pdfplumber fallback)
    ├── extract.py                 # heuristic narrowing + LLM extraction + refs cache
    ├── similarity.py              # Unicode-normalized title ratio
    ├── results.py                 # LookupResult dataclass + _Stats
    ├── format.py                  # output formatting (format_result, colors)
    ├── sidecar.py                 # results sidecar I/O and resume policy
    ├── check.py                   # driver: lookup, rate limiting, orchestration
    ├── cli/
    │   ├── __init__.py
    │   ├── main.py                # argparse subcommand dispatcher
    │   └── skill.py               # skill show / skill export subcommands
    ├── skills/
    │   └── reference-checking/
    │       ├── SKILL.md           # bundled Agent Skill (shipped as package data)
    │       └── references/
    │           └── schema.md      # single source of truth for the reference JSON schema
    └── sources/
        ├── __init__.py
        ├── openalex.py            # primary scholarly source
        ├── crossref.py            # secondary scholarly source
        ├── osti.py                # DOE OSTI (technical reports + DOE journal articles)
        ├── dblp.py                # tertiary scholarly source (CS conferences/journals)
        ├── semanticscholar.py     # quaternary scholarly source
        ├── arxiv.py               # quinary / preprint source
        ├── github.py              # GitHub URL liveness checker
        └── url.py                 # generic URL liveness fallback
```

---

## Step 1 — PDF text extraction (`ref_checker/pdf.py`)

- Uses `pypdf` as the primary extractor; falls back to `pdfplumber` if the
  result is empty or very short (< 100 chars).
- Each page is prefixed with an `<!-- page N -->` marker so downstream code
  can locate and count pages.
- `pdfplumber` is **not** used as the primary extractor despite having fewer
  split-word artifacts because it merges multi-column layouts, which badly
  confuses the LLM on two-column papers (the majority of this collection).

---

## Step 2 — Reference extraction (`ref_checker/extract.py`)

### Heuristic narrowing

Before calling the LLM, the full PDF text is narrowed to just the references
section:

1. **Heading detection**: scan for a line matching
   `^\s*(References|Bibliography|Works Cited|Literature Cited)\s*$`
   (case-insensitive). Keep from that line to end of document.
2. **Post-references trimming**: after finding the References heading, truncate
   at any subsequent section heading that signals non-reference content:
   `Appendix`, `Acknowledgements`, `Supplementary`, `About the Authors`, etc.
   This prevents appendices from inflating the token count.
3. **Tail-pages fallback**: if no heading is found, keep the last `N` pages
   (default 5, configurable via `--tail-pages`).

### PDF artifact repair (`_fix_split_words`)

Before sending to the LLM, two common PDF text extraction artifacts are repaired:

- **Hyphenated line breaks**: `frame-\nwork` → `framework`. URLs are protected
  first (placeholder substitution) so hyphens inside URL paths are never joined.
- **Split-word glyphs**: `Support V ector` → `Support Vector`. The pattern
  `(?<=\w) ([A-Z]) ([a-z]{3,})` requires the lone capital to be preceded by a
  word character, so standalone articles (`A new`, `The system`) are not merged.

### LLM extraction

The narrowed text is sent to an OpenAI-compatible LLM with a structured prompt
requesting a JSON object. The prompt:

- Explains that the input is PDF-extracted text and may contain artifacts.
- Requests these fields per reference:
  `index`, `raw`, `title`, `authors`, `year`, `doi`, `arxiv_id`, `venue`, `url`
- Instructs the LLM to set `authors: []` for corporate/organizational authors.
- Instructs the LLM to extract canonical DOI strings (no `doi:` prefix, no URL).
- Instructs the LLM to extract bare arXiv IDs (no `arXiv:` prefix, no version).
- Instructs the LLM to extract non-DOI/non-arXiv URLs into the `url` field,
  space-separated if multiple.
- Includes two few-shot examples: one with a corporate author and GitHub URLs,
  one with an arXiv preprint and ALL-CAPS author surnames.
- Uses `response_format={"type": "json_object"}` to guarantee valid JSON.
- Retries up to 3 times (2 s → 4 s backoff) on exception, JSON decode error,
  or schema-shape failure. Hard exit if all attempts fail.

### Post-extraction backfill

After LLM extraction, a regex pass fills in any identifiers the LLM missed:

- **DOI**: `(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/...)`
- **arXiv ID**: `\barXiv[:\s]+(\d{4}\.\d{4,5})(?:v\d+)?\b` (prefix required)
- **GitHub URLs**: `https?://github\.com/[^\s,;\"'<>)]+`, space-joined,
  deduplicated
- **Generic URLs**: all `https?://` URLs not matching doi.org, arxiv.org, or
  github.com — stored in `ref.url`, space-joined, deduplicated

The `github_url` and `url` fields are also seeded from the LLM's `url` field
(filtered by domain).

---

## Step 3 — Similarity scoring (`ref_checker/similarity.py`)

A single function `title_ratio(ref_title, cand_title) -> float`:

- Applies NFKD Unicode normalization + combining-character strip (handles
  diacritics like `ö` → `o`, ligatures like `ﬁ` → `fi`).
- Casefolding, punctuation→space, whitespace collapse.
- SequenceMatcher ratio on the normalized strings.

Authors are **not** part of the similarity score. Early testing showed that
LLM-extracted first authors were often incorrect (corporate names, section
numbers, ALL-CAPS surnames with diacritics stripped), causing good title
matches to be penalized.

---

## Step 4 — Multi-source lookup (`ref_checker/check.py`, `results.py`, `format.py`, `sidecar.py`)

### Source priority

For each reference, sources are tried in this order:

1. **GitHub liveness** — if `ref.github_url` is set, HEAD-check immediately.
   GitHub-only references (repos, datasets) are almost never in scholarly
   databases; checking them first saves 12+ seconds of fruitless API calls.
   Short-circuit on success.

2. **arXiv ID lookup** — if `ref.arxiv_id` is set, query arXiv directly
   (exact match, similarity = 1.0). Short-circuit on success.

3. **Scholarly loop** (skipped entirely for url-only refs):
   For each source in order — OpenAlex → CrossRef → OSTI → DBLP → Semantic Scholar → arXiv:
   - DOI lookup (if `ref.doi`)
   - arXiv-ID lookup via the source's DOI or native arXiv endpoint
     (if `ref.arxiv_id`, skipping arXiv itself which was already tried)
   - Title search (only if `result.best_similarity < 0.90` — skips expensive
     calls to SS and arXiv when OA/CR already returned a good match)

4. **Generic URL liveness** — last resort, only if:
   - `result.best_similarity < min_match`, and
   - `ref.url` is set, and
   - `ref.github_url` is not set (GitHub already handled above)

### url-only gate

References with no DOI, no arXiv ID, no venue, and at least one URL skip the
entire scholarly loop. These are web pages, documentation, and tools not
indexed in any scholarly database. Going straight to URL liveness is faster
and avoids misleading "closest candidates."

### Year mismatch penalty

When a candidate is found via title search (not DOI/arXiv ID):
- If both `ref.year` and `candidate.year` are present and differ, subtract
  `0.10` from the similarity score.
- Record a `year_mismatch_note` for display.
- DOI/arXiv-ID hits are not penalized (the identifier is the identity proof);
  year mismatches are noted informally instead.

### Rate limiting and retries

- Per-source minimum delay between consecutive calls (applied by a
  reservation-style rate limiter under a `threading.Lock`: `wait()` atomically
  computes the next available slot for a source and reserves it before
  sleeping, so under concurrency N threads calling the same source are still
  spaced exactly `delay` seconds apart):
  - OpenAlex: 2.0 s
  - CrossRef: 2.0 s
  - OSTI: 2.0 s
  - DBLP: 1.0 s
  - Semantic Scholar: 8.0 s
  - arXiv: 3.0 s
  - GitHub: 1.0 s
  - URL: 1.0 s
- Per-call retry: up to 3 attempts with 5 s / 10 s / 15 s backoff on any
  exception (HTTP 429, 5xx, network timeout). 404 and 410 are treated as
  confirmed misses (no retry).
- When all retries are exhausted for a source on a given reference, a
  `Note: retries exhausted for <source>` line is printed in the output for
  that reference, and the source's exhaustion count is included in the
  end-of-run query summary.

### Scoring and ID-hit annotation

The number shown in every status line is always **title similarity** — `title_ratio(ref.title,
candidate.title)` — with the following rules:

- **Identifier-confirmed hits** (DOI or arXiv lookup): raw title ratio, no year penalty.
  The identifier is proof of identity; year disagreement is surfaced as a Note only.
- **Title-search hits**: title ratio minus 0.10 if years differ, else raw title ratio.
- **Liveness-only hits** (GitHub, URL): displayed as `(----)` — no titles to compare.

If title similarity < 0.85 on an ID-confirmed hit, a `Note: DOI title: "..."` line is shown,
indicating the DOI may resolve to a differently-titled paper (retitled preprint, DOI typo, etc.).

---

## Step 5 — Output formatting (`ref_checker/format.py`)

Each reference block:

```
[N] Last[ et al.], "Title", Year, (Venue)
    OK (0.98)  doi:10.x/y  [source: openalex]

[N] Last et al., "Title", Year, (Venue)
    OK (0.93)  doi:10.x/y  [source: crossref]
    Note: year mismatch (ref year=X, match year=Y)

[N] Last et al., "Title", Year, (Venue)
    OK (----)  https://github.com/foo/bar  [source: github]
    Note: URL liveness check only — no bibliographic record found

[N] Last et al., "Title", Year, (Venue)
    CLOSEST (0.NN)  [source: crossref]
        Closest candidate across services:
        Last, "Candidate Title", Year, (Venue)
        https://doi.org/10.x/y
    Note: year mismatch (ref year=X, match year=Y)

[N] Last et al., "Title", Year, (Venue)
    NO MATCH (0.NN)  [source: openalex]
        Closest candidate across services:
        Last, "Nearest Miss Title", Year, (Venue)
        https://...
    Note: retries exhausted for semanticscholar — results may be incomplete
```

### Status meanings

- **OK** (green) — identifier confirmed (DOI/arXiv lookup), strong title match (score ≥ 0.90),
  or confirmed-live GitHub/web URL. The number is always title similarity (or `----` for liveness).
- **CLOSEST** (orange) — best score is ≥ `--min-match` (default 0.80) and < 0.90;
  shows the closest candidate citation and URL.
- **NO MATCH** (red) — best score is below `--min-match`. Closest candidate shown if any.

### Color

Colors are applied when stdout is a TTY and `NO_COLOR` is not set. CLOSEST and
all Note lines are orange; OK is green; NO MATCH is red. Redirect to a file
produces plain text.

### Additional note lines

- `Note: year mismatch (ref year=X, match year=Y)` — on any tier when years differ.
- `Note: DOI title: "..."` — on ID-confirmed hits when title similarity < 0.85.
- `Note: URL liveness check only — no bibliographic record found` — on OK
  results from `[source: github]` or `[source: url]`.
- `Note: retries exhausted for <source> — results may be incomplete` — when a
  source exceeded its retry budget for this reference.
- `DOI not found in any source: <doi>` — when a DOI was present in the
  reference but no source returned a hit.
- `arXiv ID not found in any source: <id>` — same for arXiv IDs.
- `URL check failed (HTTP NNN): <url>` — when a GitHub or web URL returned a
  confirmed-dead status.

---

## Step 6 — Query summary

After all references are checked, a summary is printed to stderr:

```
[ref-checker] Query summary:
[ref-checker]   arxiv                  8 queries, 4 retries
[ref-checker]   crossref               9 queries
[ref-checker]   openalex              35 queries
[ref-checker]   semanticscholar        6 queries, 11 retries, 4 exhausted
[ref-checker]   url                    5 queries
```

This makes rate-limit issues and missing API keys immediately visible.

---

## CLI subcommands

### `ref-checker check [PDF] [options]`

Full pipeline: extract references then check each one. `PDF` is optional
when `--refs-json` is supplied; a warning is printed if both are given.
When neither is supplied, an error is printed and the command exits.

```
--refs-json PATH          Load references from a bare JSON array of ref dicts,
                          skipping PDF extraction entirely. PDF argument becomes
                          optional; sidecar defaults to <refs-json-stem>.results.json.
--refs-cache PATH         Refs cache file (default: <pdf-stem>.refs.json next to PDF)
--no-refs-cache           Disable refs cache entirely
--re-extract              Force re-extraction even if refs cache is valid
--tail-pages N            Tail-page fallback page count (default: 5)
--min-match F             CLOSEST threshold (default: 0.80)
--delay-openalex S        Per-call delay in seconds (default: 2.0)
--delay-crossref S        (default: 2.0)
--delay-osti S            (default: 2.0)
--delay-dblp S            (default: 1.0)
--delay-semanticscholar S (default: 8.0)
--delay-arxiv S           (default: 3.0)
--delay-github S          (default: 1.0)
--delay-url S             (default: 1.0)
--results-json PATH       Sidecar file (default: <pdf-stem>.results.json next to PDF)
--no-results-json         Disable sidecar entirely
--no-resume               Disable resume (default: resume is ON)
--retry-all               Re-query every ref regardless of sidecar
--retry-closest           Also re-query CLOSEST refs on resume
--with-osti-id            Append '(OSTI: <id>)' to each status line on confident OSTI hits
-j, --jobs N              Refs to query in parallel (default: 3; use 1 for strictly sequential)
```

### Per-paper refs cache

Every `check` run (and `extract`) writes `<pdf-stem>.refs.json` next to the PDF
in the following wrapper format:

```json
{
  "schema_version": 1,
  "pdf": "paper.pdf",
  "pdf_sha256": "<hex>",
  "extracted_at": "<ISO-8601>",
  "extractor": { "model": "GPT-5.4", "tail_pages": 5 },
  "references": [ { "index": 1, "raw": "...", ... } ]
}
```

On subsequent `check` runs the cache is loaded if the file exists and its
`pdf_sha256` matches the current PDF content. Any mismatch (schema version,
hash, corrupt JSON, missing file) triggers a fresh LLM extraction and overwrites
the file. There is no legacy bare-list support — older `refs.json` files are
treated as schema mismatches and silently replaced.

### Per-paper resume sidecar

Every `check` run writes `<pdf-stem>.results.json` next to the PDF (atomic
write via a `.tmp` rename). The sidecar contains:

- `schema_version` — format version for future compatibility.
- `pdf` — source file name.
- `refs_hash` — SHA-256 prefix of the reference list; used to detect stale
  sidecars when the PDF is re-extracted.
- `references` — object keyed by reference index; each entry has `ref`
  (serialized `Reference`) and `result` (serialized `LookupResult` including
  `status`, `display_score`, `best_source`, `exhausted_sources`, etc.).

**Resume policy** (on by default; disable with `--no-resume`):

A ref is considered **done** (skipped) when all hold:
- `status == "OK"`
- `exhausted_sources` is empty
- `dead_urls` is empty

A ref is **retried** when any hold:
- `status` is `NO MATCH`
- `status` is `CLOSEST` and `--retry-closest` is set
- `exhausted_sources` is non-empty (transient network/rate-limit failure)
- `dead_urls` is non-empty
- ref index not present in sidecar

If the sidecar's `refs_hash` does not match the current reference list, a
warning is printed and the sidecar is ignored (full run performed).

### `ref-checker extract PDF [options]`

Extract references only; write `<stem>.refs.md` and `<stem>.refs.json`.

```
--out-dir DIR    Output directory (default: same as PDF)
--tail-pages N   (default: 5)
```

### `ref-checker lookup <source> (--doi | --arxiv-id | --id | --title) [options]`

Query a single source and print JSON to stdout.

Sources: `openalex`, `crossref`, `dblp`, `semanticscholar`, `arxiv`

```
--doi DOI        (not available for dblp)
--arxiv-id ID    (openalex, semanticscholar only)
--id ID          (arxiv only)
--title TITLE    (all sources)
```

---

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | Yes (for `check`/`extract`) | Key for the LLM extraction call |
| `OPENAI_BASE_URL` | No | Override the OpenAI-compatible base URL |
| `OPENAI_MODEL` | No | Model to use (default: `gpt-4o-mini`) |
| `OPENALEX_MAILTO` | Recommended | Email for OpenAlex/CrossRef polite pool |
| `SEMANTICSCHOLAR_API_KEY` | No | SS API key for higher rate limits |

Sensitive values (`OPENAI_API_KEY`, `SEMANTICSCHOLAR_API_KEY`) are displayed
as `<set>` in the credential summary at startup.

---

## Known limitations and future work

- **Semantic Scholar rate limiting**: without an API key, SS unauthenticated
  requests are aggressively throttled. The tool mitigates this by skipping SS
  title search when a prior source already returned ≥ 0.90 similarity, but
  registering for a free SS API key is strongly recommended.
- **Multi-column PDF layouts**: `pdfplumber` handles ligatures and split-word
  artifacts better than `pypdf`, but merges multi-column text — so `pypdf`
  remains primary. Future work: per-file engine selection or a pre-processing
  pass that detects column layout.
- **Concurrency**: refs are queried concurrently via a `ThreadPoolExecutor`
  (default 3 workers; `-j N` / `--jobs N` to tune, `--jobs 1` for strictly
  sequential). Per-source polite-pool spacing is preserved via a
  reservation-style `_RateLimiter`: `wait()` atomically computes the next
  available slot for a source and reserves it under a lock, so N threads
  calling the same source get deterministically spaced `delay` seconds apart.
  `SourceHealth` (session circuit breaker) and `_Stats` are lock-protected for
  thread-safe mutation. Formatted result blocks are buffered and emitted to
  stdout in ref-index order at end-of-run so the report is deterministic
  regardless of completion order; progress and warnings stream to stderr live.
  On SIGINT the pool is shut down cleanly waiting for in-flight refs to
  finish before flushing the sidecar.
- **Rate limiter scope**: the rate limiter is shared across all references within
  a single `check_references()` call, enforcing inter-request delays correctly
  across reference boundaries. It does not persist across separate CLI invocations.
- **API response caching**: individual API responses are not cached across runs.
  Re-running on a different paper (or after `--no-resume`) repeats all source
  queries. The refs cache and results sidecar mitigate this for iterative work
  on the same paper, but do not share across papers.
- **arXiv title search**: uses `ti:"<title>"` which requires a fairly exact
  match. A looser `all:` query would improve recall for titles with PDF
  extraction artifacts that survive the repair pass.

---

## Testing

Run the test suite with:

```bash
pytest tests/
```

126 tests, no network calls, runs in < 1 s. Coverage:

| File | What's tested |
|---|---|
| `test_similarity.py` | `title_ratio`: normalization, Unicode, ligatures, None/empty, symmetry |
| `test_sidecar.py` | `refs_hash`, `status_label`, round-trip serialize/deserialize, `needs_retry` all cases, `load`/`write` with all failure modes |
| `test_refs_cache.py` | `write_refs_cache`/`load_refs_cache`: valid, missing, corrupt, schema mismatch, hash mismatch, field round-trip |
| `test_format.py` | `format_result` for every output tier (OK ID, OK ≥0.90, CLOSEST, NO MATCH, liveness); all Note line types; `_format_citation` edge cases |
| `test_dblp.py` | `_normalize_authors` (list/dict/digit-suffix), `_normalize_doi`, `_summarize` (title, year, DOI, authors, venue, edge cases) |
| `test_sources.py` | `_summarize` for openalex, crossref, semanticscholar; `_parse_entry` for arxiv |
| `test_cli_check_refs_json.py` | `check --refs-json`: no-PDF run, sidecar defaults, PDF-ignored warning, explicit results path, missing file exit |
| `test_skill_cli.py` | `skill show`: markdown output, schema section, status labels, frontmatter; `skill export`: creates SKILL.md, matches show output, refuses non-empty without --force, --force overwrites, creates parent dirs, empty dir |

---

## Agent Skills subsystem

### Purpose

AI coding assistants can use `ref-checker` to audit references on the user's
behalf. The skill subsystem ships reusable agent instructions alongside the
Python package so that the skill content and the CLI are always versioned
together. An agent that installs from PyPI gets the matching skill; an agent
that upgrades the CLI automatically gets the updated skill.

The alternative — distributing the skill separately via `npx skills add
rbross-hpc/ref-checker` (pulling from GitHub) — was evaluated and rejected
because the skill version and the installed executable can drift independently.

### Package layout

The canonical skill location is inside the Python package so that
`importlib.resources` can resolve it whether the package is installed normally
or as a zip (wheel):

```
ref_checker/skills/reference-checking/SKILL.md
```

This path is included in the wheel via `pyproject.toml`:

```toml
[tool.setuptools.package-data]
ref_checker = ["skills/reference-checking/**/*"]
```

No `__init__.py` is needed inside `skills/` — access is via
`importlib.resources.files("ref_checker").joinpath("skills/reference-checking/…")`.

### SKILL.md frontmatter

`SKILL.md` begins with YAML frontmatter as required by the [OpenCode Agent
Skills spec](https://opencode.ai/docs/skills/):

```yaml
---
name: reference-checking
description: ...
license: BSD-3-Clause
metadata:
  audience: researchers, editors
  tool: ref-checker
---
```

`name` must match the containing directory name (`reference-checking`). The
`compatibility` field is intentionally omitted — the skill is harness-neutral
and works with any harness that supports the standard directory layout
(`.opencode/skills/`, `.claude/skills/`, `.agents/skills/`).

### CLI surface (`ref_checker/cli/skill.py`)

Two subcommands are exposed:

| Command | Behaviour |
|---|---|
| `ref-checker skill show` | Reads `SKILL.md` via `importlib.resources` and writes to stdout. |
| `ref-checker skill export PATH [--force]` | Copies the complete skill directory tree to `PATH` using `shutil.copytree(dirs_exist_ok=True)`. Refuses if `PATH` is non-empty unless `--force` is given (which first removes `PATH`). |

`show` is suitable for piping or redirection. `export` is the recommended
installation path — the user chooses the harness-specific destination and the
CLI does not auto-detect or modify any harness configuration.

### Schema single source of truth

The reference JSON schema lives in exactly one file:

```
ref_checker/skills/reference-checking/references/schema.md
```

It is used in two ways simultaneously:

1. **LLM extraction prompt** — loaded at import time in `ref_checker/extract.py`
   via `importlib.resources.files("ref_checker").joinpath("skills/…/schema.md").read_text()`,
   then interpolated into the prompt template using `string.Template`. The
   assembled `_SYSTEM_PROMPT` constant contains the full schema text inline.

2. **Agent / human reference** — `SKILL.md` links to `references/schema.md`
   in its §Reference JSON schema section; the file is exported alongside
   `SKILL.md` when the user runs `ref-checker skill export`.

To add or change a schema field, edit only `schema.md`. The LLM prompt picks
up the change automatically on the next import. `tests/test_schema_prompt.py`
asserts that all expected field names appear in the assembled prompt, so a
missing or misspelled field name in `schema.md` will fail the test suite.

---

## Dependencies

- `requests >= 2.31`
- `pypdf >= 4.0`
- `pdfplumber >= 0.10`
- `openai >= 1.0`
- Python >= 3.10

## License

BSD 3-Clause. Copyright (c) 2026, UChicago Argonne, LLC, Argonne National Laboratory.
