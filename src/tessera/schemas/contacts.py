"""S1 output schema — `contacts.json` (spec §S1)."""

from __future__ import annotations

from pydantic import Field

from .common import FeatureMode, StrictModel, SupportStrategy


class ContactParams(StrictModel):
    """Resolved S1 parameters, echoed into the artifact for provenance."""

    d_contact: float = 8.0            # Å, Cβ–Cβ contact threshold
    sep_min: int = 12                 # |i-j| minimum sequence separation
    support_strategy: SupportStrategy = SupportStrategy.REINFORCE_MARGINAL
    buriedness_radius: float = 10.0   # Å, Cβ-neighbor-count radius
    relaxed: bool = False             # whether optional light relaxation ran (§S1 geometry provenance)


class Contact(StrictModel):
    """A single long-range residue pair (i, j), 1-indexed into the backbone."""

    id: str
    i: int
    j: int
    d_cb: float = Field(description="Cβ–Cβ distance (Å); Cα substituted for glycine.")
    buriedness_i: int = Field(description="Cβ neighbor count within buriedness_radius of i.")
    buriedness_j: int
    support: int = Field(description="How many other contacts share residue i or j.")
    cluster: str = Field(description="Cluster id this contact belongs to.")
    spans_gap: bool = Field(
        default=False,
        description="True if (i,j) crosses a chain break / missing residue (§S1); such "
        "contacts have undefined geometry and are excluded from query construction.",
    )


class ContactCluster(StrictModel):
    """Coupled contacts grouped so a β-pairing or helix-packing is one motif (§S1)."""

    id: str
    contacts: list[str] = Field(description="Contact ids in this cluster.")
    segments: list[tuple[int, int]] = Field(
        description="Inclusive residue ranges (per segment) covered by the cluster."
    )
    multibody: bool = Field(
        default=False,
        description="3-body+ coupled core: pairwise oracle scores are not summed; "
        "verdict deferred to the redesign-then-refold loop (§S5, §12).",
    )


class ChainGap(StrictModel):
    """A detected chain break / missing-residue span (§S1 geometry provenance)."""

    after_residue: int
    before_residue: int
    kind: str = Field(default="chain_break", description="chain_break | missing_residue")


class ContactsDoc(StrictModel):
    """Top-level `contacts.json`."""

    backbone: str
    feature_mode: FeatureMode
    params: ContactParams
    contacts: list[Contact] = Field(default_factory=list)
    clusters: dict[str, ContactCluster] = Field(default_factory=dict)
    gaps: list[ChainGap] = Field(default_factory=list)
