"""Canonical run-directory layout and artifact IO (spec §8 run layout, §3.6).

Every stage artifact has a stable on-disk location and a lossless read/write here,
so `--from/--to` reruns reload real state rather than recompute. JSON is the
canonical (lossless, pydantic-round-tripping) form; the `.parquet` files named in
the spec layout are produced as flat, inspectable tabular dumps that also carry a
lossless `_json` column so they reload without information loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .adapters.search import parse_hits_tsv
from .schemas.candidates import CandidatesDoc
from .schemas.contacts import ContactsDoc
from .schemas.hits import ClusterHits
from .schemas.queries import QueryManifest
from .schemas.scored import ScoredCandidate
from .schemas.stats import StatsSummary
from .schemas.weak_regions import WeakRegionsDoc


@dataclass(frozen=True)
class RunLayout:
    """Resolved paths within a run directory (spec §8)."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config.resolved.yaml"

    @property
    def contacts(self) -> Path:
        return self.root / "contacts.json"

    @property
    def manifest(self) -> Path:
        return self.root / "queries" / "manifest.json"

    @property
    def queries_dir(self) -> Path:
        return self.root / "queries"

    @property
    def hits_dir(self) -> Path:
        return self.root / "hits"

    @property
    def stats_dir(self) -> Path:
        return self.root / "stats"

    @property
    def stats_summary(self) -> Path:
        return self.root / "stats" / "stats_summary.json"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates.json"

    @property
    def scored(self) -> Path:
        return self.root / "scored_candidates.parquet"

    @property
    def report(self) -> Path:
        return self.root / "report.html"

    @property
    def weak_regions(self) -> Path:
        return self.root / "weak_regions.json"

    @property
    def triage_report(self) -> Path:
        return self.root / "triage_report.html"

    @property
    def constraints_dir(self) -> Path:
        return self.root / "constraints"

    @property
    def ligandmpnn_bias(self) -> Path:
        return self.constraints_dir / "ligandmpnn_bias.jsonl"

    @property
    def resfile(self) -> Path:
        return self.constraints_dir / "design.resfile"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> RunLayout:
        for d in (self.root, self.queries_dir, self.hits_dir, self.stats_dir,
                  self.constraints_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def _write_json(model, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2))


# -- contacts -----------------------------------------------------------------
def write_contacts(doc: ContactsDoc, path: Path) -> None:
    _write_json(doc, path)


def read_contacts(path: Path) -> ContactsDoc:
    return ContactsDoc.model_validate_json(Path(path).read_text())


# -- query manifest -----------------------------------------------------------
def write_manifest(doc: QueryManifest, path: Path) -> None:
    _write_json(doc, path)


def read_manifest(path: Path) -> QueryManifest:
    return QueryManifest.model_validate_json(Path(path).read_text())


# -- hits (Folddisco TSV, one file per cluster) -------------------------------
def write_hits_tsv(hits: ClusterHits, path: Path) -> None:
    """Serialize ClusterHits back to the pinned hits/<cluster>.tsv contract."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Folddisco hits (tessera-emitted). cols: target_id\tsource_db\trmsd\tscore\tmotif_mean_plddt\tmatched"]
    for h in hits.hits:
        matched = ";".join(
            f"{r.contact_id}:{r.role}:{r.target_index}:{r.residue}:"
            f"{'NA' if r.plddt is None else r.plddt}"
            for r in h.matched_residues
        )
        motif = "NA" if h.motif_mean_plddt is None else h.motif_mean_plddt
        lines.append(f"{h.target_id}\t{h.source_db}\t{h.rmsd}\t{h.score}\t{motif}\t{matched}")
    path.write_text("\n".join(lines) + "\n")


def read_hits_tsv(path: Path, cluster_id: str, prefilter_only: bool = False) -> ClusterHits:
    return parse_hits_tsv(path, cluster_id, prefilter_only=prefilter_only)


# -- stats --------------------------------------------------------------------
def write_stats(summary: StatsSummary, layout: RunLayout) -> None:
    """stats_summary.json (canonical) + a per-contact parquet table for inspection."""
    _write_json(summary, layout.stats_summary)
    for cs in summary.contacts:
        rows = [
            {
                "contact_id": cs.contact_id,
                "a": p.a,
                "b": p.b,
                "weighted_count": p.weighted_count,
                "frequency": p.frequency,
                "background": p.background,
                "propensity": float(p.propensity),
            }
            for p in cs.top_pairs
        ]
        df = pd.DataFrame(rows, columns=["contact_id", "a", "b", "weighted_count",
                                         "frequency", "background", "propensity"])
        df.to_parquet(layout.stats_dir / f"{cs.contact_id}.parquet", index=False)


def read_stats(layout: RunLayout) -> StatsSummary:
    return StatsSummary.model_validate_json(layout.stats_summary.read_text())


# -- candidates ---------------------------------------------------------------
def write_candidates(doc: CandidatesDoc, path: Path) -> None:
    _write_json(doc, path)


def read_candidates(path: Path) -> CandidatesDoc:
    return CandidatesDoc.model_validate_json(Path(path).read_text())


# -- scored candidates (parquet, lossless via _json column) -------------------
def write_scored(scored: list[ScoredCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in scored:
        rows.append(
            {
                "candidate_id": s.candidate_id,
                "contact_id": s.contact_id,
                "cluster_id": s.cluster_id,
                "propensity": float(s.propensity),
                "primary_ddg": s.primary_ddg.value,
                "primary_ddg_uncertainty": s.primary_ddg.uncertainty,
                "oracle_sign_agreement": s.oracle_sign_agreement,
                "confident_effect": s.confident_effect,
                "surface_cys": s.flags.surface_cys,
                "increases_hydrophobicity": s.flags.increases_hydrophobicity,
                "low_neff_provenance": s.flags.low_neff_provenance,
                "multibody": s.flags.multibody,
                "within_noise": s.flags.within_noise,
                "_json": s.model_dump_json(),
            }
        )
    cols = ["candidate_id", "contact_id", "cluster_id", "propensity", "primary_ddg",
            "primary_ddg_uncertainty", "oracle_sign_agreement", "confident_effect",
            "surface_cys", "increases_hydrophobicity", "low_neff_provenance", "multibody",
            "within_noise", "_json"]
    pd.DataFrame(rows, columns=cols).to_parquet(path, index=False)


def read_scored(path: Path) -> list[ScoredCandidate]:
    df = pd.read_parquet(path)
    return [ScoredCandidate.model_validate_json(j) for j in df["_json"].tolist()]


# -- weak regions (7A) --------------------------------------------------------
def write_weak_regions(doc: WeakRegionsDoc, path: Path) -> None:
    _write_json(doc, path)


def read_weak_regions(path: Path) -> WeakRegionsDoc:
    return WeakRegionsDoc.model_validate_json(Path(path).read_text())
