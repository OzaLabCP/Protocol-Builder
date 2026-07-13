"""The Eason et al. 2020 binding-assay protocol is a hand-built emit_protocol
payload used as a demo and a regression fixture: it must stay schema-shaped and
pass host-side validation cleanly (no invariant violations, exhaustive
assumptions_log)."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schemas import EMIT_PROTOCOL_TOOL  # noqa: E402
from app.validation import validate_and_finalize  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "binding_assay_eason2020.json",
)


def _no_network(_identifier):
    raise AssertionError("fixture must not carry citations that hit the network")


def test_fixture_has_required_fields():
    proto = json.load(open(FIXTURE))
    for key in EMIT_PROTOCOL_TOOL["input_schema"]["required"]:
        assert key in proto, f"missing required field {key}"


def test_fixture_validates_clean():
    proto = json.load(open(FIXTURE))
    # No literature_grounded citations in this honest offline reconstruction, so the
    # resolver must never be called.
    report = validate_and_finalize(proto, _no_network)
    assert report["citations_checked"] == 0
    assert report["invariant_fixes"] == []
    assert report["downgraded"] == []
    assert report["consistency"]["inline_missing_from_log"] == []
    assert report["consistency"]["log_missing_from_inline"] == []


def test_fixture_provenance_is_honest():
    proto = json.load(open(FIXTURE))
    # Offline reconstruction => no literature_grounded anywhere.
    def tiers(entry):
        return entry.get("provenance")
    all_tiers = [m["provenance"] for m in proto["materials"]]
    for s in proto["steps"]:
        all_tiers.append(s["provenance"])
        all_tiers += [cp["provenance"] for cp in s.get("critical_parameters", [])]
        all_tiers += [ss["provenance"] for ss in s.get("substeps", [])]
    all_tiers += [a["provenance"] for a in proto["assumptions_log"]]
    assert "literature_grounded" not in all_tiers
    assert "stated" in all_tiers and "default_verify" in all_tiers


# --- Epic 2 regression: new checks must not false-positive on the fixture ------

def test_fixture_gate_is_ready_no_new_false_positives():
    # The corrected fixture validates clean through the full quality gate: the new
    # prep/readout/control/structure heuristics all demote to no finding, so the gate
    # is ready. (The §III assumptions-log disagreements are data-only and NOT routed
    # through the gate, so they cannot flip the status — that is asserted separately.)
    proto = json.load(open(FIXTURE))
    report = validate_and_finalize(proto, _no_network)
    gate = report["quality_gate"]
    assert gate["status"] == "ok" and gate["status_label"] == "ready"
    assert gate["counts"]["errors"] == 0 and gate["counts"]["warnings"] == 0
    fired = {f["code"] for f in gate["errors"] + gate["warnings"]}
    for code in ("PREP_MISSING", "READOUT_MISSING", "CONTROL_MISSING",
                 "STRUCT_STEP_NO_INSTRUCTION"):
        assert code not in fired, f"{code} false-fired on the example fixture"


def test_fixture_scaling_note_is_internally_consistent():
    # Regression for the corrected §VII note: the step-2 volume note now states an
    # arithmetic (18 x 50 uL = 0.9 mL ~= ~1 mL) that agrees with its own value, with
    # no contradictory intermediate figure (the old "18 x 100 uL = 1.8 mL" is gone).
    proto = json.load(open(FIXTURE))
    step2 = [s for s in proto["steps"] if s.get("number") == 2][0]
    cp = [c for c in step2["critical_parameters"]
          if c["name"] == "gSTEP working-solution volume per variant"][0]
    note = cp["provenance_note"]
    assert cp["value"] == "~1 mL each"
    assert "18 x 50 uL = 0.9 mL" in note
    assert "1.8 mL" not in note                      # the contradictory figure is gone


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
