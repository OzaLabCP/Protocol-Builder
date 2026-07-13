"""Versioned Pydantic model tree for the emitted protocol (EPIC-2, §I).

This module is a *validation/repair gate only*. The emitted protocol dict remains
the runtime source of truth: models here are ``model_validate``d, never serialized
back over the dict, and nothing re-types or reorders the protocol.

Design constraints (spec §I.2):
  * Base config is lenient — ``extra="allow"`` so every host attestation key
    (``_id``, ``quote_verified``, ``identifier_verified``, ``metadata_matched``,
    ``claim_support_status``, ``citation_verified`` and the new stable-id fields)
    survives a round-trip; ``str_strip_whitespace=True`` so an all-whitespace
    "value" is treated as empty.
  * Only *truly incomplete* entries are rejected. Everything currently-valid must
    pass, so numeric fields accept ``str | number | None`` unions with no write-back
    and provenance/enum fields are typed ``Optional[str]`` (the strict enum stays
    authoritative on the wire in ``EMIT_PROTOCOL_TOOL``).

Public API:
  * ``PROTOCOL_SCHEMA_VERSION`` — the STRING, namespaced emitted-protocol version.
    Never confuse with ``projects.CURRENT_SCHEMA_VERSION`` (an int app-data version).
  * placeholder sentinels — frozen strings written by ``repair_structure``.
  * ``coerce_emit_payload`` — pure emit-boundary gate (does NOT mutate).
  * ``repair_structure`` — in-place per-entry structural repair pre-pass.

This module imports nothing from the rest of the app (``checks.py`` imports the
sentinels from *here*), so there is no import cycle.
"""

from __future__ import annotations

from typing import Any, List, Optional, Union

from pydantic import BaseModel, ConfigDict, field_validator

# ---------------------------------------------------------------------------
# Version constant + placeholder sentinels (frozen strings)
# ---------------------------------------------------------------------------

# STRING, namespaced on purpose — never confuse with projects.CURRENT_SCHEMA_VERSION:int.
PROTOCOL_SCHEMA_VERSION = "emit_protocol/1"

STEP_INSTR_PLACEHOLDER = "[MISSING INSTRUCTION — reconstruct or delete this step]"
MATERIAL_NAME_PLACEHOLDER = "[MISSING MATERIAL NAME — supply or delete]"
PARAM_NAME_PLACEHOLDER = "[MISSING PARAMETER NAME — supply or delete]"
PARAM_VALUE_PLACEHOLDER = "[MISSING PARAMETER VALUE — supply or delete]"
SUBSTEP_INSTR_PLACEHOLDER = "[MISSING SUBSTEP INSTRUCTION — supply or delete]"
POINT_LABEL_PLACEHOLDER = "[MISSING POINT LABEL — supply or delete]"

# Every sentinel, so a detector can recognise a repaired-placeholder field.
STRUCT_PLACEHOLDERS = frozenset({
    STEP_INSTR_PLACEHOLDER,
    MATERIAL_NAME_PLACEHOLDER,
    PARAM_NAME_PLACEHOLDER,
    PARAM_VALUE_PLACEHOLDER,
    SUBSTEP_INSTR_PLACEHOLDER,
    POINT_LABEL_PLACEHOLDER,
})

# Prefix marking a repair-emitted open_question, so a repaired protocol carries a
# loud, blocker-shaped note rather than a silent placeholder.
_BLOCKED_MARK = "[BLOCKED] "


def _nonempty(v: Any) -> bool:
    """True only for a non-empty, non-whitespace string."""
    return isinstance(v, str) and v.strip() != ""


def _missing(v: Any) -> bool:
    """A structural field is *missing* when it is empty/whitespace OR carries a
    repair sentinel. Total: never raises."""
    if not isinstance(v, str):
        return True
    s = v.strip()
    return s == "" or v in STRUCT_PLACEHOLDERS


# ---------------------------------------------------------------------------
# Model tree (lenient, validate-only)
# ---------------------------------------------------------------------------

_BASE_CONFIG = ConfigDict(
    extra="allow",                 # host keys + stable-id siblings must survive
    str_strip_whitespace=True,     # whitespace-only becomes "" -> caught as empty
    coerce_numbers_to_str=False,
)

_Number = Union[str, int, float]


def _require_nonempty(field: str):
    """Build a field_validator that rejects an empty/whitespace string. With
    ``str_strip_whitespace`` the incoming value is already stripped."""

    def _check(cls, v):  # noqa: N805
        if not (isinstance(v, str) and v.strip() != ""):
            raise ValueError(f"{field} must be a non-empty string")
        return v

    return field_validator(field)(classmethod(_check))


class _Model(BaseModel):
    model_config = _BASE_CONFIG


class Evidence(_Model):
    """Lenient: nothing required (the strict enum stays on the wire)."""

    excerpt: Optional[str] = None
    section: Optional[str] = None
    evidence_type: Optional[str] = None
    source_type: Optional[str] = None


class Citation(_Model):
    """Lenient: nothing required."""

    title: Optional[str] = None
    authors: Optional[str] = None
    year: Optional[Union[int, str]] = None
    identifier: Optional[str] = None
    url: Optional[str] = None
    evidence: Optional[Evidence] = None


class Material(_Model):
    name: str
    amount: Optional[_Number] = None
    unit: Optional[str] = None
    vendor_or_grade: Optional[str] = None
    provenance: Optional[str] = None
    selected_by_user: Optional[bool] = None
    provenance_note: Optional[str] = None
    flexibility: Optional[str] = None
    needs_user_input: Optional[bool] = None
    source_quote: Optional[str] = None
    citation: Optional[Citation] = None

    _v_name = _require_nonempty("name")


class CriticalParameter(_Model):
    name: str
    value: _Number
    unit: Optional[str] = None
    provenance: Optional[str] = None
    selected_by_user: Optional[bool] = None
    provenance_note: Optional[str] = None
    flexibility: Optional[str] = None
    needs_user_input: Optional[bool] = None
    source_quote: Optional[str] = None
    citation: Optional[Citation] = None

    _v_name = _require_nonempty("name")

    @field_validator("value")
    @classmethod
    def _value_nonempty(cls, v):
        # Numeric values pass through untouched; a string value must be non-empty.
        if isinstance(v, str) and v.strip() == "":
            raise ValueError("value must be a non-empty string")
        return v


class Substep(_Model):
    number: Optional[_Number] = None
    instruction: str
    provenance: Optional[str] = None
    provenance_note: Optional[str] = None
    flexibility: Optional[str] = None
    needs_user_input: Optional[bool] = None
    source_quote: Optional[str] = None

    _v_instruction = _require_nonempty("instruction")


class Step(_Model):
    number: Optional[_Number] = None
    title: Optional[str] = None
    instruction: str
    duration: Optional[str] = None
    temperature: Optional[str] = None
    provenance: Optional[str] = None
    source_quote: Optional[str] = None
    critical_parameters: List[CriticalParameter] = []
    substeps: List[Substep] = []
    warnings: List[Any] = []

    _v_instruction = _require_nonempty("instruction")


class TitrationComponent(_Model):
    """Lenient: nothing required."""

    name: Optional[str] = None
    volume: Optional[_Number] = None


class TitrationPoint(_Model):
    label: str
    target_concentration: Optional[_Number] = None
    components: List[TitrationComponent] = []

    _v_label = _require_nonempty("label")


class TitrationSeries(_Model):
    variable: str
    unit: Optional[str] = None
    spacing: Optional[str] = None
    rationale: Optional[str] = None
    provenance: Optional[str] = None
    source_quote: Optional[str] = None
    points: List[TitrationPoint]

    _v_variable = _require_nonempty("variable")

    @field_validator("points")
    @classmethod
    def _points_nonempty(cls, v):
        if not v:
            raise ValueError("points must be non-empty")
        return v


class Assumption(_Model):
    """Lenient: nothing required (the strict enum stays on the wire)."""

    parameter: Optional[str] = None
    value: Optional[_Number] = None
    provenance: Optional[str] = None
    selected_by_user: Optional[bool] = None
    basis: Optional[str] = None
    citation: Optional[Citation] = None
    verify: Optional[bool] = None


class EmittedProtocol(_Model):
    title: str
    summary: Optional[str] = None
    source_citation: Optional[str] = None
    estimated_duration: Optional[str] = None
    materials: List[Material] = []
    equipment: List[Any] = []
    steps: List[Step]
    titration_series: Optional[TitrationSeries] = None
    assumptions_log: List[Assumption] = []
    open_questions: List[Any] = []
    # host-managed; ignored if emitted. Documented optional so no fixture breaks.
    schema_version: str = PROTOCOL_SCHEMA_VERSION

    _v_title = _require_nonempty("title")

    @field_validator("steps")
    @classmethod
    def _steps_nonempty(cls, v):
        if not v:
            raise ValueError("steps must contain at least one step")
        return v


# ---------------------------------------------------------------------------
# Emit-boundary gate (pure; does NOT mutate) — spec §I.5
# ---------------------------------------------------------------------------
#
# FATAL class (the ONLY class that triggers a bounded re-emit): a payload so
# malformed it cannot host a placeholder — not a dict, empty/missing title, or a
# steps field that is not a list. Per-entry gaps (an empty instruction/name/value)
# are NOT fatal: they are repaired deterministically by ``repair_structure`` and
# surfaced as a STRUCT_* blocker, keeping model calls bounded.


def _scan_repairable(raw: dict) -> List[str]:
    """Locations of per-entry structural gaps a downstream ``repair_structure``
    will fill. Informational only — never fatal."""
    locs: List[str] = []
    for i, mat in enumerate(raw.get("materials") or []):
        if isinstance(mat, dict) and not _nonempty(mat.get("name")):
            locs.append(f"materials[{i}].name")
    for i, step in enumerate(raw.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if not _nonempty(step.get("instruction")):
            locs.append(f"steps[{i}].instruction")
        for j, cp in enumerate(step.get("critical_parameters") or []):
            if not isinstance(cp, dict):
                continue
            if not _nonempty(cp.get("name")):
                locs.append(f"steps[{i}].critical_parameters[{j}].name")
            v = cp.get("value")
            if not (_nonempty(v) or isinstance(v, (int, float))):
                locs.append(f"steps[{i}].critical_parameters[{j}].value")
        for k, ss in enumerate(step.get("substeps") or []):
            if isinstance(ss, dict) and not _nonempty(ss.get("instruction")):
                locs.append(f"steps[{i}].substeps[{k}].instruction")
    ts = raw.get("titration_series")
    if isinstance(ts, dict):
        for p, pt in enumerate(ts.get("points") or []):
            if isinstance(pt, dict) and not _nonempty(pt.get("label")):
                locs.append(f"titration_series.points[{p}].label")
    return locs


def coerce_emit_payload(raw: Any) -> tuple[bool, List[dict], List[str]]:
    """Emit-boundary gate. Returns ``(usable, fatal_errors, repairable_locs)``.

    Pure: does NOT mutate ``raw``. ``usable`` is False only for the FATAL class
    (a structurally unusable payload that cannot host a placeholder). Repairable
    per-entry gaps leave ``usable`` True and are listed in ``repairable_locs`` so a
    caller can log them, but they never trigger a re-emit."""
    if not isinstance(raw, dict):
        return False, [{"loc": "<root>", "msg": "emit payload is not an object"}], []

    fatal: List[dict] = []
    title = raw.get("title")
    if not (isinstance(title, str) and title.strip() != ""):
        fatal.append({"loc": "title", "msg": "title is empty or missing"})

    steps = raw.get("steps")
    if not isinstance(steps, list):
        fatal.append({"loc": "steps", "msg": "steps is not a list"})

    if fatal:
        return False, fatal, []
    return True, [], _scan_repairable(raw)


# ---------------------------------------------------------------------------
# Per-entry structural repair pre-pass (in-place) — spec §I.3
# ---------------------------------------------------------------------------


def repair_structure(protocol: dict, open_questions: list) -> None:
    """Repair, never reject. For each incomplete entry, fill the empty/whitespace
    field with its frozen sentinel and append a BLOCKED open_question.

    Idempotent: only empty fields are filled, never a field already carrying a
    sentinel (``_nonempty`` is True for a sentinel), so a second run adds nothing.
    Emits NO findings — ``run_checks`` (``_check_structure``) produces the STRUCT_*
    findings that route the placeholder through the quality gate as a blocker.

    Entries so malformed they cannot host a placeholder (empty ``steps``/``title``)
    are NOT handled here — they are the FATAL class caught at the emit boundary."""
    if not isinstance(protocol, dict):
        return

    def _blocked(msg: str) -> None:
        open_questions.append(_BLOCKED_MARK + msg)

    for i, mat in enumerate(protocol.get("materials") or []):
        if isinstance(mat, dict) and not _nonempty(mat.get("name")):
            mat["name"] = MATERIAL_NAME_PLACEHOLDER
            _blocked(f"materials[{i}] has no name; supply the material name or delete the entry.")

    for i, step in enumerate(protocol.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if not _nonempty(step.get("instruction")):
            step["instruction"] = STEP_INSTR_PLACEHOLDER
            _blocked(f"steps[{i}] has no instruction; reconstruct the step instruction or delete the step.")
        for j, cp in enumerate(step.get("critical_parameters") or []):
            if not isinstance(cp, dict):
                continue
            if not _nonempty(cp.get("name")):
                cp["name"] = PARAM_NAME_PLACEHOLDER
                _blocked(f"steps[{i}].critical_parameters[{j}] has no name; supply it or delete the parameter.")
            v = cp.get("value")
            if not (_nonempty(v) or isinstance(v, (int, float))):
                cp["value"] = PARAM_VALUE_PLACEHOLDER
                _blocked(f"steps[{i}].critical_parameters[{j}] has no value; supply it or delete the parameter.")
        for k, ss in enumerate(step.get("substeps") or []):
            if isinstance(ss, dict) and not _nonempty(ss.get("instruction")):
                ss["instruction"] = SUBSTEP_INSTR_PLACEHOLDER
                _blocked(f"steps[{i}].substeps[{k}] has no instruction; supply it or delete the substep.")

    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        for p, pt in enumerate(ts.get("points") or []):
            if isinstance(pt, dict) and not _nonempty(pt.get("label")):
                pt["label"] = POINT_LABEL_PLACEHOLDER
                _blocked(f"titration_series.points[{p}] has no label; supply it or delete the point.")
