---
name: reference-checking
description: Verify bibliographic references in an academic paper against OpenAlex, CrossRef, OSTI, DBLP, Semantic Scholar, arXiv, and optionally an institutional Ex Libris Primo endpoint. Accepts either a PDF (references extracted via LLM) or a JSON list you supply directly. Use when the user asks to audit, check, or find real sources for citations.
license: BSD-3-Clause
metadata:
  audience: researchers, editors
  tool: ref-checker
---

# Skill: Reference Checking with ref-checker

Use this skill when the user asks you to verify, audit, or check the
bibliographic references in an academic paper — whether they hand you a PDF,
paste a reference list, or have a pre-structured JSON file.

## What ref-checker does

`ref-checker` verifies bibliographic references against live scholarly
databases. It accepts two equivalent input modes:

- **PDF input** — extracts every reference via an LLM, then looks each one up.
- **JSON input** — takes a list of references you provide directly, skipping
  extraction entirely. No PDF or LLM required.

For either input, it looks up each reference across **OpenAlex**, **CrossRef**,
**OSTI**, **DBLP**, **Semantic Scholar**, and **arXiv** in priority order (preceded
by an institutional **Ex Libris Primo** endpoint when configured — see env vars
below), and performs URL liveness checks for GitHub repositories and web
resources. Results are printed color-coded (**OK** / **CLOSEST** / **NO MATCH**)
with similarity scores and notes for each reference.

Progress and credential status are written to stderr. The reference report is
written to stdout and can be redirected cleanly.

## Prerequisites

### Installation

```bash
pipx install git+https://github.com/rbross-hpc/ref-checker.git
```

Verify: `ref-checker --help`

### Required environment variables

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | **Yes, for PDF input only** | Key for the LLM used to extract references from a PDF. Not needed when using `--refs-json`. |
| `OPENAI_BASE_URL` | No | Override base URL (e.g. local proxy) |
| `OPENAI_MODEL` | No | Model name (default: `GPT-5.4`) |
| `OPENAI_API_MODEL` | No | Fallback for `OPENAI_MODEL` if that's unset |
| `OPENALEX_MAILTO` | Recommended | Your email — enables the polite pool for OpenAlex/CrossRef |
| `SEMANTICSCHOLAR_API_KEY` | Recommended | Without one, Semantic Scholar rate-limits aggressively |
| `PRIMO_BASE_URL` | No | Institutional Primo host — enables Primo source when set with VID+INST |
| `PRIMO_VID` | No | Primo view ID (required with `PRIMO_BASE_URL`) |
| `PRIMO_INST` | No | Primo institution code (required with `PRIMO_BASE_URL`) |
| `PRIMO_SCOPE` | No | Primo search scope (default: `MyInst_and_CI`) |

Check credential status at startup — ref-checker prints a summary to stderr
with sensitive values masked as `<set>`. All four `PRIMO_*` vars are shown
verbatim (none are sensitive).

## Common invocations

### Check references from a PDF

```bash
ref-checker check paper.pdf
```

Extracts references via LLM (requires `OPENAI_API_KEY`), then looks up each
one. Writes a refs cache (`paper.refs.json`) and a results sidecar
(`paper.results.json`) next to the PDF on first run.

### Check references from a JSON list

```bash
ref-checker check --refs-json refs.json
```

Looks up references directly from a JSON file — no PDF and no LLM required.
See the **Reference JSON schema** section below for the expected format. The
result sidecar defaults to `<refs-stem>.results.json`.

### Save results to a file

```bash
ref-checker check paper.pdf > results.txt
ref-checker check --refs-json refs.json > results.txt
```

Progress goes to stderr; the report goes to stdout.

### Extract references from a PDF only (no lookup)

```bash
ref-checker extract paper.pdf
```

Writes `paper.refs.md` (numbered raw text) and `paper.refs.json` (structured
JSON) alongside the PDF. Running `extract` first primes the refs cache for a
subsequent `check` run, and produces a JSON file you can inspect or edit before
passing back via `--refs-json`.

### Query a single source directly

```bash
ref-checker lookup openalex --title "Attention is all you need"
ref-checker lookup openalex --doi 10.48550/arXiv.1706.03762
ref-checker lookup arxiv --id 1706.03762
```

Prints a JSON object with `summary`, `similarity`, and `source` fields.
Available sources: `openalex`, `crossref`, `osti`, `dblp`, `semanticscholar`, `arxiv`
(plus `primo` when configured).

## Resuming interrupted runs

Results are written atomically after each reference. Re-running the same
command automatically resumes: OK references are replayed from the sidecar
instantly; failures are re-queried. Use `--no-resume` to force a full re-run.

```bash
ref-checker check paper.pdf            # first run
ref-checker check paper.pdf            # resumes, skips OK refs
ref-checker check paper.pdf --no-resume    # re-queries everything
ref-checker check paper.pdf --retry-closest   # also re-queries CLOSEST refs
```

The same resume behaviour applies to `--refs-json` runs.

## Interpreting results

Each reference is printed with a status line:

```
[1] Vaswani et al., "Attention Is All You Need", 2017, (Neural Information Processing Systems)
    OK (0.99)  doi:10.48550/arxiv.1706.03762  [source: openalex]

[3] Smith, "A Somewhat Related Paper", 2019, (Journal of Things)
    CLOSEST (0.87)  [source: crossref]
        Closest candidate across services:
        Smith, "A Somewhat Related Paper on the Same Topic", 2020, (Journal of Things)
        https://doi.org/10.1000/xyz123
    Note: year mismatch (ref year=2019, match year=2020)

[5] Bar et al., "Nonexistent Paper", 2020, (Some Conference)
    NO MATCH (0.34)  [source: openalex]
```

### Status codes

| Status | Meaning |
|---|---|
| **OK** | Identifier confirmed (DOI/arXiv lookup), strong title match (≥ 0.90), or live URL |
| **CLOSEST** | Best title similarity ≥ 0.80 and < 0.90 — plausible but not certain |
| **NO MATCH** | Best similarity below 0.80 — reference not found or significantly different |

The number in parentheses is the **title similarity** (0.00–1.00). A 0.10
year-mismatch penalty is applied on title-search hits. For DOI/arXiv
identifier-confirmed hits the score is the raw title ratio with no penalty.
For GitHub/URL liveness checks the score is `----`.

### Note lines

| Note | Meaning |
|---|---|
| `year mismatch (ref year=X, match year=Y)` | Years differ — may be pre-print vs. published |
| `DOI title: "..."` | DOI-confirmed but title diverges significantly |
| `URL liveness check only` | Software/web reference; no bibliographic record |
| `retries exhausted for <source>` | Source hit its retry limit; results may be incomplete |
| `DOI not found in any source: <doi>` | DOI present in reference but unfound |
| `URL check failed (HTTP NNN): <url>` | Dead URL |

## Reference JSON schema

When using `--refs-json`, supply a bare JSON array of reference objects.
The full field-by-field schema, extraction rules, and worked examples live
in this skill's `references/schema.md`.

At minimum, include `title`. Everything else is optional but improves lookup
accuracy. `doi` and `arxiv_id` enable identifier-based lookup which is faster
and more reliable than title search.

## Workflow guidance

1. **Choose your input mode:**
   - If the user provides a **PDF**, run `ref-checker check paper.pdf`.
     Requires `OPENAI_API_KEY`.
   - If the user **pastes or provides a reference list**, construct a JSON file
     using the schema above and run `ref-checker check --refs-json refs.json`.
     `OPENAI_API_KEY` is not needed.
   - After a PDF `check` or `extract` run, `<stem>.refs.json` is written
     automatically — you can pass it back via `--refs-json` on later runs.

2. **Recommend setting `OPENALEX_MAILTO`** to the user's email before any run,
   for the OpenAlex/CrossRef polite pool.

3. **Triage results**: OK references need no action. CLOSEST references
   warrant a closer look (wrong edition, pre-print vs. published, minor title
   variation). NO MATCH references may be fabricated, retracted, or simply
   unfound — investigate further.

4. **For incomplete runs** (Semantic Scholar exhausted, network errors), re-run
   the same command — resume picks up where it left off. Use `--retry-all` or
   `--retry-closest` to broaden what gets re-queried.
