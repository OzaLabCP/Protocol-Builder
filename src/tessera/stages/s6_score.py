"""S6 · ΔΔG screening / oracle (spec §S6, §7.1, §7.2, §7.3).

Converts S5 candidates into calibrated energetics: the only stage that emits a
ΔG-like number (§S6). Each candidate is scored by the primary oracle behind the
swappable interface (§7.1) — *epistatic double* for the two residues of a contact
scored jointly, *single* for one-sided reinforcements — plus any cross-check
backends (§7.3). Every score is a :class:`~tessera.types.DeltaG` carrying its
per-prediction uncertainty (§7.2); the candidate's S5 :class:`Propensity` rides
through untouched and is never conflated with the energy (§3.1, §14.5).

Ranking and the minimum-effect-size segregation live downstream in S7 (§S7); this
stage returns unranked scored candidates with interpretation flags attached.
"""

from __future__ import annotations

from ..adapters.oracle import OracleBackend
from ..schemas.candidates import CandidatesDoc, Substitution
from ..schemas.scored import (
    CandidateFlags,
    OracleMode,
    OracleScore,
    ScoredCandidate,
)
from ..types import DeltaG

# Kyte–Doolittle hydropathy scale (§7.2 surface-hydrophobicity / aggregation
# flag). Canonical scale — larger = more hydrophobic; unknown residues score 0.
_KYTE_DOOLITTLE: dict[str, float] = {
    "A": 1.8, "R": -4.5, "N": -3.5, "D": -3.5, "C": 2.5, "Q": -3.5, "E": -3.5,
    "G": -0.4, "H": -3.2, "I": 4.5, "L": 3.8, "K": -3.9, "M": 1.9, "F": 2.8,
    "P": -1.6, "S": -0.8, "T": -0.7, "W": -0.9, "Y": -1.3, "V": 4.2,
}


def hydrophobicity_increases(subs: list[Substitution]) -> bool:
    """True if the net Kyte–Doolittle hydropathy (to_aa minus from_aa, summed over
    the substitutions) is positive — the §7.2 surface-hydrophobicity/aggregation
    flag (useful for a buried core, dangerous on the surface)."""
    net = sum(
        _KYTE_DOOLITTLE.get(s.to_aa, 0.0) - _KYTE_DOOLITTLE.get(s.from_aa, 0.0)
        for s in subs
    )
    return net > 0.0


def _sign(x: float) -> int:
    """Sign of a ΔΔG for oracle agreement (§7.3): 0 is its own sign."""
    return (x > 0.0) - (x < 0.0)


def _score_with(
    backend: OracleBackend, subs: list[Substitution], structure_id: str, sequence: str
) -> tuple[DeltaG, OracleMode]:
    """Score one candidate with one backend, picking the oracle mode from the
    substitution count (§7.1): single for one edit; epistatic-double for a contact
    pair or the first two positions of a multibody set."""
    if len(subs) == 1:
        s = subs[0]
        return backend.score_single(structure_id, sequence, s.position, s.to_aa), OracleMode.SINGLE
    ordered = sorted(subs, key=lambda s: s.position)
    a, b = ordered[0], ordered[1]
    ddg = backend.score_double(
        structure_id, sequence, a.position, a.to_aa, b.position, b.to_aa
    )
    return ddg, OracleMode.EPISTATIC_DOUBLE


def score_candidates(
    candidates: CandidatesDoc,
    structure_id: str,
    sequence: str,
    oracle: OracleBackend,
    cross_checks: list[OracleBackend],
    ddg_min_effect: float,
    n_eff_min: float = 10.0,
) -> list[ScoredCandidate]:
    """Score S5 candidates with the primary oracle and optional cross-checks (§S6).

    The primary oracle sets ``primary_ddg`` and the ranking mode; a candidate whose
    primary scoring raises is *skipped* (an S6 subprocess crash is per-candidate,
    not per-run — §S4 ``ORACLE_FAILED``), not fatal. Cross-check backends are
    best-effort second opinions (§7.3): one that raises is dropped for that
    candidate only. Returns unranked scored candidates — S7 ranks (§S7).
    """
    results: list[ScoredCandidate] = []
    for cand in candidates.candidates:
        try:
            primary_ddg, mode = _score_with(oracle, cand.substitutions, structure_id, sequence)
        except Exception:
            continue  # per-candidate oracle failure; the run continues (§S4)

        oracle_scores = [OracleScore(oracle=oracle.name, mode=mode, ddg=primary_ddg)]
        for backend in cross_checks:
            try:
                cross_ddg, cross_mode = _score_with(
                    backend, cand.substitutions, structure_id, sequence
                )
            except Exception:
                continue  # skip a cross-check that raises (§7.3 best-effort)
            oracle_scores.append(
                OracleScore(oracle=backend.name, mode=cross_mode, ddg=cross_ddg)
            )

        if len(oracle_scores) >= 2:
            primary_sign = _sign(primary_ddg.value)
            agree = sum(1 for os in oracle_scores if _sign(os.ddg.value) == primary_sign)
            oracle_sign_agreement: float | None = agree / len(oracle_scores)
        else:
            oracle_sign_agreement = None

        flags = CandidateFlags(
            surface_cys=any(s.to_aa == "C" for s in cand.substitutions),
            increases_hydrophobicity=hydrophobicity_increases(cand.substitutions),
            low_neff_provenance=cand.n_eff < n_eff_min,
            multibody=cand.multibody,
            within_noise=(
                primary_ddg.uncertainty is not None
                and abs(primary_ddg.value) <= primary_ddg.uncertainty
            ),
        )

        results.append(
            ScoredCandidate(
                candidate_id=cand.id,
                contact_id=cand.contact_id,
                cluster_id=cand.cluster_id,
                propensity=cand.propensity,
                oracle_scores=oracle_scores,
                primary_ddg=primary_ddg,
                oracle_sign_agreement=oracle_sign_agreement,
                flags=flags,
                confident_effect=primary_ddg.is_confident_effect(ddg_min_effect),
            )
        )
    return results
