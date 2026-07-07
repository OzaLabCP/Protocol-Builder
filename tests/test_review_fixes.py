"""Regression tests for adversarial-review findings.

Each test pins the corrected behavior for a confirmed defect found in the
review pass, so the fix cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.adapters import get_clustering
from tessera.config import TesseraConfig
from tessera.io.pdb import load_structure
from tessera.orchestrator import is_mock_mode, run
from tessera.schemas.common import (
    BackgroundKind,
    BackgroundNullSpec,
    FeatureMode,
    Provenance,
)
from tessera.schemas.contacts import (
    Contact,
    ContactCluster,
    ContactParams,
    ContactsDoc,
)
from tessera.schemas.hits import ClusterHits, Hit, MatchedResidue
from tessera.stages.background import BackgroundModel
from tessera.stages.s4_stats import compute_stats

FIX = Path(__file__).parent / "fixtures"
TOY = FIX / "toy.pdb"


def _contacts() -> ContactsDoc:
    return ContactsDoc(
        backbone="toy",
        feature_mode=FeatureMode.SEQUENCED,
        params=ContactParams(),
        contacts=[
            Contact(id="c001", i=4, j=23, d_cb=4.8, buriedness_i=10, buriedness_j=10,
                    support=0, cluster="k1")
        ],
        clusters={"k1": ContactCluster(id="k1", contacts=["c001"], segments=[(4, 4), (23, 23)])},
    )


def _hit(tid: str, a: str, b: str) -> Hit:
    return Hit(
        cluster_id="k1", target_id=tid, source_db="afdb50", rmsd=0.5, score=0.1,
        motif_mean_plddt=90.0,
        matched_residues=[
            MatchedResidue(contact_id="c001", role="i", target_index=4, residue=a, plddt=90.0),
            MatchedResidue(contact_id="c001", role="j", target_index=23, residue=b, plddt=90.0),
        ],
    )


def _compute(hits: list[Hit], mock_mode: bool):
    struct = load_structure(TOY)
    cfg = TesseraConfig()
    prov = Provenance(feature_mode=FeatureMode.SEQUENCED, mock_mode=mock_mode)
    return compute_stats(
        _contacts(), {"k1": ClusterHits(cluster_id="k1", hits=hits)}, struct,
        cfg.stats, get_clustering("mock"), FeatureMode.SEQUENCED, prov,
    )


def test_background_standin_disclosed_even_when_not_mock():
    # finding #1: the offline stand-in null must be disclosed regardless of mock_mode
    s = _compute([_hit("MOCK000_1", "I", "L")], mock_mode=False)
    assert s.background.corpus_id.endswith(":offline-marginal-standin")


def test_background_model_standin_property():
    assert BackgroundModel(
        BackgroundNullSpec(kind=BackgroundKind.GEOMETRY_BURIAL_CONDITIONED)
    ).is_standin
    # when the configured null IS the independent marginal, f0 is exact, not a stand-in
    assert not BackgroundModel(
        BackgroundNullSpec(kind=BackgroundKind.INDEPENDENT_MARGINAL)
    ).is_standin


def test_x_member_does_not_undercount_neff():
    # finding #3: an 'X' member must not siphon weight away from N_eff — the canonical
    # member keeps the family's full unit weight, so one cluster => n_eff == 1.0
    s = _compute([_hit("MOCK000_1", "I", "L"), _hit("MOCK000_2", "X", "L")], mock_mode=True)
    assert s.contacts[0].n_eff == pytest.approx(1.0)
    assert s.contacts[0].n_clusters == 1


def test_is_mock_mode_counts_cross_check_oracle():
    # finding #4: a faked cross-check oracle makes the run mock, even if all else is real
    cfg = TesseraConfig()
    cfg.search.backend = "folddisco"
    cfg.oracle.primary = "thermompnn-d"
    cfg.stats.clustering_backend = "mmseqs2"
    cfg.oracle.cross_check = ["mock"]
    assert is_mock_mode(cfg) is True


def test_triage_path_warns_for_licensed_oracle(tmp_path):
    # finding #2: the triage entry point must emit the §9 license warning too
    cfg = TesseraConfig(
        backbone=str(TOY), seq="ACDEFGHIKLMNPQRSTVWYACDEFG",
        out=str(tmp_path / "t"), triage=True,
    )
    cfg.oracle.primary = "foldx"
    with pytest.warns(UserWarning, match="commercial license"):  # noqa: SIM117
        with pytest.raises(NotImplementedError):  # foldx real stub raises after the warning
            run(cfg)
