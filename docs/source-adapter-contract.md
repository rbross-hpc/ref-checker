# Source adapter capability contract

Covers `ref_checker/sources/base.py` and the `DEFAULT_DELAY` /
`SUPPORTED_QUERY_KINDS` constants declared by each source module. For the
lookup priority order and rate-limit/retry mechanics that consume this
contract, see [lookup-engine.md](lookup-engine.md).

## Why

Source modules (`openalex.py`, `crossref.py`, `osti.py`, `dblp.py`,
`semanticscholar.py`, `arxiv.py`, `github.py`, `url.py`) are plain modules,
not classes, exposing free functions by convention. Historically the
engine discovered a source's capabilities implicitly —
`getattr(src, fn_name, None) is None` to check whether a source supported
a given query mode — and the same capability facts were re-encoded by
hand in three separate places: `engine.py`'s dispatch, `cli/main.py`'s
`lookup` subcommand parser (which flags to accept per source), and
`cli/main.py`'s `lookup` subcommand dispatch (an `if source == "openalex":
... elif ...` chain). Nothing enforced that these three stayed in sync.
Per-source rate-limit defaults had the same problem: hardcoded identically
in `engine.py`, `runner.py`, and eight `--delay-<source>` argparse
defaults in `cli/main.py`.

## The contract

Every scholarly source module declares two additional module-level
constants (alongside the pre-existing `SOURCE_NAME`):

```python
DEFAULT_DELAY = 2.0
SUPPORTED_QUERY_KINDS = frozenset({QueryKind.DOI, QueryKind.ARXIV_ID, QueryKind.TITLE})
```

Every source module (scholarly and liveness alike) also implements
`build_context() -> SourceContext` and takes a mandatory trailing
`ctx: SourceContext` parameter on every lookup function. See
"Shared `SourceContext`" below.

`SUPPORTED_QUERY_KINDS` declares which of `get_by_doi` / `get_by_arxiv_id`
/ `search_by_title` the module implements a corresponding function for —
not every scholarly source supports every kind (DBLP is title-only;
CrossRef and OSTI have no native arXiv-ID lookup). Liveness sources
(`github`, `url`) declare only `DEFAULT_DELAY` and implement `check_url`
instead.

`ref_checker/sources/base.py` defines two `typing.Protocol` classes,
`ScholarlySource` and `LivenessSource`, documenting this shape. They are
`@runtime_checkable` and used in `tests/test_source_contract.py` to assert
every registered source module actually satisfies the contract — but
`engine.py` still calls functions by their conventional names (`get_by_doi`
etc.), gated by an explicit `if kind not in src.SUPPORTED_QUERY_KINDS`
check, rather than dispatching through the Protocol itself. A `Protocol`
can't cleanly express "this method is present only if that flag is set,"
so the three scholarly lookup functions remain plain, optionally-present
attributes rather than part of the Protocol's required method set.

## Single sources of truth derived from the contract

- `sources/registry.py:DEFAULT_DELAYS` — `{source_name: DEFAULT_DELAY}`,
  derived from every module's own constant. `engine.py`, `runner.py`, and
  `cli/main.py`'s `--delay-<source>` argparse defaults all import this
  instead of maintaining their own copies.
- `sources/base.py:FN_BY_KIND` — `{QueryKind: function_name}` (e.g.
  `QueryKind.DOI -> "get_by_doi"`). Both `engine.py`'s dispatch and
  `cli/main.py`'s `run_lookup` import this instead of each hardcoding
  their own copy of the same mapping.
- `engine.py`'s `call()` helper gates on `kind in src.SUPPORTED_QUERY_KINDS`
  instead of `getattr(src, fn_name, None) is None`.
- `cli/main.py`'s `_build_lookup_parser` derives which of `--doi` /
  `--arxiv-id` (or `--id` for arxiv) / `--title` to register per source
  from `SUPPORTED_QUERY_KINDS`, instead of hardcoded exclusion tuples.
- `cli/main.py`'s `run_lookup` dispatches via a small
  `QueryKind`-preference table (checked against `SUPPORTED_QUERY_KINDS`)
  instead of a per-source `if/elif` chain. Every source prefers
  DOI → arXiv ID → title except `arxiv` itself, which prefers its native
  arXiv ID over DOI (`_KIND_PREFERENCE_OVERRIDES`).

## Regression coverage

`tests/test_source_contract.py` asserts, for every registered source
module:

- It satisfies `ScholarlySource` or `LivenessSource` (structural, via
  `isinstance` against the `runtime_checkable` Protocol).
- Every kind declared in `SUPPORTED_QUERY_KINDS` has a matching function
  present, and conversely every one of the three optional functions that
  exists has its kind declared — guards drift in either direction.
- `registry.DEFAULT_DELAYS[name]` matches the module's own `DEFAULT_DELAY`.

## Shared `SourceContext`

Every source module — scholarly (`openalex`, `crossref`, `osti`, `dblp`,
`semanticscholar`, `arxiv`) and liveness (`github`, `url`) alike —
implements:

```python
def build_context() -> SourceContext: ...
def get_by_doi(doi: str, ctx: SourceContext) -> tuple[dict | None, float | None]: ...   # scholarly
def check_url(urls: str, ctx: SourceContext) -> tuple[dict | None, float | None, list]:  # liveness
```

`SourceContext` (`sources/base.py`) is a small dataclass holding only
`session: requests.Session` and `credentials: dict[str, str]`. It
deliberately does **not** include rate limiting or retry — those stay in
`runtime.py`/`engine.py`, applied *around* the source call, keeping "how to
talk to this source" (the context) separate from "how often/how
resiliently to talk to any source" (the engine).

`sources/registry.py:build_all_contexts()` builds one context per source
(`SCHOLARLY_SOURCES + LIVENESS_SOURCES`) as a plain `dict`. Direct-call
test paths and `cli/main.py`'s single-shot `ref-checker lookup <source>`
subcommand use this (or build one throwaway `build_context()` directly)
since there's only one call to make, with no concurrent worker threads in
the picture.

`runner.py:check_references()` instead builds a
`sources/registry.py:ThreadLocalSourceContexts` **once per run** and
threads it through `engine.py:lookup_reference()` for every reference —
this gives each *worker thread* its own context per source (rather than
one shared globally), while every reference dispatched to a given thread
still reuses that thread's session per source. See
[lookup-engine.md](lookup-engine.md#threading-model-for-sourcecontext) for
why a single context shared across threads is not safe (`requests.Session`
mutates its cookie jar on every request/response, unsynchronized across
threads) and how sessions are closed deterministically at end-of-run.
`engine.py`'s `call()` and `call_liveness()` both use the same
`_ctx_for(src)` lookup regardless of which kind of `contexts` object they
were handed — both a plain `dict` and a `ThreadLocalSourceContexts`
satisfy the same `.get(name)` / `[name] = ...` duck-typed interface.

## Explicitly out of scope

- **Static type checking** (mypy/pyright) is not yet in CI — see
  `BACKLOG.md` — so the Protocols here serve documentation and one runtime
  test, not a static guarantee.
