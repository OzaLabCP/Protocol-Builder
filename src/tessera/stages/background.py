"""S4 background null model f0(a,b) + contact-geometry classification (spec §S4.4).

The log-odds propensity ``s(a,b) = log[f(a,b) / f0(a,b)]`` is defined against a
background ``f0`` that the spec makes a **first-class, configurable choice** — it
determines the entire candidate ordering (§S4.4). The recommended null is
geometry/burial-conditioned, estimated from a versioned corpus of high-confidence
contacts. That corpus is unavailable offline, so this module ships a documented
**stand-in**: the independent product of a standard amino-acid composition. The
stand-in is recorded in provenance (§14.2) so it is never mistaken for the real
conditioned null.
"""

from __future__ import annotations

import bisect

import numpy as np

from ..io.pdb import Structure
from ..schemas.common import (
    BackgroundNullSpec,
    BurialClass,
    OrientationClass,
)
from ..schemas.stats import ContactGeometry

# Approx UniProt amino-acid composition; sums ~1.0. Documented offline stand-in for
# the geometry/burial-conditioned corpus (§S4.4), which is unavailable offline.
STANDARD_AA_BACKGROUND: dict[str, float] = {
    "A": 0.0825, "R": 0.0553, "N": 0.0406, "D": 0.0546, "C": 0.0137, "Q": 0.0393,
    "E": 0.0675, "G": 0.0707, "H": 0.0227, "I": 0.0596, "L": 0.0966, "K": 0.0584,
    "M": 0.0242, "F": 0.0386, "P": 0.0470, "S": 0.0657, "T": 0.0534, "W": 0.0108,
    "Y": 0.0292, "V": 0.0687,
}


class BackgroundModel:
    """The null model f0(a,b) (§S4.4).

    Offline this is an independent-product stand-in regardless of ``spec.kind``,
    because the geometry/burial-conditioned corpus is unavailable offline. Always
    returns a strictly positive value so log-odds are finite.
    """

    def __init__(self, spec: BackgroundNullSpec, mock_mode: bool = True) -> None:
        self.spec = spec
        self.mock_mode = mock_mode

    def f0(self, a: str, b: str) -> float:
        """Independent product of the standard background (§S4.4 stand-in). >0 always."""
        return STANDARD_AA_BACKGROUND[a] * STANDARD_AA_BACKGROUND[b]


def _direction(structure: Structure, index: int) -> np.ndarray:
    """Cβ-direction unit vector (CA->CB); zero vector for glycine / missing Cβ (§S4.4)."""
    r = structure.residues[index - 1]
    if not r.has_cb():
        return np.zeros(3, dtype=float)
    d = r.cb() - r.atoms["CA"]
    n = float(np.linalg.norm(d))
    if n == 0.0:
        return np.zeros(3, dtype=float)
    return d / n


def classify_geometry(
    structure: Structure,
    i: int,
    j: int,
    buriedness_i: int,
    buriedness_j: int,
    spec: BackgroundNullSpec,
) -> ContactGeometry:
    """Classify a contact's geometry bin for background conditioning / provenance (§S4.4).

    - ``distance_bin``: 0-based index into ``spec.distance_bins_angstrom`` that d_cb
      falls into (clamped to a valid index).
    - ``orientation``: dot of the two Cβ-direction unit vectors — >0.5 PARALLEL,
      <-0.5 ANTIPARALLEL, else ORTHOGONAL (glycine's zero vector => ORTHOGONAL).
    - ``burial``: mean of the two Cβ-neighbor-count proxies — >=16 BURIED,
      >=8 INTERMEDIATE, else EXPOSED.
    """
    cb_i = structure.residues[i - 1].cb()
    cb_j = structure.residues[j - 1].cb()
    d_cb = float(np.linalg.norm(cb_i - cb_j))

    edges = spec.distance_bins_angstrom
    distance_bin = bisect.bisect_right(edges, d_cb) - 1
    distance_bin = max(0, min(distance_bin, len(edges) - 1))

    dot = float(np.dot(_direction(structure, i), _direction(structure, j)))
    if dot > 0.5:
        orientation = OrientationClass.PARALLEL
    elif dot < -0.5:
        orientation = OrientationClass.ANTIPARALLEL
    else:
        orientation = OrientationClass.ORTHOGONAL

    mean_burial = (buriedness_i + buriedness_j) / 2.0
    if mean_burial >= 16:
        burial = BurialClass.BURIED
    elif mean_burial >= 8:
        burial = BurialClass.INTERMEDIATE
    else:
        burial = BurialClass.EXPOSED

    return ContactGeometry(
        d_cb=d_cb, distance_bin=distance_bin, orientation=orientation, burial=burial
    )
