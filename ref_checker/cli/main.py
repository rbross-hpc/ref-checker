"""ref-checker CLI — subcommand dispatcher."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .. import check, extract, pdf
from ..sources import arxiv, crossref, dblp, openalex, semanticscholar


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ref-checker",
        description="Check paper references against OpenAlex, CrossRef, DBLP, Semantic Scholar, and arXiv.",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    _build_check_parser(sub)
    _build_extract_parser(sub)
    _build_lookup_parser(sub)

    return p


def _build_check_parser(sub) -> None:
    p = sub.add_parser(
        "check",
        help="Extract references from a PDF and check each against all sources.",
    )
    p.add_argument("pdf", help="Path to the input PDF file")
    p.add_argument("--refs-json", default=None, metavar="PATH",
                   help="Skip extraction and load references from this JSON file")
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
    p.add_argument("--results-json", default=None, metavar="PATH",
                   help="Sidecar results file (default: <pdf-stem>.results.json next to PDF)")
    p.add_argument("--no-results-json", action="store_true",
                   help="Disable sidecar entirely — do not read or write results")
    p.add_argument("--resume", action="store_true",
                   help="Skip refs already resolved in sidecar; retry only failures")
    p.add_argument("--retry-all", action="store_true",
                   help="Re-query every ref even if sidecar marks it done (implies --resume ignored for skipping)")
    p.add_argument("--retry-closest", action="store_true",
                   help="Also re-query refs previously reported as CLOSEST")


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

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

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
        refs = _load_pdf_and_extract(pdf_path, args.tail_pages)
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

    if sidecar is not None:
        print(f"[ref-checker] Sidecar: {sidecar}", file=sys.stderr)
        if args.resume and sidecar.exists():
            print("[ref-checker] Resuming from sidecar...", file=sys.stderr)

    print(f"[ref-checker] Checking {len(refs)} reference(s)...\n", file=sys.stderr)
    check.check_references(
        refs,
        delays=delays,
        min_match=args.min_match,
        sidecar=sidecar,
        resume=args.resume,
        retry_all=args.retry_all,
        retry_closest=args.retry_closest,
        pdf_name=source_name,
    )


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

    json_path.write_text(
        json.dumps([r.to_dict() for r in refs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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

    if args.command == "check":
        run_check(args)
    elif args.command == "extract":
        run_extract(args)
    elif args.command == "lookup":
        run_lookup(args)
