"""Pipeline orchestrator — `tessera run` (spec §4, §8).

Chains S1–S7 with `--from/--to` partial reruns, the `--no-retrieve` oracle-only
design mode (§3.7), and the 7A triage front-end. Skipped upstream stages reload
their artifacts from disk so a partial rerun is real, not aspirational (§3.6).
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from . import artifacts
from .adapters.clustering import get_clustering
from .adapters.oracle import get_oracle
from .adapters.search import get_folddisco
from .artifacts import RunLayout
from .config import TesseraConfig
from .io.pdb import Structure, load_structure
from .schemas.common import ContactStatus, FeatureMode, Provenance
from .schemas.stats import StatsSummary

log = logging.getLogger("tessera")

STAGE_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
_LICENSED_ORACLES = {"foldx": "foldx_license_path", "rosetta": "rosetta_license_path"}


@dataclass
class RunResult:
    layout: RunLayout
    provenance: Provenance
    stage_from: str
    stage_to: str
    status_counts: dict[str, int] = field(default_factory=dict)
    n_candidates: int = 0
    n_recommended: int = 0
    triage_regions: int | None = None
    paths: dict[str, str] = field(default_factory=dict)


def _load_sequence(cfg: TesseraConfig, structure: Structure) -> str | None:
    if cfg.seq is None:
        return None
    p = Path(cfg.seq)
    if not p.exists():
        # treat the value as an inline sequence
        return cfg.seq.strip() or None
    lines = [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith(">")]
    return "".join(lines) or None


def check_licenses(cfg: TesseraConfig) -> list[str]:
    """Warn (spec §9) if a license-requiring oracle is selected without a license path."""
    warnings_out: list[str] = []
    selected = [cfg.oracle.primary, *cfg.oracle.cross_check]
    for name in selected:
        key = _LICENSED_ORACLES.get(name)
        if key and getattr(cfg.oracle, key) is None:
            msg = f"oracle '{name}' requires a commercial license; no {key} configured (spec §9)."
            warnings_out.append(msg)
            warnings.warn(msg, stacklevel=2)
    return warnings_out


def is_mock_mode(cfg: TesseraConfig) -> bool:
    """True when ANY external tool is faked (§14.2) — including a cross-check oracle."""
    return (
        cfg.search.backend == "mock"
        or cfg.oracle.primary == "mock"
        or cfg.stats.clustering_backend == "mock"
        or any(x == "mock" for x in cfg.oracle.cross_check)
    )


def _empty_stats(cfg: TesseraConfig, feature_mode: FeatureMode, prov: Provenance) -> StatsSummary:
    return StatsSummary(
        backbone=cfg.backbone or "",
        feature_mode=feature_mode,
        background=cfg.stats.background,
        provenance=prov,
        n_eff_min=cfg.stats.n_eff_min,
        contacts=[],
    )


def run_triage(cfg: TesseraConfig) -> RunResult:
    """7A stability-triage front-end (spec §7A)."""
    from .report.html import write_triage_report  # noqa: PLC0415
    from .triage.core import run_triage as _triage  # noqa: PLC0415

    if cfg.backbone is None:
        raise ValueError("triage requires --backbone")
    layout = RunLayout(Path(cfg.out)).ensure()
    structure = load_structure(cfg.backbone)
    sequence = _load_sequence(cfg, structure) or structure.sequence()
    prov = cfg.to_provenance(is_mock_mode(cfg))
    oracle = get_oracle(cfg.oracle.primary)
    doc = _triage(
        structure,
        sequence,
        oracle,
        prov,
        ddg_threshold=cfg.report.ddg_min_effect,
        structure_id=cfg.backbone,
    )
    artifacts.write_weak_regions(doc, layout.weak_regions)
    write_triage_report(doc, layout.triage_report)
    cfg.dump_yaml(layout.config)
    return RunResult(
        layout=layout,
        provenance=prov,
        stage_from="triage",
        stage_to="triage",
        triage_regions=len(doc.regions),
        paths={"weak_regions": str(layout.weak_regions), "triage_report": str(layout.triage_report)},
    )


def run(cfg: TesseraConfig, stage_from: str = "S1", stage_to: str = "S7") -> RunResult:
    """Run the reinforcement pipeline (or triage if cfg.triage)."""
    cfg = cfg.apply_preset()
    check_licenses(cfg)  # §9 — before the triage short-circuit, so triage warns too
    if cfg.triage:
        return run_triage(cfg)

    # lazy imports so a partial checkout still imports the orchestrator
    from .report.constraints import emit_ligandmpnn_bias, emit_resfile  # noqa: PLC0415
    from .report.html import write_html_report  # noqa: PLC0415
    from .stages.s1_contacts import detect_contacts  # noqa: PLC0415
    from .stages.s2_query import build_queries  # noqa: PLC0415
    from .stages.s3_search import run_search  # noqa: PLC0415
    from .stages.s4_stats import compute_stats  # noqa: PLC0415
    from .stages.s5_candidates import enumerate_candidates  # noqa: PLC0415
    from .stages.s6_score import score_candidates  # noqa: PLC0415
    from .stages.s7_report import accepted_substitutions, rank_candidates  # noqa: PLC0415

    if cfg.backbone is None:
        raise ValueError("run requires --backbone")

    mock = is_mock_mode(cfg)
    layout = RunLayout(Path(cfg.out)).ensure()
    structure = load_structure(cfg.backbone)
    feature_mode = cfg.resolve_feature_mode()
    sequence = _load_sequence(cfg, structure)
    prov = cfg.to_provenance(mock)
    cfg.dump_yaml(layout.config)

    fi, ti = STAGE_ORDER.index(stage_from), STAGE_ORDER.index(stage_to)

    def action(stage: str) -> str:
        """'run' if in [from,to]; 'load' if before from (needed as input); 'skip' if after to."""
        idx = STAGE_ORDER.index(stage)
        if idx < fi:
            return "load"
        if idx <= ti:
            return "run"
        return "skip"

    retrieve = cfg.retrieve
    result = RunResult(layout=layout, provenance=prov, stage_from=stage_from, stage_to=stage_to)
    oracle = get_oracle(cfg.oracle.primary)

    contacts = None
    stats: StatsSummary | None = None
    candidates = None
    scored = None

    # -- S1 contacts ----------------------------------------------------------
    if action("S1") == "run":
        contacts = detect_contacts(structure, cfg.contact, feature_mode, backbone_name=cfg.backbone)
        artifacts.write_contacts(contacts, layout.contacts)
    elif action("S1") == "load":
        contacts = artifacts.read_contacts(layout.contacts)
    if contacts is None:  # S1 always runs or loads; this narrows Optional for the rest
        contacts = artifacts.read_contacts(layout.contacts)

    if retrieve and action("S2") != "skip":
        # -- S2 queries -------------------------------------------------------
        if action("S2") == "run":
            manifest = build_queries(structure, contacts, cfg.query, feature_mode, layout.root)
            artifacts.write_manifest(manifest, layout.manifest)
        else:
            manifest = artifacts.read_manifest(layout.manifest)

        # -- S3 search --------------------------------------------------------
        if action("S3") == "run":
            hits_by_cluster = run_search(manifest, get_folddisco(cfg.search.backend), cfg.search,
                                         sequence, layout)
        elif action("S3") == "load":
            hits_by_cluster = {
                q.cluster_id: artifacts.read_hits_tsv(layout.hits_dir / f"{q.cluster_id}.tsv", q.cluster_id)
                for q in manifest.queries
                if (layout.hits_dir / f"{q.cluster_id}.tsv").exists()
            }
        else:
            hits_by_cluster = {}

        # -- S4 stats ---------------------------------------------------------
        if action("S4") == "run":
            clustering = get_clustering(cfg.stats.clustering_backend)
            stats = compute_stats(contacts, hits_by_cluster, structure, cfg.stats, clustering,
                                  feature_mode, prov)
            artifacts.write_stats(stats, layout)
        elif action("S4") == "load" and layout.stats_summary.exists():
            stats = artifacts.read_stats(layout)
    elif not retrieve:
        log.info("oracle-only mode (--no-retrieve): skipping S2-S4 (spec §3.7)")

    # -- S5 candidates --------------------------------------------------------
    if action("S5") == "run":
        if stats is None:
            stats = artifacts.read_stats(layout) if layout.stats_summary.exists() \
                else _empty_stats(cfg, feature_mode, prov)
        if contacts is None:
            contacts = artifacts.read_contacts(layout.contacts)
        candidates = enumerate_candidates(
            stats, contacts, sequence, cfg.candidate, retrieve,
            oracle=oracle, structure_id=cfg.backbone,
        )
        artifacts.write_candidates(candidates, layout.candidates)
    elif action("S5") == "load":
        candidates = artifacts.read_candidates(layout.candidates)
    if candidates is not None:
        result.n_candidates = len(candidates.candidates)

    # -- S6 score -------------------------------------------------------------
    if action("S6") == "run":
        if candidates is None:
            candidates = artifacts.read_candidates(layout.candidates)
        cross = [get_oracle(n) for n in cfg.oracle.cross_check]
        scored = score_candidates(
            candidates, cfg.backbone, sequence or structure.sequence(),
            oracle, cross, cfg.report.ddg_min_effect, cfg.stats.n_eff_min,
        )
        artifacts.write_scored(scored, layout.scored)
    elif action("S6") == "load":
        scored = artifacts.read_scored(layout.scored)

    # -- S7 report + constraints ---------------------------------------------
    if action("S7") == "run":
        if scored is None:
            scored = artifacts.read_scored(layout.scored)
        if candidates is None:
            candidates = artifacts.read_candidates(layout.candidates)
        if stats is None and layout.stats_summary.exists():
            stats = artifacts.read_stats(layout)
        ranked = rank_candidates(scored, cfg.report)
        write_html_report(ranked, stats, candidates, layout.report)
        accepted = accepted_substitutions(ranked, candidates)
        if cfg.report.emit_ligandmpnn_bias:
            emit_ligandmpnn_bias(accepted, layout.ligandmpnn_bias)
        if cfg.report.emit_resfile:
            emit_resfile(accepted, layout.resfile)
        result.n_recommended = len(ranked.recommended)
        result.paths = {
            "report": str(layout.report),
            "ligandmpnn_bias": str(layout.ligandmpnn_bias),
            "resfile": str(layout.resfile),
        }

    if stats is not None:
        result.status_counts = {k.value if isinstance(k, ContactStatus) else str(k): v
                                for k, v in stats.status_counts.items()}
    return result
