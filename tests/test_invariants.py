"""Invariant §14.5 / §7A-D0: no per-residue ΔG anywhere.

Folding free energy does not decompose into additive per-residue kcal/mol. No
emitted schema may carry a per-residue absolute-ΔG field. (A per-*mutation* ΔΔG —
a change, e.g. StabilizingDensitySignal.best_ddg — is legitimate and allowed.)
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel

import tessera.schemas as schemas

pytestmark = pytest.mark.invariant

# field names that would encode a per-residue absolute ΔG (forbidden), while leaving
# per-mutation ΔΔG names like "best_ddg" / "primary_ddg" alone.
FORBIDDEN = re.compile(r"per_residue.*d?dg|residue_(?:dg|free_energy)|per_position_dg", re.IGNORECASE)


def _all_models() -> list[type[BaseModel]]:
    out: list[type[BaseModel]] = []
    for name in schemas.__all__:
        obj = getattr(schemas, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            out.append(obj)
    return out


def test_no_per_residue_dg_field_in_any_schema():
    offenders: list[str] = []
    for model in _all_models():
        for field_name in model.model_fields:
            if FORBIDDEN.search(field_name):
                offenders.append(f"{model.__name__}.{field_name}")
    assert not offenders, "per-residue ΔG fields are forbidden (§7A/D0): " + ", ".join(offenders)


def test_weak_regions_doc_has_no_per_residue_dg():
    from tessera.schemas.weak_regions import StabilizingDensitySignal, WeakRegion

    # best_ddg is a per-MUTATION ΔΔG (allowed); assert nothing per-residue sneaks in
    for model in (WeakRegion, StabilizingDensitySignal):
        assert not any(FORBIDDEN.search(f) for f in model.model_fields)
