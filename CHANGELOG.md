# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-08-06

### Added

- CI: GitHub Actions workflow running `ruff check` and `pytest` on Python
  3.10–3.13 for every push and pull request.
- `ruff` added as a dev dependency, with a lint configuration in
  `pyproject.toml`.

### Changed

- Bumped package version from `0.1.0` to `0.2.0` (no tagged release
  previously existed for `0.1.0`; the README's install example already
  referenced `v0.2.0`).

### Fixed

- Lint cleanup: removed unused imports and local variables, renamed
  ambiguous single-letter variable names, removed no-op `f`-string
  prefixes. No behavior change; full test suite (361 tests) remains green.

## [0.1.0] - historical

Everything prior to this changelog's creation. Highlights (see `git log`
for full history):

- Multi-source reference lookup against OpenAlex, CrossRef, OSTI, DBLP,
  Semantic Scholar, and arXiv.
- GitHub and generic URL liveness checking for non-scholarly references.
- PDF extraction pipeline (`pypdf`/`pdfplumber` with LLM-based reference
  extraction) with a content-addressed extraction cache.
- Resumable runs via a versioned JSON results sidecar, with smart
  re-run planning (only retry sources that are missing, disabled, or
  errored).
- Per-source rate limiting, retry/backoff, `Retry-After` handling, and a
  session-scoped circuit breaker for systematically failing sources.
- Bounded thread-pool concurrency (`-j`/`--jobs`) with deterministic,
  citation-ordered reporting regardless of completion order.
- `ref-checker show` subcommand for re-rendering a sidecar or bare refs
  JSON without re-querying any source.
- Bundled Agent Skill (`ref-checker skill`) for reference checking.
