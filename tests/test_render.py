"""Markdown export tests."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.render import (  # noqa: E402
    assay_selection_to_markdown,
    design_alignment_to_markdown,
    design_review_to_markdown,
    grounding_log_to_markdown,
    materials_to_csv,
    protocol_to_markdown,
)

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "binding_assay_eason2020.json",
)


def test_markdown_has_core_sections():
    md = protocol_to_markdown(json.load(open(FIXTURE)))
    assert md.startswith("# ")
    for section in ["## Materials", "## Procedure", "## Assumptions log", "**Provenance:**"]:
        assert section in md, f"missing {section}"


def test_markdown_renders_verified_citation_link():
    proto = {
        "title": "T", "summary": "s", "estimated_duration": "1 h",
        "materials": [{
            "name": "Mg", "amount": 10, "unit": "mM", "provenance": "literature_grounded",
            "citation": {"authors": "Doe J", "year": 2020, "identifier": "12345678",
                         "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/"},
            "citation_verified": True,
        }],
        "steps": [], "assumptions_log": [],
    }
    md = protocol_to_markdown(proto)
    assert "[Doe J 2020 — 12345678](https://pubmed.ncbi.nlm.nih.gov/12345678/)" in md
    assert "✓ verified" in md
    assert "`literature_grounded`" in md


def test_markdown_flags_unverified_literature():
    proto = {
        "title": "T", "summary": "", "estimated_duration": "1 h",
        "materials": [{"name": "X", "provenance": "literature_grounded",
                       "citation": {"authors": "A", "year": 2020, "identifier": "10.1/x", "url": None}}],
        "steps": [], "assumptions_log": [],
    }
    md = protocol_to_markdown(proto)
    assert "⚠ unverified" in md


def test_design_review_markdown_and_schema():
    import json
    from app.schemas import EMIT_DESIGN_REVIEW_TOOL
    assert "$ref" not in json.dumps(EMIT_DESIGN_REVIEW_TOOL)  # citation inlined
    assert "hypothesis" in EMIT_DESIGN_REVIEW_TOOL["input_schema"]["required"]

    d = {
        "question": "Q", "hypothesis": "X increases Y",
        "variables": {"independent": ["a"], "dependent": ["b"], "controlled": ["c"]},
        "controls": [{"name": "no-template", "type": "negative", "rules_out": "contamination",
                      "provenance": "best_practice", "citation": None}],
        "readout": {"measures": "fluorescence", "answers_question": True},
        "replication": {"biological": "3", "technical": "2", "rationale": "captures prep variance"},
        "expected_results": [{"scenario": "signal rises", "interpretation": "binding occurred"}],
        "failure_modes": [{"symptom": "no signal", "likely_cause": "inactive protein", "check": "run a gel"}],
        "interpretation_limits": ["cannot conclude in vivo relevance"],
        "design_gaps": ["add a positive control to prove the assay works"],
    }
    md = design_review_to_markdown(d)
    for chunk in ["# Experiment design review", "Controls — and what each rules out",
                  "X increases Y", "add a positive control", "cannot conclude"]:
        assert chunk in md, f"missing {chunk}"


def test_design_alignment_markdown_and_schema():
    import json
    from app.schemas import EMIT_DESIGN_ALIGNMENT_TOOL
    req = EMIT_DESIGN_ALIGNMENT_TOOL["input_schema"]["required"]
    for key in ["hypothesis", "directly_tests", "critical_comparison",
                "recommended_changes", "summary"]:
        assert key in req, f"missing required {key}"

    a = {
        "hypothesis": {"statement": "DsbC increases folded scFv yield",
                       "prediction_if_true": "yield rises vs no-DsbC",
                       "prediction_if_false": "yield unchanged"},
        "inferred": False,
        "directly_tests": {"verdict": "partial",
                           "rationale": "no side-by-side comparison condition"},
        "critical_comparison": "+DsbC reaction vs. matched -DsbC reaction",
        "alignment_gaps": [{"gap": "no -DsbC arm",
                            "why_it_breaks_the_test": "cannot attribute yield to DsbC"}],
        "confounds": [{"confound": "batch variation", "makes_result_ambiguous": "yield shifts",
                       "mitigation": "run arms from one master mix"}],
        "recommended_changes": [
            {"change": "Add a parallel reaction identical except DsbC is omitted.",
             "addresses": "no -DsbC arm", "type": "add_comparison"}],
        "summary": "Add the -DsbC arm and it becomes a direct test.",
    }
    md = design_alignment_to_markdown(a)
    for chunk in ["# Does this test your hypothesis?", "DsbC increases folded scFv yield",
                  "Partially", "+DsbC reaction vs. matched -DsbC reaction",
                  "Add a parallel reaction", "Bottom line"]:
        assert chunk in md, f"missing {chunk}"


def test_assay_selection_to_markdown():
    chosen = {"id": "fp", "name": "Fluorescence polarization", "measures": "mP",
              "why_tests_hypothesis": "binding shifts mP", "critical_comparison": "+X vs -X",
              "throughput": "high", "difficulty": "low", "key_limitation": "needs a tracer",
              "provenance": "literature_grounded", "citation_verified": True,
              "citation": {"authors": "Doe J", "year": 2020, "identifier": "12345678",
                           "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/"}}
    opts = {"hypothesis_restated": "X increases binding of Y",
            "recommendation_rationale": "cheapest high-throughput readout",
            "assays": [chosen, {"id": "itc", "name": "ITC", "key_limitation": "needs lots of protein"}]}
    md = assay_selection_to_markdown(chosen, opts)
    for chunk in ["# Assay selection", "Fluorescence polarization", "+X vs -X",
                  "Alternatives considered", "ITC", "Why this pick", "✓ verified"]:
        assert chunk in md, f"missing {chunk}"


def test_titration_series_renders_in_protocol():
    p = {
        "title": "Binding curve", "summary": "", "estimated_duration": "3 h",
        "materials": [], "steps": [], "assumptions_log": [],
        "titration_series": {
            "variable": "ligand", "unit": "nM", "spacing": "log2", "provenance": "best_practice",
            "rationale": "brackets the Kd",
            "points": [
                {"label": "C1", "target_concentration": "1000 nM",
                 "components": [{"name": "ligand", "volume": "10 uL"}, {"name": "buffer", "volume": "90 uL"}]},
                {"label": "blank", "target_concentration": "0",
                 "components": [{"name": "buffer", "volume": "100 uL"}]},
            ],
        },
    }
    md = protocol_to_markdown(p)
    assert "## Titration series" in md
    assert "brackets the Kd" in md
    # verify the table is column-aligned: header and every data row share a column count,
    # and the blank row (which omits 'ligand') still fills that column with a placeholder.
    rows = [ln for ln in md.splitlines() if ln.startswith("| ")]
    header = rows[0]
    ncol = header.count("|")
    assert "Condition" in header and "ligand" in header and "buffer" in header
    data = [r for r in rows[2:] if "C1" in r or "blank" in r]
    assert len(data) == 2
    for r in data:
        assert r.count("|") == ncol  # no ragged rows
    blank = next(r for r in data if "blank" in r)
    cells = [c.strip() for c in blank.strip("|").split("|")]
    ligand_idx = [c.strip() for c in header.strip("|").split("|")].index("ligand")
    assert cells[ligand_idx] == "—"  # missing component filled, not shifted


def test_materials_csv_export():
    p = {"materials": [
        {"name": "Mg", "amount": 10, "unit": "mM", "vendor_or_grade": "Sigma",
         "provenance": "literature_grounded", "citation_verified": True,
         "citation": {"identifier": "12345678"}},
        {"name": "Buffer", "provenance": "best_practice"},
    ]}
    csv_text = materials_to_csv(p)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("reagent,amount,unit")
    assert "Mg,10,mM,Sigma,literature_grounded,12345678,yes" in csv_text
    assert "Buffer" in csv_text


def test_grounding_log_appendix():
    assert grounding_log_to_markdown([]) == ""
    md = grounding_log_to_markdown(["search_pubmed: Mg2+ optimum", "search_preprints: CyDisCo"])
    assert "Appendix: grounding search trail" in md
    assert "Mg2+ optimum" in md


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
