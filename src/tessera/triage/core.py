"""7A stability-triage front-end — weak-region diagnostic (spec §7A, D0–D6).

Produces a ranked :class:`WeakRegionsDoc` of *incidental* weak regions: places
where converging evidence says the fold is locally sub-optimal and grafting a new
feature (handed off to install mode, out of scope §2) would help.

**Units discipline (§D0).** Every signal keeps its native units and there is no
synthesized per-residue ΔG: frustration is a Z-score (§D1), the stabilizing-density
signal carries a per-*mutation* best ΔΔG (a change, allowed §14.5) plus a count
(§D2), and flexibility is a bare pLDDT (§D3). A per-residue absolute ΔG is never
emitted.

**Burial approximation (§D6).** No per-residue burial term is available at this
stage, so buried-vs-exposed is approximated from a Cβ-neighbour-count proxy (count
of Cβ within 8 Å, buried when at/above the structure median) and routing is driven
primarily by flexibility (§D3) and frustration (§D1), per the §D6 note.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..adapters.oracle import OracleBackend
from ..io.pdb import Structure
from ..schemas.common import Provenance
from ..schemas.weak_regions import (
    FlexibilitySignal,
    FrustrationClass,
    FrustrationSignal,
    FunctionalExclusion,
    StabilizingDensitySignal,
    WeakRegion,
    WeakRegionsDoc,
)

_HIGHLY_Z = -0.5  # frustration_z at/below ⇒ highly frustrated (§D1)
_MINIMALLY_Z = 0.5  # frustration_z at/above ⇒ minimally frustrated (§D1)
_FRAC_WEAK = 0.15  # fraction_stabilizing at/above ⇒ D2 weak-signal (§D4)
_FLOPPY_PLDDT = 70.0  # mean pLDDT below ⇒ floppy (§D3/§D6)
_STRONG_Z = -1.0  # mean frustration_z at/below ⇒ strongly frustrated (§D6)
_NEIGHBOR_RADIUS = 8.0  # Å, Cβ-neighbour burial proxy (§D6 approximation)

_D6_FEATURES = ("disulfide_staple", "core_repack_or_metal_site", "hbnet")


@dataclass
class _PosSignals:
    """Per-position signal bundle (§D1–D3), native units retained (§D0)."""

    p: int  # 1-based sequential position
    res_seq: int  # PDB residue number
    frustration: FrustrationSignal
    density: StabilizingDensitySignal
    plddt: float
    neighbors: int
    weak: bool


def _classify(frustration_z: float) -> FrustrationClass:
    if frustration_z <= _HIGHLY_Z:
        return FrustrationClass.HIGHLY
    if frustration_z >= _MINIMALLY_Z:
        return FrustrationClass.MINIMALLY
    return FrustrationClass.NEUTRAL


def _neighbor_counts(structure: Structure) -> np.ndarray:
    """Cβ-neighbour count within ``_NEIGHBOR_RADIUS`` — the §D6 burial proxy."""
    cb = structure.cb_coords()
    d = np.linalg.norm(cb[:, None, :] - cb[None, :, :], axis=-1)
    return ((d < _NEIGHBOR_RADIUS) & (d > 0.0)).sum(axis=1)


def _position_signals(
    structure: Structure,
    sequence: str,
    oracle: OracleBackend,
    ddg_threshold: float,
    structure_id: str,
) -> list[_PosSignals]:
    """Compute D1/D2/D3 signals for every position (§D1–D3)."""
    neighbors = _neighbor_counts(structure)
    out: list[_PosSignals] = []
    for p in range(1, len(structure.residues) + 1):
        native = sequence[p - 1]
        sat = oracle.score_saturation(structure_id, sequence, p)
        decoys = [dg for aa, dg in sat.items() if aa != native]  # 19 non-native ΔΔG
        decoy_vals = np.array([dg.value for dg in decoys], dtype=float)
        # D1 — mutational frustration Z (native ref ΔΔG is 0 by definition, §D1).
        frustration_z = float((decoy_vals.mean() - 0.0) / (decoy_vals.std() + 1e-6))
        frustration = FrustrationSignal(
            index_z=frustration_z,
            kind="mutational",
            frustration_class=_classify(frustration_z),
            method="frustrampnn",  # oracle-saturation stand-in for FrustraMPNN (§D1)
        )
        # D2 — density of available stabilizing substitutions (§D2).
        n_stab = int((decoy_vals < ddg_threshold).sum())
        best_ddg = min(decoys, key=lambda dg: dg.value)  # most-negative decoy ΔΔG
        density = StabilizingDensitySignal(
            n_stabilizing=n_stab,
            fraction_stabilizing=n_stab / 19,
            best_ddg=best_ddg,
        )
        # D3 — flexibility overlay (native pLDDT, §D3).
        plddt = structure.residues[p - 1].plddt
        plddt = 100.0 if plddt is None else float(plddt)
        weak = (
            frustration.frustration_class is FrustrationClass.HIGHLY
            and density.fraction_stabilizing >= _FRAC_WEAK
        )
        out.append(
            _PosSignals(
                p=p,
                res_seq=structure.residues[p - 1].res_seq,
                frustration=frustration,
                density=density,
                plddt=plddt,
                neighbors=int(neighbors[p - 1]),
                weak=weak,
            )
        )
    return out


def _cluster(positions: list[_PosSignals], structure: Structure) -> list[list[_PosSignals]]:
    """Group contiguous weak-signal positions into regions (§D4).

    Contiguity requires consecutive sequential index *and* consecutive PDB residue
    number in the same chain, so a chain break never bridges two regions (§S1)."""
    residues = structure.residues
    clusters: list[list[_PosSignals]] = []
    current: list[_PosSignals] = []
    for ps in positions:
        if not ps.weak:
            if current:
                clusters.append(current)
                current = []
            continue
        if current:
            prev = current[-1]
            same_chain = residues[prev.p - 1].chain == residues[ps.p - 1].chain
            contiguous = ps.p == prev.p + 1 and ps.res_seq == prev.res_seq + 1
            if not (same_chain and contiguous):
                clusters.append(current)
                current = []
        current.append(ps)
    if current:
        clusters.append(current)
    return clusters


def _route_feature(
    mean_plddt: float, mean_z: float, buried: bool
) -> tuple[str, str]:
    """Geometry/dynamics → proposed feature (§D6 table). Returns (feature, rationale)."""
    if mean_plddt < _FLOPPY_PLDDT:
        return "disulfide_staple", "pays down entropic cost of ordering the loop"
    if mean_z <= _STRONG_Z and buried:
        return "core_repack_or_metal_site", "adds enthalpic packing / anchor"
    return "hbnet", "satisfies buried/clustered polar groups"


def _build_region(
    cluster: list[_PosSignals],
    exclude: set[int],
    median_neighbors: float,
) -> WeakRegion:
    """Assemble one region: consensus score, representative signals, exclusion,
    routing (§D4–D6)."""
    res_seqs = [ps.res_seq for ps in cluster]
    mean_plddt = float(np.mean([ps.plddt for ps in cluster]))
    mean_neighbors = float(np.mean([ps.neighbors for ps in cluster]))
    buried = mean_neighbors >= median_neighbors

    # D4 consensus: mean over positions of (fraction_stabilizing + max(0, -z)),
    # flexibility ADJUSTS (+0.2 if the region mean pLDDT is floppy).
    per_pos = [
        ps.density.fraction_stabilizing + max(0.0, -ps.frustration.index_z)
        for ps in cluster
    ]
    consensus = float(np.mean(per_pos))
    if mean_plddt < _FLOPPY_PLDDT:
        consensus += 0.2

    # Representative per-signal values (native units retained, §D4/§D0).
    worst = min(cluster, key=lambda ps: ps.frustration.index_z)  # most frustrated
    best_density_pos = min(cluster, key=lambda ps: ps.density.best_ddg)  # most stabilizing
    flexibility = FlexibilitySignal(source="plddt", value=mean_plddt)

    # D5 functional exclusion — only incidental regions are valid graft targets.
    overlap = exclude.intersection(res_seqs)
    if overlap:
        functional = FunctionalExclusion(
            excluded=True,
            reasons=["overlaps excluded functional residues"],
            incidental=False,
        )
    else:
        functional = FunctionalExclusion(
            excluded=False,
            reasons=["no functional overlap (family-conservation check unavailable offline)"],
            incidental=True,
        )

    region = WeakRegion(
        id="",  # assigned after ranking
        residues=res_seqs,
        consensus_score=consensus,
        frustration=worst.frustration,
        stabilizing_density=best_density_pos.density,
        flexibility=flexibility,
        functional=functional,
    )
    # D6 routing only for incidental regions (§D6).
    if functional.incidental:
        mean_z = float(np.mean([ps.frustration.index_z for ps in cluster]))
        feature, rationale = _route_feature(mean_plddt, mean_z, buried)
        region.proposed_feature = feature
        region.feature_rationale = rationale
        region.handoff = "install-mode (out of scope §2)"
    return region


def run_triage(
    structure: Structure,
    sequence: str,
    oracle: OracleBackend,
    provenance: Provenance,
    ddg_threshold: float = -0.5,
    exclude_residues: list[int] | None = None,
    structure_id: str = "",
) -> WeakRegionsDoc:
    """Run the §7A stability triage and return a ranked ``weak_regions.json`` doc.

    Signals per 1-based position: D1 mutational frustration Z from the oracle
    saturation scan (an offline FrustraMPNN stand-in), D2 stabilizing-substitution
    density with a per-mutation best ΔΔG, D3 pLDDT flexibility. Weak-signal
    positions (highly frustrated AND dense in stabilizing substitutions, §D4) are
    clustered into contiguous regions, scored by cross-signal consensus, triaged
    against ``exclude_residues`` for functional frustration (§D5), and routed to a
    proposed feature (§D6). Regions are ranked by ``consensus_score`` descending.
    """
    exclude = set(exclude_residues or [])
    positions = _position_signals(structure, sequence, oracle, ddg_threshold, structure_id)
    median_neighbors = float(np.median(_neighbor_counts(structure)))

    clusters = _cluster(positions, structure)
    regions = [_build_region(c, exclude, median_neighbors) for c in clusters]
    regions.sort(key=lambda r: r.consensus_score, reverse=True)
    for i, region in enumerate(regions, start=1):
        region.id = f"w{i:03d}"

    return WeakRegionsDoc(
        backbone=structure.source,
        provenance=provenance,
        regions=regions,
    )


__all__ = ["run_triage"]
