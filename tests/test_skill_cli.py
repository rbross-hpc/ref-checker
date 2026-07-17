"""Tests for the skill show and skill export subcommands."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ref_checker.cli.skill import run_skill


def _make_args_show():
    args = MagicMock()
    args.skill_action = "show"
    return args


def _make_args_export(path, force=False):
    args = MagicMock()
    args.skill_action = "export"
    args.path = str(path)
    args.force = force
    return args


def test_show_prints_markdown(capsys):
    run_skill(_make_args_show())
    out = capsys.readouterr().out
    assert out.startswith("---\n")
    assert "# " in out
    assert "ref-checker" in out
    assert len(out) > 100


def test_show_has_valid_frontmatter(capsys):
    run_skill(_make_args_show())
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    frontmatter = "\n".join(lines[1:end])
    assert "name: reference-checking" in frontmatter
    assert "description:" in frontmatter


def test_show_contains_schema_section(capsys):
    run_skill(_make_args_show())
    out = capsys.readouterr().out
    assert "Reference JSON schema" in out
    assert "`--refs-json`" in out


def test_show_contains_status_codes(capsys):
    run_skill(_make_args_show())
    out = capsys.readouterr().out
    assert "OK" in out
    assert "CLOSEST" in out
    assert "NO MATCH" in out


def test_export_creates_skill_md(tmp_path):
    dest = tmp_path / "reference-checking"
    run_skill(_make_args_export(dest))
    assert (dest / "SKILL.md").exists()


def test_export_skill_md_matches_show(tmp_path, capsys):
    run_skill(_make_args_show())
    shown = capsys.readouterr().out

    dest = tmp_path / "reference-checking"
    run_skill(_make_args_export(dest))
    capsys.readouterr()

    exported = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert exported == shown


def test_export_refuses_non_empty_without_force(tmp_path):
    dest = tmp_path / "reference-checking"
    dest.mkdir()
    (dest / "existing.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        run_skill(_make_args_export(dest, force=False))
    assert exc_info.value.code == 1


def test_export_force_overwrites_non_empty(tmp_path):
    dest = tmp_path / "reference-checking"
    dest.mkdir()
    stale = dest / "stale.txt"
    stale.write_text("stale", encoding="utf-8")

    run_skill(_make_args_export(dest, force=True))

    assert (dest / "SKILL.md").exists()
    assert not stale.exists()


def test_export_creates_parent_dirs(tmp_path):
    dest = tmp_path / "deeply" / "nested" / "reference-checking"
    run_skill(_make_args_export(dest))
    assert (dest / "SKILL.md").exists()


def test_export_to_existing_empty_dir_succeeds(tmp_path):
    dest = tmp_path / "reference-checking"
    dest.mkdir()
    run_skill(_make_args_export(dest, force=False))
    assert (dest / "SKILL.md").exists()
