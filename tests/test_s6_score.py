"""S6 · ΔΔG screening tests (spec §S6, §7.1, §7.2, §7.3).

Every scored candidate carries a DeltaG with an uncertainty (never a bare number,
§7.2); the propensity rides through as a Propensity and is never conflated with the
energy (§3.1). A per-candidate oracle crash skips that candidate, not the run (§S4).
"""

from __future__ import annotations

import pytest

from tessera.adapters import MockOracle, OracleBackend, get_oracle
from tessera.schemas.candidates import CandidatesDoc, MutationSet, Substitution
from tessera.schemas.common import FeatureMode
from tessera.schemas.scored import OracleMode
from tessera.stages.s6_score import hydrophobicity_increases, score_candidates
from tessera.types import DeltaG, Propensity

STRUCTURE_ID = "toy"
SEQUENCE = "ACDEFGHIKLMNPQRSTVWYACDEFG"
DDG_MIN_EFFECT = -0.5


def _pair_candidate() -> MutationSet:
    """A 2-substitution contact pair at two distinct positions (epistatic double)."""
    return MutationSet(
        id="m001",
        substitutions=[
            Substitution(position=4, from_aa="E", to_aa="W"),
            Substitution(position=23, from_aa="D", to_aa="Y"),
        ],
        propensity=Propensity(1.5),
        contact_id="c001",
        cluster_id="k1",
        n_eff=20.0,
        multibody=False,
    )


def _single_candidate() -> MutationSet:
    """A one-substitution reinforcement from a low-N_eff contact (single mode)."""
    return MutationSet(
        id="m002",
        substitutions=[Substitution(position=8, from_aa="I", to_aa="L")],
        propensity=Propensity(0.8),
        contact_id="c002",
        cluster_id="k2",
        n_eff=5.0,
        multibody=False,
    )


def _cys_candidate() -> MutationSet:
    """A candidate proposing cysteine (surface-Cys artifact flag, §7.2)."""
    return MutationSet(
        id="m003",
        substitutions=[Substitution(position=4, from_aa="E", to_aa="C")],
        propensity=Propensity(0.5),
        contact_id="c003",
        cluster_id="k3",
        n_eff=20.0,
        multibody=False,
    )


def _doc(*candidates: MutationSet) -> CandidatesDoc:
    return CandidatesDoc(
        backbone="toy.pdb",
        feature_mode=FeatureMode.SEQUENCED,
        retrieval_used=True,
        beam_width=1,
        candidates=list(candidates),
    )


def test_hydrophobicity_increases_kyte_doolittle() -> None:
    """E->W (−3.5 -> −0.9) raises net hydropathy; W->E lowers it (§7.2)."""
    assert hydrophobicity_increases([Substitution(position=4, from_aa="E", to_aa="W")])
    assert not hydrophobicity_increases([Substitution(position=4, from_aa="W", to_aa="E")])
    # summed over subs: net of two increases is still an increase
    assert hydrophobicity_increases(
        [
            Substitution(position=4, from_aa="D", to_aa="I"),
            Substitution(position=8, from_aa="N", to_aa="V"),
        ]
    )


def test_each_result_has_ddg_with_uncertainty_and_matching_confidence() -> None:
    """Every primary_ddg is a DeltaG with an uncertainty, and confident_effect
    exactly mirrors DeltaG.is_confident_effect(ddg_min_effect) (§7.2)."""
    doc = _doc(_pair_candidate(), _single_candidate())
    scored = score_candidates(
        doc, STRUCTURE_ID, SEQUENCE, get_oracle("mock"), [], DDG_MIN_EFFECT
    )
    assert len(scored) == 2
    for sc in scored:
        assert isinstance(sc.primary_ddg, DeltaG)
        assert sc.primary_ddg.uncertainty is not None
        assert isinstance(sc.propensity, Propensity)
        assert sc.confident_effect == sc.primary_ddg.is_confident_effect(DDG_MIN_EFFECT)


def test_modes_track_substitution_count() -> None:
    """A contact pair scores epistatic-double; a lone edit scores single (§7.1)."""
    doc = _doc(_pair_candidate(), _single_candidate())
    scored = score_candidates(
        doc, STRUCTURE_ID, SEQUENCE, get_oracle("mock"), [], DDG_MIN_EFFECT
    )
    by_id = {sc.candidate_id: sc for sc in scored}
    assert by_id["m001"].oracle_scores[0].mode is OracleMode.EPISTATIC_DOUBLE
    assert by_id["m002"].oracle_scores[0].mode is OracleMode.SINGLE


def test_no_cross_check_means_sign_agreement_is_none() -> None:
    """A single oracle leaves oracle_sign_agreement undefined (§7.3)."""
    doc = _doc(_pair_candidate(), _single_candidate())
    scored = score_candidates(
        doc, STRUCTURE_ID, SEQUENCE, get_oracle("mock"), [], DDG_MIN_EFFECT
    )
    for sc in scored:
        assert sc.oracle_sign_agreement is None
        assert len(sc.oracle_scores) == 1


def test_surface_cys_and_low_neff_flags() -> None:
    """to_aa == 'C' sets surface_cys; n_eff below the floor sets low_neff_provenance."""
    doc = _doc(_cys_candidate(), _single_candidate())
    scored = score_candidates(
        doc, STRUCTURE_ID, SEQUENCE, get_oracle("mock"), [], DDG_MIN_EFFECT, n_eff_min=10.0
    )
    by_id = {sc.candidate_id: sc for sc in scored}
    assert by_id["m003"].flags.surface_cys
    assert not by_id["m002"].flags.surface_cys
    assert by_id["m002"].flags.low_neff_provenance  # n_eff 5.0 < 10.0
    assert not by_id["m003"].flags.low_neff_provenance  # n_eff 20.0


def test_within_noise_matches_uncertainty_band() -> None:
    """within_noise is exactly |ΔΔG| <= its own uncertainty (§7.2 noise floor)."""
    doc = _doc(_pair_candidate(), _single_candidate(), _cys_candidate())
    scored = score_candidates(
        doc, STRUCTURE_ID, SEQUENCE, get_oracle("mock"), [], DDG_MIN_EFFECT
    )
    for sc in scored:
        u = sc.primary_ddg.uncertainty
        expected = u is not None and abs(sc.primary_ddg.value) <= u
        assert sc.flags.within_noise == expected


def test_cross_check_yields_sign_agreement_fraction() -> None:
    """With a cross-check, oracle_sign_agreement is a fraction in [0, 1] (§7.3).
    The same backend twice agrees perfectly, so it is 1.0."""
    doc = _doc(_pair_candidate(), _single_candidate())
    scored = score_candidates(
        doc, STRUCTURE_ID, SEQUENCE, get_oracle("mock"), [get_oracle("mock")], DDG_MIN_EFFECT
    )
    for sc in scored:
        assert sc.oracle_sign_agreement is not None
        assert 0.0 <= sc.oracle_sign_agreement <= 1.0
        assert sc.oracle_sign_agreement == pytest.approx(1.0)
        assert len(sc.oracle_scores) == 2


class _FailingDoubleOracle(MockOracle):
    """Mock whose epistatic-double mode crashes — stands in for an S6 subprocess
    failure on a single candidate (§S4 ORACLE_FAILED)."""

    name = "failing"

    def score_double(
        self, structure: str, sequence: str, pos_i: int, to_i: str, pos_j: int, to_j: str
    ) -> DeltaG:
        raise RuntimeError("oracle subprocess crashed for this candidate")


def test_oracle_exception_skips_that_candidate_only() -> None:
    """A candidate whose primary scoring raises is dropped; the run and the other
    candidates survive (§S4)."""
    oracle: OracleBackend = _FailingDoubleOracle()
    doc = _doc(_pair_candidate(), _single_candidate())
    scored = score_candidates(doc, STRUCTURE_ID, SEQUENCE, oracle, [], DDG_MIN_EFFECT)
    ids = {sc.candidate_id for sc in scored}
    assert "m001" not in ids  # the 2-sub pair went through score_double -> raised
    assert "m002" in ids       # the single-sub survived via score_single
