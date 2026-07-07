"""External-tool adapters (spec §14.2).

Every external tool sits behind a thin adapter with (a) a documented file-based
I/O contract and (b) a fake implementation, so the whole pipeline runs without the
real stack. The S6 oracle interface is the template; Folddisco and mmseqs2 follow
the same shape. GPL tools (Folddisco, mmseqs2) are subprocess-only, never imported
(§9).
"""

from __future__ import annotations

from .clustering import (
    ClusteringBackend,
    MockClustering,
    compute_weights,
    get_clustering,
)
from .oracle import (
    FoldXOracle,
    MockOracle,
    OracleBackend,
    RosettaOracle,
    ThermoMPNNDOracle,
    get_oracle,
)
from .search import (
    HITS_TSV_COLUMNS,
    FolddiscoBackend,
    MockFolddisco,
    RealFolddisco,
    get_folddisco,
    parse_hits_tsv,
)

__all__ = [
    "ClusteringBackend",
    "FoldXOracle",
    "FolddiscoBackend",
    "HITS_TSV_COLUMNS",
    "MockClustering",
    "MockFolddisco",
    "MockOracle",
    "OracleBackend",
    "RealFolddisco",
    "RosettaOracle",
    "ThermoMPNNDOracle",
    "compute_weights",
    "get_clustering",
    "get_folddisco",
    "get_oracle",
    "parse_hits_tsv",
]
