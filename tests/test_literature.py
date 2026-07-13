"""Unit tests for the PubMed search client (mocked HTTP — no network)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import literature  # noqa: E402


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, esearch, esummary):
        self.esearch, self.esummary = esearch, esummary
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResp(self.esearch if "esearch" in url else self.esummary)


def test_search_parses_hits_and_drops_empty_titles():
    es = {"esearchresult": {"idlist": ["111", "222"]}}
    su = {
        "result": {
            "111": {
                "title": "A study of buffers.",
                "authors": [{"name": "Doe J"}, {"name": "Roe R"}],
                "pubdate": "2020 May",
                "articleids": [{"idtype": "pubmed", "value": "111"}, {"idtype": "doi", "value": "10.1/x"}],
            },
            "222": {"title": "", "authors": []},  # dropped: no title
        }
    }
    hits = literature.search_pubmed("buffers", client=FakeHttp(es, su))
    assert len(hits) == 1
    h = hits[0]
    assert h["pmid"] == "111"
    assert h["authors"] == "Doe J et al."
    assert h["year"] == 2020
    assert h["doi"] == "10.1/x"
    assert h["title"] == "A study of buffers"  # trailing period stripped


def test_search_empty_idlist_returns_empty():
    es = {"esearchresult": {"idlist": []}}
    assert literature.search_pubmed("nothing", client=FakeHttp(es, {})) == []


def test_single_author_no_etal():
    es = {"esearchresult": {"idlist": ["9"]}}
    su = {"result": {"9": {"title": "Solo", "authors": [{"name": "Solo H"}], "pubdate": "1999", "articleids": []}}}
    h = literature.search_pubmed("x", client=FakeHttp(es, su))[0]
    assert h["authors"] == "Solo H"
    assert h["doi"] is None


def test_format_results_shows_pmid_and_doi():
    txt = literature.format_results([{"pmid": "111", "title": "T", "authors": "Doe J", "year": 2020, "doi": "10.1/x"}])
    assert "PMID 111" in txt and "doi:10.1/x" in txt
    assert "No PubMed results" in literature.format_results([])


class SimpleHttp:
    """Returns one fixed payload for any GET (single-endpoint backends)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResp(self.payload)


def test_preprints_parse_europepmc():
    payload = {"resultList": {"result": [
        {"source": "PPR", "title": "A preprint on CFPS.", "authorString": "Smith J, Doe A.", "pubYear": "2023", "doi": "10.1101/2023.01.01.123"},
        {"source": "PPR", "title": "", "authorString": ""},  # dropped
    ]}}
    http = SimpleHttp(payload)
    hits = literature.search_preprints("cfps", client=http)
    assert len(hits) == 1
    assert hits[0]["doi"] == "10.1101/2023.01.01.123"
    assert hits[0]["authors"] == "Smith J et al."
    assert hits[0]["year"] == 2023
    assert hits[0]["url"] == "https://doi.org/10.1101/2023.01.01.123"
    # query is restricted to preprint sources
    assert "SRC:PPR" in http.calls[0][1]["query"]


def test_protocols_requires_token_then_parses():
    from app import config
    old = config.PROTOCOLS_IO_TOKEN
    config.PROTOCOLS_IO_TOKEN = ""
    try:
        raised = False
        try:
            literature.search_protocols("x", client=SimpleHttp({}))
        except RuntimeError:
            raised = True
        assert raised, "should require a token"

        config.PROTOCOLS_IO_TOKEN = "tok"
        payload = {"items": [
            {"title": "S30 extract prep", "doi": "10.17504/protocols.io.abc", "uri": "s30-extract",
             "authors": [{"name": "Jewett M"}, {"name": "Karim A"}], "published_on": 1600000000},
        ]}
        hits = literature.search_protocols("s30 extract", client=SimpleHttp(payload))
        assert hits[0]["doi"] == "10.17504/protocols.io.abc"
        assert hits[0]["authors"] == "Jewett M et al."
        assert hits[0]["year"] == 2020
        assert "protocols.io" in literature.format_protocols(hits)
    finally:
        config.PROTOCOLS_IO_TOKEN = old


# --- Epic 1: never-fabricated evidence on search hits ----------------------

class XmlResp:
    """A minimal HTTP response exposing `.text` (for efetch XML parsing)."""

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeHttpAbstracts:
    """esearch + esummary (JSON) plus efetch (XML) for the abstract fetch."""

    def __init__(self, esearch, esummary, efetch_xml):
        self.esearch, self.esummary, self.efetch_xml = esearch, esummary, efetch_xml
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if "esearch" in url:
            return FakeResp(self.esearch)
        if "efetch" in url:
            return XmlResp(self.efetch_xml)
        return FakeResp(self.esummary)


def _pubmed_xml(pmid, abstract):
    return (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
        f"<PMID>{pmid}</PMID>"
        f"<Article><Abstract><AbstractText>{abstract}</AbstractText></Abstract></Article>"
        "</MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )


def test_pubmed_abstract_evidence_capped():
    es = {"esearchresult": {"idlist": ["111"]}}
    su = {"result": {"111": {"title": "A study", "authors": [{"name": "Doe J"}],
                             "pubdate": "2020", "articleids": []}}}
    long_abstract = "Magnesium optimum was determined. " + "word " * 400  # > cap
    hits = literature.search_pubmed(
        "mg", client=FakeHttpAbstracts(es, su, _pubmed_xml("111", long_abstract)))
    ev = hits[0]["evidence"]
    assert ev["evidence_type"] == "abstract"
    assert ev["source_type"] == "peer_reviewed"
    assert 0 < len(ev["excerpt"]) <= literature.EVIDENCE_EXCERPT_CAP + 1


def test_pubmed_metadata_only_when_no_abstract():
    es = {"esearchresult": {"idlist": ["111"]}}
    su = {"result": {"111": {"title": "A study", "authors": [{"name": "Doe J"}],
                             "pubdate": "2020", "articleids": []}}}
    # existing FakeHttp answers efetch with a JSON body (no `.text`), so the abstract
    # fetch degrades cleanly to metadata_only rather than fabricating text.
    hits = literature.search_pubmed("mg", client=FakeHttp(es, su))
    ev = hits[0]["evidence"]
    assert ev["evidence_type"] == "metadata_only"
    assert ev["excerpt"] == ""


def test_preprints_evidence_preprint_source():
    payload = {"resultList": {"result": [
        {"source": "PPR", "title": "A preprint on CFPS.", "authorString": "Smith J, Doe A.",
         "pubYear": "2023", "doi": "10.1101/2023.01.01.123",
         "abstractText": "We show cell-free protein synthesis is optimal at 2 mM Mg."},
        {"source": "PPR", "title": "", "authorString": ""},  # dropped
    ]}}
    http = SimpleHttp(payload)
    hits = literature.search_preprints("cfps", client=http)
    assert len(hits) == 1
    ev = hits[0]["evidence"]
    assert ev["source_type"] == "preprint"
    assert ev["excerpt"]  # non-empty
    # existing assertions still hold
    assert hits[0]["doi"] == "10.1101/2023.01.01.123"
    assert "SRC:PPR" in http.calls[0][1]["query"]


def test_protocols_evidence_protocol_source():
    from app import config
    old = config.PROTOCOLS_IO_TOKEN
    config.PROTOCOLS_IO_TOKEN = "tok"
    try:
        payload = {"items": [
            {"title": "S30 extract prep", "doi": "10.17504/protocols.io.abc", "uri": "s30",
             "authors": [{"name": "Jewett M"}], "published_on": 1600000000,
             "description": "Prepare an S30 cell extract from BL21 for CFPS."},
            {"title": "No description protocol", "doi": "10.17504/protocols.io.xyz", "uri": "nod",
             "authors": [{"name": "Roe R"}], "published_on": 1600000000},
        ]}
        hits = literature.search_protocols("s30", client=SimpleHttp(payload))
        assert hits[0]["evidence"]["source_type"] == "protocol"
        assert hits[0]["evidence"]["excerpt"]  # non-empty
        assert hits[1]["evidence"]["evidence_type"] == "metadata_only"
        assert hits[1]["evidence"]["excerpt"] == ""
    finally:
        config.PROTOCOLS_IO_TOKEN = old


def test_formatter_surfaces_excerpt():
    hit = {"pmid": "111", "title": "T", "authors": "Doe J", "year": 2020, "doi": None,
           "evidence": {"excerpt": "Optimal at 2 mM magnesium chloride.", "section": "Abstract",
                        "evidence_type": "abstract", "source_type": "peer_reviewed"}}
    out = literature.format_results([hit])
    assert "2 mM magnesium" in out
    assert "evidence" in out

    hit_meta = {"pmid": "222", "title": "T2", "authors": "Roe R", "year": 2019, "doi": None,
                "evidence": {"excerpt": "", "section": None,
                             "evidence_type": "metadata_only", "source_type": "peer_reviewed"}}
    out2 = literature.format_results([hit_meta])
    assert "metadata only" in out2


def test_evidence_never_fabricated():
    # PubMed metadata-only (FakeHttp efetch has no XML .text -> {}).
    es = {"esearchresult": {"idlist": ["1"]}}
    su = {"result": {"1": {"title": "A", "authors": [{"name": "D"}], "pubdate": "2020",
                           "articleids": []}}}
    ph = literature.search_pubmed("x", client=FakeHttp(es, su))
    assert ph[0]["evidence"]["excerpt"] == ""

    # Preprint with no abstractText.
    pp = literature.search_preprints("x", client=SimpleHttp(
        {"resultList": {"result": [
            {"source": "PPR", "title": "P", "authorString": "A B.", "pubYear": "2022", "doi": "10.1/z"}]}}))
    assert pp[0]["evidence"]["excerpt"] == ""

    # Protocol with no description.
    from app import config
    old = config.PROTOCOLS_IO_TOKEN
    config.PROTOCOLS_IO_TOKEN = "tok"
    try:
        pr = literature.search_protocols("x", client=SimpleHttp(
            {"items": [{"title": "S", "doi": "10.1/q", "uri": "s",
                        "authors": [{"name": "J"}], "published_on": 1600000000}]}))
        assert pr[0]["evidence"]["excerpt"] == ""
    finally:
        config.PROTOCOLS_IO_TOKEN = old


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
