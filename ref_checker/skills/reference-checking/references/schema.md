# Reference JSON schema

This file is the single source of truth for the `ref-checker` reference JSON
schema. It is used in two ways:

- **Agent / human use**: when constructing a file for `ref-checker check
  --refs-json`, follow the field definitions and examples below.
- **LLM extraction**: when extracting references from PDF text, return a JSON
  object with a single key `"references"` whose value is an array of objects
  conforming to this schema.

## Top-level structure

Supply (or return) a bare JSON array of reference objects:

```json
[
  { ... },
  { ... }
]
```

When used as a `--refs-json` file, the array is the entire file contents.
When returned by the LLM, wrap it as `{"references": [ ... ]}`.

## Fields

Each reference object may have the following fields. Use `null` for any field
that is not present. All fields except `title` are optional, but including them
improves lookup accuracy.

| Field | Type | Notes |
|---|---|---|
| `index` | integer | 1-based position in the reference list. If omitted, auto-assigned from the entry's position in the list. Duplicate explicit indices are rejected. |
| `raw` | string | Full reference text exactly as it appears in the source, excluding any leading citation label such as `[1]`, `1.`, or `(1)`. Used for display only. |
| `title` | string \| null | **Primary lookup key.** Include whenever possible. |
| `authors` | array of strings | Each entry is one author's full name in natural order, e.g. `"Bernhard Scholkopf"`. Use `[]` for corporate or organizational authors (e.g. `"IBM"`, `"scikit-learn developers"`) or when no authors are present. |
| `year` | integer \| null | Publication year as a four-digit integer. Used in year-mismatch scoring. |
| `doi` | string \| null | Canonical DOI only — e.g. `"10.1145/1234.5678"`. Strip any `doi:`, `https://doi.org/`, or `http://dx.doi.org/` prefix. If a DOI appears in any form in the source text, extract the canonical form here. |
| `arxiv_id` | string \| null | Bare arXiv ID only — e.g. `"2301.01234"`. Strip any `arXiv:` prefix or version suffix such as `v2`. If an arXiv identifier appears as `arXiv:2301.01234`, `arXiv:2301.01234v2`, or in a URL like `arxiv.org/abs/2301.01234`, extract just the bare ID. |
| `venue` | string \| null | Journal, conference, or publisher name. |
| `url` | string \| null | Any non-DOI, non-arXiv URL present in the reference — e.g. a GitHub repository, project page, or institutional landing page. Space-separate multiple URLs. Do not put `doi.org` or `arxiv.org` URLs here; those belong in `doi` / `arxiv_id`. |
| `github_url` | string \| null | Derived automatically from `url` by the tool. You do not need to set this field separately. |

## Extraction rules (for LLM use)

1. Do not include citation labels, numbers, or bracketed keys in any field,
   including `raw`.
2. If the reference has a corporate or organizational author, set `authors`
   to `[]`.
3. If a DOI appears in any form — bare, prefixed with `doi:`, or as a URL
   (`https://doi.org/…`, `http://dx.doi.org/…`) — extract the canonical DOI
   string into the `doi` field.
4. If an arXiv identifier appears in any form — e.g. `arXiv:2301.01234`,
   `arXiv:2301.01234v2`, or `arxiv.org/abs/2301.01234` — extract just the
   bare ID (no prefix, no version) into the `arxiv_id` field.
5. If the reference contains a GitHub URL or other project/software URL that
   is not a DOI or arXiv link, extract it into the `url` field.
6. Skip any text that is not a bibliographic reference — tool names, section
   headers, acknowledgements, footnotes, etc.

## Minimal example

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

## Full example with all fields

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
