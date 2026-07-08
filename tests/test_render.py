"""Markdown export tests."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.render import protocol_to_markdown  # noqa: E402

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
