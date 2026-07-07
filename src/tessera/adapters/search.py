"""S3 Folddisco search interface + backends (spec §S3).

Folddisco is GPLv3 (Foldseek lineage). Per §9 it is invoked **only** as a
subprocess with file-based I/O — never imported. The real backend shells out to
`folddisco query`; the mock synthesizes format-faithful hits so S3→S7 runs
offline (§14.2).

The on-disk contract is `hits/<cluster>.tsv`; :func:`parse_hits_tsv` is the single
parser and is what the captured-fixture test pins (§14.3).
"""

from __future__ import annotations

import hashlib
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from ..schemas.common import AA20
from ..schemas.hits import ClusterHits, Hit, MatchedResidue
from ..schemas.queries import ClusterQuery

# Folddisco `hits.tsv` column order pinned for the parser (see tests/fixtures/README).
# A captured real file MUST replace the synthetic fixture before trusting this in prod.
HITS_TSV_COLUMNS = [
    "target_id",
    "source_db",
    "rmsd",
    "score",
    "motif_mean_plddt",
    "matched",  # ';'-joined  contact:role:target_index:residue:plddt  tuples
]


def _stable_unit(*parts: object) -> float:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16) / float(1 << 48)


def parse_hits_tsv(path: str | Path, cluster_id: str, prefilter_only: bool = False) -> ClusterHits:
    """Parse a Folddisco `hits/<cluster>.tsv` into :class:`ClusterHits`.

    Written to match the pinned :data:`HITS_TSV_COLUMNS` contract. A leading
    ``#``-comment header line is tolerated and skipped.
    """
    path = Path(path)
    hits: list[Hit] = []
    for raw in path.read_text().splitlines():
        line = raw.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < len(HITS_TSV_COLUMNS):
            continue
        target_id, source_db, rmsd, score, motif_plddt, matched = cols[:6]
        residues: list[MatchedResidue] = []
        for tok in matched.split(";"):
            if not tok:
                continue
            f = tok.split(":")
            contact_id, role, tgt_idx, residue = f[0], f[1], int(f[2]), f[3]
            plddt = float(f[4]) if len(f) > 4 and f[4] not in ("", "NA") else None
            residues.append(
                MatchedResidue(
                    contact_id=contact_id,
                    role=role,
                    target_index=tgt_idx,
                    residue=residue,
                    plddt=plddt,
                )
            )
        hits.append(
            Hit(
                cluster_id=cluster_id,
                target_id=target_id,
                source_db=source_db,
                rmsd=float(rmsd),
                score=float(score),
                motif_mean_plddt=(float(motif_plddt) if motif_plddt not in ("", "NA") else None),
                matched_residues=residues,
            )
        )
    return ClusterHits(cluster_id=cluster_id, prefilter_only=prefilter_only, hits=hits)


class FolddiscoBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    def search(
        self,
        query: ClusterQuery,
        *,
        index_dir: str,
        sequence: str | None,
        top_n: int,
        skip_match: bool,
        source_db: str,
    ) -> ClusterHits:
        ...


class MockFolddisco(FolddiscoBackend):
    """Deterministic synthetic hits (§14.2). For each query (i,j) pair it draws
    residue pairs from a stable multinomial that concentrates on a few favoured
    pairs (so S4 finds real signal), spread across mock families (so redundancy
    reweighting has something to collapse) with AFDB-style pLDDT values (some below
    the §S4.2 cutoff, to exercise the filter)."""

    name = "mock"

    def __init__(self, n_hits: int = 48, n_families: int = 6) -> None:
        self.n_hits = n_hits
        self.n_families = n_families

    def _favoured_pairs(self, query: ClusterQuery, i: int, j: int) -> list[tuple[str, str, float]]:
        """A stable, per-pair distribution over (a, b) with weights summing to ~1."""
        seed = (query.cluster_id, i, j)
        # Pick 3 favoured amino-acid pairs deterministically.
        favoured: list[tuple[str, str, float]] = []
        weights = [0.5, 0.3, 0.2]
        for k in range(3):
            a = AA20[int(_stable_unit(*seed, "a", k) * 20)]
            b = AA20[int(_stable_unit(*seed, "b", k) * 20)]
            favoured.append((a, b, weights[k]))
        return favoured

    def _sample_pair(self, dist: list[tuple[str, str, float]], u: float) -> tuple[str, str]:
        acc = 0.0
        for a, b, w in dist:
            acc += w
            if u <= acc:
                return a, b
        return dist[-1][0], dist[-1][1]

    def search(self, query, *, index_dir, sequence, top_n, skip_match, source_db):
        hits: list[Hit] = []
        n = min(self.n_hits, top_n)
        dists = {(p.i, p.j): self._favoured_pairs(query, p.i, p.j) for p in query.pairs}
        for h in range(n):
            fam = h % self.n_families
            target_id = f"MOCK{fam:03d}_{h:04d}"
            residues: list[MatchedResidue] = []
            plddts: list[float] = []
            for p in query.pairs:
                dist = dists[(p.i, p.j)]
                ua = _stable_unit(target_id, p.contact_id, "i")
                ub = _stable_unit(target_id, p.contact_id, "j")
                a, b = self._sample_pair(dist, ua)
                # a small per-target perturbation so families differ slightly
                if ub > 0.85:
                    b = AA20[int(_stable_unit(target_id, p.j, "perturb") * 20)]
                plddt_i = 50.0 + 45.0 * _stable_unit(target_id, p.i, "plddt")
                plddt_j = 50.0 + 45.0 * _stable_unit(target_id, p.j, "plddt")
                plddts += [plddt_i, plddt_j]
                residues.append(
                    MatchedResidue(contact_id=p.contact_id, role="i", target_index=p.i,
                                   residue=a, plddt=plddt_i)
                )
                residues.append(
                    MatchedResidue(contact_id=p.contact_id, role="j", target_index=p.j,
                                   residue=b, plddt=plddt_j)
                )
            hits.append(
                Hit(
                    cluster_id=query.cluster_id,
                    target_id=target_id,
                    source_db=source_db,
                    rmsd=round(0.3 + 1.4 * _stable_unit(target_id, "rmsd"), 3),
                    score=round(_stable_unit(target_id, "score"), 4),
                    motif_mean_plddt=round(sum(plddts) / len(plddts), 1) if plddts else None,
                    matched_residues=residues,
                )
            )
        return ClusterHits(cluster_id=query.cluster_id, prefilter_only=skip_match, hits=hits)


class RealFolddisco(FolddiscoBackend):
    """Shells out to `folddisco query` and parses the TSV (§S3). Requires the
    binary + a built index; not exercised offline (§14.2)."""

    name = "folddisco"

    def __init__(self, binary: str = "folddisco", threads: int = 8) -> None:
        self.binary = binary
        self.threads = threads

    def search(self, query, *, index_dir, sequence, top_n, skip_match, source_db):
        out_tsv = Path(query.query_pdb).with_suffix(".hits.tsv")
        cmd = [
            self.binary, "query",
            "-i", index_dir,
            "-p", query.query_pdb,
            "-q", query.query_residues,
            "-d", str(query.dist_thresh),
            "-a", str(query.angle_thresh),
            "--top", str(top_n),
            "-t", str(self.threads),
        ]
        if skip_match:
            cmd.append("--skip-match")
        # subprocess only — never import the GPL tool (§9).
        subprocess.run(cmd, check=True, stdout=out_tsv.open("w"))
        return parse_hits_tsv(out_tsv, query.cluster_id, prefilter_only=skip_match)


_BACKENDS: dict[str, type[FolddiscoBackend]] = {"mock": MockFolddisco, "folddisco": RealFolddisco}


def get_folddisco(name: str) -> FolddiscoBackend:
    key = name.lower()
    if key not in _BACKENDS:
        raise ValueError(f"unknown folddisco backend '{name}'; choices: {sorted(_BACKENDS)}")
    return _BACKENDS[key]()
