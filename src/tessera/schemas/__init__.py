"""Frozen, typed stage schemas (spec §14.4).

Every Tessera artifact is a validated pydantic model. Freezing these contracts
*before* stage logic is what makes S1–S7 independently testable and `--from/--to`
reruns real rather than aspirational.
"""

from __future__ import annotations

from .candidates import CandidatesDoc, MutationSet, Substitution
from .common import (
    AA20,
    BackgroundKind,
    BackgroundNullSpec,
    BurialClass,
    ContactStatus,
    FeatureMode,
    OrientationClass,
    Provenance,
    StrictModel,
    SupportStrategy,
)
from .contacts import (
    ChainGap,
    Contact,
    ContactCluster,
    ContactParams,
    ContactsDoc,
)
from .hits import ClusterHits, Hit, MatchedResidue
from .queries import ClusterQuery, QueryManifest, QueryPair
from .scored import (
    CandidateFlags,
    OracleMode,
    OracleScore,
    ScoredCandidate,
)
from .stats import (
    ContactGeometry,
    ContactStats,
    PairPropensity,
    StatsSummary,
)
from .weak_regions import (
    FlexibilitySignal,
    FrustrationClass,
    FrustrationSignal,
    FunctionalExclusion,
    StabilizingDensitySignal,
    WeakRegion,
    WeakRegionsDoc,
)

__all__ = [
    "AA20",
    "BackgroundKind",
    "BackgroundNullSpec",
    "BurialClass",
    "CandidateFlags",
    "CandidatesDoc",
    "ChainGap",
    "ClusterHits",
    "ClusterQuery",
    "Contact",
    "ContactCluster",
    "ContactGeometry",
    "ContactParams",
    "ContactStats",
    "ContactStatus",
    "ContactsDoc",
    "FeatureMode",
    "FlexibilitySignal",
    "FrustrationClass",
    "FrustrationSignal",
    "FunctionalExclusion",
    "Hit",
    "MatchedResidue",
    "MutationSet",
    "OracleMode",
    "OracleScore",
    "OrientationClass",
    "PairPropensity",
    "Provenance",
    "QueryManifest",
    "QueryPair",
    "ScoredCandidate",
    "StabilizingDensitySignal",
    "StatsSummary",
    "StrictModel",
    "Substitution",
    "SupportStrategy",
    "WeakRegion",
    "WeakRegionsDoc",
]
