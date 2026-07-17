# Skill: Reference Checking with ref-checker

Use this skill when the user asks you to verify, audit, or check the
bibliographic references in an academic paper PDF, or when they provide a list
of references and ask whether they are real, findable, or correctly cited.

## What ref-checker does

`ref-checker` is a CLI tool that:

1. Extracts every bibliographic reference from a PDF (via an LLM).
2. Looks each reference up across **OpenAlex**, **CrossRef**, **DBLP**,
   **Semantic Scholar**, and **arXiv** in priority order.
3. Performs URL liveness checks for GitHub repositories and web resources.
4. Prints color-coded results (**OK** / **CLOSEST** / **NO MATCH**) with
   similarity scores and notes for each reference.

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
| `OPENAI_API_KEY` | **Yes** (for `check`/`extract`) | Key for the LLM used to extract references |
| `OPENAI_BASE_URL` | No | Override base URL (e.g. local proxy) |
| `OPENAI_MODEL` | No | Model name (default: `gpt-4o-mini`) |
| `OPENALEX_MAILTO` | Recommended | Your email — enables the polite pool for OpenAlex/CrossRef |
| `SEMANTICSCHOLAR_API_KEY` | Recommended | Without one, Semantic Scholar rate-limits aggressively |

Check credential status at startup — ref-checker prints a summary to stderr
with sensitive values masked as `<set>`.

## Common invocations

### Check a PDF

```bash
ref-checker check paper.pdf
```

Extracts references via LLM (requires `OPENAI_API_KEY`), then looks up each
one. Writes a refs cache (`paper.refs.json`) and a results sidecar
(`paper.results.json`) next to the PDF on first run.

### Check from a pre-extracted refs JSON (no PDF needed)

```bash
ref-checker check --refs-json paper.refs.json
```

Skips PDF extraction entirely. Useful when you have already extracted
references or want to construct them yourself. See the **Reference JSON
schema** section below for the expected format.

### Save results to a file

```bash
ref-checker check paper.pdf > results.txt
```

Progress goes to stderr; the report goes to stdout.

### Extract references only (no lookup)

```bash
ref-checker extract paper.pdf
```

Writes `paper.refs.md` (numbered raw text) and `paper.refs.json` (structured
JSON) alongside the PDF. Running `extract` first primes the refs cache for a
subsequent `check` run.

### Query a single source directly

```bash
ref-checker lookup openalex --title "Attention is all you need"
ref-checker lookup openalex --doi 10.48550/arXiv.1706.03762
ref-checker lookup arxiv --id 1706.03762
```

Prints a JSON object with `summary`, `similarity`, and `source` fields.
Available sources: `openalex`, `crossref`, `dblp`, `semanticscholar`, `arxiv`.

## Resuming interrupted runs

Results are written atomically after each reference. Re-running the same
command automatically resumes: OK references are replayed from the sidecar
instantly; failures are re-queried. Use `--no-resume` to force a full re-run.

```bash
ref-checker check paper.pdf           # first run
ref-checker check paper.pdf           # resumes, skips OK refs
ref-checker check paper.pdf --no-resume   # re-queries everything
ref-checker check paper.pdf --retry-closest  # also re-queries CLOSEST refs
```

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

When using `--refs-json`, supply a **bare JSON array** of reference objects.
Each object may have the following fields:

| Field | Type | Notes |
|---|---|---|
| `index` | integer | 1-based position. Defaults to 0 if omitted; sequenced from list order. |
| `raw` | string | Full reference text as it appears in the paper. Used for display only. |
| `title` | string \| null | **Primary lookup key.** Include whenever possible. |
| `authors` | array of strings | Each entry is one author's full name in natural order, e.g. `"Bernhard Scholkopf"`. Use `[]` for corporate/organizational authors or when unknown. |
| `year` | integer \| null | Publication year. Used in year-mismatch scoring. |
| `doi` | string \| null | Canonical DOI only — e.g. `"10.1145/1234.5678"`. Strip any `doi:`, `https://doi.org/`, or `http://dx.doi.org/` prefix. |
| `arxiv_id` | string \| null | Bare arXiv ID — e.g. `"2301.01234"`. Strip any `arXiv:` prefix or version suffix like `v2`. |
| `venue` | string \| null | Journal, conference, or publisher name. |
| `url` | string \| null | Any non-DOI, non-arXiv URL (GitHub repo, project page, etc.). Space-separate multiple URLs. Do not put `doi.org` or `arxiv.org` URLs here. |
| `github_url` | string \| null | Normally derived automatically from `url` — you do not need to set this separately. |

At minimum, include `title`. Everything else is optional but improves lookup
accuracy. `doi` and `arxiv_id` enable identifier-based lookup which is faster
and more reliable than title search.

### Minimal example

```json
[
  {
    "index": 1,
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "year": 2017,
    "doi": "10.48550/arXiv.1706.03762"
  },
  {
    "index": 2,
    "title": "Autotuning applications at scale",
    "authors": [],
    "year": 2023,
    "url": "https://github.com/ytopt-team/ytopt"
  }
]
```

### Full example with all fields

```json
[
  {
    "index": 1,
    "raw": "SCHOLKOPF, B. et al. (2000). New support vector algorithms. Neural Computation, 12(5), 1207-1245.",
    "title": "New support vector algorithms",
    "authors": ["Bernhard Scholkopf", "Alex Smola", "Robert Williamson", "Peter Bartlett"],
    "year": 2000,
    "doi": null,
    "arxiv_id": null,
    "venue": "Neural Computation",
    "url": null,
    "github_url": null
  }
]
```

## Workflow guidance

1. **Confirm `OPENAI_API_KEY` is set** before running `check` or `extract`.
   Recommend setting `OPENALEX_MAILTO` to your email for the polite pool.

2. **For a PDF the user provides**, run `ref-checker check paper.pdf` and
   show the user the stdout output.

3. **For a list of references the user pastes**, construct a `--refs-json`
   file using the schema above and run `ref-checker check --refs-json`.
   You do not need `OPENAI_API_KEY` for this path.

4. **Triage results**: OK references need no action. CLOSEST references
   warrant a closer look (wrong edition, pre-print vs. published, minor title
   variation). NO MATCH references may be fabricated, retracted, or simply
   unfound — investigate further.

5. **For incomplete runs** (Semantic Scholar exhausted, network errors), re-run
   the same command — resume picks up where it left off. Use `--retry-all` or
   `--retry-closest` to broaden what gets re-queried.
