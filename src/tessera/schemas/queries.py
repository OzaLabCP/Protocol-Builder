"""S2 output schema — discontinuous motif-query manifest (spec §S2)."""

from __future__ import annotations

from pydantic import Field

from .common import FeatureMode, StrictModel


class QueryPair(StrictModel):
    """One (i, j) pair whose residues are read out of hits downstream (§S4.1)."""

    contact_id: str
    i: int
    j: int


class ClusterQuery(StrictModel):
    """A discontinuous query for one contact cluster (§S2)."""

    cluster_id: str
    query_pdb: str = Field(description="Path to queries/<cluster>.pdb (motif residues only).")
    query_residues: str = Field(description="Path to queries/<cluster>.txt (Folddisco residue list).")
    residue_indices: list[int] = Field(description="Backbone residue indices in the query.")
    segments: list[tuple[int, int]] = Field(description="Residue ranges (±padding) per segment.")
    pairs: list[QueryPair] = Field(description="The (i,j) pairs to read out of the hits.")
    feature_mode: FeatureMode
    dist_thresh: float = Field(default=1.0, description="Folddisco -d per-query override.")
    angle_thresh: float = Field(default=15.0, description="Folddisco -a per-query override.")


class QueryManifest(StrictModel):
    """Top-level manifest mapping cluster → query files → pairs, with feature_mode."""

    backbone: str
    feature_mode: FeatureMode
    padding: int = Field(default=2, description="Residues flanking each partner (default ±2).")
    queries: list[ClusterQuery] = Field(default_factory=list)
