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
