"""Tests for the `ref-checker show` subcommand."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ref_checker import check as check_mod
from ref_checker import sidecar as sidecar_mod
from ref_checker.cli import show as show_mod
from ref_checker.extract import Reference


def _ref(index=1, title="A Paper", year=2020, doi=None, arxiv_id=None,
         venue=None, url=None, github_url=None, raw=None):
    return Reference(
        index=index,
        raw=raw if raw is not None else f"ref-{index}",
        title=title,
        year=year,
        doi=doi,
        arxiv_id=arxiv_id,
        venue=venue,
        url=url,
        github_url=github_url,
    )


def _summary(title="A Paper", year=2020, doi="10.1/x"):
    return {
        "source": "test",
        "title": title,
        "authors": ["A. Author"],
        "year": year,
        "venue": "Venue",
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else None,
        "external_id": doi,
    }


class TestShowSidecar:
    def test_show_returns_zero_on_missing_file(self, tmp_path, capsys):
        rc = show_mod.show(tmp_path / "does_not_exist.json")
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_show_rejects_malformed_json(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("not json at all")
        rc = show_mod.show(p)
        assert rc == 1
        err = capsys.readouterr().err
        assert "failed to parse" in err

    def test_show_rejects_unrecognized_json(self, tmp_path, capsys):
        p = tmp_path / "weird.json"
        p.write_text('{"foo": "bar"}')
        rc = show_mod.show(p)
        assert rc == 1
        err = capsys.readouterr().err
        assert "neither a sidecar" in err

    def test_show_bare_refs_json_prints_placeholder(self, tmp_path, capsys):
        p = tmp_path / "refs.json"
        refs = [
            {"index": 1, "title": "First", "authors": ["A"], "year": 2020,
             "doi": None, "arxiv_id": None, "venue": None, "url": None,
             "github_url": None, "raw": "raw1"},
            {"index": 2, "title": "Second", "authors": ["B"], "year": 2021,
             "doi": None, "arxiv_id": None, "venue": None, "url": None,
             "github_url": None, "raw": "raw2"},
        ]
        p.write_text(json.dumps(refs))

        rc = show_mod.show(p)
        assert rc == 0
        out = capsys.readouterr().out
        assert "NOT YET PROCESSED" in out
        # Both refs shown.
        assert "[1]" in out and "[2]" in out
        assert "First" in out and "Second" in out
        # Both marked unprocessed.
        assert out.count("NOT YET PROCESSED") == 2

    def test_show_bare_refs_json_assigns_missing_index(self, tmp_path, capsys):
        p = tmp_path / "refs.json"
        refs = [
            {"title": "First", "authors": [], "year": 2020, "raw": "raw1"},
            {"title": "Second", "authors": [], "year": 2021, "raw": "raw2"},
        ]
        p.write_text(json.dumps(refs))
        rc = show_mod.show(p)
        assert rc == 0
        out = capsys.readouterr().out
        assert "[1]" in out and "[2]" in out

    def test_show_sidecar_reprints_processed_refs(self, tmp_path, capsys):
        refs = [
            _ref(index=1, title="First", doi="10.1/first"),
            _ref(index=2, title="Second", doi="10.1/second"),
        ]

        r1 = check_mod.LookupResult(
            doi_attempted="10.1/first",
            best_summary=_summary(doi="10.1/first", title="First"),
            display_score=1.0,
            best_source="openalex",
            id_confirmed=True,
        )
        r1.per_source["openalex"] = {
            "status": "hit_id", "queried_by": ["doi"],
            "score": 1.0, "summary": _summary(doi="10.1/first", title="First"),
            "note": None,
        }
        r2 = check_mod.LookupResult(
            doi_attempted="10.1/second",
            best_summary=_summary(doi="10.1/second", title="Second"),
            display_score=1.0,
            best_source="crossref",
            id_confirmed=True,
        )
        r2.per_source["crossref"] = {
            "status": "hit_id", "queried_by": ["doi"],
            "score": 1.0, "summary": _summary(doi="10.1/second", title="Second"),
            "note": None,
        }

        sc = tmp_path / "results.json"
        sidecar_mod.write(sc, "p.pdf", refs, {1: r1, 2: r2}, 0.80)

        rc = show_mod.show(sc)
        assert rc == 0
        out = capsys.readouterr().out
        assert "First" in out
        assert "Second" in out
        assert "10.1/first" in out
        assert "10.1/second" in out
        assert "OK" in out
        assert "NOT YET PROCESSED" not in out

    def test_show_sidecar_mixed_processed_and_unprocessed(self, tmp_path, capsys):
        # Ref #1 has a result; ref #2 has ref data but result is None.
        refs = [
            _ref(index=1, title="Processed", doi="10.1/proc"),
            _ref(index=2, title="Skipped"),
        ]
        r1 = check_mod.LookupResult(
            doi_attempted="10.1/proc",
            best_summary=_summary(doi="10.1/proc", title="Processed"),
            display_score=1.0,
            best_source="openalex",
            id_confirmed=True,
        )
        r1.per_source["openalex"] = {
            "status": "hit_id", "queried_by": ["doi"],
            "score": 1.0, "summary": _summary(doi="10.1/proc", title="Processed"),
            "note": None,
        }
        sc = tmp_path / "results.json"
        # Manually write sidecar with ref #2 having result=None.
        data = {
            "schema_version": 3,
            "pdf": "p.pdf",
            "refs_hash": "deadbeef",
            "references": {
                "1": {
                    "ref": refs[0].to_dict(),
                    "result": sidecar_mod.result_to_dict(r1, 0.80),
                },
                "2": {
                    "ref": refs[1].to_dict(),
                    "result": None,
                },
            },
        }
        sc.write_text(json.dumps(data, indent=2))

        rc = show_mod.show(sc)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Processed" in out
        assert "Skipped" in out
        assert "OK" in out
        assert "NOT YET PROCESSED" in out
        assert out.count("NOT YET PROCESSED") == 1

    def test_show_sidecar_preserves_ref_index_order(self, tmp_path, capsys):
        refs = [_ref(index=i, title=f"Ref {i}", doi=f"10.1/{i}") for i in [3, 1, 5, 2]]
        results = {}
        for r in refs:
            lr = check_mod.LookupResult(
                doi_attempted=r.doi,
                best_summary=_summary(doi=r.doi, title=r.title),
                display_score=1.0,
                best_source="openalex",
                id_confirmed=True,
            )
            lr.per_source["openalex"] = {
                "status": "hit_id", "queried_by": ["doi"],
                "score": 1.0,
                "summary": _summary(doi=r.doi, title=r.title),
                "note": None,
            }
            results[r.index] = lr

        sc = tmp_path / "results.json"
        sidecar_mod.write(sc, "p.pdf", refs, results, 0.80)

        rc = show_mod.show(sc)
        assert rc == 0
        out = capsys.readouterr().out
        # Refs should appear in ascending index order.
        positions = {}
        for i in [1, 2, 3, 5]:
            positions[i] = out.find(f"Ref {i}")
        assert positions[1] < positions[2] < positions[3] < positions[5]


class TestEndOfRunHint:
    def test_hint_line_prints_when_sidecar_used(self, tmp_path, capsys, monkeypatch):
        from ref_checker.sources import (
            arxiv, crossref, dblp, github, openalex, osti, semanticscholar,
            url as url_source,
        )
        monkeypatch.setattr(
            check_mod, "_DEFAULT_DELAYS",
            {k: 0.0 for k in check_mod._DEFAULT_DELAYS},
        )
        for src in (openalex, crossref, osti, dblp, semanticscholar, arxiv):
            for name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
                if hasattr(src, name):
                    monkeypatch.setattr(src, name, lambda *a, **kw: (None, None))
        monkeypatch.setattr(github, "check_url", lambda *a, **kw: (None, None, []))
        monkeypatch.setattr(url_source, "check_url", lambda *a, **kw: (None, None, []))

        refs = [_ref(index=1, doi="10.1/x1")]
        sc = tmp_path / "results.json"
        check_mod.check_references(refs, sidecar=sc, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        assert "Re-emit results anytime with:" in err
        assert "ref-checker show" in err
        assert str(sc) in err

    def test_no_hint_when_sidecar_none(self, tmp_path, capsys, monkeypatch):
        from ref_checker.sources import (
            arxiv, crossref, dblp, github, openalex, osti, semanticscholar,
            url as url_source,
        )
        monkeypatch.setattr(
            check_mod, "_DEFAULT_DELAYS",
            {k: 0.0 for k in check_mod._DEFAULT_DELAYS},
        )
        for src in (openalex, crossref, osti, dblp, semanticscholar, arxiv):
            for name in ("get_by_doi", "get_by_arxiv_id", "search_by_title"):
                if hasattr(src, name):
                    monkeypatch.setattr(src, name, lambda *a, **kw: (None, None))
        monkeypatch.setattr(github, "check_url", lambda *a, **kw: (None, None, []))
        monkeypatch.setattr(url_source, "check_url", lambda *a, **kw: (None, None, []))

        refs = [_ref(index=1, doi="10.1/x1")]
        check_mod.check_references(refs, sidecar=None, pdf_name="p.pdf", jobs=1)
        err = capsys.readouterr().err
        assert "Re-emit results anytime" not in err
