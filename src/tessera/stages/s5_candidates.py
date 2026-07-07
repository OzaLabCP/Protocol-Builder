"""S5 · Candidate enumeration (spec §S5).

Two modes share one entry point. In **retrieval** mode this harvests the
per-contact log-odds propensities from S4, keeps the top-k pairs that beat the
current assignment, and composes them into per-cluster joint combinations under
a beam so no dense cluster eats the whole ``candidate_cap`` (§S5). In
**oracle-only** mode (``--no-retrieve``, §3.7) it ignores retrieval and does a
bounded site-saturation + epistatic-double scan through the S6 oracle instead.

Every emitted score is a :class:`~tessera.types.Propensity`, never a ΔG — the
oracle's ΔΔG lives downstream in S6 (§3.1, §14.5).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from itertools import product
from math import prod

from ..adapters.oracle import OracleBackend
from ..config import CandidateConfig
from ..schemas.candidates import CandidatesDoc, MutationSet, Substitution
from ..schemas.common import BurialClass, ContactStatus
from ..schemas.contacts import Contact, ContactsDoc
from ..schemas.stats import ContactStats, StatsSummary
from ..types import DeltaG, Propensity


@dataclass
class _PairCandidate:
    """One surviving pair proposal for a single contact (retrieval mode)."""

    contact_id: str
    cluster_id: str
    n_eff: float
    multibody: bool
    propensity: Propensity
    substitutions: list[Substitution]


@dataclass
class _Combination:
    """A composed (per-cluster) candidate before it becomes a MutationSet."""

    cluster_id: str
    contact_id: str
    n_eff: float
    multibody: bool
    propensity: Propensity
    substitutions: list[Substitution]


def _passes_constraints(
    substitutions: list[Substitution], burial: BurialClass | None, cfg: CandidateConfig
) -> bool:
    """Hard-constraint filter shared by both modes (§S5, §7.2): forbidden
    residues, fixed positions, and the surface-cysteine artifact."""
    for s in substitutions:
        if s.to_aa in cfg.forbidden_residues:
            return False
        if s.position in cfg.fixed_positions:
            return False
        if cfg.forbid_surface_cys and s.to_aa == "C" and burial is BurialClass.EXPOSED:
            return False
    return True


def _pair_candidates_for_contact(
    stats: ContactStats, contact: Contact, sequence: str | None, cfg: CandidateConfig
) -> list[_PairCandidate]:
    """Rank top pairs, keep top-k beating the current assignment by the margin,
    dedup against the current pair, and build per-side substitutions (§S5)."""
    i, j = contact.i, contact.j
    cur_a: str | None = None
    cur_b: str | None = None
    cur_prop: float | None = None
    if sequence is not None:
        cur_a = sequence[i - 1]
        cur_b = sequence[j - 1]
        for p in stats.top_pairs:
            if p.a == cur_a and p.b == cur_b:
                cur_prop = p.propensity.value
                break

    burial = stats.geometry.burial
    ranked = sorted(stats.top_pairs, key=lambda p: p.propensity.value, reverse=True)
    out: list[_PairCandidate] = []
    for p in ranked:
        if len(out) >= cfg.top_k:
            break
        if cur_a is not None and p.a == cur_a and p.b == cur_b:
            continue  # dedup: never propose what is already there
        if cur_prop is not None and p.propensity.value < cur_prop + cfg.propensity_margin:
            continue  # must beat the current assignment by the margin

        subs: list[Substitution] = []
        if cur_a is None or cur_b is None:
            subs.append(Substitution(position=i, from_aa="X", to_aa=p.a))
            subs.append(Substitution(position=j, from_aa="X", to_aa=p.b))
        else:
            if p.a != cur_a:
                subs.append(Substitution(position=i, from_aa=cur_a, to_aa=p.a))
            if p.b != cur_b:
                subs.append(Substitution(position=j, from_aa=cur_b, to_aa=p.b))

        if not _passes_constraints(subs, burial, cfg):
            continue
        out.append(
            _PairCandidate(
                contact_id=contact.id,
                cluster_id=contact.cluster,
                n_eff=stats.n_eff,
                multibody=False,
                propensity=p.propensity,
                substitutions=subs,
            )
        )
    return out


def _top_k_index_combos(groups: list[list[_PairCandidate]], k: int) -> list[tuple[int, ...]]:
    """Top-``k`` combinations (one pick per contact) by **summed** propensity,
    without materializing the full cartesian product.

    Each group is sorted descending, so the best combination is all-zeros; a
    best-first heap walk over single-index increments yields the next-best each
    time (the classic k-smallest-sum-over-sorted-lists algorithm). This bounds
    work to ``O(k · n)`` regardless of how dense the cluster is — a dense cluster
    would otherwise blow up as ``top_k ** n_contacts`` (§S5 explosion guard).
    """
    n = len(groups)
    if n == 0:
        return []

    def score(idx: tuple[int, ...]) -> float:
        return sum(groups[t][i].propensity.value for t, i in enumerate(idx))

    start = (0,) * n
    heap: list[tuple[float, tuple[int, ...]]] = [(-score(start), start)]
    seen: set[tuple[int, ...]] = {start}
    out: list[tuple[int, ...]] = []
    while heap and len(out) < k:
        _neg, idx = heapq.heappop(heap)
        out.append(idx)
        for t in range(n):
            if idx[t] + 1 < len(groups[t]):
                nxt = idx[:t] + (idx[t] + 1,) + idx[t + 1 :]
                if nxt not in seen:
                    seen.add(nxt)
                    heapq.heappush(heap, (-score(nxt), nxt))
    return out


def _compose_cluster(
    pairs: list[_PairCandidate], multibody: bool, beam_width: int
) -> tuple[list[_Combination], int]:
    """Beam-composed joint candidates for a cluster (§S5).

    Returns the top ``beam_width`` joint combinations (ranked by summed per-pair
    log-odds) plus the *total* number of possible combinations, so the caller can
    honestly report how many were dropped. Substitutions are deduped by position —
    contacts in a β-ladder share residues, so a naive union would emit conflicting
    residues at one position; the higher-propensity pair's assignment wins.
    """
    by_contact: dict[str, list[_PairCandidate]] = {}
    for pc in pairs:
        by_contact.setdefault(pc.contact_id, []).append(pc)
    groups = list(by_contact.values())
    for g in groups:
        g.sort(key=lambda pc: pc.propensity.value, reverse=True)

    total = prod(len(g) for g in groups) if groups else 0
    combos: list[_Combination] = []
    for idx in _top_k_index_combos(groups, beam_width):
        chosen = [groups[t][i] for t, i in enumerate(idx)]
        total_prop = Propensity(0.0)
        for pc in chosen:
            total_prop = total_prop + pc.propensity
        subs: list[Substitution] = []
        seen_pos: set[int] = set()
        for pc in sorted(chosen, key=lambda pc: pc.propensity.value, reverse=True):
            for s in pc.substitutions:
                if s.position not in seen_pos:
                    seen_pos.add(s.position)
                    subs.append(s)
        subs.sort(key=lambda s: s.position)
        combos.append(
            _Combination(
                cluster_id=chosen[0].cluster_id,
                contact_id=chosen[0].contact_id,
                n_eff=min(pc.n_eff for pc in chosen),
                multibody=multibody,
                propensity=total_prop,
                substitutions=subs,
            )
        )
    combos.sort(key=lambda c: c.propensity.value, reverse=True)
    return combos, total


def _to_mutation_sets(combos: list[_Combination]) -> list[MutationSet]:
    return [
        MutationSet(
            id=f"m{n:03d}",
            substitutions=c.substitutions,
            propensity=c.propensity,
            contact_id=c.contact_id,
            cluster_id=c.cluster_id,
            n_eff=c.n_eff,
            multibody=c.multibody,
        )
        for n, c in enumerate(combos, start=1)
    ]


def _enumerate_retrieval(
    stats: StatsSummary, contacts: ContactsDoc, sequence: str | None, cfg: CandidateConfig
) -> tuple[list[MutationSet], int, int]:
    """Retrieval harvest path — returns (mutation_sets, beam_width, dropped)."""
    by_id = {c.id: c for c in contacts.contacts}
    per_cluster: dict[str, list[_PairCandidate]] = {}
    for cs in stats.contacts:
        if cs.status is not ContactStatus.OK or cs.low_confidence:
            continue
        contact = by_id.get(cs.contact_id)
        if contact is None:
            continue
        pairs = _pair_candidates_for_contact(cs, contact, sequence, cfg)
        if pairs:
            per_cluster.setdefault(contact.cluster, []).extend(pairs)

    n_clusters = len(per_cluster)
    beam_width = max(1, cfg.candidate_cap // max(1, n_clusters))

    kept: list[_Combination] = []
    dropped = 0
    for cluster_id, pairs in per_cluster.items():
        multibody = cluster_id in contacts.clusters and contacts.clusters[cluster_id].multibody
        combos, total = _compose_cluster(pairs, multibody, beam_width)
        kept.extend(combos)
        dropped += max(0, total - len(combos))

    kept.sort(key=lambda c: c.propensity.value, reverse=True)
    if len(kept) > cfg.candidate_cap:
        dropped += len(kept) - cfg.candidate_cap
        kept = kept[: cfg.candidate_cap]
    return _to_mutation_sets(kept), beam_width, dropped


def _enumerate_oracle(
    stats: StatsSummary,
    contacts: ContactsDoc,
    sequence: str | None,
    cfg: CandidateConfig,
    oracle: OracleBackend,
    structure_id: str,
) -> tuple[list[MutationSet], int, int]:
    """Oracle-only fallback (§3.7): site-saturation + epistatic-double scan."""
    seq = sequence or ""
    burial_by_id = {cs.contact_id: cs.geometry.burial for cs in stats.contacts}
    combos: list[_Combination] = []
    for contact in contacts.contacts:
        if contact.spans_gap:
            continue
        i, j = contact.i, contact.j
        multibody = (
            contact.cluster in contacts.clusters
            and contacts.clusters[contact.cluster].multibody
        )
        burial = burial_by_id.get(contact.id)
        from_i = seq[i - 1] if 0 < i <= len(seq) else "X"
        from_j = seq[j - 1] if 0 < j <= len(seq) else "X"
        top_i = _top_stabilizing(oracle.score_saturation(structure_id, seq, i), from_i)
        top_j = _top_stabilizing(oracle.score_saturation(structure_id, seq, j), from_j)
        for to_i, to_j in product(top_i, top_j):
            ddg = oracle.score_double(structure_id, seq, i, to_i, j, to_j)
            if not ddg.is_stabilizing():
                continue
            subs = [
                Substitution(position=i, from_aa=from_i, to_aa=to_i),
                Substitution(position=j, from_aa=from_j, to_aa=to_j),
            ]
            if not _passes_constraints(subs, burial, cfg):
                continue
            combos.append(
                _Combination(
                    cluster_id=contact.cluster,
                    contact_id=contact.id,
                    n_eff=0.0,
                    multibody=multibody,
                    propensity=Propensity(0.0),
                    substitutions=subs,
                )
            )

    n_clusters = max(1, len(contacts.clusters))
    beam_width = max(1, cfg.candidate_cap // n_clusters)
    dropped = 0
    if len(combos) > cfg.candidate_cap:
        dropped = len(combos) - cfg.candidate_cap
        combos = combos[: cfg.candidate_cap]
    return _to_mutation_sets(combos), beam_width, dropped


def _top_stabilizing(saturation: dict[str, DeltaG], from_aa: str, k: int = 4) -> list[str]:
    """The k most stabilizing (most-negative ΔΔG) singles, excluding the current
    residue — the shortlist fed into the epistatic-double scan (§7.1)."""
    items = [(aa, d) for aa, d in saturation.items() if aa != from_aa]
    items.sort(key=lambda kv: kv[1].value)
    return [aa for aa, _ in items[:k]]


def enumerate_candidates(
    stats: StatsSummary,
    contacts: ContactsDoc,
    sequence: str | None,
    cfg: CandidateConfig,
    retrieve: bool,
    oracle: OracleBackend | None = None,
    structure_id: str = "",
) -> CandidatesDoc:
    """Enumerate reinforcement candidates for a design (spec §S5).

    ``retrieve=True`` harvests motif propensities and beams per cluster;
    ``retrieve=False`` is the oracle-only fallback and requires ``oracle``.
    """
    if retrieve:
        candidates, beam_width, dropped = _enumerate_retrieval(stats, contacts, sequence, cfg)
    else:
        if oracle is None:
            raise ValueError("oracle-only mode (retrieve=False) requires an oracle backend (§3.7)")
        candidates, beam_width, dropped = _enumerate_oracle(
            stats, contacts, sequence, cfg, oracle, structure_id
        )

    return CandidatesDoc(
        backbone=contacts.backbone,
        feature_mode=stats.feature_mode,
        retrieval_used=retrieve,
        candidate_cap=cfg.candidate_cap,
        beam_width=beam_width,
        dropped_combinations=dropped,
        candidates=candidates,
    )
