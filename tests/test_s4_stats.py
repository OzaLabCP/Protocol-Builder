"""Tests for S4 · Hit parsing + residue statistics (spec §S4, §6.2, §6.3, §14.6).

The reweighting golden test is the §11.6 silent-bug guard: redundancy collapse
*must* flip the top residue pair away from the over-sampled family.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tessera.adapters import get_clustering, parse_hits_tsv
from tessera.config import StatsConfig
from tessera.io.pdb import Structure
from tessera.schemas.common import ContactStatus, FeatureMode, Provenance
from tessera.schemas.contacts import (
    Contact,
    ContactCluster,
    ContactParams,
    ContactsDoc,
)
from tessera.schemas.hits import ClusterHits, Hit, MatchedResidue
from tessera.stages.s4_stats import compute_stats
from tessera.types import Propensity

FIXTURES = Path(__file__).parent / "fixtures"


def _contacts_doc(contact: Contact, cluster: ContactCluster) -> ContactsDoc:
    return ContactsDoc(
        backbone="toy.pdb",
        feature_mode=FeatureMode.SEQUENCED,
        params=ContactParams(),
        contacts=[contact],
        clusters={cluster.id: cluster},
    )


def _c001() -> Contact:
    return Contact(
        id="c001", i=4, j=23, cluster="k1", d_cb=4.8,
        buriedness_i=10, buriedness_j=10, support=0,
    )


def _k1() -> ContactCluster:
    return ContactCluster(id="k1", contacts=["c001"], segments=[(4, 4), (23, 23)])


def _provenance() -> Provenance:
    return Provenance(feature_mode=FeatureMode.SEQUENCED, mock_mode=True)


@pytest.mark.golden
def test_reweighting_flips_top_pair(toy_structure: Structure) -> None:
    """§11.6: naive counts favor (I, L) 10:3, but MOCK000's 10 members collapse to
    one cluster of weight 1, so redundancy-corrected (W, Y) wins 3.0 vs 1.0."""
    hits = parse_hits_tsv(FIXTURES / "hits_reweight.tsv", cluster_id="k1")
    doc = _contacts_doc(_c001(), _k1())

    summary = compute_stats(
        contacts=doc,
        hits_by_cluster={"k1": hits},
        structure=toy_structure,
        cfg=StatsConfig(),
        clustering=get_clustering("mock"),
        feature_mode=FeatureMode.SEQUENCED,
        provenance=_provenance(),
    )

    assert len(summary.contacts) == 1
    cs = summary.contacts[0]

    top = cs.top_pairs[0]
    assert (top.a, top.b) == ("W", "Y")  # NOT (I, L) — the point of the test (§11.6)

    assert cs.n_clusters == 4
    assert cs.n_eff == pytest.approx(4.0)
    # n_eff (4.0) < StatsConfig().n_eff_min (10) => LOW_NEFF, not OK. The task text
    # asserted OK, but that contradicts the pinned n_eff==4.0 and the default
    # n_eff_min=10; the reweighting flip above is the load-bearing assertion (§11.6).
    assert cs.status is ContactStatus.LOW_NEFF

    for pp in cs.top_pairs:
        assert isinstance(pp.propensity, Propensity)
        assert math.isfinite(pp.frequency)
        assert pp.frequency > 0.0


def test_single_hit_is_low_neff_and_finite(toy_structure: Structure) -> None:
    """A lone hit yields N_eff = 1 < N_eff_min = 10 (LOW_NEFF), and pseudocount
    smoothing keeps every frequency finite (§S4.4 low-N_eff case)."""
    hit = Hit(
        cluster_id="k1", target_id="SOLO_0001", source_db="pdb", rmsd=0.5, score=0.1,
        motif_mean_plddt=None,
        matched_residues=[
            MatchedResidue(contact_id="c001", role="i", target_index=4, residue="I"),
            MatchedResidue(contact_id="c001", role="j", target_index=23, residue="L"),
        ],
    )
    hits = ClusterHits(cluster_id="k1", hits=[hit])
    doc = _contacts_doc(_c001(), _k1())

    summary = compute_stats(
        contacts=doc,
        hits_by_cluster={"k1": hits},
        structure=toy_structure,
        cfg=StatsConfig(),
        clustering=get_clustering("mock"),
        feature_mode=FeatureMode.SEQUENCED,
        provenance=_provenance(),
    )
    cs = summary.contacts[0]
    assert cs.n_eff == pytest.approx(1.0)
    assert cs.status is ContactStatus.LOW_NEFF
    assert cs.low_confidence
    for pp in cs.top_pairs:
        assert math.isfinite(pp.frequency) and pp.frequency > 0.0
    assert all(math.isfinite(v) for v in cs.marginals_i.values())


def test_empty_cluster_is_no_hits(toy_structure: Structure) -> None:
    """An empty hit set -> NO_HITS with N_eff == 0 (§S4 failure handling)."""
    doc = _contacts_doc(_c001(), _k1())
    summary = compute_stats(
        contacts=doc,
        hits_by_cluster={"k1": ClusterHits(cluster_id="k1", hits=[])},
        structure=toy_structure,
        cfg=StatsConfig(),
        clustering=get_clustering("mock"),
        feature_mode=FeatureMode.SEQUENCED,
        provenance=_provenance(),
    )
    cs = summary.contacts[0]
    assert cs.status is ContactStatus.NO_HITS
    assert cs.n_eff == 0.0
    assert cs.top_pairs == []
