# Tessera

**Tertiary-contact stabilization by discontinuous structural-motif harvesting.**

Tessera takes a de novo backbone (typically an RFdiffusion / RFdiffusion-AA
output, before or after inverse folding), finds the **long-range tertiary
contacts** that hold the fold together, searches the predicted protein universe
for **natural recurrences of each contact's local geometry**, harvests the
**residue identities nature uses at that geometry**, and hands the candidates to a
**calibrated ΔΔG oracle** to rank which substitutions most stabilize the contact.

It is a *targeted reinforcement and diagnostic* layer, not a replacement for
inverse folding. See [`tessera_spec.md`](./tessera_spec.md) for the full design
specification — this README is the operational summary.

## The one architectural rule

**Propensity and energy are never conflated (§3.1).** Retrieval statistics
produce *propensities* (`tessera.types.Propensity`, log-odds vs a background);
the oracle produces *energies* (`tessera.types.DeltaG`, ΔΔG in kcal/mol). The two
are distinct types with **no defined arithmetic between them** — summing a
propensity into a ΔG does not compile and raises at runtime.

## Pipeline (S1–S7)

| Stage | Module | Output |
|---|---|---|
| S1 Contact detection | `tessera.stages.s1_contacts` | `contacts.json` |
| S2 Motif-query construction | `tessera.stages.s2_query` | `queries/*.pdb`, manifest |
| S3 Folddisco search | `tessera.stages.s3_search` | `hits/*.tsv` |
| S4 Hit parsing + statistics | `tessera.stages.s4_stats` | `stats/*.parquet`, `stats_summary.json` |
| S5 Candidate enumeration | `tessera.stages.s5_candidates` | `candidates.json` |
| S6 ΔΔG screening (oracle) | `tessera.stages.s6_score` | `scored_candidates.parquet` |
| S7 Ranking + reporting + constraints | `tessera.stages.s7_report` | `report.html`, `constraints/*` |
| 7A Stability triage (optional) | `tessera.triage` | `weak_regions.json`, `triage_report.html` |

## Mock-first (§14.2)

The real stack (Folddisco index ~1.5 TB, GPU oracle, licensed binaries) **cannot
run during development**. Every external tool sits behind an adapter with a
functional **mock**, so the whole pipeline — and its self-checks — run offline.
`--dry-run` / mock backends are the default; swap in real binaries on the
Linux/GPU box.

## Quickstart

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# End-to-end reinforcement run, fully mocked:
tessera run --backbone tests/fixtures/toy.pdb --seq tests/fixtures/toy.fasta \
            --out runs/demo --oracle mock --folddisco mock

# Oracle-only design mode (skip S2–S5 retrieval; the §3.7 fallback):
tessera run --backbone tests/fixtures/toy.pdb --no-retrieve --out runs/demo_oracle

# Stability-triage front-end (7A):
tessera run --backbone tests/fixtures/toy.pdb --triage --out runs/demo_triage

# Individual stages are runnable standalone and support --from/--to reruns:
tessera contacts --backbone design.pdb --out runs/demo
tessera run --from S4 --to S6 --out runs/demo
```

## Run directory layout

```
runs/demo/
  config.resolved.yaml
  contacts.json
  queries/  hits/  stats/
  candidates.json  scored_candidates.parquet
  report.html
  constraints/{ligandmpnn_bias.jsonl, design.resfile}
  logs/
```

## License & IP hygiene

Every third-party structural/energetic tool is a **subprocess** with file-based
I/O; nothing GPL is imported. See [`LICENSES.md`](./LICENSES.md). FoldX/Rosetta
require commercial licenses; ThermoMPNN-D weights license must be confirmed before
production use (§9).
