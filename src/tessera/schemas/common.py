"""Shared enums and the mandatory-provenance models (spec §14.5, §8, §6.3).

Provenance tags are **required** fields, not optional: ``feature_mode``,
database/model versions, the background-null spec, and per-contact ``status`` are
part of the frozen schemas so a run physically cannot drop them.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# The 20 canonical amino acids, fixed order used by every 20x20 table.
AA20: tuple[str, ...] = tuple("ACDEFGHIKLMNPQRSTVWY")


class StrictModel(BaseModel):
    """Base for all Tessera artifacts: extra fields rejected, assignment
    validated. Keeps schemas honest so `--from/--to` reruns are real (§14.4)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=False)


class FeatureMode(str, Enum):
    """Whether side-chain-dependent Folddisco features are available (§S2)."""

    SEQUENCED = "sequenced"          # preferred: post-inverse-folding, side chains present
    BACKBONE_ONLY = "backbone_only"  # degraded: bare backbone, χ-independent geometry only


class SupportStrategy(str, Enum):
    """Which contact-support policy S1 prioritizes (§S1). Selectable, not fixed."""

    REINFORCE_MARGINAL = "reinforce-marginal"    # default: shore up lightly-supported contacts
    PROTECT_LOADBEARING = "protect-loadbearing"  # target highly-connected, high-energy contacts


class ContactStatus(str, Enum):
    """Terminal per-contact state (§S4 failure handling). The run summary reports
    how many contacts reached each state so a partial run is never mistaken for a
    clean one."""

    OK = "ok"                              # candidates generated
    NO_HITS = "no_hits"                    # zero surviving hits — expected for rare geometry
    LOW_NEFF = "low_neff"                  # below N_eff_min, excluded from candidate generation
    GAP_EXCLUDED = "gap_excluded"          # contact spans a chain break (§S1) — undefined geometry
    MULTIBODY_DEFERRED = "multibody_deferred"  # 3-body+ cluster, verdict deferred to refold (§S5)
    ORACLE_FAILED = "oracle_failed"        # S6 subprocess crashed for this contact only


class OrientationClass(str, Enum):
    """Coarse Cβ-vector orientation class for background binning (§S4.4)."""

    PARALLEL = "parallel"
    ANTIPARALLEL = "antiparallel"
    ORTHOGONAL = "orthogonal"


class BurialClass(str, Enum):
    """Burial class from the S1 Cβ-neighbor-count proxy (§S4.4)."""

    BURIED = "buried"
    INTERMEDIATE = "intermediate"
    EXPOSED = "exposed"


class BackgroundKind(str, Enum):
    """Which null model the log-odds propensity is computed against (§S4.4)."""

    INDEPENDENT_MARGINAL = "independent_marginal"          # baseline only: f0(a)*f0(b)
    GEOMETRY_BURIAL_CONDITIONED = "geometry_burial_conditioned"  # default & recommended


class BackgroundNullSpec(StrictModel):
    """First-class, configurable background `f0(a,b)` — it determines the entire
    candidate ordering (§S4.4), so it is recorded for provenance, never implicit."""

    kind: BackgroundKind = BackgroundKind.GEOMETRY_BURIAL_CONDITIONED
    # geometry/burial binning (defaults from §S4.4; override in config)
    distance_bins_angstrom: list[float] = Field(
        default_factory=lambda: [4.0, 5.0, 6.0, 7.0, 8.0],
        description="Cβ–Cβ distance bin edges (1 Å bins over 4–8 Å).",
    )
    orientation_classes: list[OrientationClass] = Field(
        default_factory=lambda: list(OrientationClass)
    )
    burial_classes: list[BurialClass] = Field(default_factory=lambda: list(BurialClass))
    corpus_id: str = Field(
        default="pinned-highconf-contacts-v0",
        description="Versioned background corpus (PDB or pLDDT>90 AFDB), pinned like any DB.",
    )


class Provenance(StrictModel):
    """Pinned artifacts that most affect results (§8 database/model versioning).
    Two runs with the same config but different versions are not comparable."""

    tessera_version: str = "0.2.0"
    feature_mode: FeatureMode
    afdb50_release: str | None = None
    pdb_snapshot_date: str | None = None
    index_dir: str | None = None
    index_build_params: dict[str, str] = Field(default_factory=dict)
    oracle_model: str = "mock"
    oracle_weights_hash: str | None = None
    tool_versions: dict[str, str] = Field(
        default_factory=dict,
        description="e.g. {'folddisco': '...', 'mmseqs2': '...'} — mock backends record 'mock'.",
    )
    mock_mode: bool = Field(
        default=True,
        description="True when any external tool was faked (§14.2). Never compared against real runs.",
    )
