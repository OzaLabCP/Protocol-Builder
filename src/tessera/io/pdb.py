"""Dependency-light PDB/mmCIF-lite structure reader (spec §S1 notes, §14.2).

The spec suggests biotite / MDAnalysis for parsing, but the *core* pipeline must
run offline without the heavy stack. This module parses ATOM records with a tiny
pure-Python reader sufficient for contact detection (Cα/Cβ coordinates, sequence,
per-residue pLDDT from the B-factor column, chain-break detection). Install the
`structio` extra to use biotite where richer parsing is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V", "MSE": "M", "SEC": "C",
}

_CA_CA_BREAK = 4.5  # Å; consecutive Cα farther apart than this ⇒ chain break (§S1)


@dataclass
class Residue:
    index: int          # 1-based sequential index in the parsed structure
    res_seq: int        # residue number from the PDB
    resname: str        # 3-letter
    one_letter: str
    chain: str
    atoms: dict[str, np.ndarray] = field(default_factory=dict)
    plddt: float | None = None  # CA B-factor when interpreted as pLDDT

    def cb(self) -> np.ndarray:
        """Cβ coordinate, substituting Cα for glycine / missing Cβ (§S1)."""
        if "CB" in self.atoms:
            return self.atoms["CB"]
        return self.atoms["CA"]

    def has_cb(self) -> bool:
        return "CB" in self.atoms


@dataclass
class Structure:
    residues: list[Residue]
    source: str = ""

    def sequence(self) -> str:
        return "".join(r.one_letter for r in self.residues)

    def cb_coords(self) -> np.ndarray:
        return np.array([r.cb() for r in self.residues], dtype=float)

    def ca_coords(self) -> np.ndarray:
        return np.array([r.atoms["CA"] for r in self.residues], dtype=float)

    def plddt(self) -> list[float | None]:
        return [r.plddt for r in self.residues]

    def has_cb_mask(self) -> np.ndarray:
        return np.array([r.has_cb() for r in self.residues], dtype=bool)

    def gaps(self) -> list[tuple[int, int]]:
        """Return (after_index, before_index) 1-based sequential pairs that bound a
        chain break — a residue-numbering jump, a chain change, or a Cα–Cα distance
        exceeding a peptide bond. Contacts spanning these have undefined geometry."""
        out: list[tuple[int, int]] = []
        for a, b in zip(self.residues, self.residues[1:], strict=False):
            broke = (
                a.chain != b.chain
                or b.res_seq - a.res_seq != 1
                or float(np.linalg.norm(a.atoms["CA"] - b.atoms["CA"])) > _CA_CA_BREAK
            )
            if broke:
                out.append((a.index, b.index))
        return out


def load_structure(path: str | Path, bfactor_as_plddt: bool = True) -> Structure:
    """Parse a PDB file into a :class:`Structure`.

    Only ATOM records are read; the first altloc per atom wins. The B-factor column
    is interpreted as pLDDT when ``bfactor_as_plddt`` (the AFDB / design-model
    convention).
    """
    path = Path(path)
    residues: list[Residue] = []
    current_key: tuple[str, int, str] | None = None
    current: Residue | None = None
    idx = 0
    for line in path.read_text().splitlines():
        rec = line[:6].strip()
        if rec not in ("ATOM", "HETATM"):
            if rec in ("ENDMDL", "END"):  # first model only
                break
            continue
        if rec == "HETATM" and line[17:20].strip() != "MSE":
            continue
        atom_name = line[12:16].strip()
        alt = line[16].strip()
        resname = line[17:20].strip()
        chain = line[21].strip() or "A"
        res_seq = int(line[22:26])
        icode = line[26].strip()
        x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
        try:
            bfac = float(line[60:66])
        except ValueError:
            bfac = 0.0
        key = (chain, res_seq, icode)
        if key != current_key:
            idx += 1
            current = Residue(
                index=idx,
                res_seq=res_seq,
                resname=resname,
                one_letter=THREE_TO_ONE.get(resname, "X"),
                chain=chain,
                plddt=None,
            )
            residues.append(current)
            current_key = key
        assert current is not None
        if atom_name in current.atoms and alt not in ("", "A"):
            continue  # keep first altloc
        current.atoms[atom_name] = np.array([x, y, z], dtype=float)
        if atom_name == "CA" and bfactor_as_plddt:
            current.plddt = bfac
    # Drop residues with no Cα (cannot place in the contact map).
    residues = [r for r in residues if "CA" in r.atoms]
    for new_i, r in enumerate(residues, start=1):
        r.index = new_i
    return Structure(residues=residues, source=str(path))


def write_motif_pdb(structure: Structure, indices: list[int], path: str | Path) -> None:
    """Write a PDB containing only ``indices`` (1-based), coordinates preserved —
    the S2 discontinuous-query structure."""
    lines: list[str] = []
    serial = 1
    by_index = {r.index: r for r in structure.residues}
    for ri in indices:
        r = by_index[ri]
        for atom, xyz in r.atoms.items():
            lines.append(
                f"ATOM  {serial:>5} {atom:<4} {r.resname:>3} {r.chain}{r.res_seq:>4}    "
                f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00"
                f"{(r.plddt or 0.0):6.2f}           {atom[0]:>1}"
            )
            serial += 1
    lines.append("END")
    Path(path).write_text("\n".join(lines) + "\n")
