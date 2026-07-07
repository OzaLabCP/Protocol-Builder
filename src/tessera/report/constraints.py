"""S7 constraint emission for the design loop (spec §S7).

Accepted reinforcements are written as machine-readable constraints so they seed a
*constrained* LigandMPNN / Rosetta redesign rather than being applied blindly (§S7).
Unlike the pure compute stages, these functions write files — the constraint files
*are* the S7 deliverable that closes the RFdiffusion-AA → LigandMPNN → Boltz-2 loop.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..schemas.candidates import Substitution

# A positive per-position AA bias LigandMPNN can consume; a reasonable default
# nudge toward the accepted residue (§S7).
_LIGANDMPNN_BIAS: float = 2.0


def emit_ligandmpnn_bias(subs: list[Substitution], path: str | Path) -> None:
    """Write a LigandMPNN per-position AA bias as JSONL — one line per substitution,
    ``{"position": <int>, "aa": <to_aa>, "bias": 2.0}`` (§S7). Parent dirs created."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for s in subs:
            line = {"position": s.position, "aa": s.to_aa, "bias": _LIGANDMPNN_BIAS}
            fh.write(json.dumps(line))
            fh.write("\n")


def emit_resfile(subs: list[Substitution], path: str | Path) -> None:
    """Write a Rosetta resfile pinning each accepted substitution on chain A with a
    ``PIKAA`` line under a ``NATRO`` default (§S7). Parent dirs created."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["NATRO", "start"]
    lines.extend(f"{s.position} A PIKAA {s.to_aa}" for s in subs)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
