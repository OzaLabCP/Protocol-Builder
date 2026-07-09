"""Unit tests for the host-side validation logic (no network — the resolver is
injected)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.resolvers import ResolvedCitation, classify_identifier, resolve_doi  # noqa: E402
from app.validation import (  # noqa: E402
    validate_and_finalize,
    validate_assay_options,
    validate_design_review,
)


class _DoiResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _DoiClient:
    """Crossref 404 -> DataCite fallback, keyed by URL host."""

    def __init__(self, crossref_status, crossref_payload, datacite_status, datacite_payload):
        self.args = (crossref_status, crossref_payload, datacite_status, datacite_payload)

    def get(self, url, params=None):
        cs, cp, ds, dp = self.args
        if "crossref" in url:
            return _DoiResp(cs, cp)
        return _DoiResp(ds, dp)


def test_doi_datacite_fallback_when_crossref_404s():
    dc = {"data": {"attributes": {"titles": [{"title": "A protocols.io protocol"}], "publicationYear": 2021}}}
    client = _DoiClient(404, {}, 200, dc)
    resolved = resolve_doi("10.17504/protocols.io.abc", client)
    assert resolved is not None
    assert resolved.source == "datacite"
    assert resolved.year == 2021
    assert resolved.title == "A protocols.io protocol"


def test_doi_crossref_preferred_when_present():
    cr = {"message": {"title": ["A journal article"], "issued": {"date-parts": [[2019]]}}}
    client = _DoiClient(200, cr, 404, {})
    resolved = resolve_doi("10.1/journal", client)
    assert resolved.source == "crossref"
    assert resolved.year == 2019


def fake_resolver(db):
    def _resolve(identifier):
        return db.get(identifier)
    return _resolve


def base_protocol(**over):
    p = {
        "title": "T",
        "summary": "S",
        "estimated_duration": "2 h",
        "materials": [],
        "steps": [],
        "assumptions_log": [],
        "open_questions": [],
    }
    p.update(over)
    return p


def cite(identifier="12345678", title="A study of X", year=2020):
    return {"title": title, "authors": "Doe J", "year": year, "identifier": identifier, "url": None}


# --- classify --------------------------------------------------------------

def test_classify_identifier():
    assert classify_identifier("12345678") == "pmid"
    assert classify_identifier("PMID: 987654") == "pmid"
    assert classify_identifier("10.1000/abc.def") == "doi"
    assert classify_identifier("doi:10.1000/xyz") == "doi"
    assert classify_identifier("not-an-id") is None
    assert classify_identifier("") is None


# --- resolution ------------------------------------------------------------

def test_verified_citation_marked_and_url_filled():
    mat = {"name": "Mg", "provenance": "literature_grounded", "citation": cite("12345678")}
    p = base_protocol(materials=[mat])
    resolver = fake_resolver({"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")})
    report = validate_and_finalize(p, resolver)
    assert report["citations_checked"] == 1
    assert len(report["verified"]) == 1
    assert mat["citation_verified"] is True
    assert mat["provenance"] == "literature_grounded"
    assert mat["citation"]["url"].endswith("/12345678/")


def test_unresolvable_citation_downgraded():
    mat = {"name": "Mg", "provenance": "literature_grounded", "citation": cite("99999999")}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}))  # resolves to nothing
    assert len(report["downgraded"]) == 1
    assert mat["provenance"] == "default_verify"
    assert mat["citation"] is None
    assert mat["citation_verified"] is False
    assert any("could not be verified" in q for q in p["open_questions"])


def test_literature_grounded_without_citation_is_downgraded():
    mat = {"name": "K", "provenance": "literature_grounded", "citation": None}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}))
    assert mat["provenance"] == "default_verify"
    assert any(f["fix"].startswith("downgraded") for f in report["invariant_fixes"])
    assert report["citations_checked"] == 0  # nothing to resolve


def test_stray_citation_on_best_practice_stripped():
    mat = {"name": "K", "provenance": "best_practice", "citation": cite("12345678"), "selected_by_user": False}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")}))
    assert mat["citation"] is None
    assert mat["provenance"] == "best_practice"  # not downgraded, just stripped
    assert any("stripped" in f["fix"] for f in report["invariant_fixes"])


def test_user_selected_literature_option_keeps_citation():
    cp = {
        "name": "folding aid",
        "value": "DsbC",
        "provenance": "literature_grounded",
        "selected_by_user": True,
        "citation": cite("12345678"),
    }
    p = base_protocol(steps=[{"number": 1, "title": "fold", "instruction": "add", "provenance": "user_input", "critical_parameters": [cp]}])
    report = validate_and_finalize(p, fake_resolver({"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")}))
    assert cp["citation_verified"] is True
    assert cp["provenance"] == "literature_grounded"
    assert len(report["verified"]) == 1


def test_year_mismatch_downgraded():
    mat = {"name": "Mg", "provenance": "literature_grounded", "citation": cite("12345678", year=1999)}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")}))
    assert mat["provenance"] == "default_verify"
    assert any("mismatch" in d["reason"] for d in report["downgraded"])


def test_validate_design_review_verifies_and_downgrades():
    review = {"controls": [
        {"name": "pos", "type": "positive", "rules_out": "assay can't detect",
         "provenance": "literature_grounded",
         "citation": cite("12345678", title="A study of X", year=2020)},
        {"name": "neg", "type": "negative", "rules_out": "background",
         "provenance": "literature_grounded", "citation": cite("99999999")},
        {"name": "veh", "type": "vehicle", "rules_out": "solvent effect",
         "provenance": "best_practice", "citation": None},
    ]}
    resolver = fake_resolver({"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")})
    report = validate_design_review(review, resolver)
    assert [v["control"] for v in report["verified"]] == ["pos"]
    assert [d["control"] for d in report["downgraded"]] == ["neg"]
    assert review["controls"][0]["citation_verified"] is True
    assert review["controls"][1]["provenance"] == "best_practice"
    assert review["controls"][1]["citation"] is None


def test_consistency_flags_inline_value_missing_from_log():
    mat = {"name": "Magnesium glutamate", "provenance": "best_practice", "citation": None}
    p = base_protocol(materials=[mat], assumptions_log=[])
    report = validate_and_finalize(p, fake_resolver({}))
    assert "magnesium glutamate" in report["consistency"]["inline_missing_from_log"]
    assert any("missing from the assumptions_log" in q for q in p["open_questions"])


def test_validate_and_finalize_no_stated_downgrades():
    # a 'stated' material, step, and substep — all must be reclassified with no source
    mat = {"name": "Buffer", "provenance": "stated"}
    step = {"number": 1, "title": "mix", "instruction": "mix", "provenance": "stated",
            "substeps": [{"number": "1.1", "instruction": "pipette", "provenance": "stated"}],
            "critical_parameters": []}
    p = base_protocol(materials=[mat], steps=[step])
    report = validate_and_finalize(p, fake_resolver({}), allow_stated=False)
    assert mat["provenance"] == "default_verify"
    assert step["provenance"] == "default_verify"
    assert step["substeps"][0]["provenance"] == "default_verify"
    assert len(report["stated_downgrades"]) == 3
    assert any("no source document" in q for q in p["open_questions"])

    # same protocol with allow_stated=True keeps the stated tiers
    mat2 = {"name": "Buffer", "provenance": "stated"}
    p2 = base_protocol(materials=[mat2])
    validate_and_finalize(p2, fake_resolver({}), allow_stated=True)
    assert mat2["provenance"] == "stated"


def test_source_quote_verified_when_present_in_source():
    src = "Reactions were incubated at 30 C for 4 hours in a total volume of 50 uL."
    cp = {"name": "temp", "value": "30", "unit": "C", "provenance": "stated",
          "source_quote": "incubated at 30 C"}
    step = {"number": 1, "title": "incubate", "instruction": "incubate", "provenance": "stated",
            "source_quote": "incubated at 30 C for 4 hours", "critical_parameters": [cp]}
    p = base_protocol(steps=[step])
    report = validate_and_finalize(p, fake_resolver({}), source_text=src)
    assert cp["quote_verified"] is True and cp["provenance"] == "stated"
    assert step["quote_verified"] is True
    assert len(report["quotes"]["verified"]) == 2


def test_source_quote_mismatch_downgraded():
    src = "Reactions were incubated at 30 C."
    cp = {"name": "temp", "value": "37", "provenance": "stated", "source_quote": "incubated at 37 C"}
    p = base_protocol(steps=[{"number": 1, "title": "x", "instruction": "x",
                              "provenance": "best_practice", "critical_parameters": [cp]}])
    report = validate_and_finalize(p, fake_resolver({}), source_text=src)
    assert cp["provenance"] == "default_verify"
    assert cp.get("quote_verified") is False  # _downgrade clears the badge
    assert len(report["quotes"]["downgraded"]) == 1
    assert any("was not found verbatim in the source" in q for q in p["open_questions"])


def test_stated_without_quote_downgraded_when_source_present():
    mat = {"name": "buffer", "provenance": "stated"}  # no source_quote
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}), source_text="Reactions used a Tris buffer.")
    assert mat["provenance"] == "default_verify"
    assert len(report["quotes"]["downgraded"]) == 1


def test_stated_preserved_when_no_source_text():
    mat = {"name": "buffer", "provenance": "stated", "source_quote": "a Tris buffer"}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}))  # no source_text -> cannot verify
    assert mat["provenance"] == "stated"  # not downgraded when we can't check
    assert report["quotes"]["source_checked"] is False
    assert report["quotes"]["verified"] == [] and report["quotes"]["downgraded"] == []


def test_quote_present_but_value_unsupported_is_downgraded():
    # the quote is genuinely in the source, but does not contain the value it anchors
    src = "The reaction mixture was prepared and incubated at 30 C in 50 uL."
    cp = {"name": "temp", "value": "37", "provenance": "stated",
          "source_quote": "The reaction mixture was prepared"}
    p = base_protocol(steps=[{"number": 1, "title": "x", "instruction": "x",
                              "provenance": "best_practice", "critical_parameters": [cp]}])
    validate_and_finalize(p, fake_resolver({}), source_text=src)
    assert cp["provenance"] == "default_verify"  # presence != support: 37 not in the quote
    assert cp.get("quote_verified") is False


def test_forged_quote_verified_is_stripped():
    # a model that sets quote_verified itself must not be trusted (host-only attestation)
    mat = {"name": "X", "provenance": "best_practice", "source_quote": "anything", "quote_verified": True}
    p = base_protocol(materials=[mat])
    validate_and_finalize(p, fake_resolver({}))  # no source_text: verification never runs
    assert mat.get("quote_verified") is None  # stripped up front, never re-set


def test_lossy_pdf_source_does_not_accuse():
    # source_exact=False (extracted PDF): a non-match is inconclusive, not fabrication
    src = "reactions were incubated"  # lossy extraction, missing the value
    mat = {"name": "buffer", "provenance": "stated", "source_quote": "50 mM Tris pH 8.0"}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}), source_text=src, source_exact=False)
    assert mat["provenance"] == "stated"  # NOT downgraded on a lossy source
    assert mat.get("quote_verified") is not True  # but no badge either
    assert len(report["quotes"]["unverified"]) == 1 and report["quotes"]["downgraded"] == []


def test_whitespace_source_is_treated_as_no_source():
    mat = {"name": "buffer", "provenance": "stated"}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}), source_text="   \n  ")
    assert mat["provenance"] == "stated"  # blank source can't verify -> don't downgrade
    assert report["quotes"]["source_checked"] is False


def test_validate_assay_options():
    opts = {
        "recommended_assay_id": "ghost",  # dangling -> must be repaired
        "assays": [
            {"id": "fp", "name": "FP", "provenance": "literature_grounded", "citation": cite("12345678")},
            {"id": "bad", "name": "BAD", "provenance": "literature_grounded", "citation": cite("99999999")},
            {"id": "std", "name": "STD", "provenance": "best_practice", "citation": cite("12345678")},
        ],
    }
    resolver = fake_resolver({"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")})
    report = validate_assay_options(opts, resolver)
    fp, bad, std = opts["assays"]
    assert fp["citation_verified"] is True and fp["citation"]["url"].endswith("/12345678/")
    assert bad["provenance"] == "best_practice" and bad["citation"] is None  # unresolved -> downgraded
    assert std["citation"] is None  # stray citation stripped from a best_practice assay
    assert report["recommended_repaired"] is True
    assert opts["recommended_assay_id"] == "fp"  # repaired to a real id


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
