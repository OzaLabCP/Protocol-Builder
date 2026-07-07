"""S4 output schema — per-contact residue statistics + run-level summary (§S4).

`stats/<contact>.parquet` is the tabular per-cell form; this module defines the
in-memory / JSON summary models. The 20x20 tables are keyed by the fixed
:data:`tessera.schemas.common.AA20` order.
"""

from __future__ import annotations

from pydantic import Field

from ..types import Propensity
from .common import (
    AA20,
    BackgroundNullSpec,
    BurialClass,
    ContactStatus,
    FeatureMode,
    OrientationClass,
    Provenance,
    StrictModel,
)


class PairPropensity(StrictModel):
    """One (a, b) residue-pair cell that survived enumeration."""

    a: str
    b: str
    weighted_count: float
    frequency: float
    background: float = Field(description="f0(a,b) under the configured null.")
    propensity: Propensity = Field(description="log-odds s(a,b); a Propensity, never a ΔG.")


class ContactGeometry(StrictModel):
    """The geometry bin this contact fell into, for background conditioning (§S4.4)."""

    d_cb: float
    distance_bin: int
    orientation: OrientationClass
    burial: BurialClass


class ContactStats(StrictModel):
    """Statistics for one contact (`stats/<contact>.parquet` payload + metadata)."""

    contact_id: str
    status: ContactStatus
    feature_mode: FeatureMode
    n_eff: float = Field(
        description="Sum of redundancy-corrected hit weights after §6.3 collapse, "
        "with the pseudocount total folded in so smoothing is visible."
    )
    n_hits_raw: int = Field(description="Raw surviving hit count before reweighting.")
    n_clusters: int = Field(description="Independent sequence/structure clusters contributing.")
    low_confidence: bool = Field(description="True if n_eff < n_eff_min (excluded from S5).")
    geometry: ContactGeometry
    pseudocount_alpha: float = Field(default=0.5, description="Dirichlet α per cell (§S4.4).")
    top_pairs: list[PairPropensity] = Field(
        default_factory=list, description="Highest-propensity pairs, descending."
    )
    marginals_i: dict[str, float] = Field(
        default_factory=dict, description="Per-position marginal frequency at i, keyed by AA."
    )
    marginals_j: dict[str, float] = Field(default_factory=dict)


class StatsSummary(StrictModel):
    """Run-level `stats_summary.json` — provenance + per-contact terminal states."""

    backbone: str
    feature_mode: FeatureMode
    background: BackgroundNullSpec
    provenance: Provenance
    n_eff_min: float = 10.0
    aa_order: list[str] = Field(default_factory=lambda: list(AA20))
    status_counts: dict[ContactStatus, int] = Field(default_factory=dict)
    contacts: list[ContactStats] = Field(default_factory=list)
