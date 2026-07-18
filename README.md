# ref-checker

Verify bibliographic references in an academic paper against live scholarly
databases. `ref-checker` accepts two equivalent inputs — a **PDF** (references
extracted via LLM) or a **JSON list** you supply directly — and looks each
reference up across **OpenAlex**, **CrossRef**, **OSTI**, **DBLP**,
**Semantic Scholar**, and **arXiv** in sequence, performing URL liveness
checks for software repositories and web resources. Results are printed in
citation order with color-coded status. It also ships a bundled
[Agent Skill](https://opencode.ai/docs/skills/) for AI coding assistants.

For a detailed description of the design and implementation, see [PLAN.md](PLAN.md).

## Install

The easiest way to install `ref-checker` as a CLI tool is directly from GitHub using [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/rbross-hpc/ref-checker.git
```

This installs `ref-checker` into its own isolated virtual environment and puts the command on your `PATH`. To upgrade later:

```bash
pipx upgrade ref-checker
```

To install a specific tagged version or commit:

```bash
pipx install git+https://github.com/rbross-hpc/ref-checker.git@v0.2.0
```

### Other install methods

```bash
# From a local clone (isolated)
pipx install .

# Development / editable (includes pytest)
pip install -e ".[dev]"
pytest tests/
```

Python 3.10 or later required.

## Environment variables

Set these before running:

| Variable | Required | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | **Yes, for PDF input** | Key for the LLM used to extract references from a PDF. Not needed when using `--refs-json`. |
| `OPENAI_BASE_URL` | No | Override the OpenAI-compatible base URL (e.g. for a local proxy) |
| `OPENAI_MODEL` | No | Model name to use (default: `gpt-4o-mini`) |
| `OPENALEX_MAILTO` | Recommended | Your email — enables the OpenAlex/CrossRef polite pool for faster, more reliable API access |
| `SEMANTICSCHOLAR_API_KEY` | Recommended | Semantic Scholar API key — without one, unauthenticated requests are aggressively rate-limited. Register free at semanticscholar.org/product/api |

Sensitive values are displayed as `<set>` in the credential summary at startup.

## Usage

### Check references from a PDF

```bash
ref-checker check paper.pdf
```

Extracts references via LLM (requires `OPENAI_API_KEY`), then looks up each
one against all sources. Writes a refs cache (`paper.refs.json`) and a results
sidecar (`paper.results.json`) next to the PDF on first run.

### Check references from a JSON list

```bash
ref-checker check --refs-json refs.json
ref-checker check --refs-json refs.json --results-json out.json
```

Looks up references directly from a JSON file — no PDF and no `OPENAI_API_KEY`
required. The refs JSON must be a bare JSON array of ref dicts, each with at
minimum a `title` field. All other fields (`index`, `raw`, `authors`, `year`,
`doi`, `arxiv_id`, `venue`, `url`) are optional. `index` defaults to 0 and is
auto-sequenced from the list position. The result sidecar defaults to
`<refs-stem>.results.json`.

Options:

```
--refs-json PATH          Load references from a JSON array; skip PDF extraction.
                          PDF argument becomes optional; a warning is printed if supplied.
--refs-cache PATH         Refs cache file (default: <pdf-stem>.refs.json next to PDF)
--no-refs-cache           Disable refs cache — always extract, never write
--re-extract              Force re-extraction even if refs cache is valid
--tail-pages N            Pages from end to use as fallback if no References
                          heading is found (default: 5)
--min-match F             Minimum similarity to report as CLOSEST (default: 0.80)
--delay-openalex S        Seconds between OpenAlex calls (default: 2.0)
--delay-crossref S        Seconds between CrossRef calls (default: 2.0)
--delay-osti S            Seconds between OSTI calls (default: 2.0)
--delay-dblp S            Seconds between DBLP calls (default: 1.0)
--delay-semanticscholar S Seconds between Semantic Scholar calls (default: 8.0)
--delay-arxiv S           Seconds between arXiv calls (default: 3.0)
--delay-github S          Seconds between GitHub liveness checks (default: 1.0)
--delay-url S             Seconds between generic URL liveness checks (default: 1.0)
--results-json PATH       Sidecar file path (default: <pdf-stem>.results.json)
--no-results-json         Disable sidecar entirely
--no-resume               Disable resume — re-query every ref regardless of sidecar
--retry-all               Re-query every ref even if sidecar marks it done
--retry-closest           Also re-query refs previously reported as CLOSEST
--with-osti-id            Append '(OSTI: <id>)' to each status line when OSTI
                          returned a confident hit (DOI match or title
                          similarity >= 0.90 after any year penalty)
-j, --jobs N              Number of references to query in parallel (default: 3).
                          Per-source polite-pool spacing is preserved regardless
                          of N via strict reservation. Use --jobs 1 for strictly
                          sequential execution.
```

### Parallelism

`ref-checker` queries multiple references concurrently via a thread pool
(default 3 workers, tune with `-j N` / `--jobs N`). Per-source polite-pool
spacing is preserved via a strict reservation-style rate limiter — three
concurrent workers hitting OpenAlex still see calls spaced exactly the
per-source delay apart. Progress and warnings stream to stderr live; the
formatted result report is buffered and emitted to stdout in reference-index
order at end-of-run, so `> results.txt` produces a clean deterministic report
regardless of completion order. Use `--jobs 1` for strictly sequential
execution (bit-for-bit reproducible ordering of side effects).

### Cached reference extraction

Every `check` run on a PDF automatically writes `<pdf-stem>.refs.json` next to
the PDF after LLM extraction. On re-run, the cache is loaded instead of calling
the LLM again — saving ~30–120 seconds per paper:

```bash
ref-checker check paper.pdf          # extracts + writes cache
ref-checker check paper.pdf          # loads cache, skips LLM
ref-checker check paper.pdf --re-extract   # forces re-extraction
```

The cache is validated against the PDF's SHA-256 hash. If the PDF changes, the
cache is automatically invalidated and re-extracted. The `extract` subcommand
writes the same format, so running `extract` first primes the cache for `check`.
The resulting `<stem>.refs.json` can also be passed directly to
`--refs-json` for editing or re-checking.

### Resuming interrupted or incomplete runs

Every `check` run writes a results sidecar file (`<pdf-stem>.results.json` for
PDF input; `<refs-stem>.results.json` for JSON input). Resume is **on by
default** — re-runs automatically skip references that already resolved cleanly
and retry only those that failed (NO MATCH, exhausted sources, dead URLs):

```bash
ref-checker check paper.pdf          # first run: queries all, writes sidecar
ref-checker check paper.pdf          # re-run: skips OK refs, retries failures
ref-checker check paper.pdf --no-resume   # force re-query everything
```

The sidecar is written atomically after each reference, so a Ctrl-C or network
failure mid-run leaves the sidecar in a valid state. On re-run, completed refs
are replayed from the sidecar instantly and failed refs are re-queried.

If the reference list changes (e.g., after `--re-extract`), the sidecar detects
the mismatch via a content hash and falls back to a full re-run.

### Extract references from a PDF only

Writes `<stem>.refs.md` (numbered raw text) and `<stem>.refs.json` (structured
data) alongside the PDF, or to `--out-dir`. The JSON output can be passed
directly to `--refs-json` for subsequent runs.

```bash
ref-checker extract paper.pdf
ref-checker extract paper.pdf --out-dir ./refs
```

### Re-emit results from a saved sidecar

After a `check` run (interrupted or not), the sidecar contains everything
needed to re-print the per-reference output without re-querying the network.

```bash
ref-checker show paper.results.json
```

`show` also accepts a bare `--refs-json` file (a list of reference objects);
in that mode every reference is displayed with a `NOT YET PROCESSED`
placeholder, useful for previewing what a run will consume before you start it.

```bash
ref-checker show paper.refs.json
```

The `check` command now prints a hint at the end of each run showing the
exact `ref-checker show` invocation you can use to re-emit its output.

### Query a single source

```bash
ref-checker lookup openalex --doi 10.1145/...
ref-checker lookup openalex --arxiv-id 1706.03762
ref-checker lookup openalex --title "Attention is all you need"

ref-checker lookup crossref --doi 10.1145/...
ref-checker lookup crossref --title "Attention is all you need"

ref-checker lookup osti --doi 10.2172/1234567
ref-checker lookup osti --title "Exascale Computing Project Final Report"

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
    OK (0.99)  doi:10.48550/arxiv.1706.03762  [source: openalex]

[1a] Smith et al., "Exascale Computing Project Final Report", 2024, (Argonne)   # with --with-osti-id
    OK (1.00)  doi:10.2172/1234567  [source: osti]  (OSTI: 1234567)

[2] Smith, "A Good Match", 2019, (Journal of Things)
    OK (0.93)  doi:10.1000/xyz123  [source: crossref]
    Note: year mismatch (ref year=2019, match year=2020)

[3] Smith, "A Somewhat Related Paper", 2019, (Journal of Things)
    CLOSEST (0.87)  [source: crossref]
        Closest candidate across services:
        Smith, "A Somewhat Related Paper on the Same Topic", 2020, (Journal of Things)
        https://doi.org/10.1000/xyz123
    Note: year mismatch (ref year=2019, match year=2020)

[4] Wu, "ytopt code", 2024
    OK (----)  https://github.com/ytopt-team/ytopt  [source: github]
    Note: URL liveness check only — no bibliographic record found

[5] Bar et al., "Nonexistent Paper", 2020, (Some Conference)
    NO MATCH (0.34)  [source: openalex]
        Closest candidate across services:
        Foo et al., "A Different Paper", 2019, (Some Conference)
        https://doi.org/10.1234/fake.5678
    Note: retries exhausted for semanticscholar — results may be incomplete
```

The number in parentheses is always **title similarity** (0.00–1.00), with a 0.10 year-mismatch
penalty applied on title-search hits. For identifier-confirmed hits (DOI/arXiv) the number is
the raw title ratio with no penalty — the identifier is proof; year disagreement appears as a Note.
For GitHub/URL liveness checks the number is `----` (no titles to compare).

Status indicators (color-coded when stdout is a TTY):

- **OK** (green) — identifier confirmed (DOI/arXiv lookup), strong title match (≥ 0.90), or live URL.
- **CLOSEST** (orange) — best title similarity (after any year penalty) is ≥ 0.80 and < 0.90;
  shows the closest candidate with citation and URL.
- **NO MATCH** (red) — best similarity is below 0.80; shows the closest candidate found (if any).

Additional note lines (orange):

- `Note: year mismatch (ref year=X, match year=Y)` — on all tiers when years differ.
- `Note: DOI title: "..."` — on ID-confirmed hits when the DOI-retrieved title diverges
  significantly (title sim < 0.85) from the reference title.
- `Note: URL liveness check only — no bibliographic record found` — on GitHub/URL liveness hits.
- `Note: retries exhausted for <source> — results may be incomplete` — when a source hit its
  retry limit (commonly Semantic Scholar when unauthenticated).
- `DOI not found in any source: <doi>` — DOI present in reference but not found anywhere.
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

1. **Input** — references come from one of two equivalent sources:
   - **PDF**: text is extracted with `pypdf` (falling back to `pdfplumber`).
     Common PDF artifacts are repaired: hyphenated line breaks
     (e.g. `frame-\nwork` → `framework`, protecting URL hyphens) and split-word
     glyphs (e.g. `V ector` → `Vector`). The references section is located by
     heading detection; appendices and acknowledgements following the references
     are trimmed before sending to the LLM.
   - **JSON list**: a bare JSON array supplied via `--refs-json`, bypassing
     extraction entirely.

2. **LLM parsing** (PDF path only) — the narrowed text is sent to an
   OpenAI-compatible model which returns structured JSON (`title`, `authors`,
   `year`, `doi`, `arxiv_id`, `venue`, `url`) for each reference. The prompt
   explains that the input is PDF-extracted text and may contain artifacts. Up
   to 3 retries on failure. A regex post-pass backfills any DOIs, arXiv IDs,
   GitHub URLs, and general URLs the LLM missed.

3. **Multi-source lookup** — sources are tried in priority order:
   - GitHub liveness first (if a GitHub URL is present — skips scholarly
     lookups entirely for software/dataset references)
   - arXiv by ID (if an arXiv ID is present)
   - OpenAlex → CrossRef → OSTI → DBLP → Semantic Scholar → arXiv (title search
     skipped when a prior source already returned ≥ 0.90 similarity)
   - Generic URL liveness as a last resort (for web-only references)
   - Lookups stop as soon as a perfect (1.0) match is found.

4. **Similarity scoring** — title candidates are ranked by Unicode-normalized
   SequenceMatcher ratio. A penalty of 0.10 is applied when the reference year
   and candidate year are both known and differ.

## Agent Skills

`ref-checker` ships a bundled [Agent Skill](https://opencode.ai/docs/skills/)
that teaches AI coding assistants how to use the tool. Because the skill is
distributed with the Python package, the skill version is always guaranteed to
match the installed CLI.

### Inspect the bundled skill

```bash
ref-checker skill show
```

Prints the full `SKILL.md` to stdout. You can redirect it:

```bash
ref-checker skill show > SKILL.md
```

### Export the skill for your harness

```bash
ref-checker skill export .agents/skills/reference-checking
```

Copies the complete skill directory (including any supporting files) to the
path you choose. The destination must not exist or must be empty; use
`--force` to overwrite:

```bash
ref-checker skill export --force .agents/skills/reference-checking
```

Common harness locations:

| Harness | Path |
|---|---|
| OpenCode | `.opencode/skills/reference-checking/` |
| Claude Code | `.claude/skills/reference-checking/` |
| Generic | `.agents/skills/reference-checking/` |

### Contributing to the bundled skill

The reference JSON schema has a **single source of truth**:
`ref_checker/skills/reference-checking/references/schema.md`. Both the LLM
extraction prompt in `ref_checker/extract.py` (loaded via `importlib.resources`
at import time) and the `SKILL.md` agent instructions point to this file. To
add or change a schema field, edit only `schema.md`; the LLM prompt picks up
the change automatically on the next import.

The tests in `tests/test_skill_cli.py` assert that specific strings are present
in `SKILL.md` — including the section heading `"Reference JSON schema"` and the
status labels `"OK"`, `"CLOSEST"`, `"NO MATCH"`. If you rename these sections,
update the corresponding test assertions. `tests/test_schema_prompt.py` asserts
that all expected field names appear in the assembled LLM prompt.

The `SKILL.md` frontmatter (`name`, `description`) is required by the
[OpenCode Agent Skills spec](https://opencode.ai/docs/skills/). Do not remove
it.

## Testing

Run the offline test suite (no network, no LLM calls):

```bash
pytest tests/
```

Reference-JSON fixtures live under `tests/fixtures/refs/`. Five of them were
extracted from redistributable OSTI-hosted papers (CC-BY 4.0 or U.S. Federal
public domain) sourced from the sibling
[pub-analysis](https://github.com/rbross-hpc/pub-analysis) repository; two
are hand-crafted (`edge_cases.json`, `mixed_small.json`) covering explicit
lookup-mode combinations. See [tests/fixtures/README.md](tests/fixtures/README.md)
for per-fixture provenance and (rarely-needed) regeneration instructions.

## License

BSD 3-Clause. Copyright (c) 2026, UChicago Argonne, LLC, Argonne National Laboratory.
