"""7A triage output schema — `weak_regions.json` (spec §7A, §D0–D6).

**Units discipline (§D0).** This module reports converging evidence for local
sub-optimality; every signal keeps its native units. It is a spec violation to
emit a synthesized per-residue ΔG — there is deliberately no such field here, and
a CI invariant test asserts none appears in any emitted artifact (§14.5).
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..types import DeltaG
from .common import Provenance, StrictModel


class FrustrationClass(str, Enum):
    HIGHLY = "highly"
    NEUTRAL = "neutral"
    MINIMALLY = "minimally"


class FrustrationSignal(StrictModel):
    """D1 — energetic frustration, kept as a Z-score (native units, no ΔG)."""

    index_z: float = Field(description="Z-score of native interaction energy vs decoy distribution.")
    kind: str = Field(description="mutational | configurational")
    frustration_class: FrustrationClass
    method: str = Field(description="frustrampnn | frustrai-seq | frustratometer2")


class StabilizingDensitySignal(StrictModel):
    """D2 — density of available stabilizing substitutions (reuses the S6 oracle)."""

    n_stabilizing: int = Field(description="Count of substitutions below the ΔΔG threshold.")
    fraction_stabilizing: float
    best_ddg: DeltaG = Field(description="Best available ΔΔG at the position (a ΔΔG, per-site).")


class FlexibilitySignal(StrictModel):
    """D3 — corroborating flexibility overlay, native units (pLDDT / B-factor / RMSF)."""

    source: str = Field(description="plddt | pae | bfactor | rmsf")
    value: float


class FunctionalExclusion(StrictModel):
    """D5 — why a region is (or is not) a valid graft target."""

    excluded: bool
    reasons: list[str] = Field(
        default_factory=list,
        description="e.g. 'binder-target interface', 'active-site', 'allosteric-hinge'.",
    )
    incidental: bool = Field(
        description="True only for surviving regions — the ONLY valid graft targets (§D5)."
    )


class WeakRegion(StrictModel):
    """A spatially-contiguous region that agrees across signals (§D4)."""

    id: str
    residues: list[int]
    consensus_score: float = Field(description="Cross-signal agreement; units retained per signal.")
    frustration: FrustrationSignal | None = None
    stabilizing_density: StabilizingDensitySignal | None = None
    flexibility: FlexibilitySignal | None = None
    functional: FunctionalExclusion
    proposed_feature: str | None = Field(
        default=None, description="D6 routing, e.g. 'disulfide_staple', 'metal_site', 'hbnet'."
    )
    feature_rationale: str | None = None
    handoff: str | None = Field(
        default=None, description="Handoff record to install mode (out of scope here, §2)."
    )


class WeakRegionsDoc(StrictModel):
    """Top-level `weak_regions.json` — a ranked map, no per-residue ΔG column (§D0)."""

    backbone: str
    provenance: Provenance
    regions: list[WeakRegion] = Field(default_factory=list)
