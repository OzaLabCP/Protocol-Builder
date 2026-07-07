# Licensing & commercial-use hygiene (spec §9)

Tessera is built for commercial (Anfinis) use. The governing rule:

> **Every third-party structural/energetic tool is invoked as an isolated
> `subprocess` with file-based I/O. Nothing GPL is imported as a library, and no
> non-commercial-restricted tool (dTERMen/Mosaist) is in the critical path.**

A CI/lint check (`tests/test_license_hygiene.py`) asserts GPL tools are only ever
`subprocess`'d, never imported.

| Component | Role | License | How Tessera uses it | Commercial status |
|---|---|---|---|---|
| Folddisco | discontinuous motif search | GPLv3 (Foldseek lineage) | subprocess only (`tessera.adapters.search.RealFolddisco`) | **OK** to run internally; do not ship a modified binary or statically link |
| Foldseek / mmseqs2 | optional hit clustering for reweighting | GPLv3 | subprocess only (`tessera.adapters.clustering.Mmseqs2Clustering`) | OK, same terms |
| ThermoMPNN-D | ΔΔG oracle | ProteinMPNN (MIT) lineage — **verify head/weights license** | subprocess or local API | **verify before relying** |
| ProteinMPNN / LigandMPNN | upstream design (external) | MIT | interoperate via files | OK |
| FoldX | optional oracle | academic/commercial license required | subprocess | **requires commercial license** |
| Rosetta | optional oracle (`cartesian_ddg`) | commercial license required | subprocess | **requires commercial license** |
| dTERMen / Mosaist | *deliberately excluded* | non-commercial only | — | avoided to keep the path commercial-clean |
| biotite / MDAnalysis / numpy / pandas / pyarrow / pydantic | Tessera internals | BSD/permissive | library import | OK |

## Startup license check

When an oracle requiring a license (FoldX/Rosetta) is selected without a
configured license path (`oracle.foldx_license_path` / `oracle.rosetta_license_path`),
Tessera **warns at startup**. Before any production Anfinis use:

1. Confirm the **ThermoMPNN-D weight license** (the trained head, not just the
   MIT-derived ProteinMPNN code).
2. If FoldX/Rosetta oracles are enabled, confirm valid **commercial licenses** are
   in place and their binaries are reachable.
