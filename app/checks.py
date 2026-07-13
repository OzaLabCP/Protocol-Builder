"""Deterministic quality-gate checks (Epic 2).

Pure, dependency-free, deterministic. This module is the *only* producer of
quality-gate findings and structural ids. It never touches the report, the
network, the filesystem, an RNG, or the wall clock. Equal input yields
byte-identical output.

Public API (see the EPIC-2 spec):
    run_checks(protocol) -> list[Finding]
    ensure_ids(protocol) -> None            # in-place, positional, idempotent
    parse_quantity(raw)  -> dict | None     # total, never raises

Prime Directive: ambiguity always resolves DOWN
(info/pass > assumption > warning > error). A false ``error`` is the single
worst failure mode, so every check demotes on any missing/ambiguous premise.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Structural sentinels written by repair_structure. Importing them here (checks
# depends on models; models imports nothing from checks -> no cycle) lets
# _check_structure recognise a repaired-placeholder field and route it through the
# gate as a blocker.
from .models import (
    MATERIAL_NAME_PLACEHOLDER,
    PARAM_NAME_PLACEHOLDER,
    PARAM_VALUE_PLACEHOLDER,
    POINT_LABEL_PLACEHOLDER,
    STEP_INSTR_PLACEHOLDER,
    STRUCT_PLACEHOLDERS,
    SUBSTEP_INSTR_PLACEHOLDER,
)

# ---------------------------------------------------------------------------
# Frozen unit tables (§5)
# ---------------------------------------------------------------------------

# canonical lowercased token -> (dimension, factor_to_base)
_UNITS = {
    # volume  -> base: litre
    "l": ("volume", 1.0), "ml": ("volume", 1e-3), "ul": ("volume", 1e-6),
    "nl": ("volume", 1e-9),
    # molar concentration -> base: mol/L
    "m": ("conc", 1.0), "mm": ("conc", 1e-3), "um": ("conc", 1e-6),
    "nm": ("conc", 1e-9), "pm": ("conc", 1e-12),
    # mass -> base: gram
    "g": ("mass", 1.0), "mg": ("mass", 1e-3), "ug": ("mass", 1e-6),
    "ng": ("mass", 1e-9), "kg": ("mass", 1e3),
    # dimensionless
    "%": ("percent", 1.0), "x": ("fold", 1.0),
}

# Exact observed spellings -> canonical _UNITS key. Micro signs are normalized
# to 'u' *before* this lookup. Uppercase-'M'-suffix means molar; lowercase 'l'
# suffix means litre. Whole-token match only — never disambiguate by letter.
_UNIT_ALIASES = {
    # molar (uppercase M)
    "M": "m", "mM": "mm", "uM": "um", "nM": "nm", "pM": "pm",
    # volume (uppercase or lowercase L)
    "mL": "ml", "uL": "ul", "nL": "nl", "L": "l",
    "ml": "ml", "ul": "ul", "nl": "nl", "l": "l",
    # lowercase molar variants
    "m": "m", "mm": "mm", "um": "um", "nm": "nm", "pm": "pm",
    # mass
    "g": "g", "mg": "mg", "ug": "ug", "ng": "ng", "kg": "kg",
    "G": "g", "mG": "mg",
    # dimensionless
    "%": "%", "x": "x", "X": "x",
}

# ---------------------------------------------------------------------------
# Formatting / comparators (§2, §7)
# ---------------------------------------------------------------------------

_REL_TOL = 1e-6
_ABS_TOL = 1e-9


def _fmt(x) -> str:
    """Format a number as ``%.6g`` so messages are byte-stable cross-platform.

    Non-numbers pass through via ``str`` (total, never raises)."""
    try:
        return f"{float(x):.6g}"
    except (TypeError, ValueError):
        return str(x)


def _close(a, b) -> bool:
    """Machine-epsilon equality guard."""
    return abs(a - b) <= max(_REL_TOL * max(abs(a), abs(b)), _ABS_TOL)


def _within(actual, expected, rel, abs_) -> bool:
    """Domain-tolerance comparison. Edge equality always passes."""
    return abs(actual - expected) <= max(rel * abs(expected), abs_)


# ---------------------------------------------------------------------------
# parse_quantity (§6) — the single defensive choke-point
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_RANGE_RE = re.compile(r"\d\s*[-–—]\s*\d")
_COMMA_NUM_RE = re.compile(r"\d\s*,\s*\d")
_MICRO_RE = re.compile("[µμ]")  # U+00B5 MICRO SIGN, U+03BC GREEK MU


def _is_finite_number(x) -> bool:
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float)):
        return x == x and x not in (float("inf"), float("-inf"))
    return False


def _resolve_unit(token: str):
    """token (micro already normalized) -> (canonical_key, dim, factor) or
    (raw_token, None, None) when unknown."""
    if not token:
        return None, None, None
    canon = _UNIT_ALIASES.get(token)
    if canon is None:
        low = token.lower()
        if low in _UNITS:
            canon = low
    if canon is not None and canon in _UNITS:
        dim, factor = _UNITS[canon]
        return canon, dim, factor
    return token, None, None


def parse_quantity(raw):
    """Parse a value+unit into ``{value, unit, dim, base}`` or ``None``.

    Total function: returns ``None`` on anything unreadable, never raises."""
    # (1) None / bool guard (bool BEFORE numbers.Number)
    if raw is None or isinstance(raw, bool):
        return None
    # (2) bare finite number
    if isinstance(raw, (int, float)):
        if not _is_finite_number(raw):
            return None
        return {"value": float(raw), "unit": None, "dim": None, "base": None}
    # (3) string
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # (3a) reject ambiguous multi-value ranges / lists
    if _RANGE_RE.search(s) or _COMMA_NUM_RE.search(s) or " to " in s.lower():
        return None
    # (3b) first signed decimal/scientific number
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        value = float(m.group(0))
    except (TypeError, ValueError):
        return None
    if not _is_finite_number(value):
        return None
    # (3c) unit token: substring right after the number, up to whitespace/end
    tail = s[m.end():]
    token = tail.split()[0] if tail.split() else ""
    token = _MICRO_RE.sub("u", token)
    if token:
        canon, dim, factor = _resolve_unit(token)
        unit = canon
        base = value * factor if (dim is not None and factor is not None) else None
        dim = dim
    else:
        unit, dim, base = None, None, None
    return {"value": value, "unit": unit, "dim": dim, "base": base}


def _qty(value, unit=None):
    """Parse a value that may carry its unit in a *separate* field (critical
    parameters store ``value`` and ``unit`` apart). Conservative: returns
    ``None`` when the value itself is unreadable."""
    q = parse_quantity(value)
    if q is None:
        return None
    if q.get("dim") is None and isinstance(unit, str) and unit.strip():
        uq = parse_quantity("1" + _MICRO_RE.sub("u", unit.strip()))
        if uq is not None and uq.get("dim") is not None and uq.get("unit") is not None:
            factor = _UNITS[uq["unit"]][1]
            return {"value": q["value"], "unit": uq["unit"],
                    "dim": uq["dim"], "base": q["value"] * factor}
    return q


def _first_number(raw):
    """Extract a lone finite float from a scalar, or ``None``. No unit logic."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if _is_finite_number(raw) else None
    if not isinstance(raw, str):
        return None
    m = _NUM_RE.search(raw)
    if not m:
        return None
    try:
        v = float(m.group(0))
    except (TypeError, ValueError):
        return None
    return v if _is_finite_number(v) else None


def _as_int(raw):
    """Return an int only for a *cleanly integral* scalar; ranges/floats-with-
    fraction/junk -> ``None`` (so a non-integer input skips the whole check)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if (_is_finite_number(raw) and float(raw).is_integer()) else None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if _RANGE_RE.search(s) or _COMMA_NUM_RE.search(s) or " to " in s.lower():
        return None
    if re.fullmatch(r"[-+]?\d+", s):
        try:
            return int(s)
        except ValueError:
            return None
    m = re.fullmatch(r"[-+]?\d+\.0*", s)
    if m:
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# ensure_ids (§4) — positional, forgery-proof, idempotent
# ---------------------------------------------------------------------------

def ensure_ids(protocol: dict) -> None:
    for i, mat in enumerate(protocol.get("materials") or []):
        if isinstance(mat, dict):
            mat["_id"] = f"mat:{i}"
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        step["_id"] = f"step:{i}"
        for j, cp in enumerate(step.get("critical_parameters") or []):
            if isinstance(cp, dict):
                cp["_id"] = f"step:{i}/param:{j}"
        for k, ss in enumerate(step.get("substeps") or []):
            if isinstance(ss, dict):
                ss["_id"] = f"step:{i}.{k}"
    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        ts["_id"] = "tit"
        for p, pt in enumerate(ts.get("points") or []):
            if not isinstance(pt, dict):
                continue
            pt["_id"] = f"tit/pt:{p}"
            for c, comp in enumerate(pt.get("components") or []):
                if isinstance(comp, dict):
                    comp["_id"] = f"tit/pt:{p}/comp:{c}"
    for m, a in enumerate(protocol.get("assumptions_log") or []):
        if isinstance(a, dict):
            a["_id"] = f"alog:{m}"


# ---------------------------------------------------------------------------
# assign_stable_ids (§II) — content-hash, revision-stable, parallel to _id
# ---------------------------------------------------------------------------
#
# ``ensure_ids`` above stamps POSITIONAL ids (``mat:0``, ``step:1/param:2``) that
# move when the list shifts. ``assign_stable_ids`` stamps ADDITIVE, content-derived
# sibling fields (``material_id``/``step_id``/…) that survive a re-emit or an
# in-place edit. The two are independent: this function never reads or writes
# ``_id``, and ``_id`` prefixes (``mat:``/``step:``) are disjoint from the stable
# prefixes (``m_``/``s_``) so an id from one scheme can never be mistaken for the
# other.
#
# Identity basis is the entity's *identity* (its name/label/instruction), NOT its
# mutable quantity — so correcting a value keeps the same id. Idempotent (a second
# run preserves every well-formed id -> byte-identical) and deterministic in
# collision disambiguation (document-order ``_1``/``_2`` suffixes, never list index).

# stable field name per entity kind (used by the server's /edit resolver too).
_STABLE_FIELD = {
    "material": "material_id",
    "step": "step_id",
    "critical_parameter": "parameter_id",
    "substep": "substep_id",
    "titration_point": "point_id",
    "titration_component": "component_id",
}

# single-char prefixes; the id form is ``<pfx>_<8-hex>`` (e.g. ``m_1a2b3c4d``) and
# a same-identity duplicate disambiguates as ``m_1a2b3c4d_1``.
_STABLE_PREFIX = {
    "material_id": "m",
    "step_id": "s",
    "parameter_id": "p",
    "substep_id": "ss",
    "point_id": "tp",
    "component_id": "tc",
}

_STABLE_JOIN = "␟"  # ␟ U+241F symbol-for-unit-separator, matches finding_key


def _stable_id(field: str, basis: str) -> str:
    pfx = _STABLE_PREFIX[field]
    return pfx + "_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8]


def _stable_plan(protocol: dict):
    """Yield ``(entity, field, basis)`` for every stable-id-bearing entity in
    document order. Basis is identity-derived (name/label/instruction), never a
    mutable quantity."""
    for mat in protocol.get("materials") or []:
        if isinstance(mat, dict):
            yield mat, "material_id", "mat" + _STABLE_JOIN + _collapse(mat.get("name"))
    for step in protocol.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_basis = "step" + _STABLE_JOIN + _collapse(step.get("title") or step.get("instruction"))
        yield step, "step_id", step_basis
        for cp in step.get("critical_parameters") or []:
            if isinstance(cp, dict):
                yield cp, "parameter_id", (
                    step_basis + _STABLE_JOIN + "param" + _STABLE_JOIN + _collapse(cp.get("name")))
        for ss in step.get("substeps") or []:
            if isinstance(ss, dict):
                yield ss, "substep_id", (
                    step_basis + _STABLE_JOIN + "sub" + _STABLE_JOIN + _collapse(ss.get("instruction")))
    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        for pt in ts.get("points") or []:
            if not isinstance(pt, dict):
                continue
            point_basis = "tit" + _STABLE_JOIN + "pt" + _STABLE_JOIN + _collapse(pt.get("label"))
            yield pt, "point_id", point_basis
            for comp in pt.get("components") or []:
                if isinstance(comp, dict):
                    yield comp, "component_id", (
                        point_basis + _STABLE_JOIN + "comp" + _STABLE_JOIN + _collapse(comp.get("name")))


def assign_stable_ids(protocol: dict) -> None:
    """Stamp additive, content-derived stable ids (``material_id``/``step_id``/…)
    parallel to the positional ``_id``. In-place, idempotent, preserving.

    Never reads or writes ``_id``. PASS 1 reserves every well-formed id already
    present; PASS 2 (document order) preserves an existing well-formed id and
    otherwise assigns ``<pfx>_<hash>``, appending ``_1``/``_2``/… while the
    candidate collides (deterministic same-identity duplicate disambiguation)."""
    if not isinstance(protocol, dict):
        return
    plan = list(_stable_plan(protocol))
    used: set = set()
    # PASS 1 — reserve every well-formed id already on an entity.
    for entity, field, _basis in plan:
        cur = entity.get(field)
        if isinstance(cur, str) and cur.startswith(_STABLE_PREFIX[field] + "_"):
            used.add(cur)
    # PASS 2 — preserve existing well-formed ids; assign the rest in document order.
    for entity, field, basis in plan:
        cur = entity.get(field)
        if isinstance(cur, str) and cur.startswith(_STABLE_PREFIX[field] + "_"):
            continue  # preserve (survives a rename whose basis changed)
        base = _stable_id(field, basis)
        cand = base
        n = 0
        while cand in used:
            n += 1
            cand = f"{base}_{n}"
        used.add(cand)
        entity[field] = cand


# ---------------------------------------------------------------------------
# Finding constructor (§2, extended §IV)
# ---------------------------------------------------------------------------
#
# A finding keeps its seven original keys byte-identical (code/severity/id/
# location/message/expected/actual/detail) — every existing reader keys off
# ``severity`` (the internal error/warning/assumption/info vocab) and off ``id``
# — and gains four ADDITIVE keys: ``location_id`` (alias of the id anchor),
# ``severity_label`` (the spec's blocker|warning|information vocab), a
# deterministic per-code ``suggested_fix`` string, and ``host_verified`` (always
# True — run_checks is the sole deterministic producer). Nothing keys off the new
# fields for identity or ordering, so they cannot break a reader of ``severity``.

# Internal severity -> spec-facing label. error is the only blocking tier; the two
# non-actionable informational tiers (assumption/info) collapse to "information".
_SEVERITY_LABEL = {
    "error": "blocker",
    "warning": "warning",
    "assumption": "information",
    "info": "information",
}

# Deterministic, per-code remediation strings. Pure function of the finding's own
# scalars (code + expected/actual); never raises; "" for any unmapped code.
_SUGGESTED_FIX_STATIC = {
    "UNIT_MISSING": "Add an explicit unit to this value so the quantity can be verified.",
    "PHYS_NEGATIVE": "Correct the sign — this physical quantity cannot be negative.",
    "PHYS_PCT_OVER_100": "A single percentage cannot exceed 100%; correct the value.",
    "PHYS_PH_RANGE": "Set the pH within the physical range 0-14.",
    "PHYS_TEMP_BELOW_ABS_ZERO": "Correct the temperature; it is below absolute zero.",
    "PCT_OVER_100": "Reduce the component percentages so they sum to at most 100%.",
    "PCT_UNDER_100": "Confirm the remainder is solvent, or add the missing component(s).",
    "MOLAR_MASS_MISMATCH": "Reconcile the stated mass with C*V*MW (or V*density).",
    "DIL_NOT_A_DILUTION": "The final concentration exceeds the stock; correct C1/C2 — a dilution cannot concentrate.",
    "PREP_MISSING": "Add a preparation step/recipe for this reagent, or mark it purchased ready-made.",
    "READOUT_MISSING": "Add an explicit measurement/read step so the protocol produces data.",
    "CONTROL_MISSING": "Add a control/blank/reference arm to this comparative design.",
    "STRUCT_STEP_NO_INSTRUCTION": "Reconstruct the step instruction, or delete the step.",
    "STRUCT_MATERIAL_NO_NAME": "Supply the material name, or delete the entry.",
    "STRUCT_PARAM_INCOMPLETE": "Supply the parameter's name and value, or delete the parameter.",
    "STRUCT_SUBSTEP_NO_INSTRUCTION": "Supply the substep instruction, or delete the substep.",
    "STRUCT_TITRATION_POINT_NO_LABEL": "Supply the titration point label, or delete the point.",
    "ALOG_VALUE_MISMATCH": "Reconcile the inline value with its assumptions_log entry.",
    "ALOG_VALUE_UNVERIFIABLE": "Fill in a comparable value on both the inline entry and the log.",
    "ALOG_CITATION_MISMATCH": "Reconcile the citation identifier between the inline value and the log.",
    "ALOG_CITATION_MISSING": "Copy the inline citation into the assumptions_log entry.",
    "ALOG_UNIT_MISMATCH": "Use the same unit on the inline value and the log entry.",
    "ALOG_PROVENANCE_MISMATCH": "Use the same provenance tier on the inline value and the log entry.",
    "ALOG_SELECTED_MISMATCH": "Reconcile selected_by_user between the inline value and the log.",
    "ALOG_VERIFY_MISMATCH": "Recompute the log's verify flag to match the host-derived value.",
    "ALOG_INLINE_MISSING": "Add this inline parameter to the assumptions_log.",
    "ALOG_MODEL_EXTRA": "Remove the assumptions_log row, or add the matching inline parameter.",
    "ALOG_DUPLICATE": "Remove the duplicate assumptions_log row.",
}


def _suggested_fix(code, expected, actual, message) -> str:
    """Deterministic remediation hint for a finding. Pure; total (never raises).

    A handful of codes phrase the fix in terms of the computed ``expected`` value;
    the rest map to a static string. An unmapped code yields ``""``."""
    if code == "DIL_MISMATCH":
        return (f"Set the transfer volume to {_fmt(expected)} L (= C2*V2/C1), "
                f"or correct C1/C2/V2.")
    if code == "PLATE_OVER_CAPACITY":
        return (f"Reduce the layout to at most {_fmt(expected)} wells, "
                f"or move to a larger plate.")
    return _SUGGESTED_FIX_STATIC.get(code, "")


def _finding(code, severity, _id, location, message,
             expected=None, actual=None, detail=None):
    return {
        "code": code,
        "severity": severity,
        "id": _id,
        "location": location,
        "message": message,
        "expected": expected,
        "actual": actual,
        "detail": detail if isinstance(detail, dict) else {},
        # --- additive (§IV); no reader keys off these for identity/order ---
        "location_id": _id,
        "severity_label": _SEVERITY_LABEL.get(severity, "information"),
        "suggested_fix": _suggested_fix(code, expected, actual, message),
        "host_verified": True,
    }


# ---------------------------------------------------------------------------
# §7.5 unit sanity & physical possibility
# ---------------------------------------------------------------------------

_PH_RE = re.compile(r"\bph\b", re.IGNORECASE)
_TEMP_RE = re.compile(r"([-+]?\d+\.?\d*)\s*(?:°\s*)?([cCfFkK])\b")


def _walk_scalar_fields(protocol):
    """Yield ``(id, location, field_name, value_raw, unit_raw)`` for every
    quantity-bearing field. ``unit_raw`` may be None when the unit is embedded
    in ``value_raw`` (e.g. titration component volumes)."""
    for i, mat in enumerate(protocol.get("materials") or []):
        if not isinstance(mat, dict):
            continue
        loc = f"materials[{i}] {mat.get('name', '')!r}"
        yield mat.get("_id"), loc, str(mat.get("name") or ""), mat.get("amount"), mat.get("unit")
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        for j, cp in enumerate(step.get("critical_parameters") or []):
            if not isinstance(cp, dict):
                continue
            loc = f"steps[{i}].critical_parameters[{j}] {cp.get('name', '')!r}"
            yield cp.get("_id"), loc, str(cp.get("name") or ""), cp.get("value"), cp.get("unit")
    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        for p, pt in enumerate(ts.get("points") or []):
            if not isinstance(pt, dict):
                continue
            for c, comp in enumerate(pt.get("components") or []):
                if not isinstance(comp, dict):
                    continue
                loc = f"titration_series.points[{p}].components[{c}] {comp.get('name', '')!r}"
                yield comp.get("_id"), loc, str(comp.get("name") or ""), comp.get("volume"), None


def _check_physical_and_units(protocol, findings):
    for _id, loc, name, value_raw, unit_raw in _walk_scalar_fields(protocol):
        q = _qty(value_raw, unit_raw)

        # pH range: whole-word 'ph' field, single clean number.
        if _PH_RE.search(name):
            n = _first_number(value_raw)
            if n is not None and (n < 0 or n > 14):
                findings.append(_finding(
                    "PHYS_PH_RANGE", "error", _id, loc,
                    f"pH {_fmt(n)} is outside the physical range 0-14.",
                    expected="0..14", actual=n))
                continue

        if q is None:
            # Unparseable numeric field: never an error here.
            continue

        dim = q.get("dim")
        base = q.get("base")

        # Physically impossible negatives (volume/mass/conc).
        if dim in ("volume", "mass", "conc") and base is not None and base < 0:
            findings.append(_finding(
                "PHYS_NEGATIVE", "error", _id, loc,
                f"{name or 'value'} is negative ({_fmt(q['value'])}); "
                f"a {dim} cannot be below zero.",
                expected=">= 0", actual=base))
            continue

        # Single percentage value > 100.
        if dim == "percent" and q.get("value") is not None and q["value"] > 100:
            findings.append(_finding(
                "PHYS_PCT_OVER_100", "error", _id, loc,
                f"percentage {_fmt(q['value'])}% exceeds 100%.",
                expected=100.0, actual=q["value"]))
            continue

        # Missing unit where a number is present.
        has_value = q.get("value") is not None
        no_unit = q.get("unit") in (None, "")
        if has_value and no_unit:
            findings.append(_finding(
                "UNIT_MISSING", "warning", _id, loc,
                f"{name or 'value'} has a numeric value ({_fmt(q['value'])}) "
                f"but no unit; the quantity cannot be verified.",
                expected=None, actual=q["value"]))

    # Temperature below absolute zero (steps).
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        temp = step.get("temperature")
        if not isinstance(temp, str):
            continue
        m = _TEMP_RE.search(temp)
        if not m:
            continue
        val = _first_number(m.group(1))
        if val is None:
            continue
        unit = m.group(2).upper()
        floor = {"C": -273.15, "F": -459.67, "K": 0.0}[unit]
        if val < floor and not _close(val, floor):
            findings.append(_finding(
                "PHYS_TEMP_BELOW_ABS_ZERO", "error", step.get("_id"),
                f"steps[{i}].temperature",
                f"temperature {_fmt(val)}°{unit} is below absolute zero.",
                expected=floor, actual=val))


# ---------------------------------------------------------------------------
# §7.1 dilution / C1V1 = C2V2
# ---------------------------------------------------------------------------

_DIL_TOL_REL = 0.02
_DIL_TOL_ABS = 1e-12


def _dil_role(name: str):
    n = (name or "").lower()
    if re.search(r"\bc1\b", n) or "stock" in n:
        return "C1"
    if re.search(r"\bc2\b", n):
        return "C2"
    if re.search(r"\bv1\b", n) or "transfer" in n or "aliquot" in n:
        return "V1"
    if re.search(r"\bv2\b", n):
        return "V2"
    if "final" in n and ("conc" in n or "molar" in n):
        return "C2"
    if "final" in n and "vol" in n:
        return "V2"
    if "total" in n and "vol" in n:
        return "V2"
    return None


def _parse_df(value):
    """Parse a dilution factor: 'A:B' -> B/A ; 'Nx' -> N ; plain N -> N."""
    if not isinstance(value, str):
        n = _first_number(value)
        return n if (n and n > 0) else None
    s = value.strip()
    mm = re.fullmatch(r"\s*([-+]?\d+\.?\d*)\s*:\s*([-+]?\d+\.?\d*)\s*", s)
    if mm:
        try:
            a = float(mm.group(1)); b = float(mm.group(2))
        except ValueError:
            return None
        if a > 0 and b > 0:
            return b / a
        return None
    mx = re.fullmatch(r"\s*([-+]?\d+\.?\d*)\s*[xX]\s*", s)
    if mx:
        try:
            v = float(mx.group(1))
        except ValueError:
            return None
        return v if v > 0 else None
    n = _first_number(s)
    return n if (n and n > 0) else None


def _check_dilution(protocol, findings):
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        step_id = step.get("_id")
        loc = f"steps[{i}] {step.get('title', '')!r}"
        roles = {}
        df = None
        for cp in step.get("critical_parameters") or []:
            if not isinstance(cp, dict):
                continue
            name = cp.get("name") or ""
            if df is None and ("dilut" in name.lower() and "factor" in name.lower()):
                df = _parse_df(cp.get("value"))
                continue
            role = _dil_role(name)
            if role and role not in roles:
                roles[role] = _qty(cp.get("value"), cp.get("unit"))
        _classify_dilution(step_id, loc, roles, df, findings)


def _classify_dilution(anchor_id, loc, roles, df, findings):
    C1 = "C1" in roles
    C2 = "C2" in roles
    V1 = "V1" in roles
    V2 = "V2" in roles
    if not (C1 and C2):
        return
    if not (V1 or V2 or df is not None):
        return
    c1q = roles.get("C1")
    c2q = roles.get("C2")
    # (1) unparseable concentration operand -> unverifiable (never error).
    if c1q is None or c2q is None:
        findings.append(_finding(
            "DIL_UNVERIFIABLE", "warning", anchor_id, loc,
            "dilution stock/final concentration could not be parsed; "
            "the C1*V1 = C2*V2 check was skipped."))
        return
    # (2) dimension check.
    if c1q.get("dim") != "conc" or c2q.get("dim") != "conc" \
            or c1q.get("base") is None or c2q.get("base") is None:
        findings.append(_finding(
            "DIL_INCOMPATIBLE_UNITS", "warning", anchor_id, loc,
            "stock and final concentration are not both molar concentrations; "
            "the dilution check was abandoned."))
        return
    c1b = c1q["base"]
    c2b = c2q["base"]
    # (3) concentrating "by dilution" -> impossible.
    if c2b > c1b and not _within(c2b, c1b, _DIL_TOL_REL, _DIL_TOL_ABS):
        findings.append(_finding(
            "DIL_NOT_A_DILUTION", "error", anchor_id, loc,
            f"final concentration ({_fmt(c2b)} mol/L) exceeds the stock "
            f"({_fmt(c1b)} mol/L); this is a concentration, not a dilution.",
            expected=c1b, actual=c2b))
        return
    # (4) forward C1*V1 = C2*V2 with V2 present.
    v2q = roles.get("V2")
    if v2q is not None and v2q.get("dim") == "volume" and v2q.get("base") is not None:
        if _close(c1b, 0.0):
            findings.append(_finding(
                "DIL_ZERO_STOCK", "warning", anchor_id, loc,
                "stock concentration is zero; V1 = C2*V2 / C1 is undefined."))
            return
        v1_expected = c2b * v2q["base"] / c1b
        v1q = roles.get("V1")
        if v1q is not None and v1q.get("dim") == "volume" and v1q.get("base") is not None:
            v1b = v1q["base"]
            if _within(v1b, v1_expected, _DIL_TOL_REL, _DIL_TOL_ABS):
                findings.append(_finding(
                    "DIL_OK", "info", anchor_id, loc,
                    f"dilution checks out: V1 = {_fmt(v1b)} L matches the "
                    f"expected {_fmt(v1_expected)} L.",
                    expected=v1_expected, actual=v1b))
            else:
                findings.append(_finding(
                    "DIL_MISMATCH", "error", anchor_id, loc,
                    f"transfer volume V1 = {_fmt(v1b)} L disagrees with "
                    f"C2*V2 / C1 = {_fmt(v1_expected)} L.",
                    expected=v1_expected, actual=v1b))
            return
        if "V1" in roles:  # present but unparseable / incompatible
            findings.append(_finding(
                "DIL_UNVERIFIABLE", "warning", anchor_id, loc,
                "transfer volume V1 is unit-less or unparseable; "
                "the dilution math could not be verified."))
            return
        findings.append(_finding(
            "DIL_INCOMPLETE", "assumption", anchor_id, loc,
            "transfer volume V1 was not stated; dilution math left unverified.",
            expected=v1_expected,
            detail={"why": "C1*V1 = C2*V2 needs a stated transfer volume V1; none given"}))
        return
    # (5) dilution-factor path.
    if df is not None and df > 0:
        c2_expected = c1b / df
        if _within(c2b, c2_expected, _DIL_TOL_REL, _DIL_TOL_ABS):
            findings.append(_finding(
                "DIL_OK", "info", anchor_id, loc,
                f"final concentration {_fmt(c2b)} mol/L matches stock/DF "
                f"= {_fmt(c2_expected)} mol/L.",
                expected=c2_expected, actual=c2b))
        else:
            findings.append(_finding(
                "DIL_MISMATCH", "error", anchor_id, loc,
                f"final concentration {_fmt(c2b)} mol/L disagrees with "
                f"stock/DF = {_fmt(c2_expected)} mol/L.",
                expected=c2_expected, actual=c2b))
        return
    # (6) only C1,C2 and a lone volume leg -> incomplete.
    findings.append(_finding(
        "DIL_INCOMPLETE", "assumption", anchor_id, loc,
        "a final volume V2 was not stated; dilution math left unverified.",
        detail={"why": "C1*V1 = C2*V2 needs a stated final volume V2; none given"}))


# ---------------------------------------------------------------------------
# §7.2 mass / molarity consistency (conservative: MOLAR_NO_MW is the common path)
# ---------------------------------------------------------------------------

_MOLAR_TOL_REL = 0.02
_MOLAR_TOL_ABS = 1e-9


def _check_molar(protocol, findings):
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        conc = vol = mass = mw = density = None
        for cp in step.get("critical_parameters") or []:
            if not isinstance(cp, dict):
                continue
            name = (cp.get("name") or "").lower()
            q = _qty(cp.get("value"), cp.get("unit"))
            if mw is None and ("mw" in name or "molar mass" in name
                               or "molecular weight" in name or "g/mol" in name
                               or "g/mol" in str(cp.get("unit") or "").lower()):
                mw = _first_number(cp.get("value"))
                continue
            if density is None and ("density" in name or "g/ml" in name):
                density = _first_number(cp.get("value"))
                continue
            if q is None:
                continue
            if conc is None and q.get("dim") == "conc":
                conc = q
            elif vol is None and q.get("dim") == "volume":
                vol = q
            elif mass is None and q.get("dim") == "mass":
                mass = q
        if conc is None or vol is None:
            continue
        loc = f"steps[{i}] {step.get('title', '')!r}"
        anchor = step.get("_id")
        if mw is not None and mw > 0 and mass is not None:
            expected = conc["base"] * vol["base"] * mw
            actual = mass["base"]
            if _within(actual, expected, _MOLAR_TOL_REL, _MOLAR_TOL_ABS):
                findings.append(_finding(
                    "MOLAR_OK", "info", anchor, loc,
                    f"mass {_fmt(actual)} g matches C*V*MW = {_fmt(expected)} g.",
                    expected=expected, actual=actual))
            else:
                findings.append(_finding(
                    "MOLAR_MASS_MISMATCH", "error", anchor, loc,
                    f"stated mass {_fmt(actual)} g disagrees with "
                    f"C*V*MW = {_fmt(expected)} g.",
                    expected=expected, actual=actual))
        elif density is not None and density > 0 and mass is not None:
            expected = (vol["base"] / 1e-3) * density  # base L -> mL
            actual = mass["base"]
            if _within(actual, expected, _MOLAR_TOL_REL, _MOLAR_TOL_ABS):
                findings.append(_finding(
                    "MOLAR_OK", "info", anchor, loc,
                    f"mass {_fmt(actual)} g matches V*density = {_fmt(expected)} g.",
                    expected=expected, actual=actual))
            else:
                findings.append(_finding(
                    "MOLAR_MASS_MISMATCH", "error", anchor, loc,
                    f"stated mass {_fmt(actual)} g disagrees with "
                    f"V*density = {_fmt(expected)} g.",
                    expected=expected, actual=actual))
        elif mass is not None:
            findings.append(_finding(
                "MOLAR_NO_MW", "assumption", anchor, loc,
                "molecular weight not provided; the molar/mass cross-check was skipped.",
                detail={"why": "molar/mass cross-check needs a molecular weight; none stated"}))


# ---------------------------------------------------------------------------
# §7.3 percent-sum (conservative subset)
# ---------------------------------------------------------------------------

def _check_percent_sums(protocol, findings):
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        pcts = []
        for cp in step.get("critical_parameters") or []:
            if not isinstance(cp, dict):
                continue
            q = _qty(cp.get("value"), cp.get("unit"))
            if q is not None and q.get("dim") == "percent" and q.get("value") is not None:
                pcts.append(q["value"])
        if len(pcts) < 2:
            continue
        total = sum(pcts)
        loc = f"steps[{i}] {step.get('title', '')!r}"
        anchor = step.get("_id")
        if total > 100.5:
            findings.append(_finding(
                "PCT_OVER_100", "error", anchor, loc,
                f"component percentages sum to {_fmt(total)}%, exceeding 100%.",
                expected=100.0, actual=total))
        elif total < 99.5:
            findings.append(_finding(
                "PCT_UNDER_100", "warning", anchor, loc,
                f"component percentages sum to {_fmt(total)}%; the remainder "
                f"is presumably solvent.",
                expected=100.0, actual=total))


# ---------------------------------------------------------------------------
# §7.4 plate / replicate arithmetic (fires only on cleanly-integer inputs)
# ---------------------------------------------------------------------------

_WELL_RE = re.compile(r"\b(96|384)[- ]?well\b", re.IGNORECASE)


def _plate_capacity(protocol):
    for key in ("plate", "plate_format", "plate_size", "plate_capacity"):
        v = protocol.get(key)
        n = _as_int(v)
        if n in (96, 384):
            return n
        if isinstance(v, str):
            mm = _WELL_RE.search(v)
            if mm:
                return int(mm.group(1))
    # Scan equipment + step text for an explicit '96-well' / '384-well'.
    haystack = []
    for e in protocol.get("equipment") or []:
        if isinstance(e, str):
            haystack.append(e)
    for step in protocol.get("steps") or []:
        if isinstance(step, dict):
            for k in ("title", "instruction"):
                if isinstance(step.get(k), str):
                    haystack.append(step[k])
    for text in haystack:
        mm = _WELL_RE.search(text)
        if mm:
            return int(mm.group(1))
    return None


def _check_plate(protocol, findings):
    conditions = _as_int(protocol.get("conditions"))
    controls = _as_int(protocol.get("controls"))
    replicates = _as_int(protocol.get("replicates"))
    if replicates is None:
        bio = _as_int(protocol.get("biological_replicates"))
        tech = _as_int(protocol.get("technical_replicates"))
        if bio is not None and tech is not None:
            replicates = bio * tech
        elif bio is not None:
            replicates = bio
        elif tech is not None:
            replicates = tech
    capacity = _plate_capacity(protocol)

    if conditions is None or replicates is None or capacity is None:
        # Only surface an assumption if partial numbers were actually found.
        if (conditions is not None or replicates is not None) and capacity is not None:
            findings.append(_finding(
                "PLATE_INSUFFICIENT_DATA", "assumption", None, "protocol",
                "plate-capacity check skipped: not all of conditions, replicates, "
                "and plate size were cleanly stated as integers.",
                detail={"why": "well-count arithmetic needs conditions, replicates, "
                               "and plate size as integers; at least one was missing"}))
        return

    wells_used = conditions * replicates + (controls or 0)
    if wells_used > capacity:
        findings.append(_finding(
            "PLATE_OVER_CAPACITY", "error", None, "protocol",
            f"layout needs {wells_used} wells but the plate holds {capacity}.",
            expected=capacity, actual=wells_used))
    else:
        findings.append(_finding(
            "PLATE_OK", "info", None, "protocol",
            f"layout uses {wells_used} of {capacity} wells.",
            expected=capacity, actual=wells_used))


# ---------------------------------------------------------------------------
# run_checks (§1) — pure, deterministic, sorted
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Structural completeness (§I.4) — a repaired-placeholder or empty structural
# field is a loud, blocking finding, never a silent gap.
# ---------------------------------------------------------------------------

def _struct_missing(v) -> bool:
    """A structural field is missing when it is empty/whitespace OR carries a
    repair sentinel. Total: never raises (deep-read only)."""
    if not isinstance(v, str):
        return True
    return v.strip() == "" or v in STRUCT_PLACEHOLDERS


def _check_structure(protocol, findings) -> None:
    """Emit ``error`` findings for any structurally incomplete entry (a step with
    no instruction, a material with no name, an incomplete parameter, a substep
    with no instruction, a titration point with no label). Anchored on the stamped
    ``_id``. Because they are errors, the gate is ``blocked`` -> a repaired protocol
    can never ship clean."""
    for i, mat in enumerate(protocol.get("materials") or []):
        if not isinstance(mat, dict):
            continue
        if _struct_missing(mat.get("name")):
            findings.append(_finding(
                "STRUCT_MATERIAL_NO_NAME", "error", mat.get("_id"),
                f"materials[{i}]",
                "Material has no name (supply the material name or delete the entry).",
            ))
    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if _struct_missing(step.get("instruction")):
            findings.append(_finding(
                "STRUCT_STEP_NO_INSTRUCTION", "error", step.get("_id"),
                f"steps[{i}]",
                "Step has no instruction (reconstruct the instruction or delete the step).",
            ))
        for j, cp in enumerate(step.get("critical_parameters") or []):
            if not isinstance(cp, dict):
                continue
            name_missing = _struct_missing(cp.get("name"))
            val = cp.get("value")
            value_missing = _struct_missing(val) and not isinstance(val, (int, float))
            if name_missing or value_missing:
                lacks = " and ".join(
                    x for x in ["name" if name_missing else "", "value" if value_missing else ""] if x
                )
                findings.append(_finding(
                    "STRUCT_PARAM_INCOMPLETE", "error", cp.get("_id"),
                    f"steps[{i}].critical_parameters[{j}]",
                    f"Critical parameter has no {lacks} (supply it or delete the parameter).",
                ))
        for k, ss in enumerate(step.get("substeps") or []):
            if not isinstance(ss, dict):
                continue
            if _struct_missing(ss.get("instruction")):
                findings.append(_finding(
                    "STRUCT_SUBSTEP_NO_INSTRUCTION", "error", ss.get("_id"),
                    f"steps[{i}].substeps[{k}]",
                    "Substep has no instruction (supply it or delete the substep).",
                ))
    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        for p, pt in enumerate(ts.get("points") or []):
            if not isinstance(pt, dict):
                continue
            if _struct_missing(pt.get("label")):
                findings.append(_finding(
                    "STRUCT_TITRATION_POINT_NO_LABEL", "error", pt.get("_id"),
                    f"titration_series.points[{p}]",
                    "Titration point has no label (supply it or delete the point).",
                ))


# ---------------------------------------------------------------------------
# Conservative completeness heuristics (§VI) — warning-max, never a false blocker.
# Each demotes to NO finding on any ambiguity: a correct protocol produces none.
# ---------------------------------------------------------------------------

# A material name that DENOTES a prepared solution (a buffer/stock/master-mix, or a
# made-up reagent named by its working concentration like "50 mM Tris" / "10x PBS").
_PREP_NAME_RE = re.compile(
    r"\bbuffer\b|\bstock\b|\bmaster ?mix\b|\d\s*(?:mM|M|µM|μM|uM|nM|x)\b",
    re.IGNORECASE,
)
# A step that DEFINES how a solution is made (shares a token with the candidate).
_PREP_VERB_RE = re.compile(
    r"\b(?:prepare|prepar|make|made|dilut|dissolv|reconstitut|mix|formulat)",
    re.IGNORECASE,
)
# A number+unit inside a note/description reads as a composition recipe.
_RECIPE_HINT_RE = re.compile(
    r"\d\s*(?:mM|M|µM|μM|uM|nM|pM|%|mg|g|ug|µg|μg|mL|uL|µL|μL|L)\b",
    re.IGNORECASE,
)
# ≥4-char alphabetic words are the "distinctive" tokens shared between a candidate
# material name and a prep step (short words like "the"/"mix" are too generic).
_SIG_WORD_RE = re.compile(r"[a-z]{4,}")


def _sig_tokens(text) -> set:
    return set(_SIG_WORD_RE.findall(str(text or "").lower()))


def _material_has_recipe(mat: dict) -> bool:
    """A material carries its own recipe when its provenance_note (or any prose
    description) states a composition — i.e. contains a number+unit."""
    for key in ("provenance_note", "description", "notes"):
        v = mat.get(key)
        if isinstance(v, str) and _RECIPE_HINT_RE.search(v):
            return True
    return False


def _check_prep(protocol, findings) -> None:
    """PREP_MISSING (warning, anchored at the material ``_id``): a material whose
    name clearly denotes a *prepared* solution but which is neither defined by a
    recipe nor made by a prep step nor bought ready-made. Demotes on any of those,
    so a purchased reagent or a solution with a "Prepare ..." step never fires."""
    steps = protocol.get("steps") or []
    # Pre-index the significant tokens of every step that contains a prep verb.
    prep_step_tokens: list = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        text = f"{step.get('title') or ''} {step.get('instruction') or ''}"
        if _PREP_VERB_RE.search(text):
            prep_step_tokens.append(_sig_tokens(text))

    for i, mat in enumerate(protocol.get("materials") or []):
        if not isinstance(mat, dict):
            continue
        name = mat.get("name")
        if not isinstance(name, str) or not _PREP_NAME_RE.search(name):
            continue  # not a prepared-solution name -> not a candidate
        # Demote: bought ready-made (a vendor other than in-house preparation).
        vendor = mat.get("vendor_or_grade")
        if isinstance(vendor, str) and vendor.strip() and \
                _collapse(vendor) != _collapse("prepared in-house"):
            continue
        # Demote: the material carries its own composition recipe.
        if _material_has_recipe(mat):
            continue
        # Demote: a prep step shares a distinctive (≥4-char) token with this name.
        cand_tokens = _sig_tokens(name)
        if any(cand_tokens & toks for toks in prep_step_tokens):
            continue
        findings.append(_finding(
            "PREP_MISSING", "warning", mat.get("_id"),
            f"materials[{i}]",
            f"'{name}' names a prepared solution but the protocol gives no recipe, "
            f"no preparation step, and no vendor; its composition is undefined.",
        ))


# Any step whose text contains one of these tokens counts as an explicit readout.
_READOUT_RE = re.compile(
    r"\b(?:read|measur|record|acquir|imag|detect|readout|quantif|absorb"
    r"|fluoresc|luminesc|od|signal|count|cq|ct|spectr)",
    re.IGNORECASE,
)
# A design is "comparative" when its text screens/compares/titrates across a series.
_COMPARATIVE_RE = re.compile(
    r"\b(?:screen|compar|rank|versus|vs|dose|titrat|series)",
    re.IGNORECASE,
)
# A control/reference arm anywhere satisfies the comparative-design control check.
_CONTROL_RE = re.compile(
    r"\b(?:control|blank|vehicle|negative|positive|reference|mock|untreated|no-|bsa|baseline)",
    re.IGNORECASE,
)


def _protocol_text(protocol) -> str:
    """Concatenate the human-readable text of every step, material, and critical
    parameter — the haystack the readout/control heuristics scan."""
    bits: list = []
    for mat in protocol.get("materials") or []:
        if isinstance(mat, dict):
            bits.append(str(mat.get("name") or ""))
    for step in protocol.get("steps") or []:
        if not isinstance(step, dict):
            continue
        bits.append(str(step.get("title") or ""))
        bits.append(str(step.get("instruction") or ""))
        for cp in step.get("critical_parameters") or []:
            if isinstance(cp, dict):
                bits.append(str(cp.get("name") or ""))
    return " ".join(bits)


def _check_readout_control(protocol, findings) -> None:
    """Two protocol-level completeness heuristics (id=None, warning-max):

    READOUT_MISSING — a multi-step protocol (≥2 steps) whose steps never measure,
    read, or acquire anything and which has no titration_series. A single-step or
    titration protocol demotes to no finding.

    CONTROL_MISSING — a *clearly comparative* design (a titration_series, or text
    that screens/compares/titrates a series) that names no control/blank/reference
    arm anywhere. A non-comparative design is skipped entirely (ambiguity demotes)."""
    steps = [s for s in (protocol.get("steps") or []) if isinstance(s, dict)]
    has_titration = isinstance(protocol.get("titration_series"), dict)

    # --- READOUT_MISSING ---
    if len(steps) >= 2 and not has_titration:
        readout_seen = any(
            _READOUT_RE.search(f"{s.get('title') or ''} {s.get('instruction') or ''}")
            for s in steps
        )
        if not readout_seen:
            findings.append(_finding(
                "READOUT_MISSING", "warning", None, "protocol",
                "no step reads, measures, or acquires a result and there is no "
                "titration series; the protocol may produce no data.",
            ))

    # --- CONTROL_MISSING (comparative designs only) ---
    text = _protocol_text(protocol)
    comparative = has_titration or bool(_COMPARATIVE_RE.search(text))
    if comparative and not _CONTROL_RE.search(text):
        findings.append(_finding(
            "CONTROL_MISSING", "warning", None, "protocol",
            "this looks like a comparative/screening design but names no control, "
            "blank, or reference arm to interpret the comparison against.",
        ))


def run_checks(protocol: dict) -> list:
    """Run every check family over a finalized protocol and return a flat,
    deterministically-ordered ``list[Finding]``.

    Pure: does not mutate ``protocol`` (deep-read only). Equal input yields a
    byte-identical list."""
    findings: list = []
    if not isinstance(protocol, dict):
        return findings
    _check_physical_and_units(protocol, findings)
    _check_dilution(protocol, findings)
    _check_molar(protocol, findings)
    _check_percent_sums(protocol, findings)
    _check_plate(protocol, findings)
    _check_structure(protocol, findings)
    _check_prep(protocol, findings)
    _check_readout_control(protocol, findings)

    findings.sort(key=lambda f: (f.get("id") or "", f.get("location") or "", f.get("code")))
    return findings


# ---------------------------------------------------------------------------
# Finding identity — content hash (Epic 3, §4)
# ---------------------------------------------------------------------------
#
# A correctness finding's identity is a pure host-computed content hash the model
# cannot forge. It is deliberately tolerant: two independently-worded descriptions
# of the same defect hash to the SAME key (biasing toward merging near-duplicates —
# the safe direction for a skeptical verifier). Severity is intentionally NOT part
# of the basis, so identity survives severity coercion/re-sort.

# The 12-value correctness-finding category enum (mirrors the category enum on
# EMIT_CORRECTNESS_REVIEW_TOOL / EMIT_FIX_VERIFICATION_TOOL in app/schemas.py). A
# finding whose category falls outside this set normalizes to "other", so a garbage
# or coerced category cannot fracture identity.
_CORRECTNESS_CATEGORIES = frozenset({
    "missing_control", "implausible_value", "unit_or_scaling", "ordering",
    "logic", "internal_contradiction", "ambiguous_instruction",
    "readout_mismatch", "safety", "missing_detail", "impractical", "other",
})

# Stopwords stripped from the problem signature so a reworded description of the same
# defect still hashes identically. Digit/percent/unit tokens are always kept verbatim.
_STOP = frozenset(
    "a an the is are was were be to of for and or in on at "
    "this that it its with as no not".split()
)


def _collapse(s) -> str:
    """NFKC-normalize, lowercase, whitespace-collapse, and strip a scalar.

    The one shared normalizer for finding identity. Total: coerces via ``str`` and
    never raises."""
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def _norm_finding_parts(finding: dict) -> tuple[str, str, str]:
    """Reduce a correctness finding to its identity basis
    ``(category_norm, location_norm, problem_sig)``.

    ``problem_sig`` is an order-independent, de-duplicated, stopword-stripped token
    set that keeps digit/percent/unit tokens (e.g. ``9``, ``37c``, ``5ml``, ``%``),
    so two rewordings of one defect collapse to the same signature. Severity is
    excluded by design."""
    if not isinstance(finding, dict):
        finding = {}
    cat = _collapse(finding.get("category") or "other")
    if cat not in _CORRECTNESS_CATEGORIES:
        cat = "other"
    loc = _collapse(finding.get("location") or "")
    toks = [t for t in re.split(r"[^0-9a-z%./-]+", _collapse(finding.get("problem") or ""))
            if t and t not in _STOP]
    problem_sig = " ".join(sorted(set(toks)))
    return cat, loc, problem_sig


def finding_key(finding: dict) -> str:
    """Deterministic content hash identifying a correctness finding.

    Pure and idempotent: equal ``(category, location, problem)`` yields a
    byte-identical ``f_``-prefixed key, independent of severity, wording order,
    casing, whitespace, and list position. No clock, no RNG, no model id."""
    cat, loc, sig = _norm_finding_parts(finding)
    canon = "␟".join((cat, loc, sig))      # ␟-joined (unit-separator)
    return "f_" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Host-generated canonical assumptions log (§III)
# ---------------------------------------------------------------------------
#
# The model's ``assumptions_log`` is never trusted as an independent copy. The
# host derives its OWN canonical log from the inline non-stated entries, then the
# comparison below reports every field-level disagreement between the two. Both are
# pure (deep-read only) and deterministic.

# Inline provenance tiers that carry an assumption worth logging (mirrors the old
# name-only _consistency_check scope, so no new false positives).
_ALOG_SCOPE = frozenset({
    "best_practice", "default_verify", "literature_grounded", "user_input",
})

_ALOG_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm_param(name) -> str:
    """Normalize a parameter/material name for cross-log matching. Byte-identical
    to ``validation._norm_param`` (kept in sync deliberately; replicated here to
    avoid a checks<->validation import cycle)."""
    return " ".join(_ALOG_WORD_RE.findall(str(name or "").lower()))


def _derive_verify(entity: dict) -> bool:
    """Deterministic ``verify`` derivation (§III.2) from host-stamped flags.

    ``user_input`` the user selected, or a fully-verified ``literature_grounded``
    value, need no re-verification -> ``False``; everything else -> ``True``.
    Absent flags read falsy -> ``True`` (conservative in standalone unit paths)."""
    prov = entity.get("provenance")
    if prov == "user_input" and bool(entity.get("selected_by_user")):
        return False
    if (prov == "literature_grounded"
            and bool(entity.get("identifier_verified"))
            and bool(entity.get("metadata_matched"))):
        return False
    return True


def _inline_entries(protocol: dict):
    """Yield ``(entity, kind, name)`` for every in-scope inline entry (materials,
    then each step's critical_parameters), document order."""
    for mat in protocol.get("materials") or []:
        if isinstance(mat, dict) and mat.get("provenance") in _ALOG_SCOPE:
            yield mat, "material", mat.get("name")
    for step in protocol.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for cp in step.get("critical_parameters") or []:
            if isinstance(cp, dict) and cp.get("provenance") in _ALOG_SCOPE:
                yield cp, "critical_parameter", cp.get("name")


def host_assumptions_log(protocol: dict) -> list:
    """Build the host's canonical assumptions log from inline non-stated entries.

    Pure, deterministic, document order. Each row mirrors an inline material or
    critical parameter; ``verify`` is host-derived, ``stable_id`` is the entity's
    content-hash id (present once ``assign_stable_ids`` has run)."""
    log: list = []
    if not isinstance(protocol, dict):
        return log
    for entity, kind, name in _inline_entries(protocol):
        val = entity.get("value")
        if val is None:
            val = entity.get("amount")
        citation = entity.get("citation") if isinstance(entity.get("citation"), dict) else None
        citation_id = citation.get("identifier") if citation else None
        stable_field = _STABLE_FIELD["critical_parameter"] if kind == "critical_parameter" else _STABLE_FIELD["material"]
        log.append({
            "parameter": name,
            "param_norm": _norm_param(name),
            "value": val,
            "unit": entity.get("unit"),
            "provenance": entity.get("provenance"),
            "selected_by_user": bool(entity.get("selected_by_user")),
            "citation_id": citation_id,
            "verify": _derive_verify(entity),
            "_id": entity.get("_id"),
            "stable_id": entity.get(stable_field),
        })
    return log


def _alog_value_equal(host_val, host_unit, model_val, model_unit):
    """Return True/False if the two values definitely agree/disagree, or None if
    the comparison is UNVERIFIABLE (either side empty or unparseable).

    A separate ``unit`` field is folded into the value (``_qty``) so an inline
    ``value="100", unit="uL"`` compares equal to a log ``value="100 uL"``. When
    both sides parse to the SAME dimension we compare base magnitudes; otherwise we
    fall back to normalized string equality on the raw value."""
    hs = "" if host_val is None else str(host_val).strip()
    ms = "" if model_val is None else str(model_val).strip()
    if hs == "" or ms == "":
        return None
    hq = _qty(host_val, host_unit)
    mq = _qty(model_val, model_unit)
    if (hq and mq and hq.get("dim") is not None
            and hq.get("dim") == mq.get("dim")
            and hq.get("base") is not None and mq.get("base") is not None):
        return abs(hq["base"] - mq["base"]) <= 1e-12 * max(1.0, abs(hq["base"]), abs(mq["base"]))
    # Fall back to normalized string equality on the raw value scalars.
    return _collapse(host_val) == _collapse(model_val)


def _alog_disagreement(code, severity, host_row, message, host=None, model=None):
    return {
        "code": code,
        "severity": severity,
        "param_norm": host_row.get("param_norm") if host_row else "",
        "id": host_row.get("_id") if host_row else None,
        "stable_id": host_row.get("stable_id") if host_row else None,
        "host": host,
        "model": model,
        "message": message,
    }


def compare_assumptions_log(protocol: dict, host_log: list) -> list:
    """The COMPLETE field-level comparison of the host canonical log against the
    model's emitted ``assumptions_log`` (§III.3). Returns a deterministically-ordered
    list of disagreement dicts (data only — this layer does NOT route them through
    the quality gate). Pure: deep-read, no mutation, no clock/RNG.

    One disagreement per field-level difference, matched by ``param_norm``. Every
    comparison demotes on ambiguity so a correct protocol yields an empty list."""
    disagreements: list = []
    if not isinstance(protocol, dict):
        return disagreements
    host_by_norm = {}
    for row in host_log or []:
        host_by_norm.setdefault(row.get("param_norm"), row)

    model_rows = [a for a in (protocol.get("assumptions_log") or []) if isinstance(a, dict)]
    model_first = {}       # first model row per norm (document order)
    dup_seen = set()
    for a in model_rows:
        norm = _norm_param(a.get("parameter"))
        if norm == "":
            continue
        if norm in model_first:
            if norm not in dup_seen:
                dup_seen.add(norm)
                host_row = host_by_norm.get(norm) or {"param_norm": norm, "_id": None, "stable_id": None}
                disagreements.append(_alog_disagreement(
                    "ALOG_DUPLICATE", "warning", host_row,
                    f"assumptions_log lists '{norm}' more than once; compared against the first."))
            continue
        model_first[norm] = a

    # Field-level comparison for every inline (host) row.
    for norm, host_row in host_by_norm.items():
        if norm == "":
            continue
        model = model_first.get(norm)
        if model is None:
            disagreements.append(_alog_disagreement(
                "ALOG_INLINE_MISSING", "warning", host_row,
                f"'{norm}' is filled inline but absent from the model's assumptions_log.",
                host=host_row.get("value"), model=None))
            continue

        # value (fold each side's separate unit field into the quantity)
        model_val = model.get("value")
        agree = _alog_value_equal(
            host_row.get("value"), host_row.get("unit"), model_val, model.get("unit"))
        if agree is None:
            hs = host_row.get("value")
            ms = model_val
            if (hs is not None and str(hs).strip() != "") or (ms is not None and str(ms).strip() != ""):
                disagreements.append(_alog_disagreement(
                    "ALOG_VALUE_UNVERIFIABLE", "warning", host_row,
                    f"value for '{norm}' could not be compared (empty or unparseable on one side).",
                    host=hs, model=ms))
        elif agree is False:
            disagreements.append(_alog_disagreement(
                "ALOG_VALUE_MISMATCH", "error", host_row,
                f"value for '{norm}' disagrees: inline {host_row.get('value')!r} vs log {model_val!r}.",
                host=host_row.get("value"), model=model_val))

        # citation identifier
        host_cite = host_row.get("citation_id")
        model_cite_raw = model.get("citation")
        model_cite = model_cite_raw.get("identifier") if isinstance(model_cite_raw, dict) else model_cite_raw
        hc = "" if host_cite is None else str(host_cite).strip()
        mc = "" if model_cite is None else str(model_cite).strip()
        if hc and mc and hc != mc:
            disagreements.append(_alog_disagreement(
                "ALOG_CITATION_MISMATCH", "error", host_row,
                f"citation identifier for '{norm}' disagrees: inline {hc!r} vs log {mc!r}.",
                host=hc, model=mc))
        elif not hc and mc:
            # model asserts grounding the canonical lacks (fabricated grounding)
            disagreements.append(_alog_disagreement(
                "ALOG_CITATION_MISMATCH", "error", host_row,
                f"assumptions_log claims citation {mc!r} for '{norm}' but the inline value has none.",
                host=None, model=mc))
        elif hc and not mc:
            disagreements.append(_alog_disagreement(
                "ALOG_CITATION_MISSING", "warning", host_row,
                f"inline value for '{norm}' carries citation {hc!r} but the log omits it.",
                host=hc, model=None))

        # unit (both present)
        hu = host_row.get("unit")
        mu = model.get("unit")
        if hu and mu and _collapse(hu) != _collapse(mu):
            disagreements.append(_alog_disagreement(
                "ALOG_UNIT_MISMATCH", "warning", host_row,
                f"unit for '{norm}' disagrees: inline {hu!r} vs log {mu!r}.",
                host=hu, model=mu))

        # provenance tier
        hp = host_row.get("provenance")
        mp = model.get("provenance")
        if hp is not None and mp is not None and _collapse(hp) != _collapse(mp):
            disagreements.append(_alog_disagreement(
                "ALOG_PROVENANCE_MISMATCH", "warning", host_row,
                f"provenance for '{norm}' disagrees: inline {hp!r} vs log {mp!r}.",
                host=hp, model=mp))

        # selected_by_user
        hsel = bool(host_row.get("selected_by_user"))
        if "selected_by_user" in model:
            msel = bool(model.get("selected_by_user"))
            if hsel != msel:
                disagreements.append(_alog_disagreement(
                    "ALOG_SELECTED_MISMATCH", "warning", host_row,
                    f"selected_by_user for '{norm}' disagrees: inline {hsel} vs log {msel}.",
                    host=hsel, model=msel))

        # verify
        hv = bool(host_row.get("verify"))
        if "verify" in model:
            mv = bool(model.get("verify"))
            if hv != mv:
                disagreements.append(_alog_disagreement(
                    "ALOG_VERIFY_MISMATCH", "warning", host_row,
                    f"verify flag for '{norm}' disagrees: host-derived {hv} vs log {mv}.",
                    host=hv, model=mv))

    # model rows that match no inline entry
    for norm, a in model_first.items():
        if norm not in host_by_norm:
            disagreements.append(_alog_disagreement(
                "ALOG_MODEL_EXTRA", "warning",
                {"param_norm": norm, "_id": None, "stable_id": None},
                f"assumptions_log lists '{norm}' with no matching inline material or parameter.",
                host=None, model=a.get("value")))

    disagreements.sort(key=lambda d: (d.get("param_norm") or "", d.get("code") or ""))
    return disagreements
