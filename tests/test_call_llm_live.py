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
their OSTI purl and cached under /tmp/opencode/ref-checker-live-fixtures/,
verified against a pinned sha256 on every reuse so a corrupted or stale
cache entry can't silently poison results (see tests/fixtures/README.md
for full provenance of each paper). If the download fails (offline, OSTI
unreachable) or the sha256 doesn't match, the test is skipped rather than
failed -- these are live-environment smoke tests, not tests of OSTI's
uptime.

Two of the three tests below call extract._call_llm directly rather than
the public extract_references() to isolate the streaming/JSON-parsing fix
from extract_references()'s 3-attempt retry loop, which could otherwise
mask an intermittent regression as a pass. The third test
(test_live_extract_references_end_to_end) drives the actual production
call path with max_retries=1, so a failure there means the first real
attempt failed outright.

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

import hashlib
import json
import os
import urllib.request
from pathlib import Path

import pytest

from ref_checker.extract import _call_llm, _narrow_text, extract_references

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "refs"
_CACHE_DIR = Path("/tmp/opencode/ref-checker-live-fixtures")

# Pinned sha256 of each OSTI PDF, verified against a fresh download at the
# time these tests were written. Guards against a stale/corrupted entry in
# the long-lived _CACHE_DIR silently poisoning results across runs -- any
# mismatch (corrupted cache or a changed upstream file) forces a re-fetch
# rather than extracting from bad bytes.
_OSTI_SHA256 = {
    "2998448": "16ee934efb054f5110938b6fb7185309f869e46c886b12243f9d8e490ca8c4e9",
    "3002321": "8a4b26f9db6dc46f35860d5ac9a20c83140d6281777da4ec7eb3e371613c81a4",
}


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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_osti_pdf(osti_id: str) -> Path:
    """Download and cache the OSTI PDF for *osti_id*, skipping the test on
    any network failure rather than failing it. Verifies the cached file's
    sha256 against the pinned value in _OSTI_SHA256 before reusing it, and
    re-fetches on mismatch, so a corrupted or stale cache entry can't
    silently poison extraction results."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = _CACHE_DIR / f"{osti_id}.pdf"
    expected_sha = _OSTI_SHA256.get(osti_id)

    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        if expected_sha is None or _sha256(pdf_path) == expected_sha:
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

    if expected_sha is not None:
        actual_sha = _sha256(tmp)
        if actual_sha != expected_sha:
            tmp.unlink(missing_ok=True)
            pytest.skip(
                f"OSTI fixture {osti_id} sha256 mismatch: expected "
                f"{expected_sha}, got {actual_sha} (upstream file may have "
                f"changed)"
            )

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


# --- _fetch_osti_pdf caching / sha256 verification -------------------------
# These do not require OPENAI_API_KEY/OPENAI_BASE_URL or hit any live LLM
# endpoint -- they exercise the download-and-cache helper in isolation
# against a fake "OSTI" HTTP response, so they run unconditionally as part
# of the normal (non-llm_live) test suite.


class _FakeHTTPResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_osti_pdf_downloads_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_call_llm_live._CACHE_DIR", tmp_path)
    monkeypatch.setitem(_OSTI_SHA256, "fake-id", _sha256_of_bytes(b"pdf bytes"))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: _FakeHTTPResponse(b"pdf bytes"),
    )

    path = _fetch_osti_pdf("fake-id")

    assert path == tmp_path / "fake-id.pdf"
    assert path.read_bytes() == b"pdf bytes"


def test_fetch_osti_pdf_reuses_valid_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_call_llm_live._CACHE_DIR", tmp_path)
    cached = tmp_path / "fake-id.pdf"
    tmp_path.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"pdf bytes")
    monkeypatch.setitem(_OSTI_SHA256, "fake-id", _sha256_of_bytes(b"pdf bytes"))

    def _boom(*a, **kw):
        raise AssertionError("should not hit the network when cache is valid")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    path = _fetch_osti_pdf("fake-id")
    assert path.read_bytes() == b"pdf bytes"


def test_fetch_osti_pdf_refetches_on_sha256_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_call_llm_live._CACHE_DIR", tmp_path)
    cached = tmp_path / "fake-id.pdf"
    tmp_path.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"corrupted bytes")
    monkeypatch.setitem(_OSTI_SHA256, "fake-id", _sha256_of_bytes(b"good bytes"))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: _FakeHTTPResponse(b"good bytes"),
    )

    path = _fetch_osti_pdf("fake-id")

    assert path.read_bytes() == b"good bytes"


def test_fetch_osti_pdf_skips_on_persistent_sha256_mismatch(tmp_path, monkeypatch):
    """If the freshly downloaded bytes still don't match the pinned sha256
    (e.g. upstream OSTI file genuinely changed), skip rather than silently
    extracting from unverified content."""
    monkeypatch.setattr("tests.test_call_llm_live._CACHE_DIR", tmp_path)
    monkeypatch.setitem(_OSTI_SHA256, "fake-id", _sha256_of_bytes(b"expected bytes"))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: _FakeHTTPResponse(b"different bytes"),
    )

    with pytest.raises(pytest.skip.Exception, match="sha256 mismatch"):
        _fetch_osti_pdf("fake-id")

    assert not (tmp_path / "fake-id.pdf").exists()


def test_fetch_osti_pdf_skips_on_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_call_llm_live._CACHE_DIR", tmp_path)

    def _boom(*a, **kw):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    with pytest.raises(pytest.skip.Exception, match="could not fetch"):
        _fetch_osti_pdf("fake-id-no-pin")


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


@pytest.mark.llm_live
def test_live_extract_references_end_to_end():
    """The two tests above call _call_llm directly to isolate the
    streaming/JSON-parsing fix from extract_references()'s 3-attempt
    retry loop (which could otherwise mask a regression that only fails
    intermittently). This test instead drives the actual production call
    path -- extract_references(), as invoked by the CLI's `extract` and
    `check` commands -- with max_retries=1 so a failure here means the
    first real attempt failed, not that retries ran out.

    Uses the small zfp_spectral fixture (13 refs) since this test isn't
    about streaming/size, just proving the public entry point works
    end-to-end against a real endpoint."""
    from ref_checker import pdf as pdf_mod

    pdf_path = _fetch_osti_pdf("2998448")
    full_text = pdf_mod.convert(pdf_path)
    assert full_text.strip(), "PDF text extraction returned nothing"

    refs = extract_references(full_text, max_retries=1)

    golden = _load_golden("zfp_spectral.json")
    _assert_matches_golden(refs, golden)
