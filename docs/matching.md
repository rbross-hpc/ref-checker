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

## Known limitations

- No checked-in benchmark corpus yet measures false-confirmation / false-
  rejection rates across categories (abbreviated titles, OCR damage, wrong
  years, preprint vs. published, generic titles, etc.) — see `BACKLOG.md`.
  Any change to the threshold or scoring formula should be validated against
  such a corpus once it exists, rather than by intuition.
</content>
