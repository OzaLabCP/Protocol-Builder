# CLAUDE.md — Tessera conventions for coding agents

This repo implements **Tessera** (see `tessera_spec.md`, the source of truth).
Keep the spec in-repo and update it as design reality shifts (§14.8).

## Non-negotiable invariants (spec §14.5) — enforced by tests, not prose

1. **Propensity and energy are distinct types.** `tessera.types.Propensity` (log-odds
   retrieval propensity) and `tessera.types.DeltaG` (oracle ΔΔG, kcal/mol) have **no
   defined arithmetic or comparison between them**. Never coerce one into the other.
   A propensity is never presented as a ΔG (§2, §3.1, §12).
2. **No per-residue ΔG.** Folding free energy does not decompose into additive
   per-residue kcal/mol (§7A/D0). No emitted artifact may contain a per-residue ΔG
   column; `tests/test_invariants.py` fails if one appears.
3. **No GPL linkage.** Folddisco, Foldseek, mmseqs2 are GPLv3 and are invoked as
   `subprocess` only — never `import`ed. `tests/test_license_hygiene.py` enforces this.
4. **Provenance tags are mandatory schema fields**, not optional: `feature_mode`,
   database/model versions, background-null spec, and per-contact `status`. A run
   cannot silently drop them.

## Engineering conventions

- **Mock-first, real-second (§14.2).** Build and verify against mock adapters +
  captured fixtures; swap real binaries on the Linux/GPU box. Every external tool has
  a `get_*` factory returning a mock or real backend. `--dry-run` / mock backends run
  without the real stack and are the default in tests.
- **Contract-first (§14.4).** The typed stage schemas in `tessera/schemas/` are frozen
  *before* stage logic. Change a schema deliberately; stages and tests depend on it.
- **Fixtures are captured, never invented (§14.3).** External-tool output parsers must
  trace to a fixture in `tests/fixtures/`. Where a real capture is impossible offline
  (no Folddisco index), the fixture is a documented format-faithful synthetic file
  labeled as such in `tests/fixtures/README.md`, to be replaced by a real capture
  before the parser is trusted in production.
- **Golden tests for the silent-bug zone (§14.6).** S1 (contact detection) and S4
  (reweighting, log-odds) produce plausible numbers when wrong — they have
  hand-verified expected values, not just "it runs".
- **Every stage is inspectable and rerunnable (§3.6).** Artifacts have stable schemas;
  `--from/--to` reruns are real. Write intermediate artifacts to the run directory.

## Target environment (§14.1)

Linux. Python 3.11, `uv` for the venv, `pytest` + `ruff` + `mypy`. Develop in the
provided devcontainer semantics; do not target native Windows.

## Layout

```
src/tessera/
  types.py            # Propensity, DeltaG (invariant §14.5)
  config.py           # tessera.yaml model (§8)
  schemas/            # frozen typed artifacts (§14.4)
  adapters/           # oracle / folddisco / clustering — mock + real subprocess (§14.2, §9)
  io/pdb.py           # dependency-light structure reader
  stages/             # s1..s7 stage logic
  triage/             # 7A weak-region front-end (D1–D6)
  report/             # HTML report + constraint emission
  orchestrator.py     # `tessera run`, --from/--to
  cli.py              # CLI entrypoint
tests/                # golden + invariant + e2e-mock
```

## Commands

```bash
source .venv/bin/activate
pytest -q            # all tests (mock mode, offline)
ruff check src tests
mypy src/tessera
tessera run --backbone tests/fixtures/toy.pdb --seq tests/fixtures/toy.fasta --out runs/demo
```
