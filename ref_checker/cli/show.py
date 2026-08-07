"""Implementation of the `ref-checker show` subcommand.

Re-emits the per-ref formatted output that ``check_references`` prints at
end-of-run, given only a sidecar JSON or a bare refs JSON. Useful after a
Ctrl-C interrupted a run, or to inspect a saved sidecar from a prior session.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import sidecar as _sidecar
from ..extract import Reference, ReferenceLoadError, load_references_from_list
from ..format import _format_ref_header, format_result


_ORANGE = "\033[33m" if sys.stdout.isatty() else ""
_RESET = "\033[0m" if sys.stdout.isatty() else ""


def _format_unprocessed(ref: Reference) -> str:
    """Placeholder block for a ref that has no lookup result yet."""
    header = _format_ref_header(ref)
    return (
        f"{header}\n"
        f"    {_ORANGE}NOT YET PROCESSED{_RESET}  "
        f"(no lookups attempted; run `ref-checker check --refs-json <path> --resume` to process)"
    )


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_sidecar(data: object) -> bool:
    return (
        isinstance(data, dict)
        and "schema_version" in data
        and "references" in data
    )


def _is_refs_list(data: object) -> bool:
    return isinstance(data, list) and all(isinstance(x, dict) for x in data)


def show(path: Path, min_match: float = 0.80, with_osti_id: bool = False) -> int:
    """Print the sidecar or refs JSON at *path* in end-of-run format.

    Returns an exit code: 0 on success, 1 on load/format failure.
    """
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1
    try:
        data = _load_json(path)
    except Exception as exc:
        print(f"Error: failed to parse {path}: {exc}", file=sys.stderr)
        return 1

    if _is_sidecar(data):
        return _show_sidecar(data, min_match, with_osti_id)
    if _is_refs_list(data):
        return _show_refs_list(data)

    print(
        f"Error: {path} is neither a sidecar (schema_version + references) "
        f"nor a bare refs JSON (list of ref dicts).",
        file=sys.stderr,
    )
    return 1


def _show_sidecar(data: dict, min_match: float, with_osti_id: bool) -> int:
    refs_map = data.get("references") or {}
    try:
        indices = sorted(int(k) for k in refs_map.keys())
    except ValueError:
        print("Error: sidecar 'references' has non-integer keys.", file=sys.stderr)
        return 1

    blocks: list[str] = []
    for idx in indices:
        entry = refs_map[str(idx)]
        ref_dict = entry.get("ref") or {}
        result_dict = entry.get("result")
        try:
            # The sidecar's own outer key (idx) is already validated (see
            # the int(k) parse above) and is authoritative over whatever
            # the nested ref dict does or doesn't say about its own index
            # — a hand-edited or corrupted sidecar shouldn't be able to
            # produce a Reference with a missing/mismatched index just
            # because the nested "ref" object lacks or disagrees on one.
            ref = Reference.from_dict({**ref_dict, "index": idx})
        except Exception as exc:
            print(f"Warning: could not reconstruct ref #{idx}: {exc}", file=sys.stderr)
            continue
        if result_dict is None:
            blocks.append(_format_unprocessed(ref))
        else:
            try:
                result = _sidecar.result_from_dict(result_dict)
                blocks.append(
                    format_result(ref, result, min_match, with_osti_id=with_osti_id)
                )
            except Exception as exc:
                print(
                    f"Warning: could not reconstruct result for ref #{idx}: {exc}",
                    file=sys.stderr,
                )

    for block in blocks:
        print(block)
        print()
    return 0


def _show_refs_list(refs_list: list[dict]) -> int:
    try:
        refs = load_references_from_list(refs_list, strict=False)
    except ReferenceLoadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for ref in refs:
        print(_format_unprocessed(ref))
        print()
    return 0
