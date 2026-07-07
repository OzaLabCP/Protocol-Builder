"""`tessera.yaml` configuration (spec §8).

A single config captures every threshold; CLI flags override; the resolved config
is written into each run directory for provenance (§3.6, §8). Defaults are the
values the spec pins (§14.7) so stages that consume them never invent one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from .schemas.common import (
    BackgroundNullSpec,
    FeatureMode,
    Provenance,
    StrictModel,
    SupportStrategy,
)


class ContactConfig(StrictModel):
    d_contact: float = 8.0
    sep_min: int = 12
    support_strategy: SupportStrategy = SupportStrategy.REINFORCE_MARGINAL
    buriedness_radius: float = 10.0
    relax: bool = False  # optional light idealization before extraction (§S1)


class QueryConfig(StrictModel):
    padding: int = 2                 # residues flanking each partner (±2)
    dist_thresh: float = 1.0         # Folddisco -d default
    angle_thresh: float = 15.0       # Folddisco -a default
    feature_mode: FeatureMode | None = None  # None => auto (sequenced iff a sequence is present)


class SearchConfig(StrictModel):
    backend: str = "mock"            # mock | folddisco
    index_dir: str = "/data/afdb50_folddisco"
    source_db: str = "afdb50"        # afdb50 | swissprot | pdb (drives §S4.2 pLDDT exemption)
    top_n_prefilter: int = 2000
    skip_match: bool = False         # --skip-match wide prefilter-only scan (§S3)


class StatsConfig(StrictModel):
    plddt_min: float = 70.0
    n_eff_min: float = 10.0
    pseudocount_alpha: float = 0.5
    clustering_backend: str = "mock"  # mock | mmseqs2
    background: BackgroundNullSpec = Field(default_factory=BackgroundNullSpec)
    max_hits_to_cluster: int = 50000  # documented cap on hits fed to clustering (§10)


class CandidateConfig(StrictModel):
    top_k: int = 5
    candidate_cap: int = 200
    propensity_margin: float = 0.0   # must beat current assignment by this margin
    forbid_surface_cys: bool = True  # §7.2 surface-cysteine artifact
    forbidden_residues: list[str] = Field(default_factory=list)
    fixed_positions: list[int] = Field(default_factory=list)


class OracleConfig(StrictModel):
    primary: str = "mock"            # mock | thermompnn-d | foldx | rosetta
    cross_check: list[str] = Field(default_factory=list)  # optional §7.3 second opinions
    foldx_license_path: str | None = None
    rosetta_license_path: str | None = None


class ReportConfig(StrictModel):
    ddg_min_effect: float = -0.5     # §7.2 minimum effect size (kcal/mol)
    emit_ligandmpnn_bias: bool = True
    emit_resfile: bool = True


class TesseraConfig(StrictModel):
    """Top-level resolved configuration."""

    # inputs / outputs
    backbone: str | None = None
    seq: str | None = None
    out: str = "runs/run"

    # stage sections
    contact: ContactConfig = Field(default_factory=ContactConfig)
    query: QueryConfig = Field(default_factory=QueryConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    stats: StatsConfig = Field(default_factory=StatsConfig)
    candidate: CandidateConfig = Field(default_factory=CandidateConfig)
    oracle: OracleConfig = Field(default_factory=OracleConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)

    # global
    retrieve: bool = True            # False => --no-retrieve oracle-only mode (§3.7)
    preset: str | None = None        # e.g. 'strict' (§S1)
    triage: bool = False             # run the 7A weak-region front-end instead of reinforcement

    # provenance overrides (pinned artifacts, §8)
    afdb50_release: str | None = None
    pdb_snapshot_date: str | None = None

    # --- construction / merge -------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None) -> TesseraConfig:
        if path is None:
            return cls()
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)

    def apply_preset(self) -> TesseraConfig:
        if self.preset == "strict":
            self.contact.sep_min = 24
        elif self.preset not in (None, "default"):
            raise ValueError(f"unknown preset '{self.preset}'")
        return self

    def resolve_feature_mode(self) -> FeatureMode:
        if self.query.feature_mode is not None:
            return self.query.feature_mode
        return FeatureMode.SEQUENCED if self.seq else FeatureMode.BACKBONE_ONLY

    def to_provenance(self, mock_mode: bool) -> Provenance:
        tool_versions = {"folddisco": self.search.backend, "clustering": self.stats.clustering_backend}
        return Provenance(
            feature_mode=self.resolve_feature_mode(),
            afdb50_release=self.afdb50_release,
            pdb_snapshot_date=self.pdb_snapshot_date,
            index_dir=self.search.index_dir,
            oracle_model=self.oracle.primary,
            tool_versions=tool_versions,
            mock_mode=mock_mode,
        )

    def dump_yaml(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, default_flow_style=False)
        )


def merge_overrides(cfg: TesseraConfig, overrides: dict[str, Any]) -> TesseraConfig:
    """Apply flat CLI overrides (dotted keys like 'contact.d_contact') onto a config."""
    data = cfg.model_dump()
    for key, value in overrides.items():
        if value is None:
            continue
        parts = key.split(".")
        node = data
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return TesseraConfig.model_validate(data)
