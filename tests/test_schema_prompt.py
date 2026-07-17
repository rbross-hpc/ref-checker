"""Tests that the assembled _SYSTEM_PROMPT loads schema.md correctly."""
from __future__ import annotations

from ref_checker.extract import _SCHEMA_MD, _SYSTEM_PROMPT


def test_prompt_is_non_empty():
    assert len(_SYSTEM_PROMPT) > 500


def test_prompt_contains_intro():
    assert "reference-extraction assistant" in _SYSTEM_PROMPT


def test_no_unresolved_placeholder():
    assert "$schema" not in _SYSTEM_PROMPT


def test_prompt_contains_all_field_names():
    for field in ("index", "raw", "title", "authors", "year",
                  "doi", "arxiv_id", "venue", "url"):
        assert field in _SYSTEM_PROMPT, f"field '{field}' missing from prompt"


def test_prompt_contains_few_shot_examples():
    assert "ytopt" in _SYSTEM_PROMPT
    assert "Scholkopf" in _SYSTEM_PROMPT or "SCHOLKOPF" in _SYSTEM_PROMPT


def test_schema_md_is_non_empty():
    assert len(_SCHEMA_MD) > 200


def test_schema_md_contains_field_table():
    for field in ("title", "authors", "doi", "arxiv_id", "venue", "url"):
        assert field in _SCHEMA_MD, f"field '{field}' missing from schema.md"


def test_schema_md_contains_extraction_rules():
    assert "Extraction rules" in _SCHEMA_MD


def test_schema_md_contains_examples():
    assert "Attention Is All You Need" in _SCHEMA_MD
