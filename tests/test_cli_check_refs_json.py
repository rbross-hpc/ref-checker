"""Tests for check --refs-json without a PDF argument."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ref_checker.cli.main import run_check
from ref_checker.extract import Reference


def _make_refs_json(tmp_path: Path, refs: list[dict] | None = None) -> Path:
    if refs is None:
        refs = [
            {"index": 1, "raw": "Smith et al., 2020", "title": "A Paper",
             "authors": ["Smith"], "year": 2020, "doi": "10.1000/abc"},
            {"index": 2, "raw": "Jones, 2019", "title": "B Paper",
             "authors": ["Jones"], "year": 2019},
        ]
    p = tmp_path / "paper.refs.json"
    p.write_text(json.dumps(refs), encoding="utf-8")
    return p


def _make_args(refs_json=None, pdf=None, results_json=None, no_results_json=False):
    args = MagicMock()
    args.refs_json = str(refs_json) if refs_json else None
    args.pdf = str(pdf) if pdf else None
    args.results_json = str(results_json) if results_json else None
    args.no_results_json = no_results_json
    args.no_resume = True
    args.retry_all = False
    args.retry_closest = False
    args.min_match = 0.80
    args.delay_openalex = 2.0
    args.delay_crossref = 2.0
    args.delay_dblp = 1.0
    args.delay_semanticscholar = 8.0
    args.delay_arxiv = 3.0
    args.delay_github = 1.0
    args.delay_url = 1.0
    return args


@patch("ref_checker.cli.main.check.check_references")
def test_refs_json_no_pdf_runs(mock_check, tmp_path):
    refs_path = _make_refs_json(tmp_path)
    args = _make_args(refs_json=refs_path, no_results_json=True)

    run_check(args)

    mock_check.assert_called_once()
    refs_arg = mock_check.call_args[0][0]
    assert len(refs_arg) == 2
    assert isinstance(refs_arg[0], Reference)
    assert refs_arg[0].title == "A Paper"
    assert refs_arg[1].title == "B Paper"


@patch("ref_checker.cli.main.check.check_references")
def test_refs_json_no_pdf_sidecar_defaults_to_refs_stem(mock_check, tmp_path):
    refs_path = _make_refs_json(tmp_path)
    args = _make_args(refs_json=refs_path)

    run_check(args)

    kwargs = mock_check.call_args[1]
    expected_sidecar = refs_path.parent / f"{refs_path.stem}.results.json"
    assert kwargs["sidecar"] == expected_sidecar


@patch("ref_checker.cli.main.check.check_references")
def test_refs_json_with_pdf_warns_and_ignores_pdf(mock_check, tmp_path, capsys):
    refs_path = _make_refs_json(tmp_path)
    args = _make_args(refs_json=refs_path, pdf=tmp_path / "ignored.pdf", no_results_json=True)

    run_check(args)

    captured = capsys.readouterr()
    assert "ignored" in captured.err.lower() or "warning" in captured.err.lower()
    mock_check.assert_called_once()


@patch("ref_checker.cli.main.check.check_references")
def test_refs_json_explicit_results_json(mock_check, tmp_path):
    refs_path = _make_refs_json(tmp_path)
    out_path = tmp_path / "custom.results.json"
    args = _make_args(refs_json=refs_path, results_json=out_path)

    run_check(args)

    kwargs = mock_check.call_args[1]
    assert kwargs["sidecar"] == out_path


def test_no_pdf_no_refs_json_exits(tmp_path, capsys):
    args = _make_args()

    with pytest.raises(SystemExit) as exc_info:
        run_check(args)

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "pdf" in captured.err.lower() or "required" in captured.err.lower()


def test_refs_json_missing_file_exits(tmp_path, capsys):
    args = _make_args(refs_json=tmp_path / "nonexistent.json")

    with pytest.raises(SystemExit) as exc_info:
        run_check(args)

    assert exc_info.value.code != 0


@patch("ref_checker.cli.main.check.check_references")
def test_refs_json_missing_indices_auto_assigned_1_based(mock_check, tmp_path):
    refs = [
        {"title": "First Paper"},
        {"title": "Second Paper"},
        {"title": "Third Paper"},
    ]
    refs_path = _make_refs_json(tmp_path, refs=refs)
    args = _make_args(refs_json=refs_path, no_results_json=True)

    run_check(args)

    refs_arg = mock_check.call_args[0][0]
    assert [r.index for r in refs_arg] == [1, 2, 3]


def test_refs_json_duplicate_indices_rejected(tmp_path, capsys):
    refs = [
        {"index": 1, "title": "A"},
        {"index": 1, "title": "B"},
    ]
    refs_path = _make_refs_json(tmp_path, refs=refs)
    args = _make_args(refs_json=refs_path, no_results_json=True)

    with pytest.raises(SystemExit) as exc_info:
        run_check(args)

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "duplicate" in captured.err.lower()


def test_check_and_show_assign_identical_missing_indices(tmp_path, capsys):
    from ref_checker.cli.show import _show_refs_list

    refs = [
        {"title": "First Paper"},
        {"title": "Second Paper"},
        {"title": "Third Paper"},
    ]
    refs_path = _make_refs_json(tmp_path, refs=refs)

    with patch("ref_checker.cli.main.check.check_references") as mock_check:
        args = _make_args(refs_json=refs_path, no_results_json=True)
        run_check(args)
    check_indices = [r.index for r in mock_check.call_args[0][0]]

    _show_refs_list(json.loads(refs_path.read_text()))
    show_output = capsys.readouterr().out
    show_indices = [
        int(line.split("]")[0].lstrip("["))
        for line in show_output.splitlines()
        if line.startswith("[")
    ]

    assert check_indices == show_indices == [1, 2, 3]
