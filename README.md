# ref-checker

Verify the references in an academic paper against live bibliographic databases.
Given a PDF, `ref-checker` extracts every reference (via an LLM), then looks
each one up across **OpenAlex**, **CrossRef**, **Semantic Scholar**, and **arXiv**
in sequence, and performs URL liveness checks for software repositories and web
resources. Results are printed in citation order with color-coded status.

For a detailed description of the design and implementation, see [PLAN.md](PLAN.md).

## Install

```bash
# Recommended: isolated CLI install
pipx install .

# Development / editable
pip install -e .
```

Python 3.10 or later required.

## Environment variables

Set these before running:

| Variable | Required | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | **Yes** (for `check`/`extract`) | Key for the LLM used to extract references from the PDF |
| `OPENAI_BASE_URL` | No | Override the OpenAI-compatible base URL (e.g. for a local proxy) |
| `OPENAI_MODEL` | No | Model name to use (default: `gpt-4o-mini`) |
| `OPENALEX_MAILTO` | Recommended | Your email — enables the OpenAlex/CrossRef polite pool for faster, more reliable API access |
| `SEMANTICSCHOLAR_API_KEY` | Recommended | Semantic Scholar API key — without one, unauthenticated requests are aggressively rate-limited. Register free at semanticscholar.org/product/api |

Sensitive values are displayed as `<set>` in the credential summary at startup.

## Usage

### Check all references in a paper

```bash
ref-checker check paper.pdf
```

Skip the extraction step if you already have a refs JSON file:

```bash
ref-checker check paper.pdf --refs-json paper.refs.json
```

Options:

```
--refs-json PATH          Load references from a previously extracted JSON file
--tail-pages N            Pages from end to use as fallback if no References
                          heading is found (default: 5)
--min-match F             Minimum similarity to report as CLOSEST (default: 0.80)
--delay-openalex S        Seconds between OpenAlex calls (default: 2.0)
--delay-crossref S        Seconds between CrossRef calls (default: 2.0)
--delay-semanticscholar S Seconds between Semantic Scholar calls (default: 5.0)
--delay-arxiv S           Seconds between arXiv calls (default: 3.0)
--delay-github S          Seconds between GitHub liveness checks (default: 1.0)
--delay-url S             Seconds between generic URL liveness checks (default: 1.0)
```

### Extract references only

Writes `<stem>.refs.md` (numbered raw text) and `<stem>.refs.json` (structured
data) alongside the PDF, or to `--out-dir`.

```bash
ref-checker extract paper.pdf
ref-checker extract paper.pdf --out-dir ./refs
```

### Query a single source

```bash
ref-checker lookup openalex --doi 10.1145/...
ref-checker lookup openalex --arxiv-id 1706.03762
ref-checker lookup openalex --title "Attention is all you need"

ref-checker lookup crossref --doi 10.1145/...
ref-checker lookup crossref --title "Attention is all you need"

ref-checker lookup semanticscholar --doi 10.1145/...
ref-checker lookup semanticscholar --arxiv-id 1706.03762
ref-checker lookup semanticscholar --title "Attention is all you need"

ref-checker lookup arxiv --id 1706.03762
ref-checker lookup arxiv --doi 10.48550/arXiv.1706.03762
ref-checker lookup arxiv --title "Attention is all you need"
```

Each lookup prints a JSON object to stdout:

```json
{
  "summary": {
    "source": "openalex",
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "year": 2017,
    "venue": "Neural Information Processing Systems",
    "doi": "10.48550/arxiv.1706.03762",
    "url": "https://arxiv.org/abs/1706.03762",
    "external_id": "W2963403868"
  },
  "similarity": 1.0,
  "source": "openalex"
}
```

`python -m ref_checker <subcommand>` works as an alternative to the installed
`ref-checker` command.

## Output format

```
[1] Vaswani et al., "Attention Is All You Need", 2017, (Neural Information Processing Systems)
    OK  doi:10.48550/arxiv.1706.03762  [source: openalex]

[2] Smith, "A Somewhat Related Paper", 2019, (Journal of Things)
    CLOSEST (similarity 0.87)  [source: crossref]
        Closest candidate across services:
        Smith, "A Somewhat Related Paper on the Same Topic", 2020, (Journal of Things)
        https://doi.org/10.1000/xyz123
    Note: year mismatch (ref year=2019, match year=2020)

[3] Wu, "ytopt code", 2024
    OK  https://github.com/ytopt-team/ytopt  [source: github]
    Note: URL liveness check only — no bibliographic record found

[4] Bar et al., "Nonexistent Paper", 2020, (Some Conference)
    NO MATCH (similarity 0.34)  [source: openalex]
        Closest candidate across services:
        Foo et al., "A Different Paper", 2019, (Some Conference)
        https://doi.org/10.1234/fake.5678
    Note: retries exhausted for semanticscholar — results may be incomplete
```

Status indicators (color-coded when stdout is a TTY):

- **OK** (green) — exact identifier match (DOI or arXiv ID), or confirmed-live
  GitHub/web URL. Always shows `[source: name]`.
- **CLOSEST** (orange) — best title-search similarity is ≥ 0.80; shows the
  closest candidate with citation and URL.
- **NO MATCH** (red) — best similarity is below 0.80; shows the closest
  candidate found (if any) for manual inspection.

Additional note lines (orange):

- `Note: year mismatch (ref year=X, match year=Y)` — on CLOSEST results.
- `Note: title similarity X.XX — DOI title: "..."` — on OK results when the
  DOI-retrieved title diverges from the reference title.
- `Note: URL liveness check only — no bibliographic record found` — on
  GitHub/URL liveness OK results.
- `Note: retries exhausted for <source> — results may be incomplete` — when
  a source hit its retry limit for this reference (commonly Semantic Scholar
  when unauthenticated).
- `DOI not found in any source: <doi>` — DOI present in reference but not
  found anywhere.
- `URL check failed (HTTP NNN): <url>` — confirmed-dead URL.

Progress and credential status go to stderr; the reference report goes to
stdout, so you can redirect cleanly:

```bash
ref-checker check paper.pdf > results.txt
```

At the end of a run, a query summary is printed to stderr:

```
[ref-checker] Query summary:
[ref-checker]   arxiv                  8 queries, 4 retries
[ref-checker]   crossref               9 queries
[ref-checker]   openalex              35 queries
[ref-checker]   semanticscholar        6 queries, 11 retries, 4 exhausted
[ref-checker]   url                    5 queries
```

## How it works

1. **PDF extraction** — text is extracted with `pypdf` (falling back to
   `pdfplumber`). Common PDF artifacts are repaired: hyphenated line breaks
   (e.g. `frame-\nwork` → `framework`, protecting URL hyphens) and split-word
   glyphs (e.g. `V ector` → `Vector`). The references section is located by
   heading detection; appendices and acknowledgements following the references
   are trimmed before sending to the LLM.

2. **LLM parsing** — the narrowed text is sent to an OpenAI-compatible model
   which returns structured JSON (`title`, `authors`, `year`, `doi`,
   `arxiv_id`, `venue`, `url`) for each reference. The prompt explains that
   the input is PDF-extracted text and may contain artifacts. Up to 3 retries
   on failure. A regex post-pass backfills any DOIs, arXiv IDs, GitHub URLs,
   and general URLs the LLM missed.

3. **Multi-source lookup** — sources are tried in priority order:
   - GitHub liveness first (if a GitHub URL is present — skips scholarly
     lookups entirely for software/dataset references)
   - arXiv by ID (if an arXiv ID is present)
   - OpenAlex → CrossRef → Semantic Scholar → arXiv (title search skipped
     when a prior source already returned ≥ 0.90 similarity)
   - Generic URL liveness as a last resort (for web-only references)
   - Lookups stop as soon as a perfect (1.0) match is found.

4. **Similarity scoring** — title candidates are ranked by Unicode-normalized
   SequenceMatcher ratio. A penalty of 0.10 is applied when the reference year
   and candidate year are both known and differ.

## License

BSD 3-Clause. Copyright (c) 2026, UChicago Argonne, LLC, Argonne National Laboratory.
