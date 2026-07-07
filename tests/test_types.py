"""Invariant §14.5: Propensity and DeltaG are distinct, non-mixable types.

The compiler side is enforced by mypy in CI; this file enforces the runtime side.
"""

from __future__ import annotations

import pytest

from tessera.schemas.scored import CandidateFlags, OracleMode, OracleScore, ScoredCandidate
from tessera.types import DeltaG, Propensity

pytestmark = pytest.mark.invariant


def test_propensity_and_deltag_do_not_mix():
    p, d = Propensity(1.0), DeltaG(-0.5, 0.4)
    with pytest.raises(TypeError):
        _ = p + d
    with pytest.raises(TypeError):
        _ = d + p
    with pytest.raises(TypeError):
        _ = p < d
    # equality across types is False (not an error) — but never equal
    assert (p == d) is False


def test_deltag_addition_is_undefined():
    # marginal ΔΔGs must not be summed (§7.2) — the true multi-site effect is a refold
    with pytest.raises(TypeError):
        _ = DeltaG(-0.5) + DeltaG(-0.3)


def test_propensity_arithmetic_within_type():
    assert (Propensity(1.0) + Propensity(0.5)).value == 1.5
    assert sum([Propensity(1.0), Propensity(2.0)], start=Propensity(0.0)).value == 3.0
    assert Propensity(2.0) > Propensity(1.0)


def test_deltag_effect_size_semantics():
    # clears the -0.5 floor and beats its own noise band
    assert DeltaG(-0.7, 0.4).is_confident_effect(-0.5) is True
    # above the floor
    assert DeltaG(-0.3, 0.4).is_confident_effect(-0.5) is False
    # within its own uncertainty band => not confident
    assert DeltaG(-0.6, 0.8).is_confident_effect(-0.5) is False


def test_types_roundtrip_through_pydantic():
    sc = ScoredCandidate(
        candidate_id="m001", contact_id="c001", cluster_id="k1",
        propensity=Propensity(1.4),
        oracle_scores=[OracleScore(oracle="mock", mode=OracleMode.EPISTATIC_DOUBLE, ddg=DeltaG(-0.9, 0.4))],
        primary_ddg=DeltaG(-0.9, 0.4),
        flags=CandidateFlags(),
        confident_effect=True,
    )
    back = ScoredCandidate.model_validate_json(sc.model_dump_json())
    assert isinstance(back.propensity, Propensity) and back.propensity.value == 1.4
    assert isinstance(back.primary_ddg, DeltaG) and back.primary_ddg.uncertainty == 0.4
