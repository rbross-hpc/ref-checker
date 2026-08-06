# Reference extraction design

Covers `ref_checker/pdf.py` and `ref_checker/extract.py`. For the user-facing
CLI flags (`--tail-pages`, `--refs-cache`, `--re-extract`, etc.), see
[README.md](../README.md#check-references-from-a-pdf).

## PDF text extraction (`pdf.py`)

- Uses `pypdf` as the primary extractor; falls back to `pdfplumber` if the
  result is empty or very short (< 100 chars).
- Each page is prefixed with an `<!-- page N -->` marker so downstream code
  can locate and count pages.
- `pdfplumber` is **not** used as the primary extractor despite having fewer
  split-word artifacts because it merges multi-column layouts, which badly
  confuses the LLM on two-column papers (the majority of the collection this
  tool was built against).

## Reference extraction (`extract.py`)

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

Before sending to the LLM, two common PDF text extraction artifacts are
repaired:

- **Hyphenated line breaks**: `frame-\nwork` → `framework`. URLs are protected
  first (placeholder substitution) so hyphens inside URL paths are never
  joined.
- **Split-word glyphs**: `Support V ector` → `Support Vector`. The pattern
  `(?<=\w) ([A-Z]) ([a-z]{3,})` requires the lone capital to be preceded by a
  word character, so standalone articles (`A new`, `The system`) are not
  merged.

### LLM extraction

The narrowed text is sent to an OpenAI-compatible LLM with a structured
prompt requesting a JSON object. The prompt:

- Explains that the input is PDF-extracted text and may contain artifacts.
- Requests these fields per reference:
  `index`, `raw`, `title`, `authors`, `year`, `doi`, `arxiv_id`, `venue`, `url`
- Instructs the LLM to set `authors: []` for corporate/organizational authors.
- Instructs the LLM to extract canonical DOI strings (no `doi:` prefix, no
  URL).
- Instructs the LLM to extract bare arXiv IDs (no `arXiv:` prefix, no
  version).
- Instructs the LLM to extract non-DOI/non-arXiv URLs into the `url` field,
  space-separated if multiple.
- Includes two few-shot examples: one with a corporate author and GitHub
  URLs, one with an arXiv preprint and ALL-CAPS author surnames.
- Uses `response_format={"type": "json_object"}` to guarantee valid JSON.
- Retries up to 3 times (2 s → 4 s backoff) on exception, JSON decode error,
  or schema-shape failure. Hard exit if all attempts fail.

The reference JSON schema used in this prompt has a single source of truth —
see [skills-subsystem.md](skills-subsystem.md#schema-single-source-of-truth).

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

## Known limitations

- **Multi-column PDF layouts**: `pdfplumber` handles ligatures and
  split-word artifacts better than `pypdf`, but merges multi-column text —
  so `pypdf` remains primary. See `BACKLOG.md` for possible future work
  (per-file engine selection or a column-layout detection pre-pass).
</content>
