"""S4 · Hit parsing + residue statistics (spec §S4, §6.2, §6.3).

For each contact, map the matched target residues back to the (i, j) pair, filter
low-confidence AFDB positions (§6.2), collapse over-sampled families to one unit of
weight each (§6.3), and compute a smoothed weighted pair-frequency table with a
log-odds **propensity** against the background null (§S4.4). Propensities are
:class:`~tessera.types.Propensity` — a statistical log-odds, never a ΔG (§3.1).
"""

from __future__ import annotations

import math
from collections import Counter

from ..adapters import compute_weights
from ..adapters.clustering import ClusteringBackend
from ..config import StatsConfig
from ..io.pdb import Structure
from ..schemas.common import (
    AA20,
    BackgroundKind,
    ContactStatus,
    FeatureMode,
    Provenance,
)
from ..schemas.contacts import Contact, ContactsDoc
from ..schemas.hits import ClusterHits, Hit
from ..schemas.stats import (
    ContactStats,
    PairPropensity,
    StatsSummary,
)
from ..types import Propensity
from .background import BackgroundModel, classify_geometry

_AA_SET = frozenset(AA20)
_PLDDT_EXEMPT = frozenset({"pdb", "swissprot"})
_LOW_CONFIDENCE = frozenset(
    {ContactStatus.LOW_NEFF, ContactStatus.NO_HITS, ContactStatus.GAP_EXCLUDED}
)


def _pair_residues(hit: Hit, contact_id: str) -> tuple[str, str] | None:
    """Return (a, b) — the residues matched to roles 'i'/'j' of ``contact_id``.

    None if the hit lacks either role for this contact (§S4.1)."""
    a: str | None = None
    b: str | None = None
    for mr in hit.matched_residues:
        if mr.contact_id != contact_id:
            continue
        if mr.role == "i":
            a = mr.residue
        elif mr.role == "j":
            b = mr.residue
    if a is None or b is None:
        return None
    return a, b


def _passes_plddt(hit: Hit, contact_id: str, plddt_min: float) -> bool:
    """§6.2 confidence filter. PDB/SwissProt hits are exempt; AFDB hits are dropped
    if the motif-mean pLDDT or either matched-residue pLDDT is below the cutoff."""
    if hit.source_db in _PLDDT_EXEMPT:
        return True
    if hit.motif_mean_plddt is not None and hit.motif_mean_plddt < plddt_min:
        return False
    for mr in hit.matched_residues:
        if mr.contact_id != contact_id or mr.role not in ("i", "j"):
            continue
        if mr.plddt is not None and mr.plddt < plddt_min:
            return False
    return True


def _stats_for_contact(
    contact: Contact,
    hits_by_cluster: dict[str, ClusterHits],
    structure: Structure,
    cfg: StatsConfig,
    clustering: ClusteringBackend,
    feature_mode: FeatureMode,
    background: BackgroundModel,
) -> ContactStats:
    geometry = classify_geometry(
        structure,
        contact.i,
        contact.j,
        contact.buriedness_i,
        contact.buriedness_j,
        cfg.background,
    )
    alpha = cfg.pseudocount_alpha

    if contact.spans_gap:
        return ContactStats(
            contact_id=contact.id,
            status=ContactStatus.GAP_EXCLUDED,
            feature_mode=feature_mode,
            n_eff=0.0,
            n_hits_raw=0,
            n_clusters=0,
            low_confidence=True,
            geometry=geometry,
            pseudocount_alpha=alpha,
            top_pairs=[],
            marginals_i={},
            marginals_j={},
        )

    cluster_hits = hits_by_cluster.get(contact.cluster)
    raw_hits = cluster_hits.hits if cluster_hits is not None else []

    # §S4.1 read-out + §6.2 pLDDT filter: keep hits with both roles that clear pLDDT.
    surviving: list[Hit] = [
        h
        for h in raw_hits
        if _pair_residues(h, contact.id) is not None
        and _passes_plddt(h, contact.id, cfg.plddt_min)
    ]
    n_hits_raw = len(surviving)

    # §10 documented (not silent) cap before the reweighting clustering pass.
    capped = surviving[: cfg.max_hits_to_cluster]

    # §6.3 redundancy correction: each family collapses to total weight 1.
    clusters = clustering.assign_clusters(capped)
    weights = compute_weights(capped, clusters)
    n_clusters = len(set(clusters.values()))

    # §S4.4 weighted pair counts over the 20 canonical AAs (skip 'X').
    w_counts: dict[tuple[str, str], float] = {}
    for h in capped:
        pair = _pair_residues(h, contact.id)
        if pair is None:
            continue
        a, b = pair
        if a not in _AA_SET or b not in _AA_SET:
            continue
        w_counts[(a, b)] = w_counts.get((a, b), 0.0) + weights[h.target_id]

    w_tot = sum(w_counts.values())
    n_eff = round(w_tot, 4)

    if w_tot == 0.0:
        status = ContactStatus.NO_HITS
    elif n_eff < cfg.n_eff_min:
        status = ContactStatus.LOW_NEFF
    else:
        status = ContactStatus.OK

    denom = w_tot + alpha

    def freq(a: str, b: str) -> float:
        """§S4.4 background-weighted Dirichlet smoothing; finite even at low N_eff."""
        return (w_counts.get((a, b), 0.0) + alpha * background.f0(a, b)) / denom

    top_pairs: list[PairPropensity] = []
    for (a, b), w in w_counts.items():
        f = freq(a, b)
        f0 = background.f0(a, b)
        top_pairs.append(
            PairPropensity(
                a=a,
                b=b,
                weighted_count=w,
                frequency=f,
                background=f0,
                propensity=Propensity(math.log(f / f0)),
            )
        )
    top_pairs.sort(key=lambda p: p.propensity.value, reverse=True)
    top_pairs = top_pairs[:25]

    marginals_i: dict[str, float] = {}
    marginals_j: dict[str, float] = {}
    for a in AA20:
        marginals_i[a] = sum(freq(a, b) for b in AA20)
    for b in AA20:
        marginals_j[b] = sum(freq(a, b) for a in AA20)

    return ContactStats(
        contact_id=contact.id,
        status=status,
        feature_mode=feature_mode,
        n_eff=n_eff,
        n_hits_raw=n_hits_raw,
        n_clusters=n_clusters,
        low_confidence=status in _LOW_CONFIDENCE,
        geometry=geometry,
        pseudocount_alpha=alpha,
        top_pairs=top_pairs,
        marginals_i=marginals_i,
        marginals_j=marginals_j,
    )


def compute_stats(
    contacts: ContactsDoc,
    hits_by_cluster: dict[str, ClusterHits],
    structure: Structure,
    cfg: StatsConfig,
    clustering: ClusteringBackend,
    feature_mode: FeatureMode,
    provenance: Provenance,
) -> StatsSummary:
    """Compute per-contact residue statistics + the run-level summary (§S4).

    Pure compute: takes parsed hits and returns typed objects; parquet/json IO is
    centralized in the orchestrator.
    """
    background = BackgroundModel(cfg.background, mock_mode=provenance.mock_mode)

    contact_stats = [
        _stats_for_contact(
            c, hits_by_cluster, structure, cfg, clustering, feature_mode, background
        )
        for c in contacts.contacts
    ]

    status_counts: dict[ContactStatus, int] = dict(
        Counter(cs.status for cs in contact_stats)
    )

    # Make the offline stand-in null visible in provenance (§S4.4, §14.2).
    bg_spec = cfg.background
    if provenance.mock_mode and bg_spec.kind is BackgroundKind.GEOMETRY_BURIAL_CONDITIONED:
        suffix = ":offline-marginal-standin"
        if not bg_spec.corpus_id.endswith(suffix):
            bg_spec = bg_spec.model_copy(update={"corpus_id": bg_spec.corpus_id + suffix})

    return StatsSummary(
        backbone=contacts.backbone,
        feature_mode=feature_mode,
        background=bg_spec,
        provenance=provenance,
        n_eff_min=cfg.n_eff_min,
        status_counts=status_counts,
        contacts=contact_stats,
    )
