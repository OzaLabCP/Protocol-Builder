"""Milestone-1 "Projects" layer data model.

Pydantic v2 models, canonical vocabularies, a deterministic (no model I/O)
workflow-detection engine, and factory helpers. This module is the source of
truth for the ExperimentProject aggregate and its ProtocolVersion rows.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, NamedTuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

# ---------------------------------------------------------------------------
# schema_version — app-data (JSON payload) schema; imported by store.py
# ---------------------------------------------------------------------------
CURRENT_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# 1. Canonical vocabularies
# ---------------------------------------------------------------------------
class WorkflowType(str, Enum):
    reproduce = "reproduce"
    adapt = "adapt"
    design = "design"
    measure = "measure"
    review = "review"
    troubleshoot = "troubleshoot"
    scale = "scale"


WORKFLOWS = {
    "reproduce",
    "adapt",
    "design",
    "measure",
    "review",
    "troubleshoot",
    "scale",
}

# entry_mode mapping (derived, never stored as source of truth)
_ENTRY_MODE = {
    WorkflowType.reproduce: "paper",
    WorkflowType.adapt: "paper",
    WorkflowType.review: "paper",
    WorkflowType.troubleshoot: "paper",
    WorkflowType.scale: "paper",
    WorkflowType.design: "hypothesis",
    WorkflowType.measure: "hypothesis",
}


def entry_mode_for(workflow: WorkflowType | str) -> str:
    """Return the entry_mode ('paper'|'hypothesis') for a workflow value."""
    wf = WorkflowType(workflow)
    return _ENTRY_MODE[wf]


class LifecycleStatus(str, Enum):
    intake_received = "intake_received"
    awaiting_confirmation = "awaiting_confirmation"
    workflow_confirmed = "workflow_confirmed"
    building = "building"
    needs_clarification = "needs_clarification"
    selecting_assay = "selecting_assay"
    protocol_ready = "protocol_ready"
    failed = "failed"
    superseded = "superseded"
    archived = "archived"


# Server phase -> lifecycle_status map
PHASE_TO_LIFECYCLE = {
    "questions": LifecycleStatus.needs_clarification,
    "assays": LifecycleStatus.selecting_assay,
    "complete": LifecycleStatus.protocol_ready,
}


class Provenance(str, Enum):
    initial = "initial"
    revise = "revise"
    apply_fixes = "apply_fixes"
    auto_review = "auto_review"
    critique_apply = "critique_apply"


# ---------------------------------------------------------------------------
# 3.4 Detection engine thresholds
# ---------------------------------------------------------------------------
CONFIRM_THRESHOLD = 0.75
AMBIGUITY_MARGIN = 0.15


# ---------------------------------------------------------------------------
# 3.1 Sub-models
# ---------------------------------------------------------------------------
class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    kind: Literal["paper_pdf", "methods_text", "hypothesis", "protocol_ref", "other"]
    title: str | None = None
    text: str | None = None
    citation: dict[str, Any] | None = None
    sha256: str | None = None
    created_at: datetime


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    kind: Literal["clarification", "assay_choice", "revision", "fix_applied", "constraint"]
    parameter: str | None = None
    question: str | None = None
    value: Any = None
    provenance: str | None = None
    rationale: str | None = None
    created_at: datetime


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipment: str | None = None
    time: str | None = None
    skill: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["unknown", "blocked", "warnings", "clean"] = "unknown"
    error_count: int = 0
    warning_count: int = 0
    unverified_citation_count: int = 0
    report: dict[str, Any] | None = None
    checked_at: datetime | None = None


# ---------------------------------------------------------------------------
# 3.2 ProtocolVersion
# ---------------------------------------------------------------------------
class ProtocolVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str  # "ver_"+hex ; API key "id"
    project_id: str
    version_number: int  # 1-based, strictly increasing per project
    provenance: Provenance
    source_op: str
    title: str
    result: dict[str, Any]  # the COMPLETE _finish() payload, verbatim
    validation_summary: dict[str, Any]  # == result["validation_report"]
    chosen_assay: dict[str, Any] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# 3.3 ExperimentProject (aggregate root)
# ---------------------------------------------------------------------------
class ExperimentProject(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_id: str = Field(serialization_alias="id")  # "proj_"+hex ; API key "id"
    title: str
    workflow: WorkflowType  # current/confirmed
    detected_workflow: WorkflowType  # immutable, set at creation
    detection_confidence: float = 0.0
    detection_reason: str = ""
    confirmation_required: bool = False
    lifecycle_status: LifecycleStatus = LifecycleStatus.intake_received
    scientific_goal: str | None = None
    hypothesis: str | None = None
    measurement_objective: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)  # raw intake, SERVER-ONLY
    input_summary: dict[str, Any] = Field(default_factory=dict)  # digest, API-exposed
    source_documents: list[SourceDocument] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    decisions: list[Decision] = Field(default_factory=list)
    session_id: str | None = None
    current_protocol_version_id: str | None = None
    protocol_versions: list[str] = Field(default_factory=list)  # version_ids, oldest->newest
    validation_summary: ValidationSummary = Field(default_factory=ValidationSummary)
    created_at: datetime
    updated_at: datetime
    schema_version: int = CURRENT_SCHEMA_VERSION

    @model_validator(mode="after")
    def _check_invariants(self) -> "ExperimentProject":
        if self.current_protocol_version_id is not None:
            if self.current_protocol_version_id not in self.protocol_versions:
                raise ValueError(
                    "current_protocol_version_id must be an element of protocol_versions"
                )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be >= created_at")
        return self


# ---------------------------------------------------------------------------
# 3.4 Detection engine
# ---------------------------------------------------------------------------
class Detection(NamedTuple):
    detected_workflow: WorkflowType
    detection_confidence: float
    detection_reason: str
    confirmation_required: bool


class _Vote(NamedTuple):
    row: int
    workflow: WorkflowType
    confidence: float
    reason: str


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", re.IGNORECASE)
_PMID_RE = re.compile(r"\bpmid\s*:?\s*\d{6,9}\b|\bpubmed\.ncbi\b", re.IGNORECASE)
_URL_RE = re.compile(r"\bhttps?://[^\s]+", re.IGNORECASE)
_NUM_STEPS_RE = re.compile(r"(?m)^\s*\d+[\.\)]\s")
_BENCH_VERBS_RE = re.compile(
    r"\b(incubate|centrifuge|resuspend|aliquot|pipette|vortex|elute|wash|dilute|pellet|spin)\b",
    re.IGNORECASE,
)
_QTY_UNIT_RE = re.compile(
    r"\b\d+(\.\d+)?\s?(µl|ul|ml|m?m|µm|nm|ng|µg|mg|rpm|x?g|°?\s?c)\b",
    re.IGNORECASE,
)
_REVIEW_RE = re.compile(
    r"\b(review|critique|audit|sanity[- ]check|is this (correct|right)|what'?s wrong)\b",
    re.IGNORECASE,
)
_KINETIC_RE = re.compile(
    r"\bk[_\s]?m\b|\bk[_\s]?cat\b|\bk[_\s]?d\b|\b(ic|ec)[_\s]?50\b|\bk[_\s]?i\b",
    re.IGNORECASE,
)
_MEASURE_VERBS_RE = re.compile(
    r"\b(measure|quantif(y|ication|ies)|determine|characteri[sz]e|titrate|"
    r"kinetics?|affinity|potency|dose[- ]response)\b",
    re.IGNORECASE,
)
_HYP_PHRASING_RE = re.compile(
    r"\bhypothesi[sz]e?s?\b|\btest(ing)?\s+whether\b|"
    r"\bwe (predict|expect|hypothesi[sz]e)\b|"
    r"\bdoes\b.+\b(increase|decrease|affect|reduce|enhance|inhibit|improve)\b|"
    r"\bif\b.+\bthen\b",
    re.IGNORECASE,
)


def _norm(inputs: dict[str, Any]) -> dict[str, Any]:
    """Normalize the raw intake body into the fields the engine scans."""

    def s(key: str) -> str:
        v = inputs.get(key)
        return v.strip() if isinstance(v, str) else ""

    requested = s("requested_workflow")
    return {
        "requested_workflow": requested,
        "identifier": s("identifier"),
        "free_text": s("free_text"),
        "hypothesis": s("hypothesis"),
        "measurement_goal": s("measurement_goal"),
        "existing_protocol_text": s("existing_protocol_text"),
        "pdf_present": bool(inputs.get("pdf_present"))
        or bool((inputs.get("pdf_text") or "").strip()),
        "pdf_text": s("pdf_text"),
        "constraints": inputs.get("constraints") if isinstance(inputs.get("constraints"), dict) else {},
    }


def detect_workflow(inputs: dict[str, Any]) -> Detection:
    """Pure, deterministic workflow detection. No model I/O.

    Same inputs => same output. Aggregation per spec §3.4.
    """
    n = _norm(inputs)

    # R0 — explicit override
    if n["requested_workflow"] in WORKFLOWS:
        wf = WorkflowType(n["requested_workflow"])
        return Detection(wf, 1.00, f"You asked for the {wf.value} workflow.", False)

    text = "\n".join(
        [
            n["free_text"],
            n["hypothesis"],
            n["measurement_goal"],
            n["existing_protocol_text"],
        ]
    )

    votes: list[_Vote] = []

    def doi_hit() -> str | None:
        m = _DOI_RE.search(n["identifier"]) or _DOI_RE.search(text)
        return m.group(0) if m else None

    doi = doi_hit()
    if doi:
        votes.append(
            _Vote(1, WorkflowType.reproduce, 0.92, f"Found a DOI ({doi}); reproducing from the paper.")
        )
    if _PMID_RE.search(n["identifier"]) or _PMID_RE.search(text):
        votes.append(
            _Vote(2, WorkflowType.reproduce, 0.90, "Found a PubMed identifier; reproducing from the paper.")
        )
    if n["pdf_present"]:
        votes.append(
            _Vote(3, WorkflowType.reproduce, 0.90, "A PDF was uploaded; reproducing from the paper.")
        )
    url_hit = _URL_RE.search(n["identifier"]) or _URL_RE.search(text)
    if url_hit:
        votes.append(
            _Vote(4, WorkflowType.reproduce, 0.80, "Found a URL; reproducing from the linked source.")
        )
    if n["existing_protocol_text"]:
        votes.append(
            _Vote(5, WorkflowType.adapt, 0.85, "You provided an existing protocol; adapting it.")
        )

    # R6 — protocol-shaped free text (>= 2 of 3 signals)
    ft = n["free_text"]
    shape_hits = 0
    if _NUM_STEPS_RE.search(ft):
        shape_hits += 1
    if _BENCH_VERBS_RE.search(ft):
        shape_hits += 1
    if _QTY_UNIT_RE.search(ft):
        shape_hits += 1
    r6_fired = shape_hits >= 2
    if r6_fired:
        votes.append(
            _Vote(6, WorkflowType.adapt, 0.68, "Your text looks like a protocol; adapting it.")
        )

    # R7 — review intent, requires R5 or R6
    if _REVIEW_RE.search(text) and (n["existing_protocol_text"] or r6_fired):
        votes.append(
            _Vote(7, WorkflowType.review, 0.80, "You want to review an existing protocol.")
        )
    if n["measurement_goal"]:
        votes.append(
            _Vote(8, WorkflowType.measure, 0.85, "You gave a measurement objective.")
        )
    if _KINETIC_RE.search(text):
        votes.append(
            _Vote(9, WorkflowType.measure, 0.80, "Detected a kinetic/affinity parameter to measure.")
        )
    if _MEASURE_VERBS_RE.search(text):
        votes.append(
            _Vote(10, WorkflowType.measure, 0.72, "Language points to measuring or quantifying a parameter.")
        )
    if n["hypothesis"]:
        votes.append(
            _Vote(11, WorkflowType.design, 0.85, "You provided a hypothesis to design around.")
        )
    if _HYP_PHRASING_RE.search(text):
        votes.append(
            _Vote(12, WorkflowType.design, 0.72, "Your text reads like a hypothesis to test.")
        )

    if not votes:
        # R13 — fallback if any input present
        any_input = any(
            [
                n["free_text"],
                n["hypothesis"],
                n["measurement_goal"],
                n["existing_protocol_text"],
                n["identifier"],
                n["pdf_present"],
            ]
        )
        if any_input:
            return Detection(
                WorkflowType.design,
                0.30,
                "Not sure what you want — defaulting to design; please confirm.",
                True,
            )
        # No input at all: endpoint 400s before aggregation. Fail-closed here.
        raise ValueError("No input provided to detect_workflow.")

    # Aggregate: score(workflow) = max conf among its votes.
    # leader = argmax (ties break by lower row #).
    best_by_wf: dict[WorkflowType, _Vote] = {}
    for v in votes:
        cur = best_by_wf.get(v.workflow)
        if cur is None or v.confidence > cur.confidence or (
            v.confidence == cur.confidence and v.row < cur.row
        ):
            best_by_wf[v.workflow] = v

    ranked = sorted(
        best_by_wf.values(),
        key=lambda v: (-v.confidence, v.row),
    )
    leader = ranked[0]
    runner_up_score = ranked[1].confidence if len(ranked) > 1 else 0.0

    confidence = round(leader.confidence, 2)
    reason = leader.reason
    ambiguous = runner_up_score >= leader.confidence - AMBIGUITY_MARGIN
    confirmation_required = leader.confidence < CONFIRM_THRESHOLD or ambiguous
    if ambiguous and len(ranked) > 1:
        reason = reason + f" (also looked like {ranked[1].workflow.value} — please confirm.)"

    return Detection(leader.workflow, confidence, reason, confirmation_required)


# ---------------------------------------------------------------------------
# 3.5 input_summary & title derivation
# ---------------------------------------------------------------------------
def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    m = re.search(r"[.!?](\s|$)", text)
    return text[: m.end()].strip() if m else text


def _derive_title(inputs: dict[str, Any]) -> str:
    n = _norm(inputs)
    candidates: list[str] = []
    if n["hypothesis"]:
        candidates.append(n["hypothesis"])
    if n["measurement_goal"]:
        candidates.append(n["measurement_goal"])
    if n["identifier"]:
        candidates.append(n["identifier"])
    fs = _first_sentence(n["free_text"])
    if fs:
        candidates.append(fs)
    pdf_filename = inputs.get("pdf_filename")
    if isinstance(pdf_filename, str) and pdf_filename.strip():
        stem = re.sub(r"\.[^.]+$", "", pdf_filename.strip())
        if stem:
            candidates.append(stem)
    title = candidates[0] if candidates else "Untitled project"
    title = title.strip()
    return title[:80] if len(title) > 80 else title


def _identifier_descriptor(inputs: dict[str, Any]) -> dict[str, str] | None:
    n = _norm(inputs)
    doi = _DOI_RE.search(n["identifier"]) or _DOI_RE.search(n["free_text"])
    if doi:
        return {"kind": "doi", "value": doi.group(0)}
    pmid = _PMID_RE.search(n["identifier"]) or _PMID_RE.search(n["free_text"])
    if pmid:
        return {"kind": "pmid", "value": pmid.group(0)}
    url = _URL_RE.search(n["identifier"]) or _URL_RE.search(n["free_text"])
    if url:
        return {"kind": "url", "value": url.group(0)}
    return None


def build_input_summary(inputs: dict[str, Any]) -> dict[str, Any]:
    """Build the API-exposed digest of the intake. Never includes full PDF text."""
    n = _norm(inputs)

    modalities: list[str] = []
    if n["pdf_present"]:
        modalities.append("pdf")
    if n["identifier"]:
        modalities.append("identifier")
    if n["free_text"]:
        modalities.append("free_text")
    if n["hypothesis"]:
        modalities.append("hypothesis")
    if n["measurement_goal"]:
        modalities.append("measurement_goal")
    if n["existing_protocol_text"]:
        modalities.append("existing_protocol")

    char_counts = {
        "free_text": len(n["free_text"]),
        "hypothesis": len(n["hypothesis"]),
        "measurement_goal": len(n["measurement_goal"]),
        "existing_protocol": len(n["existing_protocol_text"]),
        "pdf_text": len(n["pdf_text"]),
    }

    # dominant non-empty text field (excludes full PDF text)
    dominant = ""
    for field in ("free_text", "hypothesis", "measurement_goal", "existing_protocol_text"):
        if len(n[field]) > len(dominant):
            dominant = n[field]
    preview = dominant[:200]

    pdf_filename = inputs.get("pdf_filename")
    if not (isinstance(pdf_filename, str) and pdf_filename.strip()):
        pdf_filename = None

    return {
        "title": _derive_title(inputs),
        "modalities": modalities,
        "identifier": _identifier_descriptor(inputs),
        "pdf_filename": pdf_filename,
        "char_counts": char_counts,
        "preview": preview,
        "constraints": n["constraints"] or None,
    }


# ---------------------------------------------------------------------------
# 3.6 Factory helpers
# ---------------------------------------------------------------------------
def new_project_id() -> str:
    return "proj_" + uuid.uuid4().hex


def new_version_id() -> str:
    return "ver_" + uuid.uuid4().hex


def make_project(
    *,
    title: str,
    workflow: WorkflowType | str | None,
    detected_workflow: WorkflowType | str,
    detection_confidence: float = 0.0,
    detection_reason: str = "",
    confirmation_required: bool = False,
    scientific_goal: str | None = None,
    hypothesis: str | None = None,
    measurement_objective: str | None = None,
    constraints: Constraints | dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    input_summary: dict[str, Any] | None = None,
    source_documents: list[SourceDocument] | None = None,
) -> ExperimentProject:
    now = datetime.now(timezone.utc)

    detected = WorkflowType(detected_workflow)
    wf = WorkflowType(workflow) if workflow else detected

    if constraints is None:
        constraints_obj = Constraints()
    elif isinstance(constraints, Constraints):
        constraints_obj = constraints
    else:
        known = {"equipment", "time", "skill"}
        typed = {k: v for k, v in constraints.items() if k in known}
        extra = {k: v for k, v in constraints.items() if k not in known}
        constraints_obj = Constraints(**typed, extra=extra)

    lifecycle = (
        LifecycleStatus.awaiting_confirmation
        if confirmation_required
        else LifecycleStatus.intake_received
    )

    return ExperimentProject(
        project_id=new_project_id(),
        title=title,
        workflow=wf,
        detected_workflow=detected,
        detection_confidence=detection_confidence,
        detection_reason=detection_reason,
        confirmation_required=confirmation_required,
        lifecycle_status=lifecycle,
        scientific_goal=scientific_goal,
        hypothesis=hypothesis,
        measurement_objective=measurement_objective,
        inputs=inputs or {},
        input_summary=input_summary or {},
        source_documents=source_documents or [],
        constraints=constraints_obj,
        created_at=now,
        updated_at=now,
        schema_version=CURRENT_SCHEMA_VERSION,
    )
