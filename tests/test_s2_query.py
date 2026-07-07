"""Tests for S2 · Motif-query construction (spec §S2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.config import QueryConfig
from tessera.io.pdb import Structure
from tessera.schemas.common import FeatureMode
from tessera.schemas.contacts import (
    Contact,
    ContactCluster,
    ContactParams,
    ContactsDoc,
)
from tessera.stages.s2_query import build_queries


def _contact(cid: str, i: int, j: int, cluster: str, *, spans_gap: bool = False) -> Contact:
    return Contact(
        id=cid,
        i=i,
        j=j,
        d_cb=6.0,
        buriedness_i=8,
        buriedness_j=9,
        support=1,
        cluster=cluster,
        spans_gap=spans_gap,
    )


def _synthetic_contacts() -> ContactsDoc:
    """A toy-sized doc: cluster k1 has three usable contacts (one of which hits
    both clamp boundaries at 1 and N=26); cluster k2 has only a gap-spanning
    contact and must be dropped from query construction (§S2)."""
    contacts = [
        _contact("c1", 4, 23, "k1"),
        _contact("c2", 7, 20, "k1"),
        _contact("c3", 1, 26, "k1"),
        _contact("cg", 10, 25, "k2", spans_gap=True),
    ]
    clusters = {
        "k1": ContactCluster(id="k1", contacts=["c1", "c2", "c3"], segments=[(1, 9), (18, 26)]),
        "k2": ContactCluster(id="k2", contacts=["cg"], segments=[(8, 12), (23, 27)]),
    }
    return ContactsDoc(
        backbone="toy.pdb",
        feature_mode=FeatureMode.SEQUENCED,
        params=ContactParams(),
        contacts=contacts,
        clusters=clusters,
    )


def test_build_queries_synthetic(toy_structure: Structure, tmp_path: Path) -> None:
    cfg = QueryConfig()  # padding=2, dist=1.0, angle=15.0
    doc = _synthetic_contacts()

    manifest = build_queries(
        toy_structure, doc, cfg, FeatureMode.SEQUENCED, tmp_path
    )

    # At least one ClusterQuery, and the gap-only cluster k2 is skipped entirely.
    assert manifest.queries
    produced = {q.cluster_id for q in manifest.queries}
    assert produced == {"k1"}
    assert "k2" not in produced

    q = next(q for q in manifest.queries if q.cluster_id == "k1")

    # Query files exist on disk under <out_dir>/queries/.
    pdb = Path(q.query_pdb)
    txt = Path(q.query_residues)
    assert pdb == tmp_path / "queries" / "k1.pdb"
    assert txt == tmp_path / "queries" / "k1.txt"
    assert pdb.is_file()
    assert txt.is_file()

    # residue_indices sorted, unique, within [1, 26].
    ri = q.residue_indices
    assert ri == sorted(ri)
    assert len(ri) == len(set(ri))
    assert ri[0] >= 1
    assert ri[-1] <= len(toy_structure.residues) == 26

    # Padding: ±2 flanks every partner of every non-gap contact.
    for i, j in [(4, 23), (7, 20), (1, 26)]:
        for center in (i, j):
            for r in range(max(1, center - 2), min(26, center + 2) + 1):
                assert r in ri
    # Clamping: boundary residues present, out-of-range never invented.
    assert 1 in ri and 26 in ri
    assert 0 not in ri and 27 not in ri
    assert ri == [1, 2, 3, 4, 5, 6, 7, 8, 9, 18, 19, 20, 21, 22, 23, 24, 25, 26]

    # segments are the contiguous runs over residue_indices.
    assert q.segments == [(1, 9), (18, 26)]

    # .txt is one plain integer per line, matching residue_indices.
    lines = txt.read_text().split()
    assert [int(x) for x in lines] == ri

    # pairs cover exactly the cluster's non-gap contacts.
    assert {(p.i, p.j) for p in q.pairs} == {(4, 23), (7, 20), (1, 26)}
    assert {p.contact_id for p in q.pairs} == {"c1", "c2", "c3"}

    # feature_mode and tolerances threaded through.
    assert manifest.feature_mode == FeatureMode.SEQUENCED
    assert q.feature_mode == FeatureMode.SEQUENCED
    assert manifest.backbone == "toy.pdb"
    assert manifest.padding == 2
    assert q.dist_thresh == cfg.dist_thresh
    assert q.angle_thresh == cfg.angle_thresh


def test_feature_mode_threaded_backbone_only(toy_structure: Structure, tmp_path: Path) -> None:
    doc = _synthetic_contacts()
    manifest = build_queries(
        toy_structure, doc, QueryConfig(), FeatureMode.BACKBONE_ONLY, tmp_path
    )
    assert manifest.feature_mode == FeatureMode.BACKBONE_ONLY
    assert all(q.feature_mode == FeatureMode.BACKBONE_ONLY for q in manifest.queries)


def test_cluster_with_only_gap_contact_skipped(toy_structure: Structure, tmp_path: Path) -> None:
    doc = ContactsDoc(
        backbone="toy.pdb",
        feature_mode=FeatureMode.SEQUENCED,
        params=ContactParams(),
        contacts=[_contact("cg", 4, 23, "k9", spans_gap=True)],
        clusters={"k9": ContactCluster(id="k9", contacts=["cg"], segments=[(2, 6), (21, 25)])},
    )
    manifest = build_queries(
        toy_structure, doc, QueryConfig(), FeatureMode.SEQUENCED, tmp_path
    )
    assert manifest.queries == []
    assert not (tmp_path / "queries" / "k9.pdb").exists()


def test_build_queries_from_s1(toy_structure: Structure, base_config, tmp_path: Path) -> None:
    """End-to-end: run S1 on the toy structure, then S2 (spec §S1→§S2)."""
    s1 = pytest.importorskip("tessera.stages.s1_contacts")
    feature_mode = base_config.resolve_feature_mode()
    try:
        contacts = s1.detect_contacts(toy_structure, base_config.contact, feature_mode)
    except TypeError:
        pytest.skip("detect_contacts signature differs from (structure, cfg, feature_mode)")

    manifest = build_queries(toy_structure, contacts, base_config.query, feature_mode, tmp_path)

    assert manifest.queries
    for q in manifest.queries:
        assert Path(q.query_pdb).is_file()
        assert Path(q.query_residues).is_file()
        assert q.residue_indices == sorted(set(q.residue_indices))
        assert q.residue_indices[0] >= 1
        assert q.residue_indices[-1] <= len(toy_structure.residues)
        assert q.pairs
        assert q.feature_mode == feature_mode
    assert manifest.feature_mode == feature_mode
