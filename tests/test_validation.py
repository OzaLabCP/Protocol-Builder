"""Unit tests for the host-side validation logic (no network — the resolver is
injected)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.resolvers import ResolvedCitation, classify_identifier  # noqa: E402
from app.validation import validate_and_finalize  # noqa: E402


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


def test_consistency_flags_inline_value_missing_from_log():
    mat = {"name": "Magnesium glutamate", "provenance": "best_practice", "citation": None}
    p = base_protocol(materials=[mat], assumptions_log=[])
    report = validate_and_finalize(p, fake_resolver({}))
    assert "magnesium glutamate" in report["consistency"]["inline_missing_from_log"]
    assert any("missing from the assumptions_log" in q for q in p["open_questions"])


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
