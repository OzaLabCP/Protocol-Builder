"""S5 candidate-enumeration tests (spec §S5).

Hand-built StatsSummary + ContactsDoc so the propensity ranking, dedup against
the current sequence, hard constraints (forbidden / fixed / surface-Cys), the
per-cluster cap, and the oracle-only fallback are all exercised deterministically.
"""

from __future__ import annotations

import pytest

from tessera.adapters.oracle import get_oracle
from tessera.config import CandidateConfig
from tessera.schemas.common import (
    BackgroundNullSpec,
    BurialClass,
    ContactStatus,
    FeatureMode,
    OrientationClass,
    Provenance,
)
from tessera.schemas.contacts import (
    Contact,
    ContactCluster,
    ContactParams,
    ContactsDoc,
)
from tessera.schemas.stats import (
    ContactGeometry,
    ContactStats,
    PairPropensity,
    StatsSummary,
)
from tessera.stages.s5_candidates import enumerate_candidates
from tessera.types import Propensity

SEQ = "ACDEFGHIKLMNPQRSTVWYACDEFG"  # matches conftest TOY_SEQUENCE


def _pp(a: str, b: str, prop: float) -> PairPropensity:
    return PairPropensity(
        a=a, b=b, weighted_count=10.0, frequency=0.3, background=0.05,
        propensity=Propensity(prop),
    )


def _geom(burial: BurialClass) -> ContactGeometry:
    return ContactGeometry(
        d_cb=5.0, distance_bin=1, orientation=OrientationClass.ANTIPARALLEL, burial=burial
    )


def _build() -> tuple[StatsSummary, ContactsDoc]:
    """Two OK contacts (c1 buried, c2 exposed) in separate clusters, plus a
    LOW_NEFF contact c3 that must contribute nothing."""
    contacts = ContactsDoc(
        backbone="toy",
        feature_mode=FeatureMode.SEQUENCED,
        params=ContactParams(),
        contacts=[
            Contact(id="c1", i=4, j=23, d_cb=4.8, buriedness_i=20, buriedness_j=18,
                    support=1, cluster="cl1"),
            Contact(id="c2", i=5, j=20, d_cb=5.0, buriedness_i=15, buriedness_j=12,
                    support=1, cluster="cl2"),
            Contact(id="c3", i=6, j=18, d_cb=6.0, buriedness_i=10, buriedness_j=9,
                    support=0, cluster="cl3"),
        ],
        clusters={
            "cl1": ContactCluster(id="cl1", contacts=["c1"], segments=[(4, 4), (23, 23)]),
            "cl2": ContactCluster(id="cl2", contacts=["c2"], segments=[(5, 5), (20, 20)]),
            "cl3": ContactCluster(id="cl3", contacts=["c3"], segments=[(6, 6), (18, 18)]),
        },
    )
    stats = StatsSummary(
        backbone="toy",
        feature_mode=FeatureMode.SEQUENCED,
        background=BackgroundNullSpec(),
        provenance=Provenance(feature_mode=FeatureMode.SEQUENCED),
        contacts=[
            # c1: current (E,D) @ 0.5; W,Y and I,L beat it, A,A does not.
            ContactStats(
                contact_id="c1", status=ContactStatus.OK, feature_mode=FeatureMode.SEQUENCED,
                n_eff=42.0, n_hits_raw=50, n_clusters=5, low_confidence=False,
                geometry=_geom(BurialClass.BURIED),
                top_pairs=[
                    _pp("W", "Y", 2.0), _pp("I", "L", 1.5),
                    _pp("E", "D", 0.5), _pp("A", "A", 0.2),
                ],
            ),
            # c2 exposed: current (F,Y) @ 0.5; C,K would win but is surface Cys; M,L kept.
            ContactStats(
                contact_id="c2", status=ContactStatus.OK, feature_mode=FeatureMode.SEQUENCED,
                n_eff=30.0, n_hits_raw=35, n_clusters=4, low_confidence=False,
                geometry=_geom(BurialClass.EXPOSED),
                top_pairs=[
                    _pp("C", "K", 2.0), _pp("M", "L", 1.8), _pp("F", "Y", 0.5),
                ],
            ),
            # c3: below N_eff floor — must yield nothing even with a strong pair.
            ContactStats(
                contact_id="c3", status=ContactStatus.LOW_NEFF, feature_mode=FeatureMode.SEQUENCED,
                n_eff=3.0, n_hits_raw=4, n_clusters=1, low_confidence=True,
                geometry=_geom(BurialClass.BURIED),
                top_pairs=[_pp("W", "F", 3.0)],
            ),
        ],
    )
    return stats, contacts


def test_retrieval_basic() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(stats, contacts, SEQ, CandidateConfig(), retrieve=True)

    assert doc.retrieval_used is True
    assert doc.backbone == "toy"
    assert doc.feature_mode is FeatureMode.SEQUENCED
    assert doc.candidates

    for m in doc.candidates:
        assert isinstance(m.propensity, Propensity)
        for s in m.substitutions:
            assert s.from_aa != s.to_aa  # never propose the current residue

    # Only OK contacts contribute — c3's positions (6, 18) never appear.
    positions = {s.position for m in doc.candidates for s in m.substitutions}
    assert 6 not in positions
    assert 18 not in positions

    assert len(doc.candidates) <= doc.candidate_cap


def test_low_neff_yields_no_candidates() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(stats, contacts, SEQ, CandidateConfig(), retrieve=True)
    assert all(m.contact_id != "c3" for m in doc.candidates)
    assert all(m.cluster_id != "cl3" for m in doc.candidates)


def test_margin_dedup_against_current() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(stats, contacts, SEQ, CandidateConfig(), retrieve=True)
    # c1 keeps only pairs beating current (0.5): W,Y and I,L; A,A (0.2) is dropped.
    c1_props = sorted(
        float(m.propensity) for m in doc.candidates if m.contact_id == "c1"
    )
    assert c1_props == [1.5, 2.0]


def test_surface_cys_excluded_when_exposed() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(stats, contacts, SEQ, CandidateConfig(), retrieve=True)
    to_aas = {s.to_aa for m in doc.candidates for s in m.substitutions}
    assert "C" not in to_aas          # C,K at exposed c2 dropped
    assert "M" in to_aas              # M,L survived at c2


def test_buried_cys_allowed() -> None:
    stats, contacts = _build()
    # Flip c2 to buried: the surface-Cys guard no longer applies.
    stats.contacts[1].geometry = _geom(BurialClass.BURIED)
    doc = enumerate_candidates(stats, contacts, SEQ, CandidateConfig(), retrieve=True)
    to_aas = {s.to_aa for m in doc.candidates for s in m.substitutions}
    assert "C" in to_aas


def test_forbidden_residues_excluded() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(
        stats, contacts, SEQ, CandidateConfig(forbidden_residues=["W", "M"]), retrieve=True
    )
    to_aas = {s.to_aa for m in doc.candidates for s in m.substitutions}
    assert "W" not in to_aas
    assert "M" not in to_aas


def test_fixed_positions_excluded() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(
        stats, contacts, SEQ, CandidateConfig(fixed_positions=[4]), retrieve=True
    )
    positions = {s.position for m in doc.candidates for s in m.substitutions}
    assert 4 not in positions
    assert all(m.contact_id != "c1" for m in doc.candidates)  # every c1 candidate touched 4


def test_candidate_cap_and_dropped() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(
        stats, contacts, SEQ, CandidateConfig(candidate_cap=1), retrieve=True
    )
    assert doc.candidate_cap == 1
    assert len(doc.candidates) <= 1
    assert doc.dropped_combinations >= 1


def test_no_sequence_proposes_both_sides() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(stats, contacts, None, CandidateConfig(), retrieve=True)
    assert doc.candidates
    for m in doc.candidates:
        for s in m.substitutions:
            assert s.from_aa == "X"  # de-novo proposal, no current residue known


def test_oracle_mode() -> None:
    stats, contacts = _build()
    doc = enumerate_candidates(
        stats, contacts, SEQ, CandidateConfig(), retrieve=False,
        oracle=get_oracle("mock"), structure_id="toy",
    )
    assert doc.retrieval_used is False
    assert doc.candidates
    for m in doc.candidates:
        assert isinstance(m.propensity, Propensity)
        assert m.propensity == Propensity(0.0)
        assert m.n_eff == 0.0
        assert len(m.substitutions) == 2


def test_oracle_mode_requires_oracle() -> None:
    stats, contacts = _build()
    with pytest.raises(ValueError):
        enumerate_candidates(stats, contacts, SEQ, CandidateConfig(), retrieve=False, oracle=None)
