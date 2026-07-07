"""Tests for the 7A stability-triage front-end (spec §7A, D0–D6)."""

from __future__ import annotations

import pytest

from tessera.adapters.oracle import get_oracle
from tessera.io.pdb import Structure
from tessera.schemas.common import FeatureMode, Provenance
from tessera.schemas.weak_regions import WeakRegion, WeakRegionsDoc
from tessera.triage.core import _D6_FEATURES, run_triage

_EXCLUDED_RESIDUE = 13  # lands inside the 12–17 weak region on the toy structure


@pytest.fixture
def provenance() -> Provenance:
    return Provenance(feature_mode=FeatureMode.SEQUENCED, oracle_model="mock")


@pytest.fixture
def doc(toy_structure: Structure, toy_sequence: str, provenance: Provenance) -> WeakRegionsDoc:
    return run_triage(
        toy_structure,
        toy_sequence,
        get_oracle("mock"),
        provenance,
        exclude_residues=[_EXCLUDED_RESIDUE],
        structure_id="toy",
    )


def test_returns_ranked_doc(doc: WeakRegionsDoc) -> None:
    assert isinstance(doc, WeakRegionsDoc)
    assert doc.regions, "toy structure should surface weak regions"
    scores = [r.consensus_score for r in doc.regions]
    assert scores == sorted(scores, reverse=True)  # ranked by consensus desc (§D4)
    assert [r.id for r in doc.regions] == [f"w{i:03d}" for i in range(1, len(doc.regions) + 1)]


def test_functional_exclusion(doc: WeakRegionsDoc) -> None:
    excluded = [r for r in doc.regions if _EXCLUDED_RESIDUE in r.residues]
    assert len(excluded) == 1
    region = excluded[0]
    assert region.functional.excluded is True
    assert region.functional.incidental is False
    assert region.proposed_feature is None  # only incidental regions are routed (§D6)
    for other in doc.regions:
        if _EXCLUDED_RESIDUE not in other.residues:
            assert other.functional.excluded is False
            assert other.functional.incidental is True


def test_routing_features_valid(doc: WeakRegionsDoc) -> None:
    for region in doc.regions:
        if region.proposed_feature is not None:
            assert region.proposed_feature in _D6_FEATURES
            assert region.functional.incidental is True
            assert region.handoff == "install-mode (out of scope §2)"


def test_best_ddg_is_per_mutation_change(doc: WeakRegionsDoc) -> None:
    # best_ddg is a per-*mutation* ΔΔG (a change) — allowed (§14.5); assert the
    # signal carries it and no schema field synthesizes a per-residue absolute ΔG.
    for region in doc.regions:
        density = region.stabilizing_density
        assert density is not None
        assert density.best_ddg.value < 0.0
        fields = set(WeakRegionsDoc.model_fields) | set(WeakRegion.model_fields)
        assert not any("per_residue" in f for f in fields)


def test_no_per_residue_dg_in_serialization(doc: WeakRegionsDoc) -> None:
    blob = doc.model_dump_json()
    assert "per_residue_dg" not in blob
    assert "per_residue_ddg" not in blob


def test_signals_retain_native_units(doc: WeakRegionsDoc) -> None:
    for region in doc.regions:
        assert region.frustration is not None
        assert region.frustration.kind == "mutational"
        assert region.flexibility is not None
        assert region.flexibility.source == "plddt"
        # native-units flexibility for the toy structure is a bare pLDDT (§D3).
        assert 0.0 <= region.flexibility.value <= 100.0
