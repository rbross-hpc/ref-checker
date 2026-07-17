"""ref-checker skill subcommand — show and export the bundled Agent Skill."""
from __future__ import annotations

import shutil
import sys
from importlib.resources import as_file, files
from pathlib import Path

_SKILL_PACKAGE = "ref_checker.skills.reference-checking"
_SKILL_DIR_NAME = "reference-checking"


def _skill_files():
    return files("ref_checker").joinpath(f"skills/{_SKILL_DIR_NAME}")


def run_skill(args) -> None:
    action = args.skill_action
    if action == "show":
        _run_show()
    elif action == "export":
        _run_export(args)
    else:
        print(f"Unknown skill action: {action}", file=sys.stderr)
        sys.exit(1)


def _run_show() -> None:
    skill_md = _skill_files().joinpath("SKILL.md")
    with as_file(skill_md) as p:
        print(p.read_text(encoding="utf-8"), end="")


def _run_export(args) -> None:
    dest = Path(args.path).resolve()

    if dest.exists() and any(dest.iterdir()):
        if not args.force:
            print(
                f"Error: destination already exists and is non-empty: {dest}\n"
                "Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
        shutil.rmtree(dest)

    skill_root = _skill_files()
    with as_file(skill_root) as src:
        shutil.copytree(src, dest, dirs_exist_ok=True)

    print(f"[ref-checker] Skill exported to: {dest}", file=sys.stderr)
