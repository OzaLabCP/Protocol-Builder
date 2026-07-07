# Test fixtures

Per spec §14.3, external-tool output parsers must trace to a **captured** fixture.
Where a real capture is impossible in the offline dev environment (no Folddisco
index, no GPU oracle, no licensed binaries), the fixture here is a **format-faithful
synthetic** file, clearly labeled, to be **replaced by a real capture before the
parser is trusted in production**.

## `toy.pdb` / `toy.fasta` — geometrically-controlled structure (real geometry)

A 26-residue antiparallel β-hairpin generated with explicit coordinates so Cβ–Cβ
distances are exact and hand-verifiable. Consecutive Cα–Cα = 3.8 Å (no spurious
chain breaks). Residue 6 is glycine (no Cβ ⇒ Cα substituted, per §S1).

**Known contacts** (`sep_min=12`, `d_contact=8.0`), used by the S1 golden test —
21 contacts total. Anchors:

| pair (i, j) | d_cb (Å) |
|---|---|
| (4, 23) | 4.800 |
| (4, 22) | 6.122 |
| (4, 24) | 6.122 |
| (7, 20) | 4.800 |
| (1, 26) | 5.029 (Cα-substituted wrap residues) |
| (6, 21) | 5.029 (Gly Cα substitution) |

No contact has `|i−j| < 12`. The two strands form one contact cluster.

## `hits_parse.tsv` — Folddisco hits parser fixture (SYNTHETIC)

Exercises every column of the pinned `hits/<cluster>.tsv` contract:
`target_id ⇥ source_db ⇥ rmsd ⇥ score ⇥ motif_mean_plddt ⇥ matched`, where
`matched` is `;`-joined `contact:role:target_index:residue:plddt` tuples.
Covers an AFDB50 hit with pLDDT, a PDB hit with `NA` pLDDT (§S4.2 exempt), and an
AFDB50 hit with a residue below the pLDDT cutoff (parser reads it; S4 filters it).

## `hits_reweight.tsv` — redundancy-reweighting golden fixture (SYNTHETIC)

Family `MOCK000` is over-sampled (10 members) all showing pair **(I, L)**; three
independent families each show **(W, Y)**. The §11.6 property:

- **Naive** frequency top = (I, L) (count 10 > 3).
- **Redundancy-corrected** top = (W, Y) (weight 3.0 > 1.0), because MOCK000's 10
  members collapse to a single cluster of total weight 1.

Reweighting *must* flip the top residue — a golden test for the "what got
sequenced a lot" failure mode (§6.3).

## Pinned tool versions

Synthetic fixtures assume the column order above. When replacing with real
captures, record the exact Folddisco / ThermoMPNN versions and column order here.
