"""S2 · Motif-query construction (spec §S2).

Turn each S1 contact cluster into a **discontinuous** Folddisco query: a few
residues (±``padding``) flanking each partner of every non-gap contact, emitted
both as a motif-only PDB (coordinates preserved) and as a Folddisco residue-index
list. Gap-spanning contacts have undefined geometry (§S1) and are excluded here;
a cluster left with no usable contact produces no query at all. The chosen
``feature_mode`` is threaded into every ``ClusterQuery`` so downstream artifacts
never compare sequenced against backbone-only runs (§S2).
"""

from __future__ import annotations

from pathlib import Path

from tessera.config import QueryConfig
from tessera.io.pdb import Structure, write_motif_pdb
from tessera.schemas.common import FeatureMode
from tessera.schemas.contacts import ContactsDoc
from tessera.schemas.queries import ClusterQuery, QueryManifest, QueryPair


def _segments(indices: list[int]) -> list[tuple[int, int]]:
    """Contiguous inclusive runs over sorted, unique residue indices (§S1/§S2)."""
    segments: list[tuple[int, int]] = []
    if not indices:
        return segments
    start = prev = indices[0]
    for idx in indices[1:]:
        if idx == prev + 1:
            prev = idx
        else:
            segments.append((start, prev))
            start = prev = idx
    segments.append((start, prev))
    return segments


def build_queries(
    structure: Structure,
    contacts: ContactsDoc,
    cfg: QueryConfig,
    feature_mode: FeatureMode,
    out_dir: str | Path,
) -> QueryManifest:
    """Build one discontinuous query per non-empty contact cluster (§S2).

    For each cluster, only its non-gap contacts contribute; a cluster whose every
    contact spans a chain break is skipped entirely. Writes ``queries/<id>.pdb``
    (motif residues, coordinates preserved) and ``queries/<id>.txt`` (Folddisco
    residue list) under ``out_dir`` and returns the manifest tying cluster → files
    → the (i, j) pairs read out of the hits, tagged with ``feature_mode``.
    """
    queries_dir = Path(out_dir) / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)

    n = len(structure.residues)
    pad = cfg.padding
    contact_by_id = {c.id: c for c in contacts.contacts}

    cluster_queries: list[ClusterQuery] = []
    for cluster_id, cluster in contacts.clusters.items():
        cluster_contacts = [contact_by_id[cid] for cid in cluster.contacts]
        non_gap = [c for c in cluster_contacts if not c.spans_gap]
        if not non_gap:
            continue

        idx_set: set[int] = set()
        for c in non_gap:
            for center in (c.i, c.j):
                lo = max(1, center - pad)
                hi = min(n, center + pad)
                idx_set.update(range(lo, hi + 1))
        residue_indices = sorted(idx_set)

        pdb_path = queries_dir / f"{cluster_id}.pdb"
        txt_path = queries_dir / f"{cluster_id}.txt"
        write_motif_pdb(structure, residue_indices, pdb_path)
        txt_path.write_text("\n".join(str(i) for i in residue_indices) + "\n")

        cluster_queries.append(
            ClusterQuery(
                cluster_id=cluster_id,
                query_pdb=str(pdb_path),
                query_residues=str(txt_path),
                residue_indices=residue_indices,
                segments=_segments(residue_indices),
                pairs=[QueryPair(contact_id=c.id, i=c.i, j=c.j) for c in non_gap],
                feature_mode=feature_mode,
                dist_thresh=cfg.dist_thresh,
                angle_thresh=cfg.angle_thresh,
            )
        )

    return QueryManifest(
        backbone=contacts.backbone,
        feature_mode=feature_mode,
        padding=cfg.padding,
        queries=cluster_queries,
    )
