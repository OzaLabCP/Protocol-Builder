"""Unit-carrying scalar types that make spec §3.1's propensity/energy separation
*structurally* unbreachable (spec §14.5).

The single most important architectural commitment in Tessera is that retrieval
statistics produce **propensities** and the oracle produces **energies**, and the
two are never conflated (§3.1). Here that rule is enforced by the type system:
:class:`Propensity` and :class:`DeltaG` are distinct types with **no defined
arithmetic or comparison between them**. "Sum a propensity into a ΔG" raises
``TypeError`` at runtime and is a type error under mypy — it cannot silently
happen.

Both types are pydantic-v2 aware, so they can be used directly as model fields:
a :class:`Propensity` serializes as a bare float; a :class:`DeltaG` serializes as
``{"value": ..., "uncertainty": ...}`` so its noise estimate (§7.2) travels with
it and is never dropped.
"""

from __future__ import annotations

from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["Propensity", "DeltaG"]


class Propensity:
    """A log-odds propensity score ``s(a,b) = log[f(a,b) / f0(a,b)]`` (§S4.4).

    This is a *statistical propensity* — the same currency as dTERMen — and is
    **explicitly not a free energy** (§2, §3.1). Propensities may be added to and
    compared with other propensities (S5 beam ranking sums per-pair log-odds), but
    any operation mixing a :class:`Propensity` with a :class:`DeltaG` (or a raw
    number treated as an energy) is undefined and raises ``TypeError``.
    """

    __slots__ = ("value",)

    def __init__(self, value: float) -> None:
        self.value = float(value)

    # -- arithmetic / comparison, propensity-only --------------------------------
    def __add__(self, other: Any) -> Propensity:
        if isinstance(other, Propensity):
            return Propensity(self.value + other.value)
        return NotImplemented

    def __radd__(self, other: Any) -> Propensity:
        # allow sum([...], start=Propensity(0.0)) style folds over propensities
        if other == 0:
            return Propensity(self.value)
        if isinstance(other, Propensity):
            return Propensity(self.value + other.value)
        return NotImplemented

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, Propensity):
            return self.value < other.value
        return NotImplemented

    def __le__(self, other: Any) -> bool:
        if isinstance(other, Propensity):
            return self.value <= other.value
        return NotImplemented

    def __gt__(self, other: Any) -> bool:
        if isinstance(other, Propensity):
            return self.value > other.value
        return NotImplemented

    def __ge__(self, other: Any) -> bool:
        if isinstance(other, Propensity):
            return self.value >= other.value
        return NotImplemented

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Propensity) and self.value == other.value

    def __hash__(self) -> int:
        return hash(("Propensity", self.value))

    def __float__(self) -> float:
        return self.value

    def __repr__(self) -> str:
        return f"Propensity({self.value:.4f})"

    # -- pydantic v2 integration -------------------------------------------------
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        from_float = core_schema.no_info_plain_validator_function(cls._validate)
        return core_schema.json_or_python_schema(
            json_schema=from_float,
            python_schema=core_schema.union_schema(
                [core_schema.is_instance_schema(cls), from_float]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda p: p.value, return_schema=core_schema.float_schema()
            ),
        )

    @classmethod
    def _validate(cls, v: Any) -> Propensity:
        if isinstance(v, Propensity):
            return v
        if isinstance(v, DeltaG):
            raise TypeError("refusing to coerce a DeltaG into a Propensity (spec §3.1)")
        return cls(float(v))


class DeltaG:
    """A ΔΔG value in kcal/mol relative to a reference structure (§S6).

    More negative = more stabilizing. Carries an optional per-prediction
    ``uncertainty`` (the ~1 kcal/mol noise floor of ThermoMPNN-class predictors,
    §7.2) so downstream ranking can treat "within noise" as a tie.

    **Addition is intentionally undefined.** Multi-site ΔΔGs are *marginal,
    independent* estimates; the combined effect of several reinforcements is not
    their sum (cross-contact epistasis, §7.2) and must be arbitrated by a Boltz-2
    refold (§11.4), not by summing the table. Attempting ``d1 + d2`` raises.
    Comparison between two ``DeltaG`` values is allowed (ranking by ΔΔG, §S7).
    """

    __slots__ = ("value", "uncertainty")

    def __init__(self, value: float, uncertainty: float | None = None) -> None:
        self.value = float(value)
        self.uncertainty = float(uncertainty) if uncertainty is not None else None

    # -- addition is a spec violation --------------------------------------------
    def __add__(self, other: Any) -> Any:
        raise TypeError(
            "DeltaG addition is undefined: marginal ΔΔGs must not be summed "
            "(spec §7.2). Arbitrate multi-site effects with a refold (§11.4)."
        )

    __radd__ = __add__

    # -- comparison, energy-only -------------------------------------------------
    def __lt__(self, other: Any) -> bool:
        if isinstance(other, DeltaG):
            return self.value < other.value
        return NotImplemented

    def __le__(self, other: Any) -> bool:
        if isinstance(other, DeltaG):
            return self.value <= other.value
        return NotImplemented

    def __gt__(self, other: Any) -> bool:
        if isinstance(other, DeltaG):
            return self.value > other.value
        return NotImplemented

    def __ge__(self, other: Any) -> bool:
        if isinstance(other, DeltaG):
            return self.value >= other.value
        return NotImplemented

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, DeltaG)
            and self.value == other.value
            and self.uncertainty == other.uncertainty
        )

    def __hash__(self) -> int:
        return hash(("DeltaG", self.value, self.uncertainty))

    def __float__(self) -> float:
        return self.value

    def __repr__(self) -> str:
        if self.uncertainty is None:
            return f"DeltaG({self.value:.3f} kcal/mol)"
        return f"DeltaG({self.value:.3f}±{self.uncertainty:.3f} kcal/mol)"

    # -- semantics used by S7 (§7.2 effect-size floor) ---------------------------
    def is_stabilizing(self) -> bool:
        """More-negative-than-zero. Not the same as a *confident* effect."""
        return self.value < 0.0

    def is_confident_effect(self, min_effect: float) -> bool:
        """True only if the ΔΔG clears the minimum effect size **and** is larger
        than its own uncertainty band (§7.2). ``min_effect`` is negative
        (e.g. -0.5 kcal/mol); a candidate must be at least that stabilizing.
        """
        if self.value > min_effect:
            return False
        if self.uncertainty is not None and abs(self.value) <= self.uncertainty:
            return False
        return True

    # -- pydantic v2 integration -------------------------------------------------
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        from_any = core_schema.no_info_plain_validator_function(cls._validate)
        return core_schema.json_or_python_schema(
            json_schema=from_any,
            python_schema=core_schema.union_schema(
                [core_schema.is_instance_schema(cls), from_any]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda d: {"value": d.value, "uncertainty": d.uncertainty},
            ),
        )

    @classmethod
    def _validate(cls, v: Any) -> DeltaG:
        if isinstance(v, DeltaG):
            return v
        if isinstance(v, Propensity):
            raise TypeError("refusing to coerce a Propensity into a DeltaG (spec §3.1)")
        if isinstance(v, dict):
            return cls(float(v["value"]), v.get("uncertainty"))
        return cls(float(v))
