"""S7 · Ranking, reporting, constraint emission (spec §S7, §7.2).

Segregates confident wins from "no confident effect" (candidates within the ΔΔG
noise floor or above the minimum effect size, §7.2) and orders the recommended set
by ΔΔG with propensity / oracle-agreement tie-breaks. ΔΔGs are **never summed**
across candidates (§7.2): each is ranked on its own ``primary_ddg`` and multi-site
effects are arbitrated by a refold (§11.4), not by adding the table. Propensity
(a log-odds score) and ΔΔG (an energy) stay distinct throughout (§3.1, §14.5).
"""

from __future__ import annotations

from ..config import ReportConfig
from ..schemas.candidates import CandidatesDoc, Substitution
from ..schemas.common import StrictModel
from ..schemas.scored import ScoredCandidate


class RankedReport(StrictModel):
    """The S7 verdict: confident reinforcements vs. within-noise candidates (§S7).

    ``recommended`` holds the ``confident_effect`` winners, best (most-negative
    ``primary_ddg``) first; ``no_confident_effect`` holds everything within its own
    uncertainty band or above the effect-size floor (§7.2) — segregated, not
    dropped, so a null result is never presented as a win.
    """

    recommended: list[ScoredCandidate]
    no_confident_effect: list[ScoredCandidate]


def _is_demoted(c: ScoredCandidate) -> bool:
    """Flagged candidates are ranked below clean ones but never dropped (§S7)."""
    return c.flags.low_neff_provenance or c.flags.surface_cys


def _rank_key(c: ScoredCandidate) -> tuple[bool, float, float, float]:
    """Composite S7 sort key (§S7): flagged-last, then ΔΔG ascending (more negative
    = better), then propensity descending, then oracle sign-agreement descending
    (``None`` treated as 0). Only ``.value`` floats enter the key, so a Propensity
    is never compared against a DeltaG (§3.1, §14.5)."""
    agreement = c.oracle_sign_agreement if c.oracle_sign_agreement is not None else 0.0
    return (_is_demoted(c), c.primary_ddg.value, -c.propensity.value, -agreement)


def rank_candidates(scored: list[ScoredCandidate], cfg: ReportConfig) -> RankedReport:
    """Split confident wins from "no confident effect" and order each set (§7.2, §S7).

    The ``confident_effect`` verdict — set in S6 against ``cfg.ddg_min_effect`` and
    the oracle's own uncertainty band (§7.2) — decides the split. Within
    ``recommended`` candidates are ordered most-stabilizing first with flagged ones
    demoted (§S7); ΔΔGs are ranked individually and never summed (§7.2).
    """
    recommended = sorted((c for c in scored if c.confident_effect), key=_rank_key)
    no_confident_effect = sorted(
        (c for c in scored if not c.confident_effect), key=_rank_key
    )
    return RankedReport(recommended=recommended, no_confident_effect=no_confident_effect)


def accepted_substitutions(
    ranked: RankedReport, candidates: CandidatesDoc
) -> list[Substitution]:
    """Union of the recommended set's substitutions, deduped by position with the
    best-ranked candidate winning (first wins, §S7).

    The substitutions are joined from ``candidates`` by ``candidate_id`` because the
    frozen ``ScoredCandidate`` schema references mutation sets by id and does not
    itself carry the edits (see the module-level note / deviation).
    """
    subs_by_id = {m.id: m.substitutions for m in candidates.candidates}
    seen: set[int] = set()
    out: list[Substitution] = []
    for sc in ranked.recommended:  # already best-first from rank_candidates
        for sub in subs_by_id.get(sc.candidate_id, []):
            if sub.position in seen:
                continue  # position already claimed by a better-ranked set (first wins)
            seen.add(sub.position)
            out.append(sub)
    return out
