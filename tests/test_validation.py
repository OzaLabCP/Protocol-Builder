"""Unit tests for the host-side validation logic (no network — the resolver is
injected)."""

from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.resolvers import ResolvedCitation, classify_identifier, resolve_doi  # noqa: E402
from app.validation import (  # noqa: E402
    _evidence_relevant,
    validate_and_finalize,
    validate_assay_options,
    validate_design_alignment,
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


def test_consistency_lines_do_not_accumulate_across_runs():
    # Re-validating the same protocol must not stack duplicate consistency lines in
    # open_questions (regression: they were appended unconditionally, unlike gate lines).
    mat = {"name": "Magnesium glutamate", "provenance": "best_practice", "citation": None}
    p = base_protocol(materials=[mat], assumptions_log=[])
    validate_and_finalize(p, fake_resolver({}))
    oq1 = list(p["open_questions"])
    validate_and_finalize(p, fake_resolver({}))
    assert p["open_questions"] == oq1  # no accumulation on the second run
    n = sum(1 for q in p["open_questions"] if "missing from the assumptions_log" in q)
    assert n == 1


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


def test_malformed_entries_are_dropped_not_crashed():
    # a model can emit a bare string where a material object is expected — must not crash.
    from app.render import materials_to_csv, protocol_to_markdown
    p = base_protocol(materials=[
        "BsaI enzyme",  # malformed: a string, not an object
        {"name": "T4 ligase", "provenance": "best_practice"},
    ], steps=[
        {"number": 1, "title": "Digest", "provenance": "stated",
         "critical_parameters": ["oops-not-an-object", {"name": "temp", "provenance": "stated"}]},
        "not-a-step",
    ])
    report = validate_and_finalize(p, fake_resolver({}))
    assert report["malformed_dropped"] == 3  # 1 material + 1 cp + 1 step
    assert [m["name"] for m in p["materials"]] == ["T4 ligase"]
    assert len(p["steps"]) == 1 and p["steps"][0]["critical_parameters"][0]["name"] == "temp"
    # render + csv must also survive the (now-clean) protocol
    assert "T4 ligase" in protocol_to_markdown(p)
    assert "T4 ligase" in materials_to_csv(p)
    assert any("malformed" in q for q in p["open_questions"])


def test_validate_correctness_review_normalizes_and_verifies():
    from app.validation import validate_correctness_review
    review = {
        "verdict": "sound",  # inconsistent: findings present -> must be corrected
        "summary": "s",
        "findings": [
            {"severity": "minor", "category": "logic", "problem": "p1", "fix": "f1"},
            {"severity": "bogus", "category": "ordering", "problem": "p2", "fix": "f2"},  # -> major
            {"severity": "critical", "category": "unit_or_scaling", "problem": "p3", "fix": "f3",
             "citation": cite("99999999")},  # unresolvable -> citation nulled, finding kept
            "not-a-dict",  # dropped
        ],
    }
    report = validate_correctness_review(review, fake_resolver({}))
    sev = [f["severity"] for f in review["findings"]]
    assert sev == ["critical", "major", "minor"]  # sorted, bogus coerced to major
    assert review["findings"][0]["citation"] is None and review["findings"][0]["citation_verified"] is False
    assert review["verdict"] == "serious_issues"  # has a critical -> upgraded from bogus "sound"
    assert report["citations_checked"] == 1 and len(report["downgraded"]) == 1


def test_validate_correctness_review_sound_when_empty():
    from app.validation import validate_correctness_review
    review = {"verdict": "issues_found", "summary": "s", "findings": []}
    validate_correctness_review(review, fake_resolver({}))
    assert review["verdict"] == "sound"


def _alignment(**over):
    a = {
        "hypothesis": {"statement": "X increases Y", "prediction_if_true": "up",
                       "prediction_if_false": "flat"},
        "inferred": False,
        "directly_tests": {"verdict": "yes", "rationale": "r"},
        "critical_comparison": "X vs no-X",
        "alignment_gaps": [],
        "confounds": [],
        "recommended_changes": [],
        "summary": "ok",
    }
    a.update(over)
    return a


def test_alignment_inferred_set_from_host_truth_not_model():
    # model claims the student stated it; the host knows none was supplied -> corrected.
    a = _alignment(inferred=False)
    report = validate_design_alignment(a, hypothesis_supplied=False)
    assert a["inferred"] is True
    assert report["inferred_corrected"] is True
    # and the reverse: a supplied hypothesis is never labelled inferred
    b = _alignment(inferred=True)
    r2 = validate_design_alignment(b, hypothesis_supplied=True)
    assert b["inferred"] is False and r2["inferred_corrected"] is True


def test_alignment_verdict_enum_coerced():
    a = _alignment(directly_tests={"verdict": "maybe", "rationale": "r"},
                   alignment_gaps=[{"gap": "g", "why_it_breaks_the_test": "w"}])
    report = validate_design_alignment(a, hypothesis_supplied=True)
    assert a["directly_tests"]["verdict"] == "partial"
    assert any("verdict" in n for n in report["normalized"])


def test_alignment_change_type_enum_coerced():
    a = _alignment(recommended_changes=[{"change": "do X", "addresses": "g", "type": "bogus"}])
    validate_design_alignment(a, hypothesis_supplied=True)
    assert a["recommended_changes"][0]["type"] == "other"


def test_alignment_flags_nonpassing_verdict_with_no_gaps():
    a = _alignment(directly_tests={"verdict": "no", "rationale": "r"}, alignment_gaps=[])
    report = validate_design_alignment(a, hypothesis_supplied=True)
    assert any("no alignment_gaps" in s for s in report["inconsistencies"])


def test_alignment_flags_yes_verdict_that_still_lists_problems():
    a = _alignment(
        directly_tests={"verdict": "yes", "rationale": "r"},
        alignment_gaps=[{"gap": "g", "why_it_breaks_the_test": "w"}],
        confounds=[{"confound": "c", "makes_result_ambiguous": "m", "mitigation": None}],
    )
    report = validate_design_alignment(a, hypothesis_supplied=True)
    assert any("verdict 'yes'" in s for s in report["inconsistencies"])


def test_alignment_clean_yes_has_no_inconsistencies():
    a = _alignment()  # verdict yes, no gaps, no confounds
    report = validate_design_alignment(a, hypothesis_supplied=True)
    assert report["inconsistencies"] == [] and report["normalized"] == []


def test_alignment_defensive_list_coercion():
    a = _alignment(alignment_gaps=None, confounds="oops", recommended_changes=None,
                   directly_tests={"verdict": "yes", "rationale": "r"})
    validate_design_alignment(a, hypothesis_supplied=True)
    assert a["alignment_gaps"] == [] and a["confounds"] == [] and a["recommended_changes"] == []


# --- Epic 1: claim-support status (evidence relevance) ---------------------

def _matched_resolver():
    return fake_resolver(
        {"12345678": ResolvedCitation("12345678", "pmid", "A study of X", 2020, "pubmed")}
    )


def _lit_material(citation, value="2", unit="mM", name="Mg"):
    return {"name": name, "value": value, "unit": unit,
            "provenance": "literature_grounded", "citation": citation}


def test_unrelated_real_citation_cannot_be_supported():
    resolver = _matched_resolver()

    # sub-case A: the citation resolves + metadata matches, but NO evidence is attached.
    c = cite("12345678")
    c["evidence"] = None
    mat = _lit_material(c)
    p = base_protocol(materials=[mat])
    validate_and_finalize(p, resolver)
    assert mat["claim_support_status"] == "evidence_unavailable"
    assert mat["claim_support_status"] != "supported"
    assert mat["provenance"] == "literature_grounded"
    assert mat["identifier_verified"] is True
    assert mat["metadata_matched"] is True
    assert mat["citation_verified"] is True

    # sub-case B: evidence present, evidence_type="abstract", excerpt does NOT contain "2".
    c2 = cite("12345678")
    c2["evidence"] = {"excerpt": "The optimal magnesium concentration was five millimolar.",
                      "section": "Abstract", "evidence_type": "abstract",
                      "source_type": "peer_reviewed"}
    mat2 = _lit_material(c2)
    p2 = base_protocol(materials=[mat2])
    validate_and_finalize(p2, resolver)
    assert mat2["claim_support_status"] == "evidence_unavailable"
    assert mat2["claim_support_status"] != "supported"
    assert mat2["provenance"] == "literature_grounded"
    assert mat2["identifier_verified"] is True
    assert mat2["metadata_matched"] is True


def test_evidence_relevance_step_substep_not_vacuous():
    # Regression: a step/substep stores its text in title/instruction (not name), and has no
    # scalar value — so an UNRELATED excerpt must NOT vacuously "support" it.
    unrelated = {"evidence_type": "abstract", "source_type": "peer_reviewed",
                 "excerpt": "The weather in Paris was sunny and pleasant throughout the afternoon."}
    relevant = {"evidence_type": "abstract", "source_type": "peer_reviewed",
                "excerpt": "Incubate the reaction mixture for 30 minutes at 37 C."}
    step = {"title": "Incubate the reaction mixture", "provenance": "literature_grounded"}
    substep = {"number": "3a", "instruction": "Add the quench buffer to each well"}
    assert _evidence_relevant(step, unrelated) is False          # was True (vacuous) before the fix
    assert _evidence_relevant(step, relevant) is True            # genuinely relevant
    assert _evidence_relevant(substep, {"evidence_type": "abstract", "source_type": "other",
                                        "excerpt": "quench the reaction with EDTA buffer"}) is True
    assert _evidence_relevant({}, unrelated) is False            # no descriptive text -> not supported
    assert _evidence_relevant(step, {"evidence_type": "metadata_only", "source_type": "other",
                                     "excerpt": ""}) is False    # metadata-only never supports


def test_metadata_only_evidence_not_supported():
    c = cite("12345678")
    c["evidence"] = {"excerpt": "", "section": None,
                     "evidence_type": "metadata_only", "source_type": "peer_reviewed"}
    mat = _lit_material(c)
    p = base_protocol(materials=[mat])
    validate_and_finalize(p, _matched_resolver())
    assert mat["claim_support_status"] == "evidence_unavailable"
    assert mat["metadata_matched"] is True


def test_abstract_supported():
    c = cite("12345678")
    c["evidence"] = {"excerpt": "Reactions were optimal at 2 mM Mg2+ in the assay buffer.",
                     "section": "Abstract", "evidence_type": "abstract",
                     "source_type": "peer_reviewed"}
    mat = _lit_material(c)
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, _matched_resolver())
    assert mat["claim_support_status"] == "supported"
    assert mat["provenance"] == "literature_grounded"
    assert mat["citation_verified"] is True
    assert "materials[0] 'Mg'" in report["support"]["supported"]


def test_unresolved_identifier_fields():
    c = cite("99999999")
    c["evidence"] = None
    mat = _lit_material(c)
    p = base_protocol(materials=[mat])
    validate_and_finalize(p, fake_resolver({}))  # resolves to nothing
    assert mat["identifier_verified"] is False
    assert mat["metadata_matched"] is False
    assert mat["claim_support_status"] == "unchecked"
    assert mat["provenance"] == "default_verify"
    assert mat["citation"] is None


def test_mismatched_work_fields():
    # resolves to a real record, but a clearly different work (title + year mismatch).
    c = cite("12345678", title="An unrelated paper about zebrafish", year=1990)
    mat = _lit_material(c)
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, _matched_resolver())
    assert mat["identifier_verified"] is True
    assert mat["metadata_matched"] is False
    assert mat["claim_support_status"] == "mismatch"
    assert mat["provenance"] == "default_verify"
    assert mat["citation"] is None
    assert "materials[0] 'Mg'" in report["support"]["mismatch"]


def test_citation_verified_alias_semantics():
    resolver = _matched_resolver()
    matched = _lit_material(cite("12345678"), name="Mg")
    mismatched = _lit_material(cite("12345678", title="unrelated", year=1990), name="K", value="5")
    p = base_protocol(materials=[matched, mismatched])
    validate_and_finalize(p, resolver)
    # matched entry -> alias True
    assert matched["citation_verified"] is True
    assert matched["citation_verified"] == bool(
        matched["identifier_verified"] and matched["metadata_matched"])
    # mismatched entry -> alias False
    assert mismatched["citation_verified"] is False
    assert mismatched["citation_verified"] == bool(
        mismatched["identifier_verified"] and mismatched["metadata_matched"])
    # unresolved entry -> alias False
    unresolved = _lit_material(cite("00000000"), name="Na", value="1")
    p2 = base_protocol(materials=[unresolved])
    validate_and_finalize(p2, resolver)
    assert unresolved["citation_verified"] is False
    assert unresolved["citation_verified"] == bool(
        unresolved["identifier_verified"] and unresolved["metadata_matched"])


def test_backcompat_old_json_no_evidence():
    # a citation with no `evidence` key at all (old payload) that resolves + matches.
    c = cite("12345678")  # note: no evidence key
    assert "evidence" not in c
    mat = _lit_material(c)
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, _matched_resolver())  # must not raise
    assert mat["claim_support_status"] == "evidence_unavailable"
    assert mat["claim_support_status"] != "supported"
    assert report["support"]["supported"] == []


# --- Epic 2: deterministic quality gate (integrated path) ------------------

def _qg_cp(name, value, unit=None):
    return {"name": name, "value": value, "unit": unit, "provenance": "stated"}


def _dilution_step(v1_value):
    """A C1V1=C2V2 dilution step. Correct transfer volume is 10 uL
    (C1=100 mM, C2=10 mM, V2=100 uL -> V1 = C2*V2/C1 = 10 uL)."""
    return {"number": 1, "title": "Dilute", "instruction": "dilute",
            "provenance": "stated", "critical_parameters": [
                _qg_cp("stock concentration", "100", "mM"),
                _qg_cp("final concentration", "10", "mM"),
                _qg_cp("final volume", "100", "uL"),
                _qg_cp("transfer volume", v1_value, "uL"),
            ]}


def test_gate_correct_protocol_is_ok():
    p = base_protocol(steps=[_dilution_step("10")],  # correct dilution
                      conditions=4, replicates=3, plate="96-well")  # 12 <= 96
    report = validate_and_finalize(p, fake_resolver({}))
    gate = report["quality_gate"]
    assert gate["status"] == "ok"
    assert gate["counts"]["errors"] == 0
    assert gate["errors"] == []
    assert not any(q.startswith("[QUALITY GATE] ") for q in p["open_questions"])
    assert report["id_scheme"] == "structural-path-v1"


def test_gate_wrong_dilution_is_blocked():
    p = base_protocol(steps=[_dilution_step("50")])  # 50 uL, should be 10 uL
    report = validate_and_finalize(p, fake_resolver({}))
    gate = report["quality_gate"]
    assert gate["status"] == "blocked"
    assert gate["counts"]["errors"] >= 1
    assert any(f["code"] == "DIL_MISMATCH" for f in gate["errors"])
    assert any(q.startswith("[QUALITY GATE] ") for q in p["open_questions"])


def test_gate_plate_over_capacity_is_blocked():
    p = base_protocol(conditions=20, replicates=30, plate="384-well")  # 600 > 384
    report = validate_and_finalize(p, fake_resolver({}))
    gate = report["quality_gate"]
    assert gate["status"] == "blocked"
    over = [f for f in gate["errors"] if f["code"] == "PLATE_OVER_CAPACITY"]
    assert len(over) == 1
    assert over[0]["actual"] == 600 and over[0]["expected"] == 384


def test_gate_missing_unit_is_warning_not_error():
    mat = {"name": "salt", "amount": 5, "unit": None, "provenance": "stated"}
    p = base_protocol(materials=[mat])
    report = validate_and_finalize(p, fake_resolver({}))
    gate = report["quality_gate"]
    assert gate["counts"]["errors"] == 0
    assert gate["counts"]["warnings"] >= 1
    assert gate["status"] == "warnings"
    assert any(f["code"] == "UNIT_MISSING" for f in gate["warnings"])
    assert not any(q.startswith("[QUALITY GATE] ") for q in p["open_questions"])


def test_gate_missing_mw_is_assumption_not_error():
    step = {"number": 1, "title": "Weigh", "instruction": "weigh",
            "provenance": "stated", "critical_parameters": [
                _qg_cp("concentration", "10", "mM"),
                _qg_cp("volume", "100", "uL"),
                _qg_cp("mass", "5", "mg")]}  # mass, but no molecular weight
    p = base_protocol(steps=[step])
    report = validate_and_finalize(p, fake_resolver({}))
    gate = report["quality_gate"]
    assert gate["counts"]["errors"] == 0
    assert gate["status"] in ("ok", "warnings")  # never blocked
    mw = [f for f in gate["assumptions"] if f["code"] == "MOLAR_NO_MW"]
    assert len(mw) == 1 and mw[0]["severity"] == "assumption"
    # projected into the human-facing log with a non-empty rationale and matching id
    projected = [a for a in report["assumptions"] if a["id"] == mw[0]["id"]]
    assert projected and projected[0]["why"]


def test_gate_is_idempotent():
    p = base_protocol(steps=[_dilution_step("50")])  # a blocking protocol
    validate_and_finalize(p, fake_resolver({}))
    gate1 = copy.deepcopy(p["validation_report"]["quality_gate"])
    ids1 = [s["_id"] for s in p["steps"]] + \
           [cp["_id"] for cp in p["steps"][0]["critical_parameters"]]
    oq1 = list(p["open_questions"])
    validate_and_finalize(p, fake_resolver({}))
    gate2 = p["validation_report"]["quality_gate"]
    ids2 = [s["_id"] for s in p["steps"]] + \
           [cp["_id"] for cp in p["steps"][0]["critical_parameters"]]
    assert gate1 == gate2                                   # identical gate
    assert ids1 == ids2                                     # identical structural ids
    assert oq1 == p["open_questions"]                       # no accumulation / drift
    assert "_assumption_keys" not in p["validation_report"]  # scratch state never ships


def test_gate_blocked_report_yields_blocked_summary():
    from app.server import _validation_summary_from_report
    p = base_protocol(steps=[_dilution_step("50")])  # blocking dilution
    report = validate_and_finalize(p, fake_resolver({}))
    summary = _validation_summary_from_report(report)
    assert summary.status == "blocked"
    assert summary.error_count >= 1


# --- Epic 2 §III: host-generated canonical assumptions log + comparison --------

def _inline_cp_step(name, value, unit=None, provenance="best_practice", **extra):
    cp = {"name": name, "value": value, "unit": unit, "provenance": provenance}
    cp.update(extra)
    return {"number": 1, "title": "Add", "instruction": "add", "provenance": "stated",
            "critical_parameters": [cp]}


def _alog_disagreements(report):
    return report["consistency"]["disagreements"]


def test_host_assumptions_log_is_built_and_attached():
    # the host derives its OWN canonical log from inline non-stated entries.
    mat = {"name": "Magnesium", "value": "2", "unit": "mM", "provenance": "best_practice",
           "selected_by_user": False}
    p = base_protocol(materials=[mat], assumptions_log=[])
    report = validate_and_finalize(p, fake_resolver({}))
    log = report["host_assumptions_log"]
    assert p["host_assumptions_log"] == log            # attached to the protocol too
    assert len(log) == 1
    row = log[0]
    assert row["parameter"] == "Magnesium" and row["value"] == "2" and row["unit"] == "mM"
    assert row["provenance"] == "best_practice"
    assert row["_id"] == mat["_id"] and row["stable_id"] == mat["material_id"]


def test_host_log_equals_model_log_when_consistent():
    # a model assumptions_log that agrees with the inline entry -> NO disagreements.
    step = _inline_cp_step("Mg concentration", "2", "mM", selected_by_user=False)
    p = base_protocol(
        steps=[step],
        assumptions_log=[{"parameter": "Mg concentration", "value": "2", "unit": "mM",
                          "provenance": "best_practice", "selected_by_user": False,
                          "verify": True}])
    report = validate_and_finalize(p, fake_resolver({}))
    assert _alog_disagreements(report) == []           # canonical == model log


def test_log_value_disagreement_detected():
    # inline 10 mM vs model log 100 mM for the SAME parameter -> ALOG_VALUE_MISMATCH (error).
    step = _inline_cp_step("Mg concentration", "10", "mM")
    p = base_protocol(
        steps=[step],
        assumptions_log=[{"parameter": "Mg concentration", "value": "100", "unit": "mM",
                          "provenance": "best_practice"}])
    report = validate_and_finalize(p, fake_resolver({}))
    dis = _alog_disagreements(report)
    vm = [d for d in dis if d["code"] == "ALOG_VALUE_MISMATCH"]
    assert len(vm) == 1
    assert vm[0]["severity"] == "error"
    assert str(vm[0]["host"]) == "10" and str(vm[0]["model"]) == "100"
    assert vm[0]["id"] == step["critical_parameters"][0]["_id"]  # anchored on the inline entry


def test_log_empty_value_is_unverifiable_not_blocker():
    step = _inline_cp_step("Mg concentration", "10", "mM")
    p = base_protocol(
        steps=[step],
        assumptions_log=[{"parameter": "Mg concentration", "value": "", "unit": "mM",
                          "provenance": "best_practice"}])
    report = validate_and_finalize(p, fake_resolver({}))
    dis = _alog_disagreements(report)
    assert any(d["code"] == "ALOG_VALUE_UNVERIFIABLE" and d["severity"] == "warning" for d in dis)
    assert all(d["code"] != "ALOG_VALUE_MISMATCH" for d in dis)  # empty -> never a blocker


def test_log_provenance_mismatch_detected():
    step = _inline_cp_step("temp", "30", "C", provenance="best_practice")
    p = base_protocol(
        steps=[step],
        assumptions_log=[{"parameter": "temp", "value": "30", "unit": "C",
                          "provenance": "default_verify"}])
    report = validate_and_finalize(p, fake_resolver({}))
    pm = [d for d in _alog_disagreements(report) if d["code"] == "ALOG_PROVENANCE_MISMATCH"]
    assert len(pm) == 1 and pm[0]["severity"] == "warning"
    assert pm[0]["host"] == "best_practice" and pm[0]["model"] == "default_verify"


def test_citation_identifier_mismatch_detected():
    # A literature_grounded inline value whose log copy claims a DIFFERENT citation
    # identifier -> ALOG_CITATION_MISMATCH (error). Exercised on the pure host functions
    # so neither identifier is nulled by resolution (the mismatch is what we assert).
    from app.checks import (assign_stable_ids, compare_assumptions_log, ensure_ids,
                            host_assumptions_log)
    p = base_protocol(
        materials=[{"name": "Mg", "value": "2", "unit": "mM",
                    "provenance": "literature_grounded",
                    "citation": {"identifier": "12345678"}}],
        assumptions_log=[{"parameter": "Mg", "value": "2", "unit": "mM",
                          "provenance": "literature_grounded",
                          "citation": {"identifier": "99999999"}}])
    ensure_ids(p); assign_stable_ids(p)
    dis = compare_assumptions_log(p, host_assumptions_log(p))
    cm = [d for d in dis if d["code"] == "ALOG_CITATION_MISMATCH"]
    assert len(cm) == 1 and cm[0]["severity"] == "error"
    assert cm[0]["host"] == "12345678" and cm[0]["model"] == "99999999"


def test_consistency_legacy_keys_preserved_on_clean_protocol():
    # the pre-change name-based keys stay present and empty on a consistent protocol.
    step = _inline_cp_step("Mg concentration", "2", "mM", selected_by_user=False)
    p = base_protocol(
        steps=[step],
        assumptions_log=[{"parameter": "Mg concentration", "value": "2", "unit": "mM",
                          "provenance": "best_practice", "selected_by_user": False}])
    report = validate_and_finalize(p, fake_resolver({}))
    assert report["consistency"]["inline_missing_from_log"] == []
    assert report["consistency"]["log_missing_from_inline"] == []


# --- Epic 2 §II: revision-stable ids (integrated through validate_and_finalize) -

def test_stable_ids_additive_positional_id_intact():
    mat = {"name": "Tris", "provenance": "stated"}
    step = {"number": 1, "title": "Mix reagents", "instruction": "mix",
            "provenance": "stated", "critical_parameters": [
                {"name": "temp", "value": "30", "unit": "C", "provenance": "stated"}]}
    p = base_protocol(materials=[mat], steps=[step])
    validate_and_finalize(p, fake_resolver({}))
    # additive stable fields present AND positional _id unchanged.
    assert mat["_id"] == "mat:0" and mat["material_id"].startswith("m_")
    assert step["_id"] == "step:0" and step["step_id"].startswith("s_")
    cp = step["critical_parameters"][0]
    assert cp["_id"] == "step:0/param:0" and cp["parameter_id"].startswith("p_")


def test_stable_id_survives_simulated_revision():
    # capture a step's stable id, prepend an unrelated step (its positional _id shifts),
    # re-finalize -> the stable id is unchanged while the positional _id moved.
    step = {"number": 1, "title": "Mix reagents", "instruction": "mix",
            "provenance": "stated", "critical_parameters": []}
    mat = {"name": "Tris", "provenance": "stated"}
    p = base_protocol(materials=[mat], steps=[step])
    validate_and_finalize(p, fake_resolver({}))
    step_id_before = step["step_id"]
    id_before = step["_id"]
    mat_id_before = mat["material_id"]

    p["steps"].insert(0, {"number": 0, "title": "Warm up the incubator",
                          "instruction": "warm", "provenance": "stated",
                          "critical_parameters": []})
    validate_and_finalize(p, fake_resolver({}))
    moved = [s for s in p["steps"] if s["title"] == "Mix reagents"][0]
    assert moved["step_id"] == step_id_before          # content-derived id is stable
    assert moved["_id"] != id_before                    # positional id moved (step:0 -> step:1)
    assert mat["material_id"] == mat_id_before           # material id unchanged too


def test_assign_stable_ids_idempotent_through_finalize():
    p = base_protocol(
        materials=[{"name": "Tris", "provenance": "stated"}],
        steps=[{"number": 1, "title": "Mix", "instruction": "mix", "provenance": "stated",
                "critical_parameters": []}])
    validate_and_finalize(p, fake_resolver({}))
    ids1 = (p["materials"][0]["material_id"], p["steps"][0]["step_id"])
    validate_and_finalize(p, fake_resolver({}))
    ids2 = (p["materials"][0]["material_id"], p["steps"][0]["step_id"])
    assert ids1 == ids2
    assert report_scheme(p) == "content-hash-v1"


def report_scheme(p):
    return p["validation_report"]["stable_id_scheme"]


def test_duplicate_names_disambiguate_deterministically():
    from app.checks import assign_stable_ids
    p = {"materials": [{"name": "buffer"}, {"name": "buffer"}, {"name": "buffer"}]}
    assign_stable_ids(p)
    ids = [m["material_id"] for m in p["materials"]]
    assert ids[0].startswith("m_")
    assert ids[1] == ids[0] + "_1"
    assert ids[2] == ids[0] + "_2"
    assert len(set(ids)) == 3


# --- Epic 2 §I: missing step instruction -> repaired + blocked, never silent ----

def test_missing_step_instruction_is_repaired_and_blocked():
    from app.models import STEP_INSTR_PLACEHOLDER
    from app.render import protocol_to_markdown
    step = {"number": 1, "title": "Mystery step", "instruction": "",
            "provenance": "stated", "critical_parameters": []}
    p = base_protocol(steps=[step])
    report = validate_and_finalize(p, fake_resolver({}))
    # repaired to the frozen sentinel (never left blank).
    assert step["instruction"] == STEP_INSTR_PLACEHOLDER
    # a loud STRUCT blocker -> gate blocked (a repaired protocol can't ship clean).
    gate = report["quality_gate"]
    assert gate["status"] == "blocked" and gate["status_label"] == "blocked"
    assert any(f["code"] == "STRUCT_STEP_NO_INSTRUCTION" for f in gate["errors"])
    # a BLOCKED open_question was appended (loud, not silent).
    assert any(q.startswith("[BLOCKED] ") for q in p["open_questions"])
    # the renderer shows a visible marker, never a blank step.
    assert "MISSING INSTRUCTION" in protocol_to_markdown(p)


def test_gate_status_label_ready_on_clean_protocol():
    p = base_protocol(steps=[_dilution_step("10")],  # correct dilution
                      conditions=4, replicates=3, plate="96-well")
    report = validate_and_finalize(p, fake_resolver({}))
    assert report["quality_gate"]["status"] == "ok"
    assert report["quality_gate"]["status_label"] == "ready"


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
