"""Tests for source module _summarize and _normalize_doi helpers (no network)."""


class TestOpenAlexSummarize:
    def _make_work(self, **kwargs):
        base = {
            "display_name": "Attention Is All You Need",
            "publication_year": 2017,
            "doi": "https://doi.org/10.48550/arxiv.1706.03762",
            "id": "https://openalex.org/W123",
            "authorships": [
                {"author": {"display_name": "Ashish Vaswani"}},
                {"author": {"display_name": "Noam Shazeer"}},
            ],
            "primary_location": {
                "source": {"display_name": "NeurIPS"},
                "landing_page_url": "https://arxiv.org/abs/1706.03762",
            },
        }
        base.update(kwargs)
        return base

    def test_basic(self):
        from ref_checker.sources.openalex import _summarize
        s = _summarize(self._make_work())
        assert s["title"] == "Attention Is All You Need"
        assert s["year"] == 2017
        assert s["doi"] == "10.48550/arxiv.1706.03762"
        assert "Ashish Vaswani" in s["authors"]
        assert s["source"] == "openalex"

    def test_doi_normalized(self):
        from ref_checker.sources.openalex import _normalize_doi
        assert _normalize_doi("https://doi.org/10.1/ABC") == "10.1/abc"
        assert _normalize_doi("doi:10.1/X") == "10.1/x"
        assert _normalize_doi(None) is None

    def test_missing_authors(self):
        from ref_checker.sources.openalex import _summarize
        work = self._make_work(authorships=[])
        s = _summarize(work)
        assert s["authors"] == []


class TestCrossRefSummarize:
    def _make_entry(self, **kwargs):
        base = {
            "title": ["Attention Is All You Need"],
            "author": [{"given": "Ashish", "family": "Vaswani"},
                       {"family": "Shazeer"}],
            "published-print": {"date-parts": [[2017]]},
            "container-title": ["NeurIPS"],
            "DOI": "10.1/test",
            "URL": "https://doi.org/10.1/test",
        }
        base.update(kwargs)
        return base

    def test_basic(self):
        from ref_checker.sources.crossref import _summarize
        s = _summarize(self._make_entry())
        assert s["title"] == "Attention Is All You Need"
        assert s["year"] == 2017
        assert "Ashish Vaswani" in s["authors"]
        assert s["venue"] == "NeurIPS"
        assert s["source"] == "crossref"

    def test_author_given_only_family(self):
        from ref_checker.sources.crossref import _summarize
        s = _summarize(self._make_entry())
        assert "Shazeer" in s["authors"]

    def test_no_title_returns_none(self):
        from ref_checker.sources.crossref import _summarize
        s = _summarize(self._make_entry(title=[]))
        assert s["title"] is None

    def test_published_online_fallback(self):
        from ref_checker.sources.crossref import _summarize
        entry = self._make_entry()
        del entry["published-print"]
        entry["published-online"] = {"date-parts": [[2016]]}
        s = _summarize(entry)
        assert s["year"] == 2016


class TestSemanticScholarSummarize:
    def _make_entry(self, **kwargs):
        base = {
            "title": "Attention Is All You Need",
            "year": 2017,
            "venue": "NeurIPS",
            "paperId": "abc123",
            "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
            "externalIds": {"DOI": "10.1/test", "ArXiv": "1706.03762"},
        }
        base.update(kwargs)
        return base

    def test_basic(self):
        from ref_checker.sources.semanticscholar import _summarize
        s = _summarize(self._make_entry())
        assert s["title"] == "Attention Is All You Need"
        assert s["year"] == 2017
        assert s["doi"] == "10.1/test"
        assert "Ashish Vaswani" in s["authors"]
        assert s["source"] == "semanticscholar"

    def test_arxiv_url_when_no_doi(self):
        from ref_checker.sources.semanticscholar import _summarize
        entry = self._make_entry()
        entry["externalIds"] = {"ArXiv": "1706.03762"}
        s = _summarize(entry)
        assert "arxiv" in s["url"]

    def test_ss_url_fallback(self):
        from ref_checker.sources.semanticscholar import _summarize
        entry = self._make_entry()
        entry["externalIds"] = {}
        s = _summarize(entry)
        assert "semanticscholar" in s["url"]


class TestArxivParse:
    def test_parse_entry(self):
        import xml.etree.ElementTree as ET
        from ref_checker.sources.arxiv import _parse_entry
        xml = """<entry xmlns="http://www.w3.org/2005/Atom">
            <id>http://arxiv.org/abs/1706.03762v5</id>
            <title>Attention Is All You Need</title>
            <published>2017-06-12T17:57:34Z</published>
            <author><name>Ashish Vaswani</name></author>
            <author><name>Noam Shazeer</name></author>
        </entry>"""
        entry = ET.fromstring(xml)
        s = _parse_entry(entry)
        assert s["title"] == "Attention Is All You Need"
        assert s["year"] == 2017
        assert s["external_id"] == "1706.03762"
        assert s["doi"] == "10.48550/arXiv.1706.03762"
        assert "Ashish Vaswani" in s["authors"]
        assert s["source"] == "arxiv"

    def test_parse_entry_strips_version(self):
        import xml.etree.ElementTree as ET
        from ref_checker.sources.arxiv import _parse_entry
        xml = """<entry xmlns="http://www.w3.org/2005/Atom">
            <id>http://arxiv.org/abs/2303.08797v3</id>
            <title>A Paper</title>
            <published>2023-03-15T00:00:00Z</published>
        </entry>"""
        entry = ET.fromstring(xml)
        s = _parse_entry(entry)
        assert s["external_id"] == "2303.08797"
