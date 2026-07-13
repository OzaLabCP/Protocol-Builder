"""Unit tests for the deterministic quality-gate checks (app/checks.py) and the
report-side gate helper (validation._apply_quality_gate). Pure and offline: no
network, no resolver, no model — run_checks is a pure function and the gate is
exercised directly on hand-built findings."""

from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.checks import (  # noqa: E402
    ensure_ids,
    parse_quantity,
    run_checks,
)
from app.validation import _apply_quality_gate, _GATE_MARK  # noqa: E402


# --- fixtures --------------------------------------------------------------

def _step(title, params):
    return {"number": 1, "title": title, "instruction": "do it",
            "provenance": "stated", "critical_parameters": params}


def _cp(name, value, unit=None):
    return {"name": name, "value": value, "unit": unit, "provenance": "stated"}


def _correct_protocol():
    """A valid protocol: a correct C1V1=C2V2 dilution and an in-capacity plate.
    Every quantity carries a unit, so nothing should flag."""
    dilution = _step("Dilute the stock", [
        _cp("stock concentration", "100", "mM"),   # C1
        _cp("final concentration", "10", "mM"),     # C2
        _cp("final volume", "100", "uL"),           # V2
        _cp("transfer volume", "10", "uL"),         # V1 = C2*V2/C1 = 10 uL  (correct)
    ])
    return {
        "title": "T", "summary": "S", "estimated_duration": "1 h",
        "materials": [{"name": "Tris", "provenance": "stated"}],
        "steps": [dilution],
        "conditions": 4, "replicates": 3, "plate": "96-well",  # 12 wells <= 96
        "assumptions_log": [], "open_questions": [],
    }


def _codes(findings, severity=None):
    return [f["code"] for f in findings
            if severity is None or f["severity"] == severity]


# --- run_checks: the seven mandated behaviours -----------------------------

def test_correct_protocol_has_no_errors():
    findings = run_checks(_correct_protocol())
    assert _codes(findings, "error") == []            # no false positives
    assert "DIL_OK" in _codes(findings, "info")       # the correct dilution passed
    assert "PLATE_OK" in _codes(findings, "info")     # in-capacity plate passed
    assert "UNIT_MISSING" not in _codes(findings)     # every quantity had a unit


def test_wrong_dilution_is_an_error():
    p = _correct_protocol()
    # break the transfer volume: 50 uL instead of the correct 10 uL
    p["steps"][0]["critical_parameters"][3]["value"] = "50"
    findings = run_checks(p)
    dil = [f for f in findings if f["code"] == "DIL_MISMATCH"]
    assert len(dil) == 1
    assert dil[0]["severity"] == "error"
    assert dil[0]["actual"] != dil[0]["expected"]


def test_plate_over_capacity_is_an_error():
    p = _correct_protocol()
    p["conditions"], p["replicates"], p["plate"] = 20, 30, "384-well"  # 600 > 384
    findings = run_checks(p)
    over = [f for f in findings if f["code"] == "PLATE_OVER_CAPACITY"]
    assert len(over) == 1
    assert over[0]["severity"] == "error"
    assert over[0]["actual"] == 600 and over[0]["expected"] == 384
    assert over[0]["actual"] > over[0]["expected"]


def test_value_without_unit_is_a_warning_not_error():
    p = _correct_protocol()
    p["materials"].append({"name": "salt", "amount": 5, "unit": None,
                           "provenance": "stated"})
    findings = run_checks(p)
    assert _codes(findings, "error") == []                 # never an error
    um = [f for f in findings if f["code"] == "UNIT_MISSING"]
    assert len(um) == 1 and um[0]["severity"] == "warning"


def test_missing_mw_is_an_assumption_not_error():
    p = _correct_protocol()
    p["steps"].append(_step("Weigh reagent", [
        _cp("concentration", "10", "mM"),
        _cp("volume", "100", "uL"),
        _cp("mass", "5", "mg"),          # mass present but NO molecular weight
    ]))
    findings = run_checks(p)
    assert _codes(findings, "error") == []
    mw = [f for f in findings if f["code"] == "MOLAR_NO_MW"]
    assert len(mw) == 1
    assert mw[0]["severity"] == "assumption"
    assert mw[0]["detail"].get("why")             # non-empty rationale for the log


def test_physical_impossibility_negative_volume_is_error():
    p = _correct_protocol()
    p["materials"].append({"name": "buffer", "amount": "-5 mL", "provenance": "stated"})
    findings = run_checks(p)
    neg = [f for f in findings if f["code"] == "PHYS_NEGATIVE"]
    assert len(neg) == 1 and neg[0]["severity"] == "error"


# --- run_checks: purity, determinism, ordering -----------------------------

def test_run_checks_is_pure_and_deterministic():
    p = _correct_protocol()
    p["steps"][0]["critical_parameters"][3]["value"] = "50"  # a blocking protocol
    snapshot = copy.deepcopy(p)
    a = run_checks(p)
    b = run_checks(copy.deepcopy(p))
    assert a == b                     # equal input -> equal output
    assert p == snapshot              # input not mutated


def test_findings_sorted_by_id_location_code():
    p = _correct_protocol()
    p["steps"][0]["critical_parameters"][3]["value"] = "50"
    findings = run_checks(p)
    keys = [(f.get("id") or "", f.get("location") or "", f["code"]) for f in findings]
    assert keys == sorted(keys)


# --- parse_quantity (§6) ---------------------------------------------------

def test_parse_quantity_rejects_the_unreadable():
    for bad in (None, True, False, float("nan"), float("inf"), float("-inf"),
                "", "   ", "20-25", "1, 2, 3", "5 to 10", [1, 2], {"a": 1}):
        assert parse_quantity(bad) is None, bad


def test_parse_quantity_reads_the_readable():
    assert parse_quantity(37) == {"value": 37.0, "unit": None, "dim": None, "base": None}
    mm = parse_quantity("50 mM")
    assert mm["value"] == 50.0 and mm["dim"] == "conc" and mm["base"] == 50e-3
    assert parse_quantity("1:10") is not None            # first number extracted
    assert parse_quantity("10x")["dim"] == "fold"
    over = parse_quantity("150% w/v")
    assert over["value"] == 150.0 and over["dim"] == "percent"


def test_parse_quantity_micro_signs_identical():
    a = parse_quantity("5 uL")   # ascii u
    b = parse_quantity("5 µL")  # MICRO SIGN
    c = parse_quantity("5 μL")  # GREEK SMALL LETTER MU
    assert a == b == c
    assert a["dim"] == "volume" and abs(a["base"] - 5e-6) < 1e-15


# --- ensure_ids (§4) -------------------------------------------------------

def test_ensure_ids_positional_and_idempotent():
    p = {
        "materials": [{"name": "a", "_id": "forged"}, {"name": "b"}],
        "steps": [{"title": "s", "critical_parameters": [{"name": "cp"}],
                   "substeps": [{"number": "1.1"}]}],
        "titration_series": {"points": [{"components": [{"name": "x"}]}]},
        "assumptions_log": [{"parameter": "p"}],
    }
    ensure_ids(p)
    first = copy.deepcopy(p)
    assert p["materials"][0]["_id"] == "mat:0"    # forged id overwritten
    assert p["materials"][1]["_id"] == "mat:1"
    assert p["steps"][0]["_id"] == "step:0"
    assert p["steps"][0]["critical_parameters"][0]["_id"] == "step:0/param:0"
    assert p["steps"][0]["substeps"][0]["_id"] == "step:0.0"
    assert p["titration_series"]["_id"] == "tit"
    assert p["titration_series"]["points"][0]["_id"] == "tit/pt:0"
    assert p["titration_series"]["points"][0]["components"][0]["_id"] == "tit/pt:0/comp:0"
    assert p["assumptions_log"][0]["_id"] == "alog:0"
    ensure_ids(p)                                  # second call is a no-op
    assert p == first


# --- the gate directly (validation._apply_quality_gate) --------------------

def _blank_report():
    return {"assumptions": []}


def _mk(code, severity, _id=None, location="protocol", detail=None):
    return {"code": code, "severity": severity, "id": _id, "location": location,
            "message": f"{code} happened", "expected": None, "actual": None,
            "detail": detail or {}}


def test_gate_buckets_and_status_blocked():
    report = _blank_report()
    oq = []
    findings = [
        _mk("DIL_MISMATCH", "error", location="steps[0]"),
        _mk("UNIT_MISSING", "warning", location="materials[0]"),
        _mk("MOLAR_NO_MW", "assumption", _id="step:1",
            location="steps[1]", detail={"why": "no MW stated"}),
        _mk("PLATE_OK", "info"),
    ]
    _apply_quality_gate(report, oq, findings)
    gate = report["quality_gate"]
    assert gate["status"] == "blocked"
    assert gate["counts"] == {"errors": 1, "warnings": 1, "assumptions": 1, "info": 1}
    assert [f["code"] for f in gate["errors"]] == ["DIL_MISMATCH"]
    # only the error surfaces into open_questions, tagged with the gate mark
    gate_lines = [q for q in oq if q.startswith(_GATE_MARK)]
    assert len(gate_lines) == 1 and "DIL_MISMATCH happened" in gate_lines[0]
    # the assumption is projected into the human-facing log with its 'why'
    assert report["assumptions"] == [
        {"location": "steps[1]", "id": "step:1",
         "assumption": "MOLAR_NO_MW happened", "why": "no MW stated"}]
    assert "_assumption_keys" in report  # scratch state present until the caller pops it


def test_gate_status_warnings_then_ok():
    r = _blank_report(); oq = []
    _apply_quality_gate(r, oq, [_mk("UNIT_MISSING", "warning")])
    assert r["quality_gate"]["status"] == "warnings"
    assert [q for q in oq if q.startswith(_GATE_MARK)] == []  # warnings never surface

    r2 = _blank_report(); oq2 = []
    _apply_quality_gate(r2, oq2, [_mk("PLATE_OK", "info")])
    assert r2["quality_gate"]["status"] == "ok"
    assert oq2 == []


def test_gate_strips_prior_lines_no_accumulation():
    report = _blank_report()
    oq = ["a real open question"]
    findings = [_mk("PHYS_NEGATIVE", "error", location="materials[0]")]
    _apply_quality_gate(report, oq, findings)
    report["assumptions"] = []                 # simulate the fresh per-call rebuild
    _apply_quality_gate(report, oq, findings)  # run twice on the same input
    gate_lines = [q for q in oq if q.startswith(_GATE_MARK)]
    assert len(gate_lines) == 1                # not duplicated
    assert "a real open question" in oq        # pre-existing questions preserved


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
