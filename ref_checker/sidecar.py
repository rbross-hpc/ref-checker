"""Per-paper results sidecar: serialization, resume policy, atomic I/O."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .extract import Reference
from .results import LookupResult

SIDECAR_SCHEMA_VERSION = 1


def refs_hash(refs: list[Reference]) -> str:
    raw = "\n".join(str(r.index) + r.raw for r in sorted(refs, key=lambda r: r.index))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def status_label(result: LookupResult, min_match: float) -> str:
    if result.is_liveness or result.id_confirmed:
        return "OK"
    score = result.display_score if result.display_score is not None else 0.0
    if score >= 0.90:
        return "OK"
    if score >= min_match:
        return "CLOSEST"
    return "NO MATCH"


def result_to_dict(result: LookupResult, min_match: float) -> dict:
    return {
        "status": status_label(result, min_match),
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
    }


def result_from_dict(d: dict) -> LookupResult:
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
    )


def needs_retry(result_dict: dict, retry_closest: bool) -> bool:
    """Return True if this result dict should be re-queried on resume."""
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
    """Load sidecar JSON. Returns (entries_by_index, hash_ok)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, False
    if data.get("schema_version") != SIDECAR_SCHEMA_VERSION:
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
