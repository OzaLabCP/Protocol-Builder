"""End-to-end pipeline runs in mock mode (spec §14.2) — the offline self-check.

Exercises the full S1→S7 chain, the `--no-retrieve` oracle-only mode (§3.7), and
the 7A triage front-end, entirely against mock adapters. Also asserts no emitted
artifact contains a per-residue ΔG (§7A/D0).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tessera.config import TesseraConfig
from tessera.orchestrator import run

pytestmark = pytest.mark.e2e

# A per-residue absolute ΔG presented as data (§7A/D0). Requires an actual ΔG/ddg
# token near "per-residue" — a prose disclaimer that merely mentions "per-mutation
# ΔΔG" or "energy" is fine; a "per-residue ΔG" column is not.
FORBIDDEN_TEXT = re.compile(r"per[-_ ]residue\s*(?:Δ?ΔG|d?dg)\b", re.IGNORECASE)

TOY_PDB = Path(__file__).parent / "fixtures" / "toy.pdb"
TOY_SEQUENCE = "ACDEFGHIKLMNPQRSTVWYACDEFG"


def _cfg(tmp_path, **over) -> TesseraConfig:
    base = dict(backbone=str(TOY_PDB), seq=TOY_SEQUENCE, out=str(tmp_path / "run"))
    base.update(over)
    return TesseraConfig(**base)


def test_full_pipeline_mock(tmp_path):
    res = run(_cfg(tmp_path))
    L = res.layout
    for path in (L.contacts, L.manifest, L.stats_summary, L.candidates, L.scored, L.report):
        assert path.exists(), f"missing artifact: {path}"
    assert L.ligandmpnn_bias.exists() and L.resfile.exists()
    assert res.provenance.mock_mode is True
    # at least the retrieval path ran and produced some contacts' stats
    assert res.status_counts, "expected per-contact status counts"
    # no per-residue ΔG in the report
    assert not FORBIDDEN_TEXT.search(L.report.read_text())


def test_no_retrieve_oracle_only(tmp_path):
    res = run(_cfg(tmp_path, retrieve=False))
    from tessera.artifacts import read_candidates

    cands = read_candidates(res.layout.candidates)
    assert cands.retrieval_used is False
    # S2-S4 skipped => no hits directory content required; candidates come from the oracle
    assert res.layout.report.exists()


def test_partial_rerun_from_to(tmp_path):
    cfg = _cfg(tmp_path)
    run(cfg, "S1", "S4")            # produce contacts..stats
    assert cfg_out(cfg, "stats_summary")
    res = run(cfg, "S5", "S7")      # resume from candidates using persisted artifacts
    assert res.layout.report.exists()


def test_triage_mode(tmp_path):
    res = run(_cfg(tmp_path, triage=True))
    assert res.triage_regions is not None
    assert res.layout.weak_regions.exists() and res.layout.triage_report.exists()
    assert not FORBIDDEN_TEXT.search(res.layout.triage_report.read_text())
    assert "per_residue_dg" not in res.layout.weak_regions.read_text()


def cfg_out(cfg: TesseraConfig, which: str) -> bool:
    from pathlib import Path

    from tessera.artifacts import RunLayout

    return getattr(RunLayout(Path(cfg.out)), which).exists()
