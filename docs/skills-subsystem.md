# Agent Skills subsystem design

Covers `ref_checker/skills/` and `ref_checker/cli/skill.py`. For user-facing
usage (`ref-checker skill show` / `skill export`), see
[README.md](../README.md#agent-skills).

## Purpose

AI coding assistants can use `ref-checker` to audit references on the user's
behalf. The skill subsystem ships reusable agent instructions alongside the
Python package so that the skill content and the CLI are always versioned
together. An agent that installs from PyPI gets the matching skill; an agent
that upgrades the CLI automatically gets the updated skill.

The alternative — distributing the skill separately via `npx skills add
rbross-hpc/ref-checker` (pulling from GitHub) — was evaluated and rejected
because the skill version and the installed executable can drift
independently.

## Package layout

The canonical skill location is inside the Python package so that
`importlib.resources` can resolve it whether the package is installed
normally or as a zip (wheel):

```
ref_checker/skills/reference-checking/SKILL.md
```

This path is included in the wheel via `pyproject.toml`:

```toml
[tool.setuptools.package-data]
ref_checker = ["skills/reference-checking/**/*"]
```

No `__init__.py` is needed inside `skills/` — access is via
`importlib.resources.files("ref_checker").joinpath("skills/reference-checking/…")`.

## SKILL.md frontmatter

`SKILL.md` begins with YAML frontmatter as required by the [OpenCode Agent
Skills spec](https://opencode.ai/docs/skills/):

```yaml
---
name: reference-checking
description: ...
license: BSD-3-Clause
metadata:
  audience: researchers, editors
  tool: ref-checker
---
```

`name` must match the containing directory name (`reference-checking`). The
`compatibility` field is intentionally omitted — the skill is
harness-neutral and works with any harness that supports the standard
directory layout (`.opencode/skills/`, `.claude/skills/`,
`.agents/skills/`).

## CLI surface (`ref_checker/cli/skill.py`)

Two subcommands are exposed:

| Command | Behaviour |
|---|---|
| `ref-checker skill show` | Reads `SKILL.md` via `importlib.resources` and writes to stdout. |
| `ref-checker skill export PATH [--force]` | Copies the complete skill directory tree to `PATH` using `shutil.copytree(dirs_exist_ok=True)`. Refuses if `PATH` is non-empty unless `--force` is given (which first removes `PATH`). |

`show` is suitable for piping or redirection. `export` is the recommended
installation path — the user chooses the harness-specific destination and
the CLI does not auto-detect or modify any harness configuration.

## Schema single source of truth

The reference JSON schema lives in exactly one file:

```
ref_checker/skills/reference-checking/references/schema.md
```

It is used in two ways simultaneously:

1. **LLM extraction prompt** — loaded at import time in
   `ref_checker/extract.py` via
   `importlib.resources.files("ref_checker").joinpath("skills/…/schema.md").read_text()`,
   then interpolated into the prompt template using `string.Template`. The
   assembled `_SYSTEM_PROMPT` constant contains the full schema text inline.

2. **Agent / human reference** — `SKILL.md` links to `references/schema.md`
   in its §Reference JSON schema section; the file is exported alongside
   `SKILL.md` when the user runs `ref-checker skill export`.

To add or change a schema field, edit only `schema.md`. The LLM prompt picks
up the change automatically on the next import. `tests/test_schema_prompt.py`
asserts that all expected field names appear in the assembled prompt, so a
missing or misspelled field name in `schema.md` will fail the test suite.
</content>
