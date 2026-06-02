"""Extract structured references from PDF text via heuristic narrowing + LLM."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

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

_SYSTEM_PROMPT = """\
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
a JSON object with a single key "references" whose value is an array.

Rules:
1. Each element must have these fields (use null when a field is not present):
     index     (integer, 1-based, in the order the reference appears)
     raw       (string, the full reference text exactly as it appears, excluding
                any leading citation label such as "[1]", "1.", or "(1)")
     title     (string or null)
     authors   (array of strings, each a single author's full name in
                natural order e.g. "Bernhard Scholkopf"; empty array if none)
     year      (integer or null)
     doi       (string or null — the canonical DOI only, e.g. "10.1145/1234.5678";
                strip any "doi:", "https://doi.org/" or "http://dx.doi.org/" prefix)
     arxiv_id  (string or null — bare arXiv ID only, e.g. "2301.01234";
                strip any "arXiv:" prefix or version suffix like "v2")
     venue     (string or null, journal/conference/publisher name)
     url       (string or null — any non-DOI, non-arXiv URL present in the
                reference, such as a GitHub repository, project page, or
                institutional landing page; if multiple URLs are present,
                separate them with a single space; do not include doi.org or
                arxiv.org URLs here as those belong in doi/arxiv_id)
2. If the reference has a corporate or organizational author (e.g. "IBM",
   "World Health Organization", "scikit-learn developers"), set authors to [].
3. Do not include citation labels, numbers, or bracketed keys in any field,
   including raw.
4. If a DOI appears in any form in the reference text — bare, prefixed with
   "doi:", or as a URL (https://doi.org/..., http://dx.doi.org/...) — extract
   the canonical DOI string into the doi field.
5. If an arXiv identifier appears in any form (e.g. "arXiv:2301.01234",
   "arXiv:2301.01234v2", or in a URL like arxiv.org/abs/2301.01234), extract
   just the bare ID (no prefix, no version) into the arxiv_id field.
6. If the reference contains a GitHub URL (or other project/software URL that
   is not a DOI or arXiv link), extract it into the url field.
7. Skip any text that is not a bibliographic reference (tool names, section
   headers, acknowledgements, footnotes, etc.).

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

Return only the JSON object, no commentary."""

_USER_PROMPT_TEMPLATE = """\
Below is text automatically extracted from the references section of an \
academic paper PDF. The text may contain PDF extraction artifacts such as \
split words, hyphenated line breaks, and garbled multi-column text. \
Please extract all bibliographic references as instructed.

---
{text}
---"""


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

        return cls(
            index=int(d.get("index", 0)),
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


def _trim_post_references(text: str) -> str:
    """Truncate at any section that follows the references (appendix, acknowledgements, etc.)."""
    end = _END_SECTION_RE.search(text)
    if end:
        return text[:end.start()]
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
    )

    raw_json = response.choices[0].message.content or ""
    data = json.loads(raw_json)

    if "references" not in data or not isinstance(data["references"], list):
        raise ValueError(f"LLM response missing 'references' list: {raw_json[:200]}")

    return [Reference.from_dict(r) for r in data["references"]]


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
