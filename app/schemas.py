"""Tool `input_schema` definitions for the two forced-tool-choice calls.

The `citation` object is defined once as a Python value and shared by reference
wherever a value can be literature-grounded, so it is fully inlined on the wire
(no `$ref`) while staying DRY in code. The five provenance tiers are the same
everywhere.
"""

from __future__ import annotations

PROVENANCE_TIERS = [
    "stated",
    "literature_grounded",
    "best_practice",
    "user_input",
    "default_verify",
]

# Provenance enum used inside the emitted protocol (materials/steps/params).
_PROVENANCE_ENUM = {"type": "string", "enum": PROVENANCE_TIERS}

# assumptions_log never contains "stated" values (it lists only what was filled).
_ASSUMPTION_PROVENANCE_ENUM = {
    "type": "string",
    "enum": [t for t in PROVENANCE_TIERS if t != "stated"],
}

# The one canonical citation shape. Nullable: present (non-null) only when a
# value is literature-grounded (or user-selected from a literature option).
CITATION_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "authors": {"type": "string"},
        "year": {"type": "integer"},
        "identifier": {
            "type": "string",
            "description": "A DOI (starts with '10.') or a PubMed ID (digits only).",
        },
        "url": {"type": ["string", "null"]},
    },
    "required": ["title", "authors", "year", "identifier"],
    "description": (
        "Required (non-null) when provenance is literature_grounded; may also be "
        "present on a value the user picked from a literature-derived option "
        "(then provenance is literature_grounded and selected_by_user is true); "
        "null otherwise. The host resolves this identifier after emit."
    ),
}

_SELECTED_BY_USER = {
    "type": "boolean",
    "description": "True when the user chose this value from an option menu. "
    "Independent of the provenance tier.",
}


# ---------------------------------------------------------------------------
# search_pubmed — client-side grounding tool (available in both phases)
# ---------------------------------------------------------------------------

SEARCH_PUBMED_TOOL = {
    "name": "search_pubmed",
    "description": (
        "Search PubMed for real biomedical articles to ground a value you intend to "
        "fill. Returns up to `retmax` results, each with a PMID, title, authors, "
        "year, and DOI when available. Use a returned PMID or DOI as the citation "
        "identifier for a literature_grounded value — never invent one. Good for "
        "outcome-critical parameters (buffer optima, incubation conditions, assay "
        "conventions); do not burn searches on trivia."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "PubMed query. Supports field tags and boolean "
                "operators, e.g. 'cell-free protein synthesis Mg2+ optimum PANOx-SP'.",
            },
            "retmax": {
                "type": "integer",
                "description": "Max results to return (1-8).",
            },
        },
        "required": ["query"],
    },
}


_DESIGN_PROVENANCE = {
    "type": "string",
    "enum": ["stated", "literature_grounded", "best_practice", "default_verify"],
}

EMIT_DESIGN_REVIEW_TOOL = {
    "name": "emit_design_review",
    "description": (
        "Emit an experiment-design review that teaches the student to reason about "
        "the experiment AROUND this protocol — hypothesis, controls, variables, "
        "readout adequacy, replication, expected results, failure modes, and the "
        "limits of interpretation. Ground controls in the literature where you can."
    ),
    "input_schema": {
        "type": "object",
        "$defs": {"citation": CITATION_SCHEMA},
        "properties": {
            "question": {
                "type": "string",
                "description": "The scientific question this experiment addresses, in one sentence.",
            },
            "hypothesis": {
                "type": "string",
                "description": "A specific, testable hypothesis that predicts a direction or "
                "outcome (not a topic). If the student's goal is unstated, infer the most "
                "likely one and say so.",
            },
            "variables": {
                "type": "object",
                "properties": {
                    "independent": {"type": "array", "items": {"type": "string"},
                                    "description": "What is deliberately varied."},
                    "dependent": {"type": "array", "items": {"type": "string"},
                                  "description": "What is measured (the outcome)."},
                    "controlled": {"type": "array", "items": {"type": "string"},
                                   "description": "What is held constant to avoid confounds."},
                },
                "required": ["independent", "dependent", "controlled"],
            },
            "controls": {
                "type": "array",
                "description": "Every control the experiment needs. A control the student "
                "cannot explain is cargo-culting — so each states what it rules out.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string",
                                 "enum": ["positive", "negative", "vehicle", "no_template",
                                          "loading", "sham", "other"]},
                        "rules_out": {"type": "string",
                                      "description": "The artifact or alternative explanation "
                                      "this control eliminates."},
                        "provenance": _DESIGN_PROVENANCE,
                        "citation": CITATION_SCHEMA,
                    },
                    "required": ["name", "type", "rules_out"],
                },
            },
            "readout": {
                "type": "object",
                "properties": {
                    "measures": {"type": "string", "description": "What the readout physically measures."},
                    "answers_question": {"type": "boolean",
                                         "description": "Does this readout actually test the hypothesis?"},
                    "caveat": {"type": ["string", "null"],
                               "description": "Where the readout could mislead (proxy, saturation, indirect)."},
                },
                "required": ["measures", "answers_question"],
            },
            "replication": {
                "type": "object",
                "properties": {
                    "biological": {"type": ["string", "null"], "description": "Independent biological replicates."},
                    "technical": {"type": ["string", "null"], "description": "Technical replicates per sample."},
                    "rationale": {"type": "string",
                                  "description": "Why this many, and what source of variation each captures."},
                },
                "required": ["rationale"],
            },
            "expected_results": {
                "type": "array",
                "description": "If you see X, it means Y — before you're staring at the data.",
                "items": {
                    "type": "object",
                    "properties": {
                        "scenario": {"type": "string"},
                        "interpretation": {"type": "string"},
                    },
                    "required": ["scenario", "interpretation"],
                },
            },
            "failure_modes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symptom": {"type": "string"},
                        "likely_cause": {"type": "string"},
                        "check": {"type": ["string", "null"], "description": "How to confirm/rule out the cause."},
                    },
                    "required": ["symptom", "likely_cause"],
                },
            },
            "interpretation_limits": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What this experiment cannot conclude even if it works perfectly.",
            },
            "design_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Weaknesses in the CURRENT plan the student should fix "
                "(missing control, underpowered, confounded readout, no baseline).",
            },
        },
        "required": ["question", "hypothesis", "variables", "controls", "readout",
                     "replication", "expected_results", "interpretation_limits"],
    },
}


EMIT_DESIGN_ALIGNMENT_TOOL = {
    "name": "emit_design_alignment",
    "description": (
        "Assess whether the drafted protocol DIRECTLY tests the student's hypothesis, "
        "and recommend concrete, applicable protocol changes to make it a valid test. "
        "The crux: is there a manipulation or comparison whose outcome distinguishes "
        "hypothesis-true from hypothesis-false, measured by an adequate readout, free of "
        "confounds that would make a positive result ambiguous?"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hypothesis": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string",
                                  "description": "A specific, falsifiable restatement of what is being tested."},
                    "prediction_if_true": {"type": "string",
                                           "description": "What you should observe if the hypothesis holds."},
                    "prediction_if_false": {"type": "string",
                                            "description": "What you should observe if it does not."},
                },
                "required": ["statement", "prediction_if_true", "prediction_if_false"],
            },
            "inferred": {"type": "boolean",
                         "description": "True if the hypothesis was inferred, not supplied by the student."},
            "directly_tests": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["yes", "partial", "no"]},
                    "rationale": {"type": "string"},
                },
                "required": ["verdict", "rationale"],
            },
            "critical_comparison": {
                "type": "string",
                "description": "The single comparison that must hold for this to be a valid test "
                "(what condition vs. what condition, measured how).",
            },
            "alignment_gaps": {
                "type": "array",
                "description": "Where the protocol as drafted fails to directly test the hypothesis.",
                "items": {
                    "type": "object",
                    "properties": {
                        "gap": {"type": "string"},
                        "why_it_breaks_the_test": {"type": "string"},
                    },
                    "required": ["gap", "why_it_breaks_the_test"],
                },
            },
            "confounds": {
                "type": "array",
                "description": "Factors that would make a positive result fail to support the hypothesis.",
                "items": {
                    "type": "object",
                    "properties": {
                        "confound": {"type": "string"},
                        "makes_result_ambiguous": {"type": "string"},
                        "mitigation": {"type": ["string", "null"]},
                    },
                    "required": ["confound", "makes_result_ambiguous"],
                },
            },
            "recommended_changes": {
                "type": "array",
                "description": "Concrete, directly-applicable protocol edits that make the experiment "
                "a valid test of the hypothesis. Each should read as an instruction the tool can apply.",
                "items": {
                    "type": "object",
                    "properties": {
                        "change": {"type": "string",
                                   "description": "An imperative, self-contained protocol edit."},
                        "addresses": {"type": "string",
                                      "description": "Which gap or confound this closes."},
                        "type": {"type": "string",
                                 "enum": ["add_control", "add_condition", "add_comparison",
                                          "change_readout", "increase_replication", "other"]},
                    },
                    "required": ["change", "addresses"],
                },
            },
            "summary": {"type": "string",
                        "description": "One-sentence verdict the student can act on."},
        },
        "required": ["hypothesis", "directly_tests", "critical_comparison",
                     "alignment_gaps", "recommended_changes", "summary"],
    },
}


SEARCH_PREPRINTS_TOOL = {
    "name": "search_preprints",
    "description": (
        "Search preprints (bioRxiv / medRxiv / Research Square via Europe PMC) for "
        "recent methods not yet indexed in PubMed. Returns results with a DOI (and "
        "PMID when present). Cite the DOI for a literature_grounded value. Use when a "
        "method is new or when PubMed came up empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms, e.g. 'cell-free protein synthesis oxidative folding CyDisCo'."},
            "retmax": {"type": "integer", "description": "Max results (1-8)."},
        },
        "required": ["query"],
    },
}

SEARCH_PROTOCOLS_TOOL = {
    "name": "search_protocols",
    "description": (
        "Search published protocols.io protocols — step-by-step methods with their "
        "own DOIs. Best for grounding a METHOD or step (not just a numeric value): if "
        "a returned protocol matches this technique, cite its DOI for the relevant "
        "step as literature_grounded. Returns title, authors, year, DOI, and URL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms, e.g. 'S30 extract preparation cell-free'."},
            "retmax": {"type": "integer", "description": "Max results (1-8)."},
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Schema 1 — request_clarifications (phase 1 output)
# ---------------------------------------------------------------------------

REQUEST_CLARIFICATIONS_TOOL = {
    "name": "request_clarifications",
    "description": (
        "Return the reconstructed protocol summary and the ranked set of gaps "
        "that require a human answer. Call this exactly once when you are done "
        "reading and (if needed) scoping the literature."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "usable": {
                "type": "boolean",
                "description": "False if the input is not an experimental "
                "methods/protocol section.",
            },
            "reason": {
                "type": ["string", "null"],
                "description": "If usable is false, a brief explanation. Otherwise null.",
            },
            "protocol_title": {"type": ["string", "null"]},
            "source_summary": {
                "type": ["string", "null"],
                "description": "One or two sentences on what the protocol accomplishes.",
            },
            "reconstructed_step_count": {"type": ["integer", "null"]},
            "gaps": {
                "type": "array",
                "maxItems": 5,
                "description": "Empty array is valid and means no user input is needed.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Stable identifier used to map the "
                            "user's answer back in phase 2.",
                        },
                        "parameter": {"type": "string"},
                        "step_reference": {
                            "type": "string",
                            "description": "Which reconstructed step this affects.",
                        },
                        "why_it_matters": {
                            "type": "string",
                            "description": "The downstream effect of this parameter.",
                        },
                        "classification": {
                            "type": "string",
                            "enum": ["user_dependent", "ambiguous", "deferred"],
                        },
                        "question": {"type": "string"},
                        "answer_type": {
                            "type": "string",
                            "enum": ["single_select", "multi_select", "number", "text"],
                        },
                        "option_context": {
                            "type": ["string", "null"],
                            "description": "For ambiguous gaps: short phrase "
                            "explaining why these options were surfaced.",
                        },
                        "options": {
                            "type": "array",
                            "description": "Present for select answer_types. The UI "
                            "always adds an 'Other / not sure' escape; do not "
                            "include it here.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "string"},
                                    "source": {
                                        "type": "string",
                                        "enum": [
                                            "literature",
                                            "standard_practice",
                                            "model_suggestion",
                                        ],
                                    },
                                    "note": {
                                        "type": ["string", "null"],
                                        "description": "One-line 'pick this if…' guidance.",
                                    },
                                    "citation": CITATION_SCHEMA,
                                },
                                "required": ["label", "value", "source"],
                            },
                        },
                        "unit": {
                            "type": ["string", "null"],
                            "description": "Expected unit for number answers "
                            "(e.g. 'uL', 'mM').",
                        },
                        "suggested_default": {
                            "type": ["string", "null"],
                            "description": "Fallback value if the user skips; becomes "
                            "a default_verify entry.",
                        },
                    },
                    "required": [
                        "id",
                        "parameter",
                        "step_reference",
                        "why_it_matters",
                        "classification",
                        "question",
                        "answer_type",
                    ],
                },
            },
        },
        "required": ["usable", "gaps"],
    },
}


# ---------------------------------------------------------------------------
# Schema 2 — emit_protocol (phase 3 output)
# ---------------------------------------------------------------------------

_MATERIAL = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "amount": {"type": ["number", "null"]},
        "unit": {"type": ["string", "null"]},
        "vendor_or_grade": {"type": ["string", "null"]},
        "provenance": _PROVENANCE_ENUM,
        "selected_by_user": _SELECTED_BY_USER,
        "provenance_note": {
            "type": ["string", "null"],
            "description": "Basis/standard named; scaling math if scaled; verify "
            "note for defaults.",
        },
        "citation": CITATION_SCHEMA,
    },
    "required": ["name", "provenance"],
}

_CRITICAL_PARAMETER = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "value": {"type": "string"},
        "unit": {"type": ["string", "null"]},
        "provenance": _PROVENANCE_ENUM,
        "selected_by_user": _SELECTED_BY_USER,
        "provenance_note": {"type": ["string", "null"]},
        "citation": CITATION_SCHEMA,
    },
    "required": ["name", "value", "provenance"],
}

_SUBSTEP = {
    "type": "object",
    "properties": {
        "number": {"type": "string"},
        "instruction": {"type": "string"},
        "provenance": _PROVENANCE_ENUM,
        "provenance_note": {"type": ["string", "null"]},
    },
    "required": ["number", "instruction", "provenance"],
}

_STEP = {
    "type": "object",
    "properties": {
        "number": {"type": "integer"},
        "title": {"type": "string"},
        "instruction": {"type": "string"},
        "duration": {"type": ["string", "null"]},
        "temperature": {"type": ["string", "null"]},
        "provenance": _PROVENANCE_ENUM,
        "critical_parameters": {"type": "array", "items": _CRITICAL_PARAMETER},
        "substeps": {"type": "array", "items": _SUBSTEP},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["number", "title", "instruction", "provenance"],
}

_ASSUMPTION = {
    "type": "object",
    "properties": {
        "parameter": {"type": "string"},
        "value": {"type": "string"},
        "provenance": _ASSUMPTION_PROVENANCE_ENUM,
        "selected_by_user": _SELECTED_BY_USER,
        "basis": {
            "type": "string",
            "description": "The citation summary, the named standard, or how the "
            "value was derived.",
        },
        "citation": CITATION_SCHEMA,
        "verify": {
            "type": "boolean",
            "description": "True if the user should confirm before running.",
        },
    },
    "required": ["parameter", "value", "provenance", "basis", "verify"],
}

EMIT_PROTOCOL_TOOL = {
    "name": "emit_protocol",
    "description": "Emit the complete, executable, provenance-tagged protocol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "source_citation": {"type": ["string", "null"]},
            "estimated_duration": {"type": "string"},
            "materials": {"type": "array", "items": _MATERIAL},
            "equipment": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": _STEP},
            "assumptions_log": {
                "type": "array",
                "description": "Every value NOT stated in the source. A projection "
                "of the inline provenance: each entry must match an inline value's "
                "tier, value, and citation exactly. Must be exhaustive.",
                "items": _ASSUMPTION,
            },
            "open_questions": {
                "type": "array",
                "description": "Gaps that remain unresolved even after user input, "
                "plus any user/literature conflicts and any citation that failed "
                "host validation.",
                "items": {"type": "string"},
            },
        },
        "required": [
            "title",
            "summary",
            "estimated_duration",
            "materials",
            "steps",
            "assumptions_log",
        ],
    },
}
