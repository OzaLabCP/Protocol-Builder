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

import re

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
# Finding constructor (§2)
# ---------------------------------------------------------------------------

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

    findings.sort(key=lambda f: (f.get("id") or "", f.get("location") or "", f.get("code")))
    return findings
