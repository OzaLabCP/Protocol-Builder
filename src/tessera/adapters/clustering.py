"""Redundancy-correction clustering interface (spec §6.3).

Proteome-scale counts are dominated by over-sampled families. Hits are clustered
by sequence/structure identity and each cluster is assigned total weight 1,
distributed across members — sequence-weighting analogous to MSA reweighting.

mmseqs2 / Foldseek-cluster are GPLv3: subprocess only, never imported (§9). The
mock groups by the family prefix the mock search encodes into target ids, so the
reweighting path is exercised offline.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ..schemas.hits import Hit

_FAMILY_RE = re.compile(r"^(MOCK\d{3})_")


class ClusteringBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    def assign_clusters(self, hits: list[Hit]) -> dict[str, str]:
        """Return target_id -> cluster_key. Each cluster later gets total weight 1."""


class MockClustering(ClusteringBackend):
    """Group by the `MOCKNNN_` family prefix emitted by :class:`MockFolddisco`.
    Targets without the prefix are treated as singleton clusters (weight 1 each)."""

    name = "mock"

    def assign_clusters(self, hits: list[Hit]) -> dict[str, str]:
        out: dict[str, str] = {}
        for h in hits:
            m = _FAMILY_RE.match(h.target_id)
            out[h.target_id] = m.group(1) if m else h.target_id
        return out


class Mmseqs2Clustering(ClusteringBackend):
    """Real mmseqs2 easy-cluster over the hit target sequences (§6.3). Subprocess
    only; not exercised offline."""

    name = "mmseqs2"

    def __init__(self, binary: str = "mmseqs", min_seq_id: float = 0.3) -> None:
        self.binary = binary
        self.min_seq_id = min_seq_id

    def assign_clusters(self, hits: list[Hit]) -> dict[str, str]:  # pragma: no cover - needs binary
        raise NotImplementedError(
            "mmseqs2 clustering needs target sequences + the mmseqs binary; wire on the "
            "Linux box (§14.2). Use clustering: mock offline."
        )


def compute_weights(hits: list[Hit], clusters: dict[str, str]) -> dict[str, float]:
    """Distribute a total weight of 1 across each cluster's members (§6.3).

    Returns target_id -> weight. Used by S4 so N_eff ≈ number of independent
    clusters, not raw hit count.
    """
    members: dict[str, list[str]] = {}
    for h in hits:
        members.setdefault(clusters.get(h.target_id, h.target_id), []).append(h.target_id)
    weights: dict[str, float] = {}
    for _key, ids in members.items():
        share = 1.0 / len(ids)
        for tid in ids:
            weights[tid] = share
    return weights


_BACKENDS: dict[str, type[ClusteringBackend]] = {
    "mock": MockClustering,
    "mmseqs2": Mmseqs2Clustering,
}


def get_clustering(name: str) -> ClusteringBackend:
    key = name.lower()
    if key not in _BACKENDS:
        raise ValueError(f"unknown clustering backend '{name}'; choices: {sorted(_BACKENDS)}")
    return _BACKENDS[key]()
