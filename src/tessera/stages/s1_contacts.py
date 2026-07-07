"""S1 · Contact detection (spec §S1).

Reads Cβ–Cβ geometry from a fixed backbone and enumerates long-range contacts,
clusters coupled pairs (β-ladder rungs / helix packings) into motif clusters, and
flags any pair spanning a chain break — such geometry is undefined and excluded
from query construction downstream (§S1 geometry provenance).
"""

from __future__ import annotations

import numpy as np

from ..config import ContactConfig
from ..io.pdb import Structure
from ..schemas.common import FeatureMode
from ..schemas.contacts import (
    ChainGap,
    Contact,
    ContactCluster,
    ContactParams,
    ContactsDoc,
)


def _find(parent: list[int], x: int) -> int:
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _union(parent: list[int], a: int, b: int) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[rb] = ra


def _segments(residues: list[int]) -> list[tuple[int, int]]:
    """Contiguous inclusive residue runs; a new run starts on a gap > 1 (§S1)."""
    segs: list[tuple[int, int]] = []
    start = prev = residues[0]
    for r in residues[1:]:
        if r - prev > 1:
            segs.append((start, prev))
            start = r
        prev = r
    segs.append((start, prev))
    return segs


def detect_contacts(
    structure: Structure,
    cfg: ContactConfig,
    feature_mode: FeatureMode,
    backbone_name: str | None = None,
) -> ContactsDoc:
    """Enumerate long-range Cβ–Cβ contacts and cluster them into motifs (§S1).

    A pair ``(i, j)`` (1-based, ``i < j``) is a contact iff ``d_cb ≤ d_contact``
    and ``j - i ≥ sep_min``. Contacts are clustered by shared residue or adjacent
    β-ladder rung; pairs bridging a chain break are flagged ``spans_gap``.
    """
    cb = structure.cb_coords()
    n = cb.shape[0]
    dmat = np.linalg.norm(cb[:, None, :] - cb[None, :, :], axis=-1)

    # Buriedness proxy: Cβ neighbours within radius, excluding self (§S1).
    buriedness = (dmat <= cfg.buriedness_radius).sum(axis=1) - 1

    gaps = structure.gaps()

    pairs: list[tuple[int, int]] = [
        (i + 1, j + 1)
        for i in range(n)
        for j in range(i + 1, n)
        if j - i >= cfg.sep_min and dmat[i, j] <= cfg.d_contact
    ]
    pairs.sort()
    m = len(pairs)

    # Contact support: other contacts sharing residue i or j (§S1).
    support = [
        sum(
            1
            for k, (a, b) in enumerate(pairs)
            if k != idx and (i in (a, b) or j in (a, b))
        )
        for idx, (i, j) in enumerate(pairs)
    ]

    # Union-find: shared residue OR adjacent β-ladder rung (|Δi|,|Δj| ≤ 2) (§S1).
    parent = list(range(m))
    for a in range(m):
        i1, j1 = pairs[a]
        for b in range(a + 1, m):
            i2, j2 = pairs[b]
            shares = bool({i1, j1} & {i2, j2})
            ladder = abs(i1 - i2) <= 2 and abs(j1 - j2) <= 2
            if shares or ladder:
                _union(parent, a, b)

    # Cluster ids "k1","k2",... in order of first appearance.
    cluster_of: list[str] = [""] * m
    root_to_id: dict[int, str] = {}
    for idx in range(m):
        root = _find(parent, idx)
        if root not in root_to_id:
            root_to_id[root] = f"k{len(root_to_id) + 1}"
        cluster_of[idx] = root_to_id[root]

    contacts: list[Contact] = [
        Contact(
            id=f"c{idx + 1:03d}",
            i=i,
            j=j,
            d_cb=round(float(dmat[i - 1, j - 1]), 3),
            buriedness_i=int(buriedness[i - 1]),
            buriedness_j=int(buriedness[j - 1]),
            support=support[idx],
            cluster=cluster_of[idx],
            spans_gap=any(i <= a and b <= j for a, b in gaps),
        )
        for idx, (i, j) in enumerate(pairs)
    ]

    clusters: dict[str, ContactCluster] = {}
    for cid in root_to_id.values():
        members = [k for k in range(m) if cluster_of[k] == cid]
        residues = sorted({r for k in members for r in pairs[k]})
        segments = _segments(residues)
        clusters[cid] = ContactCluster(
            id=cid,
            contacts=[contacts[k].id for k in members],
            segments=segments,
            multibody=len(segments) >= 3,
        )

    return ContactsDoc(
        backbone=backbone_name or structure.source,
        feature_mode=feature_mode,
        params=ContactParams(
            d_contact=cfg.d_contact,
            sep_min=cfg.sep_min,
            support_strategy=cfg.support_strategy,
            buriedness_radius=cfg.buriedness_radius,
            relaxed=cfg.relax,
        ),
        contacts=contacts,
        clusters=clusters,
        gaps=[ChainGap(after_residue=a, before_residue=b) for a, b in gaps],
    )
