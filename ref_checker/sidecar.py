"""Per-paper results sidecar: serialization, resume policy, atomic I/O."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from .extract import Reference
from .model import EvidenceLevel, SourceOutcome
from .results import STRONG_MATCH_THRESHOLD, LookupResult

SIDECAR_SCHEMA_VERSION = 4
_OUTDATED_SCHEMA_VERSIONS = {1, 2, 3}


def refs_hash(refs: list[Reference]) -> str:
    """Hash every lookup-relevant field of every reference.

    Covers the full canonical dict (index, raw, title, authors, year, doi,
    arxiv_id, venue, url, github_url) rather than just index+raw, so editing
    a structured field (e.g. correcting a DOI or title) without touching
    raw reliably invalidates any sidecar computed against the old value.
    """
    canonical = json.dumps(
        [r.to_dict() for r in sorted(refs, key=lambda r: r.index)],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def status_label(result: LookupResult, min_match: float) -> str:
    if result.is_liveness or result.id_confirmed:
        return "OK"
    score = result.display_score if result.display_score is not None else 0.0
    if score >= STRONG_MATCH_THRESHOLD:
        return "OK"
    if score >= min_match:
        return "CLOSEST"
    return "NO MATCH"


def result_to_dict(result: LookupResult, min_match: float) -> dict:
    return {
        "status": status_label(result, min_match),
        "evidence": result.evidence,
        "display_score": result.display_score,
        "best_source": result.best_source,
        "id_confirmed": result.id_confirmed,
        "is_liveness": result.is_liveness,
        "best_summary": result.best_summary,
        "doi_attempted": result.doi_attempted,
        "doi_found_in": result.doi_found_in,
        "arxiv_attempted": result.arxiv_attempted,
        "arxiv_found_in": result.arxiv_found_in,
        "year_mismatch_note": result.year_mismatch_note,
        "id_notes": result.id_notes,
        "dead_urls": [list(t) for t in result.dead_urls],
        "exhausted_sources": result.exhausted_sources,
        "url_liveness_check": result.url_liveness_check,
        "per_source": {k: v.to_dict() for k, v in result.per_source.items()},
    }


def result_from_dict(d: dict) -> LookupResult:
    evidence_raw = d.get("evidence")
    return LookupResult(
        best_summary=d.get("best_summary"),
        display_score=d.get("display_score"),
        best_source=d.get("best_source"),
        id_confirmed=d.get("id_confirmed", False),
        is_liveness=d.get("is_liveness", False),
        doi_attempted=d.get("doi_attempted"),
        doi_found_in=d.get("doi_found_in") or [],
        arxiv_attempted=d.get("arxiv_attempted"),
        arxiv_found_in=d.get("arxiv_found_in") or [],
        year_mismatch_note=d.get("year_mismatch_note"),
        id_notes=d.get("id_notes") or [],
        dead_urls=[tuple(t) for t in (d.get("dead_urls") or [])],
        exhausted_sources=d.get("exhausted_sources") or [],
        url_liveness_check=d.get("url_liveness_check", False),
        per_source={
            k: SourceOutcome.from_dict(k, v)
            for k, v in (d.get("per_source") or {}).items()
        },
        evidence=EvidenceLevel(evidence_raw) if evidence_raw else None,
    )


def needs_retry(result_dict: dict, retry_closest: bool) -> bool:
    """Return True if this result should be revisited on resume.

    Coarse-grained: says only whether the ref is "done" or worth re-planning.
    Fine-grained per-source planning lives in check._plan_ref_work.
    """
    status = result_dict.get("status", "NO MATCH")
    if status not in ("OK", "CLOSEST", "NO MATCH"):
        return True
    if status == "NO MATCH":
        return True
    if status == "CLOSEST" and retry_closest:
        return True
    if result_dict.get("exhausted_sources"):
        return True
    if result_dict.get("dead_urls"):
        return True
    return False


def load(path: Path, refs: list[Reference]) -> tuple[dict[int, dict], bool]:
    """Load sidecar JSON. Returns (entries_by_index, hash_ok).

    A schema-version mismatch is treated as invalid — returns ({}, False)
    so the driver starts fresh. This is intentional: cross-version upgrade
    is not supported; users who care about the prior data can hand-migrate.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, False
    stored_version = data.get("schema_version")
    if stored_version != SIDECAR_SCHEMA_VERSION:
        if stored_version in _OUTDATED_SCHEMA_VERSIONS:
            print(
                f"[ref-checker] WARNING: sidecar at {path} is schema "
                f"v{stored_version} (current: v{SIDECAR_SCHEMA_VERSION}); "
                f"discarding and re-querying all refs. Delete the old file "
                f"to silence this warning.",
                file=sys.stderr,
            )
        return {}, False
    stored_hash = data.get("refs_hash")
    current_hash = refs_hash(refs)
    hash_ok = stored_hash == current_hash
    entries = {int(k): v for k, v in (data.get("references") or {}).items()}
    return entries, hash_ok


def write(
    path: Path,
    pdf_name: str,
    refs: list[Reference],
    all_results: dict[int, LookupResult],
    min_match: float,
) -> None:
    """Write sidecar atomically (tmp file then os.replace)."""
    data = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "pdf": pdf_name,
        "refs_hash": refs_hash(refs),
        "references": {
            str(ref.index): {
                "ref": ref.to_dict(),
                "result": result_to_dict(all_results[ref.index], min_match),
            }
            for ref in refs
            if ref.index in all_results
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
