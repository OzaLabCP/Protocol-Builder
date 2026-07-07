"""Thin functional Python API (spec §8 "Python API").

Notebook-friendly wrappers over the stage functions that return pandas tables,
for interactive use and integration. The CLI and the orchestrator share the same
stage functions, so these stay behaviorally identical.
"""

from __future__ import annotations

import pandas as pd

from .adapters.clustering import get_clustering
from .adapters.oracle import OracleBackend, get_oracle
from .config import TesseraConfig
from .io.pdb import load_structure
from .schemas.contacts import ContactsDoc
from .schemas.stats import StatsSummary


def detect_contacts(backbone: str, cfg: TesseraConfig | None = None) -> pd.DataFrame:
    """Run S1 on a backbone; return the contact list as a table (§S1)."""
    from .stages.s1_contacts import detect_contacts as _detect  # noqa: PLC0415

    cfg = cfg or TesseraConfig(backbone=backbone)
    structure = load_structure(backbone)
    doc = _detect(structure, cfg.contact, cfg.resolve_feature_mode(), backbone_name=backbone)
    return pd.DataFrame([c.model_dump() for c in doc.contacts])


def harvest(
    backbone: str,
    sequence: str | None = None,
    cfg: TesseraConfig | None = None,
    out_dir: str = "runs/_api",
) -> StatsSummary:
    """Run S1→S4 (contacts → queries → mock/real search → statistics), returning the
    per-contact StatsSummary (§S1–§S4). Uses the configured backends (mock by default)."""
    from pathlib import Path  # noqa: PLC0415

    from .adapters.search import get_folddisco  # noqa: PLC0415
    from .artifacts import RunLayout  # noqa: PLC0415
    from .stages.s1_contacts import detect_contacts as _detect  # noqa: PLC0415
    from .stages.s2_query import build_queries  # noqa: PLC0415
    from .stages.s3_search import run_search  # noqa: PLC0415
    from .stages.s4_stats import compute_stats  # noqa: PLC0415

    cfg = cfg or TesseraConfig(backbone=backbone, seq=sequence)
    structure = load_structure(backbone)
    feature_mode = cfg.resolve_feature_mode()
    prov = cfg.to_provenance(mock_mode=True)
    layout = RunLayout(Path(out_dir)).ensure()
    contacts = _detect(structure, cfg.contact, feature_mode, backbone_name=backbone)
    manifest = build_queries(structure, contacts, cfg.query, feature_mode, layout.root)
    hits = run_search(manifest, get_folddisco(cfg.search.backend), cfg.search, sequence, layout)
    return compute_stats(contacts, hits, structure, cfg.stats,
                         get_clustering(cfg.stats.clustering_backend), feature_mode, prov)


def score(
    stats: StatsSummary,
    contacts: ContactsDoc,
    backbone: str,
    sequence: str,
    cfg: TesseraConfig | None = None,
    oracle: OracleBackend | None = None,
) -> pd.DataFrame:
    """Run S5→S6 (candidate enumeration → ΔΔG screening) and return the scored table."""
    from .stages.s5_candidates import enumerate_candidates  # noqa: PLC0415
    from .stages.s6_score import score_candidates  # noqa: PLC0415

    cfg = cfg or TesseraConfig(backbone=backbone, seq=sequence)
    oracle = oracle or get_oracle(cfg.oracle.primary)
    candidates = enumerate_candidates(stats, contacts, sequence, cfg.candidate, cfg.retrieve,
                                      oracle=oracle, structure_id=backbone)
    scored = score_candidates(candidates, backbone, sequence, oracle, [],
                              cfg.report.ddg_min_effect, cfg.stats.n_eff_min)
    rows = [
        {
            "candidate_id": s.candidate_id, "contact_id": s.contact_id,
            "propensity": float(s.propensity), "ddg": s.primary_ddg.value,
            "ddg_uncertainty": s.primary_ddg.uncertainty, "confident": s.confident_effect,
        }
        for s in scored
    ]
    return pd.DataFrame(rows)


__all__ = ["detect_contacts", "harvest", "score"]
