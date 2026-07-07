"""S1 golden + gap-spanning tests (spec §S1, §14.6).

The toy hairpin has hand-verified Cβ distances (tests/fixtures/README.md), so the
contact count and anchor distances are exact expected values, not just "it runs".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.config import ContactConfig
from tessera.io.pdb import Structure, load_structure
from tessera.schemas.common import FeatureMode
from tessera.stages.s1_contacts import detect_contacts


def _atom(
    serial: int, atom: str, resname: str, chain: str, resseq: int,
    x: float, y: float, z: float,
) -> str:
    return (
        f"ATOM  {serial:>5} {atom:<4} {resname:>3} {chain}{resseq:>4}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00 90.00           {atom[0]}"
    )


@pytest.mark.golden
def test_detect_contacts_golden(toy_structure: Structure) -> None:
    doc = detect_contacts(
        toy_structure,
        ContactConfig(),
        feature_mode=FeatureMode.SEQUENCED,
        backbone_name="toy",
    )

    assert doc.backbone == "toy"
    assert doc.feature_mode is FeatureMode.SEQUENCED

    # Hand-verified count and anchor distances (fixtures/README.md).
    assert len(doc.contacts) == 21

    by_pair = {(c.i, c.j): c for c in doc.contacts}
    assert (4, 23) in by_pair
    assert by_pair[(4, 23)].d_cb == pytest.approx(4.8, abs=1e-2)
    assert by_pair[(4, 22)].d_cb == pytest.approx(6.122, abs=1e-2)
    assert by_pair[(4, 24)].d_cb == pytest.approx(6.122, abs=1e-2)

    # sep_min=12 default: no short-range pair leaks in.
    assert all((c.j - c.i) >= 12 for c in doc.contacts)

    # Toy has no chain break.
    assert doc.gaps == []

    # Every contact carries a cluster id that resolves to a real cluster.
    for c in doc.contacts:
        assert c.cluster
        assert c.cluster in doc.clusters

    # Cluster segments cover both strands of the hairpin.
    covered = {
        r
        for cl in doc.clusters.values()
        for lo, hi in cl.segments
        for r in range(lo, hi + 1)
    }
    assert covered & set(range(1, 9))     # strand 1 (some of 1..8)
    assert covered & set(range(19, 27))   # strand 2 (some of 19..26)


def test_contact_spanning_gap_is_flagged(tmp_path: Path) -> None:
    """A pair bridging a residue-numbering jump is flagged and recorded (§S1).

    Two short antiparallel strands numbered 1..5 then 20..23; load_structure
    reindexes them 1..9 sequentially, so the break sits between sequential
    indices 5 and 6 and gap-spanning contacts appear across the strands.
    """
    lines: list[str] = []
    serial = 1
    # Strand 1: res_seq 1..5 along +x at y=0.
    for k in range(5):
        lines.append(_atom(serial, "CA", "ALA", "A", k + 1, 3.8 * k, 0.0, 0.0))
        serial += 1
    # Strand 2: res_seq 20..23 back along +x at y=5.0 (numbering jump ⇒ break).
    xs = [15.2, 11.4, 7.6, 3.8]
    for k, x in enumerate(xs):
        lines.append(_atom(serial, "CA", "ALA", "A", 20 + k, x, 5.0, 0.0))
        serial += 1
    lines.append("END")
    pdb = tmp_path / "gap.pdb"
    pdb.write_text("\n".join(lines) + "\n")

    structure = load_structure(pdb)
    doc = detect_contacts(
        structure,
        ContactConfig(sep_min=5),
        feature_mode=FeatureMode.BACKBONE_ONLY,
    )

    # Sequential indices: res3 -> 3, res22 -> 8; that pair straddles the break.
    by_pair = {(c.i, c.j): c for c in doc.contacts}
    assert (3, 8) in by_pair
    assert by_pair[(3, 8)].spans_gap is True

    # The break is emitted in the doc (after index 5, before index 6).
    gap_pairs = {(g.after_residue, g.before_residue) for g in doc.gaps}
    assert (5, 6) in gap_pairs

    # Every gap-spanning contact is flagged, none silently passes as clean geometry.
    assert all(c.spans_gap for c in doc.contacts)
