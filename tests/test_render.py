"""Markdown export tests."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.render import design_review_to_markdown, protocol_to_markdown  # noqa: E402

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
