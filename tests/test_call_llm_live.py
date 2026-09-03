"""Live regression tests for extract._call_llm against a real Argo-backed
LLM endpoint.

Argo rejects long-running non-streaming Chat Completions requests (HTTP
500, or a "streaming required for operations that may take longer than 10
minutes" error) once a request runs long enough, regardless of prompt size
or actual output length. tests/test_call_llm.py covers the streaming
reassembly logic with mocks, but mocks can't catch a regression back to a
non-streaming request actually being rejected by Argo. These tests do:
they hit a real endpoint with real papers (including the "big" fixture
most likely to reproduce the original failure) and check the extraction
actually completes and returns the expected references.

Skipped unless both OPENAI_API_KEY and OPENAI_BASE_URL are set (the latter
is required so a bare OpenAI key doesn't silently spend real OpenAI credit
against api.openai.com -- these tests are specifically about the
Argo-shaped failure mode).

PDFs are not committed to the repo. They are downloaded on first use from
their OSTI purl and cached under /tmp/opencode/ref-checker-live-fixtures/
(see tests/fixtures/README.md for full provenance of each paper). If the
download fails (offline, OSTI unreachable), the test is skipped rather
than failed -- these are live-environment smoke tests, not tests of OSTI's
uptime.

We compare against the committed golden fixtures in tests/fixtures/refs/
on two axes only:
  - reference count must match exactly ("full stop": if it doesn't,
    something is broken -- either Argo dropped part of the response, or
    the extraction prompt/model regressed)
  - doi / arxiv_id / github_url must match per-index for any golden ref
    that has them, since those are backfilled deterministically by
    extract._backfill_identifiers() via regex over ref.raw, not generated
    by the LLM, so they're stable across models/runs/temperature.
We deliberately do NOT assert exact title/author/venue text, since the
LLM's phrasing of those fields is not guaranteed byte-for-byte stable.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from ref_checker.extract import _call_llm, _narrow_text

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "refs"
_CACHE_DIR = Path("/tmp/opencode/ref-checker-live-fixtures")


@pytest.fixture(autouse=True)
def _skip_live_if_unconfigured():
    """Skip llm_live tests at call time unless both OPENAI_API_KEY and
    OPENAI_BASE_URL are set. Requiring OPENAI_BASE_URL (not just an API
    key) ensures these tests only run against an explicitly configured
    endpoint (e.g. Argo), never accidentally against api.openai.com."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
    if not os.environ.get("OPENAI_BASE_URL"):
        pytest.skip("OPENAI_BASE_URL not set")


def _fetch_osti_pdf(osti_id: str) -> Path:
    """Download and cache the OSTI PDF for *osti_id*, skipping the test on
    any network failure rather than failing it."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = _CACHE_DIR / f"{osti_id}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path

    url = f"https://www.osti.gov/servlets/purl/{osti_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ref-checker-tests"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as exc:
        pytest.skip(f"could not fetch OSTI fixture {osti_id}: {exc}")

    if not data:
        pytest.skip(f"OSTI fixture {osti_id} download was empty")

    tmp = pdf_path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, pdf_path)
    return pdf_path


def _load_golden(name: str) -> list[dict]:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _assert_matches_golden(refs, golden: list[dict]) -> None:
    assert len(refs) == len(golden), (
        f"expected {len(golden)} references, got {len(refs)}"
    )

    by_index = {r.index: r for r in refs}
    for g in golden:
        idx = g.get("index")
        assert idx in by_index, f"golden ref #{idx} missing from LLM output"
        got = by_index[idx]

        if g.get("doi"):
            assert got.doi == g["doi"], (
                f"ref #{idx}: expected doi {g['doi']!r}, got {got.doi!r}"
            )
        if g.get("arxiv_id"):
            assert got.arxiv_id == g["arxiv_id"], (
                f"ref #{idx}: expected arxiv_id {g['arxiv_id']!r}, "
                f"got {got.arxiv_id!r}"
            )
        if g.get("github_url"):
            assert got.github_url == g["github_url"], (
                f"ref #{idx}: expected github_url {g['github_url']!r}, "
                f"got {got.github_url!r}"
            )


@pytest.mark.llm_live
def test_live_small_paper_extracts_all_references():
    """zfp_spectral: 13 references. Fast smoke test that the streaming
    request path works against a real endpoint at all."""
    from ref_checker import pdf as pdf_mod
    from ref_checker.extract import _backfill_identifiers

    pdf_path = _fetch_osti_pdf("2998448")
    full_text = pdf_mod.convert(pdf_path)
    assert full_text.strip(), "PDF text extraction returned nothing"

    refs = _call_llm(_narrow_text(full_text))
    _backfill_identifiers(refs)

    golden = _load_golden("zfp_spectral.json")
    _assert_matches_golden(refs, golden)


@pytest.mark.llm_live
def test_live_big_paper_extracts_all_references_via_streaming():
    """dorier_mofka: 64 references. This is the streaming regression
    guard -- the paper's reference section is large enough to reproduce
    the original Argo failure (HTTP 500 / "streaming required") if
    _call_llm ever regresses back to a non-streaming request."""
    from ref_checker import pdf as pdf_mod
    from ref_checker.extract import _backfill_identifiers

    pdf_path = _fetch_osti_pdf("3002321")
    full_text = pdf_mod.convert(pdf_path)
    assert full_text.strip(), "PDF text extraction returned nothing"

    refs = _call_llm(_narrow_text(full_text))
    _backfill_identifiers(refs)

    golden = _load_golden("dorier_mofka.json")
    _assert_matches_golden(refs, golden)
