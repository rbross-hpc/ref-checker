"""Tests for extract._trim_post_references / extract._narrow_text.

Bug background: _trim_post_references used to truncate the references
section at the *first* _END_SECTION_RE match ("Appendix", "Acknowledgments",
etc.) unconditionally. In two-column journal layouts, pdf.convert() emits
text page by page, which routinely places a page's trailing boilerplate
(running footer, funding statement, acknowledgments block) between two
reference entries that are visually contiguous to a human reader across the
page break -- the reference list resumes on the next page. The naive
first-match trim silently discarded everything after that false match, with
no warning: a verification tool reporting on a small prefix of the
references and saying nothing about the rest.

The fix: a candidate _END_SECTION_RE match is only trusted if it starts a
new page (falls within _PAGE_PROXIMITY_LOOKBACK characters after a
<!-- page N --> marker -- see _match_starts_new_page). A genuine
post-references section essentially always starts a page; a mid-list
boilerplate artifact does not. If no candidate qualifies, the text is
returned untrimmed rather than guessing wrong. An informational note is
printed to stderr in the two cases where this isn't obvious enough to stay
silent -- see TestNarrowingNotes below.

Two real papers anchor this test file (see tests/fixtures/README.md for
full provenance):
  - zhang_2019 (OpenAlex W2944224695): the reported false-positive case --
    a false "Acknowledgments" match 5.1% into the references section, with
    no preceding page marker, followed by 81 more references. Reproduced
    here as an inline synthetic (report's minimal repro) since the paper's
    published-version license (CC-BY-NC-ND) precludes committing an
    extracted-text fixture.
  - li_2025 (OpenAlex W4416004292, CC-BY): the reported genuine-positive
    case -- a real trailing "Appendix" section that starts a new page 33%
    into the references section, committed as
    tests/fixtures/pdf_text/li_2025_trim_site.txt.
"""
from __future__ import annotations

from pathlib import Path

from ref_checker.extract import (
    _match_starts_new_page,
    _narrow_text,
    _PAGE_MARKER_RE,
    _PAGE_PROXIMITY_LOOKBACK,
    _trim_post_references,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdf_text"


# --- The report's minimal reproduction (false-positive case) --------------

_ZHANG_STYLE_FALSE_POSITIVE = (
    "References\n"
    "Adcroft, A. (2004). Rescaled height coordinates. Ocean Modelling, 7, 269-284. "
    "https://doi.org/10.1016/j.ocemod.2003.09.003\n"
    "Acknowledgments\n"
    "This study was jointly supported by the National Key R&D Program.\n"
    "Chen, X. (2013). A control-volume model. Monthly Weather Review, 141, 2526-2544. "
    "https://doi.org/10.1175/MWR-D-12-00129.1\n"
    "Li, J., Liao, W. K., Choudhary, A. (2003). Parallel netCDF: A high-performance "
    "scientific I/O interface. SC '03. https://doi.org/10.1145/1048935.1050189\n"
)


class TestReportedFalsePositiveReproduction:
    """tests/test_narrow_text.py acceptance criterion 1: the minimal
    reproduction from the bug report must retain the Parallel netCDF entry
    (and both post-Acknowledgments references)."""

    def test_parallel_netcdf_entry_retained(self):
        out = _trim_post_references(_ZHANG_STYLE_FALSE_POSITIVE)
        assert "Parallel netCDF" in out

    def test_chen_entry_retained(self):
        out = _trim_post_references(_ZHANG_STYLE_FALSE_POSITIVE)
        assert "Chen, X." in out

    def test_text_not_truncated_at_false_match(self):
        out = _trim_post_references(_ZHANG_STYLE_FALSE_POSITIVE)
        assert out == _ZHANG_STYLE_FALSE_POSITIVE

    def test_narrow_text_end_to_end_retains_all_references(self):
        full_text = "Some intro text.\n\n" + _ZHANG_STYLE_FALSE_POSITIVE
        out = _narrow_text(full_text)
        assert "Parallel netCDF" in out
        assert "Chen, X." in out
        assert "Adcroft, A." in out


# --- Genuine trailing section must still be trimmed ------------------------


class TestGenuineTrailingSectionIsTrimmed:
    """Acceptance criterion 2: a genuine trailing Appendix/Acknowledgments
    section must still be trimmed -- the fix must skip false matches, not
    stop trimming altogether."""

    def test_trailing_acknowledgments_after_page_marker_is_trimmed(self):
        text = (
            "References\n"
            "Smith, J. (2020). A paper. Journal, 1, 1-10.\n"
            "Jones, A. (2021). Another paper. Journal, 2, 11-20.\n"
            "<!-- page 12 -->\n\n"
            "Acknowledgments\n"
            "This work was supported by a grant.\n"
        )
        out = _trim_post_references(text)
        assert "Acknowledgments" not in out
        assert "Smith, J." in out
        assert "Jones, A." in out

    def test_trailing_appendix_immediately_after_page_marker(self):
        text = (
            "References\n"
            "Smith, J. (2020). A paper. Journal, 1, 1-10.\n"
            "<!-- page 5 -->\nAppendix A: Extra material.\nMore appendix content.\n"
        )
        out = _trim_post_references(text)
        assert "Appendix" not in out
        assert "Smith, J." in out

    def test_trim_at_second_candidate_when_first_is_false(self):
        """A mid-page false match followed by a real page-boundary match:
        only the second (real) candidate should trigger the trim."""
        text = (
            "References\n"
            "Smith, J. (2020). A paper. Journal, 1, 1-10.\n"
            "Acknowledgments\n"  # false match: no preceding page marker
            "Funding statement prose here.\n"
            "Jones, A. (2021). Another paper. Journal, 2, 11-20.\n"
            "<!-- page 9 -->\n\n"
            "Appendix B: Supplementary tables.\n"  # real match: starts new page
            "Table data here.\n"
        )
        out = _trim_post_references(text)
        assert "Jones, A." in out, "false match must not have truncated the list"
        assert "Appendix B" not in out, "real trailing match must still be trimmed"
        assert "Table data" not in out


# --- No DOI reliance ---------------------------------------------------


class TestNoDoiReliance:
    """Acceptance criterion 3: the fix must work for reference lists
    containing no DOIs at all (do not rely on DOI presence as the sole
    signal)."""

    def test_false_positive_with_no_dois_anywhere(self):
        text = (
            "References\n"
            "[1] J. Smith, \"A paper,\" in Proc. SC, 2019, pp. 1-10.\n"
            "Acknowledgments\n"
            "Funding statement, no identifiers here.\n"
            "[2] A. Jones, \"Another paper,\" in Proc. SC, 2020, pp. 11-20.\n"
        )
        out = _trim_post_references(text)
        assert "[2] A. Jones" in out

    def test_genuine_trailing_section_with_no_dois_is_trimmed(self):
        text = (
            "References\n"
            "[1] J. Smith, \"A paper,\" in Proc. SC, 2019, pp. 1-10.\n"
            "<!-- page 7 -->\n\n"
            "Acknowledgments\n"
            "Funding statement, no identifiers here.\n"
        )
        out = _trim_post_references(text)
        assert "Acknowledgments" not in out
        assert "[1] J. Smith" in out


# --- No match at all --------------------------------------------------


class TestNoEndSectionMatch:
    def test_text_unchanged_when_no_match(self):
        text = "References\nSmith, J. (2020). A paper.\nJones, A. (2021). Another.\n"
        assert _trim_post_references(text) == text


# --- _match_starts_new_page boundary behavior --------------------------


class TestMatchStartsNewPage:
    def test_no_page_markers_returns_false(self):
        assert _match_starts_new_page(100, []) is False

    def test_within_lookback_window_is_true(self):
        text = "x" * 50 + "<!-- page 1 -->" + "y" * 10
        page_markers = list(_PAGE_MARKER_RE.finditer(text))
        marker_end = page_markers[0].end()
        match_start = marker_end + _PAGE_PROXIMITY_LOOKBACK  # exactly at the edge
        assert _match_starts_new_page(match_start, page_markers) is True

    def test_just_outside_lookback_window_is_false(self):
        text = "x" * 50 + "<!-- page 1 -->" + "y" * 10
        page_markers = list(_PAGE_MARKER_RE.finditer(text))
        marker_end = page_markers[0].end()
        match_start = marker_end + _PAGE_PROXIMITY_LOOKBACK + 1
        assert _match_starts_new_page(match_start, page_markers) is False

    def test_uses_nearest_preceding_marker_not_first(self):
        text = "<!-- page 1 -->" + "z" * 500 + "<!-- page 2 -->" + "w" * 10
        page_markers = list(_PAGE_MARKER_RE.finditer(text))
        second_marker_end = page_markers[1].end()
        match_start = second_marker_end + 5
        assert _match_starts_new_page(match_start, page_markers) is True

    def test_match_before_any_marker_is_false(self):
        text = "a" * 10 + "<!-- page 1 -->" + "b" * 10
        page_markers = list(_PAGE_MARKER_RE.finditer(text))
        assert _match_starts_new_page(5, page_markers) is False


# --- Informational notes on stderr --------------------------------------


class TestNarrowingNotes:
    """_trim_post_references prints an informational note to stderr in the
    two cases where the trim decision isn't obvious enough to stay silent:
    a heading is trimmed at only after an earlier heading was skipped
    (non-trivial disambiguation happened), or a heading is found but none
    qualifies (the shape of the bug this function used to have -- the text
    is kept whole rather than silently truncated). Silent in the common
    cases: no candidate headings, or the first one is accepted outright.

    If either note looks wrong for a given paper, the fallback is to copy
    the reference list into a JSON file and use `check --refs-json`
    (see README.md)."""

    def test_no_note_when_no_candidate_headings(self, capsys):
        text = "References\nSmith, J. (2020). A paper.\nJones, A. (2021). Another.\n"
        _trim_post_references(text)
        assert capsys.readouterr().err == ""

    def test_no_note_when_first_candidate_accepted(self, capsys):
        text = (
            "References\n"
            "Smith, J. (2020). A paper. Journal, 1, 1-10.\n"
            "<!-- page 5 -->\nAppendix A: Extra material.\n"
        )
        _trim_post_references(text)
        assert capsys.readouterr().err == ""

    def test_note_fires_when_earlier_candidate_skipped_before_trim(self, capsys):
        text = (
            "References\n"
            "Smith, J. (2020). A paper.\n"
            "Acknowledgments\n"  # skipped: no preceding page marker
            "Funding statement prose.\n"
            "Jones, A. (2021).\n"
            "<!-- page 9 -->\n\n"
            "Appendix B: Supplementary tables.\n"  # accepted
        )
        _trim_post_references(text)
        err = capsys.readouterr().err
        assert "interpreted 'Appendix" in err
        assert "end of the references" in err

    def test_note_fires_when_all_candidates_rejected(self, capsys):
        _trim_post_references(_ZHANG_STYLE_FALSE_POSITIVE)
        err = capsys.readouterr().err
        assert "found 'Acknowledgments'" in err
        assert "did not treat it as the end" in err

    def test_note_lists_multiple_rejected_candidates(self, capsys):
        text = (
            "References\n"
            "Smith, J. (2020).\n"
            "Acknowledgments\n"
            "Prose.\n"
            "Jones, A. (2021).\n"
            "Supplementary\n"
            "More prose.\n"
            "Brown, C. (2022).\n"
        )
        _trim_post_references(text)
        err = capsys.readouterr().err
        assert "'Acknowledgments'" in err
        assert "'Supplementary'" in err
        assert "did not treat them as the end" in err


# --- Real-paper regression fixtures -------------------------------------


class TestRealPaperFixtures:
    """li_2025 (CC-BY, OpenAlex W4416004292): the reported genuine-positive
    case, a real trailing Appendix that starts a new page 33% into the
    references section. See tests/fixtures/README.md for full provenance.
    """

    def _load(self, name: str) -> str:
        return (_FIXTURES_DIR / name).read_text(encoding="utf-8")

    def test_li_2025_trim_site_is_trimmed_at_appendix(self):
        text = self._load("li_2025_trim_site.txt")
        out = _trim_post_references(text)
        assert "Appendix" not in out
        assert "Artifact Description" not in out

    def test_li_2025_trim_site_retains_preceding_references(self):
        text = self._load("li_2025_trim_site.txt")
        out = _trim_post_references(text)
        assert "Zheng" in out  # last reference entry before the page break
        assert "hdfgroup.org" in out

    def test_li_2025_trim_site_emits_no_note(self, capsys):
        """li_2025's Appendix is the first candidate and starts a new page,
        so this is the common case -- no note should fire."""
        text = self._load("li_2025_trim_site.txt")
        _trim_post_references(text)
        assert capsys.readouterr().err == ""
