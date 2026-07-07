"""S7 ranking / reporting / constraint-emission tests (spec §S7, §7.2).

Hand-built ScoredCandidates (confident, within-noise, low-N_eff, surface-Cys) plus
a joining CandidatesDoc exercise the confident/no-confident split, the flagged
demotion, first-wins substitution dedup, and the three emitted artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from tessera.config import ReportConfig
from tessera.report.constraints import emit_ligandmpnn_bias, emit_resfile
from tessera.report.html import write_html_report
from tessera.schemas.candidates import CandidatesDoc, MutationSet, Substitution
from tessera.schemas.common import (
    BackgroundNullSpec,
    BurialClass,
    ContactStatus,
    FeatureMode,
    OrientationClass,
    Provenance,
)
from tessera.schemas.scored import (
    CandidateFlags,
    OracleMode,
    OracleScore,
    ScoredCandidate,
)
from tessera.schemas.stats import (
    ContactGeometry,
    ContactStats,
    PairPropensity,
    StatsSummary,
)
from tessera.stages.s7_report import (
    RankedReport,
    accepted_substitutions,
    rank_candidates,
)
from tessera.types import DeltaG, Propensity


def _sc(
    cid: str,
    ddg: float,
    unc: float,
    prop: float,
    confident: bool,
    *,
    low_neff: bool = False,
    surface_cys: bool = False,
    agreement: float | None = None,
) -> ScoredCandidate:
    d = DeltaG(ddg, unc)
    return ScoredCandidate(
        candidate_id=cid,
        contact_id="c1",
        cluster_id="cl1",
        propensity=Propensity(prop),
        oracle_scores=[OracleScore(oracle="mock", mode=OracleMode.EPISTATIC_DOUBLE, ddg=d)],
        primary_ddg=d,
        oracle_sign_agreement=agreement,
        flags=CandidateFlags(
            low_neff_provenance=low_neff,
            surface_cys=surface_cys,
            within_noise=(abs(ddg) <= unc),
        ),
        confident_effect=confident,
    )


def _candidates() -> CandidatesDoc:
    """Mutation sets whose ids match the scored candidates; c_best and c_mid share
    position 4 so the first-wins dedup can be checked."""
    def _mset(mid: str, subs: list[Substitution]) -> MutationSet:
        return MutationSet(
            id=mid, substitutions=subs, propensity=Propensity(1.0),
            contact_id="c1", cluster_id="cl1", n_eff=20.0,
        )

    return CandidatesDoc(
        backbone="toy",
        feature_mode=FeatureMode.SEQUENCED,
        retrieval_used=True,
        beam_width=5,
        candidates=[
            _mset("best", [Substitution(position=4, from_aa="F", to_aa="W"),
                           Substitution(position=23, from_aa="L", to_aa="Y")]),
            _mset("mid", [Substitution(position=4, from_aa="F", to_aa="H"),   # dup pos 4
                          Substitution(position=7, from_aa="G", to_aa="A")]),
            _mset("cys", [Substitution(position=10, from_aa="S", to_aa="C")]),
            _mset("lowneff", [Substitution(position=12, from_aa="V", to_aa="I")]),
            _mset("noise", [Substitution(position=15, from_aa="T", to_aa="Y")]),
            _mset("floor", [Substitution(position=17, from_aa="R", to_aa="K")]),
        ],
    )


def _scored() -> list[ScoredCandidate]:
    return [
        # confident, clean
        _sc("best", -1.5, 0.3, 2.0, True, agreement=1.0),
        _sc("mid", -0.9, 0.4, 1.0, True, agreement=0.5),
        # confident but flagged -> demoted below clean despite more-negative ΔΔG
        _sc("cys", -2.0, 0.3, 1.5, True, surface_cys=True),
        _sc("lowneff", -1.2, 0.3, 1.5, True, low_neff=True),
        # not confident
        _sc("noise", -0.3, 0.5, 0.8, False),  # within its own uncertainty band
        _sc("floor", -0.2, 0.05, 0.8, False),  # above the -0.5 effect floor
    ]


def test_rank_splits_and_demotes() -> None:
    ranked = rank_candidates(_scored(), ReportConfig())
    rec_ids = [c.candidate_id for c in ranked.recommended]
    nce_ids = {c.candidate_id for c in ranked.no_confident_effect}

    # confident ones are recommended; within-noise / below-floor are segregated
    assert set(rec_ids) == {"best", "mid", "cys", "lowneff"}
    assert nce_ids == {"noise", "floor"}

    # clean ones sorted most-negative first, ahead of the demoted (flagged) ones
    assert rec_ids[:2] == ["best", "mid"]
    assert set(rec_ids[2:]) == {"cys", "lowneff"}  # demoted but present, not dropped
    # cys has the most-negative ΔΔG yet is demoted below both clean candidates
    assert rec_ids.index("cys") > rec_ids.index("mid")


def test_accepted_substitutions_first_wins() -> None:
    ranked = rank_candidates(_scored(), ReportConfig())
    subs = accepted_substitutions(ranked, _candidates())
    by_pos = {s.position: s for s in subs}

    # position 4 appears in both 'best' and 'mid'; best ranks first so its W wins
    assert by_pos[4].to_aa == "W"
    # union across the whole recommended set, deduped by position
    assert sorted(by_pos) == [4, 7, 10, 12, 23]
    assert len(subs) == len(by_pos)  # no duplicate positions


def test_emit_ligandmpnn_bias_jsonl(tmp_path: Path) -> None:
    subs = [Substitution(position=4, from_aa="F", to_aa="W"),
            Substitution(position=23, from_aa="L", to_aa="Y")]
    out = tmp_path / "bias" / "ligandmpnn.jsonl"
    emit_ligandmpnn_bias(subs, out)

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    for line, sub in zip(lines, subs, strict=True):
        rec = json.loads(line)
        assert isinstance(rec, dict)
        assert rec["position"] == sub.position
        assert rec["aa"] == sub.to_aa
        assert rec["bias"] == 2.0


def test_emit_resfile(tmp_path: Path) -> None:
    subs = [Substitution(position=4, from_aa="F", to_aa="W"),
            Substitution(position=23, from_aa="L", to_aa="Y")]
    out = tmp_path / "nested" / "design.resfile"
    emit_resfile(subs, out)

    text = out.read_text()
    lines = text.splitlines()
    assert lines[0] == "NATRO"
    assert "start" in lines
    assert "PIKAA" in text
    assert "4 A PIKAA W" in text
    assert "23 A PIKAA Y" in text


def _stats() -> StatsSummary:
    geom = ContactGeometry(
        d_cb=4.8, distance_bin=1, orientation=OrientationClass.ANTIPARALLEL,
        burial=BurialClass.BURIED,
    )
    return StatsSummary(
        backbone="toy",
        feature_mode=FeatureMode.SEQUENCED,
        background=BackgroundNullSpec(),
        provenance=Provenance(feature_mode=FeatureMode.SEQUENCED, mock_mode=True),
        contacts=[
            ContactStats(
                contact_id="c1", status=ContactStatus.OK, feature_mode=FeatureMode.SEQUENCED,
                n_eff=42.0, n_hits_raw=50, n_clusters=5, low_confidence=False, geometry=geom,
                top_pairs=[
                    PairPropensity(a="W", b="Y", weighted_count=10.0, frequency=0.3,
                                   background=0.05, propensity=Propensity(2.0)),
                ],
            ),
        ],
    )


def test_write_html_report(tmp_path: Path) -> None:
    ranked = rank_candidates(_scored(), ReportConfig())
    out = tmp_path / "report" / "report.html"
    write_html_report(ranked, _stats(), _candidates(), out)

    html = out.read_text()
    assert html.strip()  # non-empty
    assert "<html" in html and "</html>" in html  # valid-ish HTML document

    # ΔΔG values are shown WITH units
    assert "kcal/mol" in html
    assert "-1.50" in html  # best candidate ΔΔG
    assert "-0.90" in html  # mid candidate ΔΔG

    # propensity is explicitly labeled as NOT a ΔG
    assert "log-odds propensity, not a ΔG" in html

    # the within-noise section and its candidates are present, not dropped
    assert "No confident effect" in html
    assert "noise" in html and "floor" in html

    # per-contact context + mock provenance footer rendered
    assert "N_eff" in html
    assert "mock" in html.lower()

    # NEVER a synthesized per-residue ΔG column (§7A/D0)
    assert "per-residue" not in html.lower()


def test_rank_report_is_strict_model() -> None:
    ranked = rank_candidates(_scored(), ReportConfig())
    assert isinstance(ranked, RankedReport)
    # round-trips through pydantic with the DeltaG uncertainty preserved (§7.2)
    dumped = ranked.model_dump()
    assert dumped["recommended"][0]["primary_ddg"]["uncertainty"] == 0.3
