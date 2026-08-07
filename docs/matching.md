# Similarity scoring design

Covers `ref_checker/similarity.py`.

A single function `title_ratio(ref_title, cand_title) -> float`:

- Applies NFKD Unicode normalization + combining-character strip (handles
  diacritics like `ö` → `o`, ligatures like `ﬁ` → `fi`).
- Casefolding, punctuation→space, whitespace collapse.
- `SequenceMatcher` ratio on the normalized strings.

Authors are **not** part of the similarity score. Early testing showed that
LLM-extracted first authors were often incorrect (corporate names, section
numbers, ALL-CAPS surnames with diacritics stripped), causing good title
matches to be penalized.

See [lookup-engine.md](lookup-engine.md#year-mismatch-penalty) for how this
score is combined with year information and identifier confirmation to
produce the final per-source score.

## Matching-quality benchmark

`tests/fixtures/matching_benchmark.json` + `tests/test_matching_benchmark.py`
measure false-confirmation / false-rejection / ambiguous rates of the full
title-search scoring path (`title_ratio` → `apply_year_mismatch_penalty` →
classification against `STRONG_MATCH_THRESHOLD` and `min_match`) across 8
categories: exact, abbreviated, OCR damage, wrong years, preprint vs.
published, similar papers by the same authors, generic titles, and
intentionally unresolvable references. See
[../tests/fixtures/README.md](../tests/fixtures/README.md) for corpus
provenance. Any future change to `title_ratio`, `apply_year_mismatch_penalty`,
or the thresholds should be checked against this corpus — `pytest
tests/test_matching_benchmark.py` will fail on any case whose classification
shifts, requiring an explicit decision (fix a regression, or update the
checked-in baseline with justification).

## Known limitations

- **No alias/abbreviation table.** A bare acronym or project name (e.g.
  `"scikit-learn"`) scores far below `min_match` against that project's full
  paper title, even though it identifies the same work — similarity scoring
  alone can't bridge this; see the benchmark's `abbreviated` category (cases
  tagged `KNOWN FALSE REJECT`).
- **No author/venue scoring** (see "Authors are not part of the similarity
  score" above). This is the single biggest source of the benchmark's
  `same_author_similar` and `generic_titles` categories landing in
  "ambiguous" rather than a clean reject: two different papers in a numbered
  series, or two unrelated papers with generically similar phrasing (e.g. "A
  Survey of X Techniques for Y"), can score close enough to `min_match` that
  only author/venue agreement could reliably tell them apart. These are
  checked-in as known, accepted gaps in the benchmark (cases tagged `KNOWN
  ACCEPTED GAP`) rather than fixed here — see `BACKLOG.md`.
- **Preprint retitling.** A paper's title can change materially between
  preprint and published versions (verified real example in the benchmark's
  `preprint_vs_published` category); title-only scoring under-confirms these
  even when the DOI/arXiv-ID-driven identity proof (see
  [lookup-engine.md](lookup-engine.md#scoring-and-id-hit-annotation)) is
  available and handles it correctly at the engine level.
</content>
