"""Tessera — tertiary-contact stabilization by discontinuous structural-motif
harvesting.

Two-stage separation of propensity and energy (§3.1): retrieval + statistics
produce *candidates* (:class:`~tessera.types.Propensity`); a calibrated oracle
produces *energies* (:class:`~tessera.types.DeltaG`). The two are never conflated
— the type system makes that structurally impossible (§14.5).
"""

from __future__ import annotations

from .config import TesseraConfig
from .types import DeltaG, Propensity

__version__ = "0.2.0"

__all__ = ["DeltaG", "Propensity", "TesseraConfig", "__version__"]
