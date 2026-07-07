"""S6/S7 output schema — `scored_candidates.parquet` (spec §S6, §S7)."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..types import DeltaG, Propensity
from .common import StrictModel


class OracleMode(str, Enum):
    """Which oracle mode produced the ΔΔG (§S6/§7.1)."""

    SINGLE = "single"
    EPISTATIC_DOUBLE = "epistatic_double"
    SATURATION = "saturation"


class OracleScore(StrictModel):
    """One backend's ΔΔG for a candidate (ThermoMPNN-D primary, FoldX/Rosetta optional)."""

    oracle: str = Field(description="Backend name, e.g. 'thermompnn-d', 'foldx', 'rosetta', 'mock'.")
    mode: OracleMode
    ddg: DeltaG = Field(description="ΔΔG (kcal/mol); more negative = more stabilizing.")


class CandidateFlags(StrictModel):
    """Interpretation flags that must surface in the report (§7.2)."""

    surface_cys: bool = False           # megascale assay artifact — forbidden by default in S5
    increases_hydrophobicity: bool = False  # aggregation risk on surface, fine in a buried core
    low_neff_provenance: bool = False   # harvested from a low-N_eff contact
    multibody: bool = False             # cross-contact epistasis not captured by pairwise score
    within_noise: bool = False          # |ΔΔG| within its own uncertainty band (§7.2)


class ScoredCandidate(StrictModel):
    """A candidate with propensity (S5), per-oracle ΔΔG, agreement, and flags."""

    candidate_id: str
    contact_id: str
    cluster_id: str
    propensity: Propensity = Field(description="From S5 — marginal/independent estimate, not a ΔG.")
    oracle_scores: list[OracleScore] = Field(default_factory=list)
    primary_ddg: DeltaG = Field(description="Primary-oracle ΔΔG used for ranking.")
    oracle_sign_agreement: float | None = Field(
        default=None, description="Fraction of oracles agreeing on sign; None if single oracle."
    )
    flags: CandidateFlags = Field(default_factory=CandidateFlags)
    confident_effect: bool = Field(
        description="Clears §7.2 minimum effect size AND exceeds its own uncertainty band."
    )
