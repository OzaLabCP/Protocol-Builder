"""S3 output schema — parsed Folddisco hit rows (spec §S3).

The on-disk artifact is `hits/<cluster>.tsv` in Folddisco's native column order
(see tests/fixtures/README for the pinned column contract). These models are the
*parsed* form used by S4.
"""

from __future__ import annotations

from pydantic import Field

from .common import StrictModel


class MatchedResidue(StrictModel):
    """One residue of a hit, mapped back to a query pair position."""

    contact_id: str
    role: str = Field(description="'i' or 'j' — which side of the (i,j) pair this residue matched.")
    target_index: int = Field(description="Residue index in the target structure.")
    residue: str = Field(description="One-letter amino acid at the matched position ('X' if unknown).")
    plddt: float | None = Field(
        default=None, description="Per-position pLDDT (AFDB); None for PDB/SwissProt (exempt)."
    )


class Hit(StrictModel):
    """One matched motif (one Folddisco output line)."""

    cluster_id: str
    target_id: str
    source_db: str = Field(description="afdb50 | swissprot | pdb — drives the §S4.2 pLDDT exemption.")
    rmsd: float
    score: float = Field(description="Folddisco coverage/rarity score.")
    motif_mean_plddt: float | None = Field(default=None)
    matched_residues: list[MatchedResidue] = Field(default_factory=list)


class ClusterHits(StrictModel):
    """All hits for one cluster, as parsed from hits/<cluster>.tsv."""

    cluster_id: str
    prefilter_only: bool = Field(
        default=False, description="True if produced with Folddisco --skip-match (§S3)."
    )
    hits: list[Hit] = Field(default_factory=list)
