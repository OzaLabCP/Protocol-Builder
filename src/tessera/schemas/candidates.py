"""S5 output schema — `candidates.json` (spec §S5)."""

from __future__ import annotations

from pydantic import Field

from ..types import Propensity
from .common import FeatureMode, StrictModel


class Substitution(StrictModel):
    """One (position, from_aa, to_aa) edit, 1-indexed."""

    position: int
    from_aa: str
    to_aa: str


class MutationSet(StrictModel):
    """A candidate reinforcement: an ordered list of substitutions with provenance."""

    id: str
    substitutions: list[Substitution]
    propensity: Propensity = Field(
        description="Summed per-pair log-odds for this set — a Propensity, never a ΔG."
    )
    contact_id: str = Field(description="Originating contact.")
    cluster_id: str
    n_eff: float = Field(description="N_eff provenance of the originating contact.")
    multibody: bool = Field(
        default=False, description="From a 3-body+ cluster; verdict deferred to refold (§S5)."
    )


class CandidatesDoc(StrictModel):
    """Top-level `candidates.json`."""

    backbone: str
    feature_mode: FeatureMode
    retrieval_used: bool = Field(
        description="False in --no-retrieve oracle-only mode (§3.7); candidates then come "
        "from oracle enumeration, not motif harvesting."
    )
    candidate_cap: int = 200
    beam_width: int = Field(description="Per-cluster beam width used under the cap (§S5).")
    dropped_combinations: int = Field(
        default=0, description="How many joint combinations were pruned — so the report is never "
        "read as exhaustive when it was capped."
    )
    candidates: list[MutationSet] = Field(default_factory=list)
