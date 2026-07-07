"""S6 ΔΔG oracle interface + backends (spec §7.1, single-point-of-failure §7.1).

The oracle sits behind a narrow interface (`score_single`, `score_double`,
`score_saturation`) so *any* backend satisfying it is swappable — ThermoMPNN-D
primary, FoldX/Rosetta as promoted fallbacks or cross-checks. Phase 0 gates on
verifying the real ThermoMPNN-D double mode before it is trusted (§13); until
then :class:`MockOracle` is the default and every artifact is stamped mock.

License note (§9): FoldX and Rosetta require commercial licenses and are invoked
strictly as subprocesses with file-based I/O — never imported.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from ..schemas.common import AA20
from ..types import DeltaG

# Kyte–Doolittle hydropathy, used only to give the mock oracle a biophysically
# plausible (and fully deterministic) shape. Not a real energy function.
_HYDROPATHY = {
    "A": 1.8, "R": -4.5, "N": -3.5, "D": -3.5, "C": 2.5, "Q": -3.5, "E": -3.5,
    "G": -0.4, "H": -3.2, "I": 4.5, "L": 3.8, "K": -3.9, "M": 1.9, "F": 2.8,
    "P": -1.6, "S": -0.8, "T": -0.7, "W": -0.9, "Y": -1.3, "V": 4.2,
}
# Approximate side-chain volume (Å³), for a crude packing term in the mock.
_VOLUME = {
    "A": 88.6, "R": 173.4, "N": 114.1, "D": 111.1, "C": 108.5, "Q": 143.8,
    "E": 138.4, "G": 60.1, "H": 153.2, "I": 166.7, "L": 166.7, "K": 168.6,
    "M": 162.9, "F": 189.9, "P": 112.7, "S": 89.0, "T": 116.1, "W": 227.8,
    "Y": 193.6, "V": 140.0,
}


def _stable_unit(*parts: object) -> float:
    """Deterministic float in [0, 1) from a seed — replaces Math.random so runs
    are reproducible (§3.6)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16) / float(1 << 48)


class OracleBackend(ABC):
    """Swappable ΔΔG backend. All ΔΔG are kcal/mol relative to the input
    structure; more negative = more stabilizing (§S6)."""

    name: str = "abstract"
    #: True if the *epistatic-double-with-distance-filter* mode is real for this
    #: backend. The core value proposition rests on it (§7.1); Phase 0 gates on it.
    supports_epistatic_double: bool = False
    weights_hash: str | None = None

    @abstractmethod
    def score_single(self, structure: str, sequence: str, position: int, to_aa: str) -> DeltaG:
        """One-sided reinforcement (§7.1 single mode)."""

    @abstractmethod
    def score_double(
        self, structure: str, sequence: str, pos_i: int, to_i: str, pos_j: int, to_j: str
    ) -> DeltaG:
        """The two residues of a contact scored *jointly*, not the additive sum
        of singles (§7.1 epistatic-double mode)."""

    @abstractmethod
    def score_saturation(self, structure: str, sequence: str, position: int) -> dict[str, DeltaG]:
        """ΔΔG for all 20 substitutions at a position (§7.1 / §D2 saturation)."""


class MockOracle(OracleBackend):
    """Deterministic, biophysically-flavoured fake for mock-first development
    (§14.2). NOT an energy function — a stable stand-in so the pipeline and its
    self-checks run without the real stack. Reports a fixed per-prediction
    uncertainty to exercise the §7.2 noise-floor logic."""

    name = "mock"
    supports_epistatic_double = True
    weights_hash = "mock-0000"

    def __init__(self, uncertainty: float = 0.4) -> None:
        self.uncertainty = uncertainty

    def _single(self, structure: str, sequence: str, position: int, to_aa: str) -> float:
        from_aa = sequence[position - 1] if 0 < position <= len(sequence) else "A"
        if to_aa == from_aa:
            return 0.0
        # Crude, deterministic terms: a hydropathy delta and a packing delta,
        # plus a bounded position-seeded jitter so the landscape is not monotone.
        dhyd = (_HYDROPATHY[to_aa] - _HYDROPATHY[from_aa]) / 4.5
        dvol = (_VOLUME[to_aa] - _VOLUME[from_aa]) / 167.0
        jitter = (_stable_unit(structure, position, from_aa, to_aa) - 0.5) * 2.0
        # Mostly small effects, occasionally a clear win/loss.
        return round(-0.4 * dhyd + 0.5 * abs(dvol) - 0.6 + 0.8 * jitter, 3)

    def score_single(self, structure: str, sequence: str, position: int, to_aa: str) -> DeltaG:
        return DeltaG(self._single(structure, sequence, position, to_aa), self.uncertainty)

    def score_double(
        self, structure: str, sequence: str, pos_i: int, to_i: str, pos_j: int, to_j: str
    ) -> DeltaG:
        a = self._single(structure, sequence, pos_i, to_i)
        b = self._single(structure, sequence, pos_j, to_j)
        # Epistatic coupling term: deliberately NOT a+b, so the double mode differs
        # from additive singles (the property §11.5 checks).
        coupling = (_stable_unit(structure, pos_i, pos_j, to_i, to_j) - 0.5) * 0.9
        return DeltaG(round(a + b + coupling, 3), self.uncertainty * 1.3)

    def score_saturation(self, structure: str, sequence: str, position: int) -> dict[str, DeltaG]:
        return {
            aa: DeltaG(self._single(structure, sequence, position, aa), self.uncertainty)
            for aa in AA20
        }


class _SubprocessOracleStub(OracleBackend):
    """Base for real oracles that shell out. Kept unimplemented until the real
    stack is wired on the Linux/GPU box (§14.2); raising here is intentional so a
    build never silently ships an unverified hard dependency (§7.1)."""

    binary_env: str = ""

    def _not_ready(self) -> DeltaG:
        raise NotImplementedError(
            f"{self.name}: real backend not wired in this environment. Run in mock mode "
            f"(oracle: mock) or provide the {self.binary_env} binary and implement the "
            f"subprocess call. Phase 0 must verify this backend before it is trusted (§13)."
        )

    def score_single(self, structure, sequence, position, to_aa):
        return self._not_ready()

    def score_double(self, structure, sequence, pos_i, to_i, pos_j, to_j):
        return self._not_ready()

    def score_saturation(self, structure, sequence, position):
        return {aa: self._not_ready() for aa in AA20}


class ThermoMPNNDOracle(_SubprocessOracleStub):
    """Primary oracle (§7.1). ProteinMPNN-derived; epistatic-double-with-distance-
    filter is the load-bearing mode. Its head/weights license is unverified (§9)."""

    name = "thermompnn-d"
    supports_epistatic_double = True
    binary_env = "THERMOMPNN_D"


class FoldXOracle(_SubprocessOracleStub):
    """Optional/fallback empirical oracle. Requires a commercial license (§9)."""

    name = "foldx"
    supports_epistatic_double = False
    binary_env = "FOLDX_BINARY"


class RosettaOracle(_SubprocessOracleStub):
    """Optional physics-based cross-check (`cartesian_ddg`). Commercial license (§9)."""

    name = "rosetta"
    supports_epistatic_double = False
    binary_env = "ROSETTA_BIN"


_ORACLES: dict[str, type[OracleBackend]] = {
    "mock": MockOracle,
    "thermompnn-d": ThermoMPNNDOracle,
    "foldx": FoldXOracle,
    "rosetta": RosettaOracle,
}


def get_oracle(name: str) -> OracleBackend:
    """Resolve an oracle backend by name (config `oracle:` / `--oracle`)."""
    key = name.lower()
    if key not in _ORACLES:
        raise ValueError(f"unknown oracle '{name}'; choices: {sorted(_ORACLES)}")
    return _ORACLES[key]()
