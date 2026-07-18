"""ref-checker CLI — subcommand dispatcher."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .. import check, extract, pdf
from ..sources import arxiv, crossref, dblp, openalex, semanticscholar
from . import skill as skill_mod


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ref-checker",
        description="Check paper references against OpenAlex, CrossRef, DBLP, Semantic Scholar, and arXiv.",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    _build_check_parser(sub)
    _build_extract_parser(sub)
    _build_lookup_parser(sub)
    _build_skill_parser(sub)

    return p


def _build_check_parser(sub) -> None:
    p = sub.add_parser(
        "check",
        help="Extract references from a PDF and check each against all sources.",
    )
    p.add_argument("pdf", nargs="?", default=None,
                   help="Path to the input PDF file. Optional when --refs-json is supplied.")
    p.add_argument("--refs-json", default=None, metavar="PATH",
                   help="Load references from a JSON file (bare list of ref dicts) and skip "
                        "PDF extraction entirely. The PDF argument is ignored when this flag "
                        "is used; a warning is printed if one is supplied.")
    p.add_argument("--tail-pages", type=int, default=5, metavar="N",
                   help="Trailing pages to use as fallback when no References heading found (default: 5)")
    p.add_argument("--min-match", type=float, default=0.80, metavar="F",
                   help="Minimum similarity to report as CLOSEST (default: 0.80)")
    p.add_argument("--delay-openalex", type=float, default=2.0, metavar="S",
                   help="Seconds between OpenAlex calls (default: 2.0)")
    p.add_argument("--delay-crossref", type=float, default=2.0, metavar="S",
                   help="Seconds between CrossRef calls (default: 2.0)")
    p.add_argument("--delay-dblp", type=float, default=1.0, metavar="S",
                   help="Seconds between DBLP calls (default: 1.0)")
    p.add_argument("--delay-semanticscholar", type=float, default=8.0, metavar="S",
                   help="Seconds between Semantic Scholar calls (default: 8.0)")
    p.add_argument("--delay-arxiv", type=float, default=3.0, metavar="S",
                   help="Seconds between arXiv calls (default: 3.0)")
    p.add_argument("--delay-github", type=float, default=1.0, metavar="S",
                   help="Seconds between GitHub liveness checks (default: 1.0)")
    p.add_argument("--delay-url", type=float, default=1.0, metavar="S",
                   help="Seconds between generic URL liveness checks (default: 1.0)")
    p.add_argument("--refs-cache", default=None, metavar="PATH",
                   help="Refs cache file (default: <pdf-stem>.refs.json next to PDF)")
    p.add_argument("--no-refs-cache", action="store_true",
                   help="Disable refs cache entirely — always extract, never write")
    p.add_argument("--re-extract", action="store_true",
                   help="Force re-extraction even if refs cache is valid")
    p.add_argument("--results-json", default=None, metavar="PATH",
                   help="Sidecar results file (default: <pdf-stem>.results.json next to PDF)")
    p.add_argument("--no-results-json", action="store_true",
                   help="Disable sidecar entirely — do not read or write results")
    p.add_argument("--no-resume", action="store_true",
                   help="Disable resume — re-query every ref regardless of sidecar")
    p.add_argument("--retry-all", action="store_true",
                   help="Re-query every ref even if sidecar marks it done")
    p.add_argument("--retry-closest", action="store_true",
                   help="Also re-query refs previously reported as CLOSEST")
    p.add_argument("--no-retry-errored", action="store_true",
                   help="On resume, do NOT retry sources previously marked as errored "
                        "(default: retry them). Disabled sources are always retried.")
    p.add_argument("--source-error-threshold", type=int, default=3, metavar="N",
                   help="Disable a source for the rest of the session after N consecutive "
                        "errors (default: 3)")


def _build_extract_parser(sub) -> None:
    p = sub.add_parser(
        "extract",
        help="Extract references from a PDF and write <stem>.refs.md + <stem>.refs.json.",
    )
    p.add_argument("pdf", help="Path to the input PDF file")
    p.add_argument("--out-dir", default=None, metavar="DIR",
                   help="Output directory (default: same directory as PDF)")
    p.add_argument("--tail-pages", type=int, default=5, metavar="N",
                   help="Trailing pages to use as fallback when no References heading found (default: 5)")


def _build_skill_parser(sub) -> None:
    p = sub.add_parser(
        "skill",
        help="Show or export the bundled Agent Skill for ref-checker.",
    )
    ssub = p.add_subparsers(dest="skill_action", required=True, metavar="ACTION")

    ssub.add_parser(
        "show",
        help="Print the bundled SKILL.md to stdout.",
    )

    ep = ssub.add_parser(
        "export",
        help="Copy the complete skill directory to PATH.",
    )
    ep.add_argument("path", metavar="PATH",
                    help="Destination directory to write the skill into.")
    ep.add_argument("--force", action="store_true",
                    help="Overwrite PATH if it already exists and is non-empty.")


def _build_lookup_parser(sub) -> None:
    p = sub.add_parser(
        "lookup",
        help="Query a single source for one paper and print JSON.",
    )
    lsub = p.add_subparsers(dest="source", required=True, metavar="SOURCE")

    for name in ("openalex", "crossref", "dblp", "semanticscholar", "arxiv"):
        sp = lsub.add_parser(name, help=f"Query {name}.")
        group = sp.add_mutually_exclusive_group(required=True)
        if name != "dblp":
            group.add_argument("--doi", help="DOI to look up")
        if name not in ("crossref", "dblp"):
            id_flag = "--id" if name == "arxiv" else "--arxiv-id"
            id_dest = "arxiv_id"
            id_help = "arXiv ID" if name == "arxiv" else "arXiv ID to look up"
            group.add_argument(id_flag, dest=id_dest, help=id_help)
        group.add_argument("--title", help="Title to search for")


def _log_credentials() -> None:
    items = [
        ("OPENAI_API_KEY",            "LLM extraction (required for extract/check)"),
        ("OPENAI_BASE_URL",           "LLM base URL override"),
        ("OPENAI_MODEL",              "LLM model override (default: gpt-4o-mini)"),
        ("OPENALEX_MAILTO",           "OpenAlex/CrossRef polite pool (recommended)"),
        ("SEMANTICSCHOLAR_API_KEY",   "Semantic Scholar authenticated tier"),
    ]
    _SENSITIVE = {"OPENAI_API_KEY", "SEMANTICSCHOLAR_API_KEY"}
    for var, description in items:
        val = os.environ.get(var, "")
        if val:
            display = "<set>" if var in _SENSITIVE else val
            print(f"[ref-checker]   {var}: {display}  ({description})", file=sys.stderr)
        else:
            print(f"[ref-checker]   {var}: NOT SET  ({description})", file=sys.stderr)


def _load_pdf_and_extract(pdf_path, tail_pages: int) -> list[extract.Reference]:
    from pathlib import Path
    p = Path(pdf_path).resolve()
    if not p.exists():
        print(f"Error: file not found: {p}", file=sys.stderr)
        sys.exit(1)
    print(f"[ref-checker] Converting PDF: {p}", file=sys.stderr)
    full_text = pdf.convert(p)
    if not full_text.strip():
        print("Error: could not extract text from PDF.", file=sys.stderr)
        sys.exit(1)
    print("[ref-checker] Extracting references via LLM...", file=sys.stderr)
    try:
        refs = extract.extract_references(full_text, tail_pages=tail_pages)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[ref-checker] Extracted {len(refs)} reference(s).", file=sys.stderr)
    return refs


def run_check(args) -> None:
    from pathlib import Path

    print("[ref-checker] Credential / configuration status:", file=sys.stderr)
    _log_credentials()

    delays = {
        "openalex":        args.delay_openalex,
        "crossref":        args.delay_crossref,
        "dblp":            args.delay_dblp,
        "semanticscholar": args.delay_semanticscholar,
        "arxiv":           args.delay_arxiv,
        "github":          args.delay_github,
        "url":             args.delay_url,
    }

    if args.refs_json:
        if args.pdf is not None:
            print(
                "[ref-checker] Warning: PDF argument is ignored when --refs-json is used.",
                file=sys.stderr,
            )
        refs_path = Path(args.refs_json).resolve()
        if not refs_path.exists():
            print(f"Error: refs JSON not found: {refs_path}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(refs_path.read_text(encoding="utf-8"))
        refs = [extract.Reference.from_dict(r) for r in data]
        print(f"[ref-checker] Loaded {len(refs)} reference(s) from {refs_path}", file=sys.stderr)
        source_name = refs_path.stem
        default_sidecar = refs_path.parent / f"{refs_path.stem}.results.json"
    else:
        if args.pdf is None:
            print(
                "Error: a PDF path is required unless --refs-json is supplied.",
                file=sys.stderr,
            )
            sys.exit(1)
        pdf_path = Path(args.pdf).resolve()
        if not pdf_path.exists():
            print(f"Error: file not found: {pdf_path}", file=sys.stderr)
            sys.exit(1)
        refs_cache_path = (
            Path(args.refs_cache).resolve() if args.refs_cache
            else pdf_path.parent / f"{pdf_path.stem}.refs.json"
        )
        refs = None
        if not args.no_refs_cache and not args.re_extract:
            cached_refs, reason = extract.load_refs_cache(refs_cache_path, pdf_path)
            if reason == "valid":
                refs = cached_refs
                print(
                    f"[ref-checker] Refs cache: loaded {len(refs)} reference(s) from {refs_cache_path}",
                    file=sys.stderr,
                )
            elif reason != "missing":
                print(
                    f"[ref-checker] Refs cache: {reason.replace('_', ' ')} — re-extracting",
                    file=sys.stderr,
                )
            else:
                print("[ref-checker] Refs cache: not found — extracting from PDF", file=sys.stderr)
        if refs is None:
            refs = _load_pdf_and_extract(pdf_path, args.tail_pages)
            if refs and not args.no_refs_cache:
                extractor_meta = {
                    "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                    "tail_pages": args.tail_pages,
                }
                extract.write_refs_cache(refs_cache_path, pdf_path, refs, extractor_meta)
                print(f"[ref-checker] Refs cache: written to {refs_cache_path}", file=sys.stderr)
        source_name = pdf_path.name
        default_sidecar = pdf_path.parent / f"{pdf_path.stem}.results.json"

    if not refs:
        print("No references found.", file=sys.stderr)
        sys.exit(0)

    if args.no_results_json:
        sidecar = None
    elif args.results_json:
        sidecar = Path(args.results_json).resolve()
    else:
        sidecar = default_sidecar

    resume = not args.no_resume

    if sidecar is not None:
        print(f"[ref-checker] Sidecar: {sidecar}", file=sys.stderr)
        if resume and sidecar.exists():
            print("[ref-checker] Resuming from sidecar...", file=sys.stderr)
        elif resume and not sidecar.exists():
            print(
                "[ref-checker] No sidecar found — results will be saved for future --resume runs.",
                file=sys.stderr,
            )

    print(f"[ref-checker] Checking {len(refs)} reference(s)...\n", file=sys.stderr)
    reason = check.check_references(
        refs,
        delays=delays,
        min_match=args.min_match,
        sidecar=sidecar,
        resume=resume,
        retry_all=args.retry_all,
        retry_closest=args.retry_closest,
        retry_errored=not args.no_retry_errored,
        source_error_threshold=args.source_error_threshold,
        pdf_name=source_name,
    )
    if reason == "keyboard_interrupt":
        sys.exit(130)
    if reason == "all_scholarly_sources_disabled":
        sys.exit(2)


def run_extract(args) -> None:
    from pathlib import Path

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    refs = _load_pdf_and_extract(pdf_path, args.tail_pages)

    if not refs:
        print("Warning: no references extracted.", file=sys.stderr)

    stem = pdf_path.stem
    md_path = out_dir / f"{stem}.refs.md"
    json_path = out_dir / f"{stem}.refs.json"

    md_lines = ["# References\n"]
    for ref in refs:
        md_lines.append(f"{ref.index}. {ref.raw}\n")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"[ref-checker] Written: {md_path}", file=sys.stderr)

    extractor_meta = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "tail_pages": args.tail_pages,
    }
    extract.write_refs_cache(json_path, pdf_path, refs, extractor_meta)
    print(f"[ref-checker] Written: {json_path}", file=sys.stderr)


def run_lookup(args) -> None:
    source = args.source
    arxiv_id = getattr(args, "arxiv_id", None)

    if source == "openalex":
        if args.doi:
            summary, sim = openalex.get_by_doi(args.doi)
        elif arxiv_id:
            summary, sim = openalex.get_by_arxiv_id(arxiv_id)
        else:
            summary, sim = openalex.search_by_title(args.title)

    elif source == "crossref":
        if args.doi:
            summary, sim = crossref.get_by_doi(args.doi)
        else:
            summary, sim = crossref.search_by_title(args.title)

    elif source == "dblp":
        summary, sim = dblp.search_by_title(args.title)

    elif source == "semanticscholar":
        if args.doi:
            summary, sim = semanticscholar.get_by_doi(args.doi)
        elif arxiv_id:
            summary, sim = semanticscholar.get_by_arxiv_id(arxiv_id)
        else:
            summary, sim = semanticscholar.search_by_title(args.title)

    elif source == "arxiv":
        if arxiv_id:
            summary, sim = arxiv.get_by_arxiv_id(arxiv_id)
        elif args.doi:
            summary, sim = arxiv.get_by_doi(args.doi)
        else:
            summary, sim = arxiv.search_by_title(args.title)

    else:
        print(f"Unknown source: {source}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"summary": summary, "similarity": sim, "source": source}, indent=2))


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "check":
            run_check(args)
        elif args.command == "extract":
            run_extract(args)
        elif args.command == "lookup":
            run_lookup(args)
        elif args.command == "skill":
            skill_mod.run_skill(args)
    except KeyboardInterrupt:
        print("[ref-checker] Interrupted.", file=sys.stderr)
        sys.exit(130)
