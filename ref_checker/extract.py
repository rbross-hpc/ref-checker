"""Extract structured references from PDF text via heuristic narrowing + LLM."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from string import Template

_HEADING_RE = re.compile(
    r"^\s*(References|Bibliography|Works\s+Cited|Literature\s+Cited)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_END_SECTION_RE = re.compile(
    r"^\s*(Appendix|Acknowledgements?|Acknowledgments?|Supplementary|About the Authors?)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PAGE_MARKER_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
_SPLIT_WORD_RE = re.compile(r"(?<=\w) ([A-Z]) ([a-z]{3,})\b")
_HYPHEN_JOIN_RE = re.compile(r"([a-zA-Z])-\n([a-z])")
_URL_RE_PROTECT = re.compile(r"https?://\S+", re.IGNORECASE)

# How close (in characters) an _END_SECTION_RE match must be to a preceding
# page marker to count as "starting a new page" -- see _trim_post_references.
_PAGE_PROXIMITY_LOOKBACK = 200

_DOI_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.IGNORECASE,
)
_ARXIV_RE = re.compile(
    r"\barXiv[:\s]+(\d{4}\.\d{4,5})(?:v\d+)?\b",
    re.IGNORECASE,
)
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/[^\s,;\"'<>)]+",
    re.IGNORECASE,
)
_GENERIC_URL_RE = re.compile(
    r"https?://[^\s,;\"'<>)]+",
    re.IGNORECASE,
)
_SKIP_URL_PATTERNS = re.compile(
    r"https?://(?:(?:dx\.)?doi\.org|arxiv\.org/abs|github\.com)/",
    re.IGNORECASE,
)

# The reference JSON schema is the single source of truth in
# ref_checker/skills/reference-checking/references/schema.md.
# It is loaded here at import time and interpolated into the LLM prompt,
# so the extraction prompt and the human/agent-facing SKILL.md always share
# one definition. To change a schema field, edit only schema.md.
_SCHEMA_MD = (
    files("ref_checker")
    .joinpath("skills/reference-checking/references/schema.md")
    .read_text(encoding="utf-8")
)

_SYSTEM_PROMPT = Template("""\
You are a reference-extraction assistant. The user will provide text that was \
automatically extracted from a PDF of an academic paper using a PDF-to-text \
tool. The text represents the references section of the paper (or the tail \
portion of the document if the References heading was not detected). It may \
contain artifacts typical of PDF extraction: split words (e.g. "V ector" \
instead of "Vector"), hyphenated line breaks (e.g. "frame-\\nwork"), ligatures \
rendered as separate characters, and occasional garbled or merged lines from \
multi-column layouts. Do your best to interpret the text correctly despite \
these artifacts.

Extract every bibliographic reference and return \
a JSON object with a single key "references" whose value is an array. \
Each element must conform to the following schema:

$schema

Examples:

Input reference:
  [1] ytopt (2023). Autotuning applications at scale. \
https://github.com/ytopt-team/ytopt https://github.com/ytopt-team/ytopt-libensemble

Output:
  {
    "index": 1,
    "raw": "ytopt (2023). Autotuning applications at scale. https://github.com/ytopt-team/ytopt https://github.com/ytopt-team/ytopt-libensemble",
    "title": "Autotuning applications at scale",
    "authors": [],
    "year": 2023,
    "doi": null,
    "arxiv_id": null,
    "venue": null,
    "url": "https://github.com/ytopt-team/ytopt https://github.com/ytopt-team/ytopt-libensemble"
  }

Input reference:
  [2] SCHOLKOPF, B., SMOLA, A. J., WILLIAMSON, R. C., & BARTLETT, P. L. (2000). \
New support vector algorithms. Neural Computation, 12(5), 1207-1245. \
arXiv:cs/9994126v2

Output:
  {
    "index": 2,
    "raw": "SCHOLKOPF, B., SMOLA, A. J., WILLIAMSON, R. C., & BARTLETT, P. L. (2000). New support vector algorithms. Neural Computation, 12(5), 1207-1245. arXiv:cs/9994126v2",
    "title": "New support vector algorithms",
    "authors": ["Bernhard Scholkopf", "Alex Smola", "Robert Williamson", "Peter Bartlett"],
    "year": 2000,
    "doi": null,
    "arxiv_id": "cs/9994126",
    "venue": "Neural Computation",
    "url": null
  }

Return only the JSON object, no commentary.""").substitute(schema=_SCHEMA_MD)

_USER_PROMPT_TEMPLATE = """\
Below is text automatically extracted from the references section of an \
academic paper PDF. The text may contain PDF extraction artifacts such as \
split words, hyphenated line breaks, and garbled multi-column text. \
Please extract all bibliographic references as instructed.

---
{text}
---"""


def _validate_index(value: object) -> int:
    """Validate a candidate ``index`` value: must be a native ``int`` (not
    ``bool`` — a subclass of ``int`` — and not ``float`` or ``str``), and
    positive (``>= 1``, matching the 1-based contract documented in
    schema.md). Raises ``ValueError`` on any other input.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"index must be a positive integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"index must be a positive integer, got {value!r}")
    return value


@dataclass
class Reference:
    index: int
    raw: str
    title: str | None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    github_url: str | None = None
    url: str | None = None

    @property
    def first_author(self) -> str | None:
        return self.authors[0] if self.authors else None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "raw": self.raw,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "venue": self.venue,
            "github_url": self.github_url,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        """Build a ``Reference`` from a dict that already carries a valid,
        resolved ``index`` (a positive ``int`` — see ``_validate_index``).
        Callers that accept index-less or untrusted input (the reference
        loader, LLM extraction, sidecar display) are responsible for
        resolving a valid index into the dict *before* calling this —
        e.g. via 1-based list position — since what "missing index" should
        fall back to differs by caller (a strict parse error for
        ``check --refs-json``, a permissive positional fallback for LLM
        output, the sidecar's own outer index key for ``show``).
        """
        llm_url = d.get("url") or ""
        github_urls = _GITHUB_URL_RE.findall(llm_url)
        cleaned_github = list(dict.fromkeys(u.rstrip(".,;)") for u in github_urls))
        github_url = " ".join(cleaned_github) if cleaned_github else (d.get("github_url") or None)

        generic_urls = [
            u.rstrip(".,;)")
            for u in _GENERIC_URL_RE.findall(llm_url)
            if not _SKIP_URL_PATTERNS.match(u)
        ]
        cleaned_generic = list(dict.fromkeys(generic_urls))
        url = " ".join(cleaned_generic) if cleaned_generic else (d.get("url") or None)

        if "index" not in d:
            raise ValueError("reference dict is missing required 'index' field")

        return cls(
            index=_validate_index(d["index"]),
            raw=d.get("raw", ""),
            title=d.get("title") or None,
            authors=d.get("authors") or [],
            year=d.get("year") or None,
            doi=d.get("doi") or None,
            arxiv_id=d.get("arxiv_id") or None,
            venue=d.get("venue") or None,
            github_url=github_url,
            url=url,
        )


class ReferenceLoadError(ValueError):
    """Raised by load_references_from_list on a structurally invalid input."""


def load_references_from_list(
    data: object,
    strict: bool = True,
) -> list[Reference]:
    """Parse a bare JSON array of reference dicts into ``Reference`` objects.

    Shared by both ``check --refs-json`` and ``show`` so the two commands
    interpret the same input identically. Behavior:

    - The top-level value must be a list of dicts; otherwise raises
      ``ReferenceLoadError`` regardless of *strict*.
    - Entries missing an explicit ``index`` are auto-assigned 1-based
      positions from their position in the list (matching citation-style
      display, e.g. ``[1]``, ``[2]``, ...).
    - Duplicate explicit indices are rejected — raises ``ReferenceLoadError``
      regardless of *strict*, since silently colliding indices would corrupt
      sidecar state and in-memory dicts keyed by index.
    - If *strict* is True (the default — used by ``check``, which performs
      paid/rate-limited API calls), a malformed individual entry raises
      ``ReferenceLoadError`` immediately.
    - If *strict* is False (used by ``show``, a read-only inspection tool),
      a malformed individual entry is skipped, and a warning is printed to
      stderr rather than raising.
    """
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise ReferenceLoadError(
            "expected a bare JSON array of reference objects"
        )

    # First pass: resolve the final index for every entry (explicit, or
    # auto-assigned from 1-based list position) and reject collisions
    # before constructing any Reference objects. Malformed explicit
    # indices (non-integer) are treated as parse errors, subject to
    # *strict* like any other malformed field.
    resolved: list[tuple[int, dict]] = []
    seen_indices: set[int] = set()

    for position, entry in enumerate(data, start=1):
        if "index" in entry:
            try:
                idx = _validate_index(entry["index"])
            except (TypeError, ValueError) as exc:
                if strict:
                    raise ReferenceLoadError(
                        f"invalid index at list position {position}: {entry['index']!r}"
                    ) from exc
                print(
                    f"Warning: could not parse ref #{position}: invalid index "
                    f"{entry['index']!r}",
                    file=sys.stderr,
                )
                continue
        else:
            idx = position

        if idx in seen_indices:
            raise ReferenceLoadError(
                f"duplicate reference index {idx} (entry at list position {position})"
            )
        seen_indices.add(idx)
        resolved.append((idx, {**entry, "index": idx}))

    refs: list[Reference] = []
    for position, (idx, entry_for_index) in enumerate(resolved, start=1):
        try:
            ref = Reference.from_dict(entry_for_index)
        except Exception as exc:
            if strict:
                raise ReferenceLoadError(
                    f"could not parse reference at list position {position} "
                    f"(index {idx}): {exc}"
                ) from exc
            print(
                f"Warning: could not parse ref #{idx}: {exc}",
                file=sys.stderr,
            )
            continue
        refs.append(ref)

    return refs


def _fix_split_words(text: str) -> str:
    """Repair PDF text extraction artifacts.

    - Hyphen-join: 'frame-\\nwork' -> 'framework'
      (URLs are protected so hyphens inside URL paths are never joined)
    - Split-word:  'Support V ector' -> 'Support Vector'
      (only when the lone capital is preceded by another letter,
      so standalone article 'A' at line/sentence start is not affected)
    """
    placeholders: list[str] = []

    def _protect(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00URL{len(placeholders) - 1}\x00"

    text = _URL_RE_PROTECT.sub(_protect, text)
    text = _HYPHEN_JOIN_RE.sub(r"\1\2", text)
    text = _SPLIT_WORD_RE.sub(r" \1\2", text)

    for i, original in enumerate(placeholders):
        text = text.replace(f"\x00URL{i}\x00", original)

    return text


def _match_starts_new_page(match_start: int, page_markers: list[re.Match]) -> bool:
    """True if *match_start* falls shortly after a <!-- page N --> marker.

    In a two-column journal layout, pypdf/pdfplumber emit text page by page,
    which routinely places a page's trailing boilerplate (running footer,
    funding statement, acknowledgments block) between two reference entries
    that are visually contiguous to a human reader across the page break --
    the reference list resumes on the next page. A genuine post-references
    section (a real Appendix/Acknowledgments), by contrast, essentially
    always starts a new page. This structural signal is available for every
    PDF processed by pdf.convert() regardless of citation style, unlike a
    content-density heuristic (DOI density is silent on reference lists with
    no DOIs; a quick check found year-density too easily confused by a short
    prose block, e.g. a real funding statement, sitting between a false match
    and the page break that follows it).
    """
    preceding = None
    for pm in page_markers:
        if pm.end() > match_start:
            break
        preceding = pm
    if preceding is None:
        return False
    return match_start - preceding.end() <= _PAGE_PROXIMITY_LOOKBACK


def _format_heading_list(matches: list[re.Match]) -> str:
    """Render matched heading text as a human list: 'A', 'A' and 'B', or
    'A', 'B' and 'C'."""
    names = [f"{m.group(0).strip()!r}" for m in matches]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _trim_post_references(text: str) -> str:
    """Truncate at the first section that genuinely follows the references
    (appendix, acknowledgements, etc.), skipping any _END_SECTION_RE match
    that doesn't start a new page -- see _match_starts_new_page for why that
    signal distinguishes a real post-references section from a PDF-extraction
    artifact that interleaves page-trailing boilerplate into the middle of
    the reference list. Returns *text* unchanged if no match qualifies.

    Prints an informational note to stderr in the two cases where the
    decision isn't obvious enough to stay silent: a heading is used to trim
    only after an earlier heading was skipped (non-trivial disambiguation
    happened -- worth a look), or a heading is found but none qualifies (the
    text is kept whole, which is the shape of the bug this function used to
    have). Silent in the common cases: no candidate headings, or the first
    one is accepted outright.
    """
    page_markers = list(_PAGE_MARKER_RE.finditer(text))
    skipped: list[re.Match] = []

    for match in _END_SECTION_RE.finditer(text):
        if _match_starts_new_page(match.start(), page_markers):
            if skipped:
                print(
                    f"[ref-checker] Note: interpreted {match.group(0).strip()!r} "
                    f"as the end of the references.",
                    file=sys.stderr,
                )
            return text[:match.start()]
        skipped.append(match)

    if skipped:
        print(
            f"[ref-checker] Note: found {_format_heading_list(skipped)} after "
            f"the References heading but did not treat "
            f"{'it' if len(skipped) == 1 else 'them'} as the end.",
            file=sys.stderr,
        )

    return text


def _narrow_text(full_text: str, tail_pages: int = 5) -> str:
    """Return the references section text using heuristic narrowing."""
    m = _HEADING_RE.search(full_text)
    if m:
        return _fix_split_words(_trim_post_references(full_text[m.start():]))

    page_markers = list(_PAGE_MARKER_RE.finditer(full_text))
    if page_markers and len(page_markers) >= tail_pages:
        cutoff = page_markers[-tail_pages].start()
        return _fix_split_words(full_text[cutoff:])

    return _fix_split_words(full_text)


def _backfill_identifiers(refs: list[Reference]) -> None:
    """Regex-scan ref.raw for DOIs, arXiv IDs, and GitHub URLs the LLM may have missed."""
    for ref in refs:
        if ref.doi is None:
            m = _DOI_RE.search(ref.raw)
            if m:
                ref.doi = m.group(1).rstrip(".,;")

        if ref.arxiv_id is None:
            m = _ARXIV_RE.search(ref.raw)
            if m:
                ref.arxiv_id = m.group(1)

        if ref.github_url is None:
            raw_urls = _GITHUB_URL_RE.findall(ref.raw)
            cleaned = list(dict.fromkeys(u.rstrip(".,;)") for u in raw_urls))
            if cleaned:
                ref.github_url = " ".join(cleaned)

        if ref.url is None:
            raw_urls = [
                u.rstrip(".,;)")
                for u in _GENERIC_URL_RE.findall(ref.raw)
                if not _SKIP_URL_PATTERNS.match(u)
            ]
            cleaned = list(dict.fromkeys(raw_urls))
            if cleaned:
                ref.url = " ".join(cleaned)


def _strip_markdown_fence(text: str) -> str:
    """Strip a leading/trailing ``` or ```json code fence some models wrap
    JSON output in despite response_format={"type": "json_object"} or
    explicit prompt instructions not to (observed live against Argo with
    both GPT-4.1/4o and Claude models)."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        text = "\n".join(lines[start:end]).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    return text


def _extract_json_object(text: str) -> str:
    """Best-effort recovery when a model prefixes its JSON response with
    prose (despite instructions not to). Finds the first '{' and its
    matching closing '}' (brace-depth counting, string-aware so braces
    inside quoted strings don't confuse it) and returns just that span.
    Returns *text* unchanged if no balanced object is found, so the
    caller's json.loads still raises a clear error rather than silently
    returning something wrong.
    """
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text


def _parse_llm_json(raw: str) -> dict:
    """Parse *raw* as JSON, recovering from a markdown code fence and/or
    prefixed prose that some models emit despite response_format=json_object
    (or explicit prompt instructions). Raises ValueError with the raw
    response (truncated) if no recovery succeeds.
    """
    stripped = _strip_markdown_fence(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_extract_json_object(stripped))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM response was not valid JSON, even after markdown-fence and "
            f"prefixed-prose recovery: {exc}. Raw response (truncated): {raw[:200]!r}"
        ) from exc


def _call_llm(text: str) -> list[Reference]:
    """Send *text* to the LLM and return parsed references. Raises on failure."""
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_PROMPT_TEMPLATE.format(text=text)},
        ],
        timeout=120,
        stream=True,
    )

    # Argo rejects long-running non-streaming Chat Completions requests
    # (HTTP 500, or a "streaming required for operations that may take
    # longer than 10 minutes" error) once the request runs long enough,
    # regardless of prompt size or actual output length. Streaming keeps
    # the connection alive for the duration of generation, so we request
    # and consume a stream, then reassemble it into the same JSON string
    # a non-streaming call would have returned.
    raw_json = "".join(
        chunk.choices[0].delta.content or ""
        for chunk in response
        if chunk.choices and chunk.choices[0].delta.content
    )
    data = _parse_llm_json(raw_json)

    if "references" not in data or not isinstance(data["references"], list):
        raise ValueError(f"LLM response missing 'references' list: {raw_json[:200]}")

    return [Reference.from_dict(r) for r in _resolve_llm_indices(data["references"])]


def _resolve_llm_indices(entries: list[dict]) -> list[dict]:
    """Resolve a valid, unique 1-based index for every LLM-returned entry.

    LLM output is untrusted: the system prompt asks for a 1-based ``index``
    per entry, but nothing guarantees the model actually returns one, or
    that it's valid (positive int, not a duplicate). Unlike
    ``load_references_from_list`` (which treats a bad explicit index as a
    hard parse error, subject to *strict*), an LLM index quirk here falls
    back to the entry's 1-based list position instead of raising — an LLM
    formatting slip on one field isn't reason to fail the whole extraction
    (and trigger ``extract_references()``'s retry loop), unlike a
    structurally invalid response.
    """
    resolved: list[dict] = []
    seen_indices: set[int] = set()
    for position, entry in enumerate(entries, start=1):
        idx = position
        if isinstance(entry, dict) and "index" in entry:
            try:
                candidate = _validate_index(entry["index"])
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None and candidate not in seen_indices:
                idx = candidate
        seen_indices.add(idx)
        resolved.append({**entry, "index": idx} if isinstance(entry, dict) else entry)
    return resolved


def extract_references(
    full_text: str,
    tail_pages: int = 5,
    max_retries: int = 3,
) -> list[Reference]:
    """Extract references from *full_text*, retrying the LLM up to *max_retries* times."""
    narrowed = _narrow_text(full_text, tail_pages=tail_pages)

    delays = [2, 4, 8]
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            refs = _call_llm(narrowed)
            _backfill_identifiers(refs)
            return refs
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = delays[min(attempt, len(delays) - 1)]
                print(
                    f"[ref-checker] LLM extraction attempt {attempt + 1} failed: {exc}. "
                    f"Retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                print(
                    f"[ref-checker] LLM extraction failed after {max_retries} attempts: {exc}",
                    file=sys.stderr,
                )

    raise RuntimeError(
        f"Reference extraction failed after {max_retries} attempts"
    ) from last_exc


_REFS_CACHE_SCHEMA_VERSION = 1


def _pdf_sha256(pdf_path: Path) -> str:
    h = hashlib.sha256()
    with pdf_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_refs_cache(
    cache_path: Path,
    pdf_path: Path,
    refs: list[Reference],
    extractor_meta: dict | None = None,
) -> None:
    """Write refs to cache_path in wrapper format, atomically."""
    data = {
        "schema_version": _REFS_CACHE_SCHEMA_VERSION,
        "pdf": pdf_path.name,
        "pdf_sha256": _pdf_sha256(pdf_path),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "extractor": extractor_meta or {},
        "references": [r.to_dict() for r in refs],
    }
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cache_path)


def load_refs_cache(
    cache_path: Path,
    pdf_path: Path,
) -> tuple[list[Reference] | None, str]:
    """Load refs from cache_path, validating schema version and PDF hash.

    Returns (refs, reason) where reason is one of:
      "valid", "missing", "corrupt", "schema_mismatch", "hash_mismatch"
    refs is None on any reason other than "valid".
    """
    if not cache_path.exists():
        return None, "missing"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None, "corrupt"
    if not isinstance(data, dict) or data.get("schema_version") != _REFS_CACHE_SCHEMA_VERSION:
        return None, "schema_mismatch"
    if data.get("pdf_sha256") != _pdf_sha256(pdf_path):
        return None, "hash_mismatch"
    try:
        refs = [Reference.from_dict(r) for r in data.get("references") or []]
    except Exception:
        return None, "corrupt"
    return refs, "valid"
