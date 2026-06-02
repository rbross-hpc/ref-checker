# ref-checker — Design and Implementation Plan

## Overview

`ref-checker` is a Python CLI tool that extracts bibliographic references from
an academic paper PDF and verifies each one against four external sources:
OpenAlex, CrossRef, Semantic Scholar, and arXiv. For references that are
software repositories or web resources (with no scholarly record), it performs
URL liveness checks against GitHub and general web URLs. Results are printed
in citation order with color-coded status indicators.

---

## Package layout

```
ref-checker/
├── pyproject.toml
├── LICENSE                        # BSD 3-Clause, Argonne National Laboratory
├── README.md
├── PLAN.md                        # this file
├── SAMPLE-PLAN.md                 # the original design plan passed to build
└── ref_checker/
    ├── __init__.py
    ├── __main__.py                # python -m ref_checker entry point
    ├── pdf.py                     # PDF → text (pypdf → pdfplumber fallback)
    ├── extract.py                 # heuristic narrowing + LLM extraction
    ├── similarity.py              # Unicode-normalized title ratio
    ├── check.py                   # driver: lookup, rate limiting, formatting
    ├── cli/
    │   ├── __init__.py
    │   └── main.py                # argparse subcommand dispatcher
    └── sources/
        ├── __init__.py
        ├── openalex.py            # primary scholarly source
        ├── crossref.py            # secondary scholarly source
        ├── semanticscholar.py     # tertiary scholarly source
        ├── arxiv.py               # quaternary / preprint source
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

## Step 4 — Multi-source lookup (`ref_checker/check.py`)

### Source priority

For each reference, sources are tried in this order:

1. **GitHub liveness** — if `ref.github_url` is set, HEAD-check immediately.
   GitHub-only references (repos, datasets) are almost never in scholarly
   databases; checking them first saves 12+ seconds of fruitless API calls.
   Short-circuit on success.

2. **arXiv ID lookup** — if `ref.arxiv_id` is set, query arXiv directly
   (exact match, similarity = 1.0). Short-circuit on success.

3. **Scholarly loop** (skipped entirely for url-only refs):
   For each source in order — OpenAlex → CrossRef → Semantic Scholar → arXiv:
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

- Per-source minimum delay between consecutive calls (applied by a monotonic
  timer, so natural processing time counts toward the gap):
  - OpenAlex: 2.0 s
  - CrossRef: 2.0 s
  - Semantic Scholar: 5.0 s
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

### ID-hit annotation

DOI and arXiv-ID lookups return similarity = 1.0. After recording the hit,
the driver computes the title ratio between the reference title and the
candidate title as an informational check:

- If title similarity < 0.85, a `Note: title similarity X.XX — DOI title: "..."` 
  line is shown. This can indicate a DOI collision, a subtitle/edition
  difference, or a LLM extraction error.
- If the years differ, a `Note: year mismatch (ref year=X, match year=Y)` line
  is shown. This is common for preprints (arXiv year vs journal publication
  year) and online-first articles.

---

## Step 5 — Output formatting

Each reference block:

```
[N] Last[ et al.], "Title", Year, (Venue)
    OK  doi:10.x/y  [source: openalex]

[N] Last et al., "Title", Year, (Venue)
    CLOSEST (similarity 0.NN)  [source: crossref]
        Closest candidate across services:
        Last, "Candidate Title", Year, (Venue)
        https://doi.org/10.x/y
    Note: year mismatch (ref year=X, match year=Y)

[N] Last et al., "Title", Year, (Venue)
    NO MATCH (similarity 0.NN)  [source: openalex]
        Closest candidate across services:
        Last, "Nearest Miss Title", Year, (Venue)
        https://...
    Note: retries exhausted for semanticscholar — results may be incomplete
```

### Status meanings

- **OK** (green) — exact identifier match (DOI or arXiv ID lookup returned a
  hit), GitHub URL liveness confirmed live, or generic URL liveness confirmed
  live. Similarity is always 1.0.
- **CLOSEST** (orange) — best title-search similarity is ≥ `--min-match`
  (default 0.80) but not a direct identifier match.
- **NO MATCH** (red) — best similarity across all sources is below
  `--min-match`. The closest candidate found (if any) is shown for reference.

### Color

Colors are applied when stdout is a TTY and `NO_COLOR` is not set. CLOSEST and
all Note lines are orange; OK is green; NO MATCH is red. Redirect to a file
produces plain text.

### Additional note lines

- `Note: year mismatch (ref year=X, match year=Y)` — on CLOSEST results when
  the reference year and candidate year differ.
- `Note: title similarity X.XX — DOI title: "..."` — on OK results when the
  DOI-retrieved title diverges significantly from the reference title.
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

### `ref-checker check PDF [options]`

Full pipeline: extract references then check each one.

```
--refs-json PATH          Load pre-extracted references (skip LLM extraction)
--tail-pages N            Tail-page fallback page count (default: 5)
--min-match F             CLOSEST threshold (default: 0.80)
--delay-openalex S        Per-call delay in seconds (default: 2.0)
--delay-crossref S        (default: 2.0)
--delay-semanticscholar S (default: 5.0)
--delay-arxiv S           (default: 3.0)
--delay-github S          (default: 1.0)
--delay-url S             (default: 1.0)
```

### `ref-checker extract PDF [options]`

Extract references only; write `<stem>.refs.md` and `<stem>.refs.json`.

```
--out-dir DIR    Output directory (default: same as PDF)
--tail-pages N   (default: 5)
```

### `ref-checker lookup <source> (--doi | --arxiv-id | --id | --title) [options]`

Query a single source and print JSON to stdout.

Sources: `openalex`, `crossref`, `semanticscholar`, `arxiv`

```
--doi DOI
--arxiv-id ID    (openalex, semanticscholar)
--id ID          (arxiv)
--title TITLE
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
- **Concurrency**: lookups are strictly sequential. For a 60-reference paper
  with default delays, a full run takes 3–5 minutes. A per-source worker thread
  pool (with shared rate-limiter) would reduce this significantly.
- **Caching**: API responses are not cached. Re-running on the same paper
  repeats all API calls. An optional on-disk cache (keyed by query hash) would
  make iteration much faster.
- **arXiv title search**: uses `ti:"<title>"` which requires a fairly exact
  match. A looser `all:` query would improve recall for titles with PDF
  extraction artifacts that survive the repair pass.

---

## Dependencies

- `requests >= 2.31`
- `pypdf >= 4.0`
- `pdfplumber >= 0.10`
- `openai >= 1.0`
- Python >= 3.10

## License

BSD 3-Clause. Copyright (c) 2026, UChicago Argonne, LLC, Argonne National Laboratory.
