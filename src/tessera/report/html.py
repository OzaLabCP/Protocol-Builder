"""S7 self-contained HTML report (spec §S7, §7.2).

Renders the recommended reinforcements with their ΔΔG **in kcal/mol and with the
per-prediction uncertainty** (§7.2), and the S5 propensity **labeled as a log-odds
score, never a ΔG** (§3.1, §14.5). Candidates with no confident effect (within the
noise floor or above the minimum effect size, §7.2) get their own section. A
per-residue ΔG column is **never** rendered (§7A/D0). No external assets: all CSS is
inline and every interpolated value is HTML-escaped.
"""

from __future__ import annotations

from html import escape as _esc
from pathlib import Path

from ..schemas.candidates import CandidatesDoc, Substitution
from ..schemas.scored import CandidateFlags
from ..schemas.stats import StatsSummary
from ..schemas.weak_regions import WeakRegionsDoc
from ..stages.s7_report import RankedReport
from ..types import DeltaG, Propensity

# Label reused everywhere a propensity is shown so it can never be mistaken for an
# energy (§3.1, §14.5).
_PROP_LABEL = "log-odds propensity, not a ΔG"

_STYLE = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;color:#1a1a1a;line-height:1.4}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.25rem}
h3{font-size:1rem;margin-bottom:.25rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.9rem}
th,td{border:1px solid #ccc;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#f4f4f4}
.units{background:#eef4fb;border:1px solid #cdddef;padding:.6rem .8rem;border-radius:6px;font-size:.9rem}
.muted{color:#666;font-size:.85rem}
footer{margin-top:2rem;padding-top:.5rem;border-top:1px solid #ddd}
</style>"""


def _fmt_ddg(d: DeltaG) -> str:
    """ΔΔG with units and its uncertainty, e.g. ``-0.90 ± 0.40 kcal/mol`` (§7.2)."""
    if d.uncertainty is not None:
        return f"{d.value:.2f} ± {d.uncertainty:.2f} kcal/mol"
    return f"{d.value:.2f} kcal/mol"


def _fmt_prop(p: Propensity) -> str:
    return f"{p.value:+.2f}"


def _fmt_agreement(a: float | None) -> str:
    return "single oracle" if a is None else f"{a * 100:.0f}%"


def _fmt_flags(flags: CandidateFlags) -> str:
    names = [name for name, val in flags.model_dump().items() if val]
    return ", ".join(names) if names else "—"


def _fmt_subs(subs: list[Substitution] | None) -> str:
    if not subs:
        return "—"
    return ", ".join(f"{s.from_aa}{s.position}{s.to_aa}" for s in subs)


def _footer(stats: StatsSummary | None) -> str:
    if stats is None:
        return '<footer class="muted">Provenance unavailable (no stats summary supplied).</footer>'
    prov = stats.provenance
    mode = "mock (external tools faked — not comparable to a real run)" if prov.mock_mode else "real"
    return (
        '<footer class="muted">Provenance: '
        f"{_esc(mode)} · oracle: {_esc(prov.oracle_model)} · "
        f"feature mode: {_esc(prov.feature_mode.value)} · tessera {_esc(prov.tessera_version)}"
        "</footer>"
    )


def write_html_report(
    ranked: RankedReport,
    stats: StatsSummary | None,
    candidates: CandidatesDoc | None,
    path: str | Path,
    title: str = "Tessera reinforcement report",
) -> None:
    """Write the self-contained S7 HTML report (§S7).

    Substitutions for each row are joined from ``candidates`` by ``candidate_id``
    (the frozen ``ScoredCandidate`` schema carries only the id); ``stats`` supplies
    per-contact N_eff / top-pair context and the mock-vs-real provenance footer.
    Both are optional so the report renders from ranking alone.
    """
    subs_by_id: dict[str, list[Substitution]] = {}
    if candidates is not None:
        subs_by_id = {m.id: m.substitutions for m in candidates.candidates}

    p: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_esc(title)}</title>",
        _STYLE,
        "</head><body>",
        f"<h1>{_esc(title)}</h1>",
        '<p class="units">ΔΔG values are in <strong>kcal/mol</strong> (more negative = '
        "more stabilizing) and carry their per-prediction uncertainty (§7.2). "
        f"Propensity is a <strong>{_esc(_PROP_LABEL)}</strong> (§3.1) — the two are "
        "distinct quantities and are never summed across candidates (§7.2).</p>",
    ]

    # -- recommended reinforcements ------------------------------------------------
    p.append("<h2>Recommended reinforcements</h2>")
    if ranked.recommended:
        p.append("<table><thead><tr>")
        p.append(
            "<th>Candidate</th><th>Substitutions</th>"
            "<th>Primary ΔΔG (kcal/mol)</th>"
            f"<th>Propensity ({_esc(_PROP_LABEL)})</th>"
            "<th>Oracle agreement</th><th>Flags</th>"
        )
        p.append("</tr></thead><tbody>")
        for c in ranked.recommended:
            p.append(
                "<tr>"
                f"<td>{_esc(c.candidate_id)}</td>"
                f"<td>{_esc(_fmt_subs(subs_by_id.get(c.candidate_id)))}</td>"
                f"<td>{_esc(_fmt_ddg(c.primary_ddg))}</td>"
                f"<td>{_esc(_fmt_prop(c.propensity))}</td>"
                f"<td>{_esc(_fmt_agreement(c.oracle_sign_agreement))}</td>"
                f"<td>{_esc(_fmt_flags(c.flags))}</td>"
                "</tr>"
            )
        p.append("</tbody></table>")
    else:
        p.append("<p><em>No candidates cleared the minimum effect size (§7.2).</em></p>")

    # -- no confident effect -------------------------------------------------------
    p.append("<h2>No confident effect (within noise / below effect size)</h2>")
    if ranked.no_confident_effect:
        p.append("<table><thead><tr>")
        p.append(
            "<th>Candidate</th><th>Substitutions</th>"
            "<th>Primary ΔΔG (kcal/mol)</th>"
            f"<th>Propensity ({_esc(_PROP_LABEL)})</th><th>Flags</th>"
        )
        p.append("</tr></thead><tbody>")
        for c in ranked.no_confident_effect:
            p.append(
                "<tr>"
                f"<td>{_esc(c.candidate_id)}</td>"
                f"<td>{_esc(_fmt_subs(subs_by_id.get(c.candidate_id)))}</td>"
                f"<td>{_esc(_fmt_ddg(c.primary_ddg))}</td>"
                f"<td>{_esc(_fmt_prop(c.propensity))}</td>"
                f"<td>{_esc(_fmt_flags(c.flags))}</td>"
                "</tr>"
            )
        p.append("</tbody></table>")
    else:
        p.append("<p><em>Every scored candidate cleared the effect-size floor.</em></p>")

    # -- per-contact context (N_eff, top harvested pairs) --------------------------
    if stats is not None and stats.contacts:
        p.append("<h2>Per-contact context</h2>")
        for cs in stats.contacts:
            p.append(
                f"<h3>{_esc(cs.contact_id)} "
                f'<span class="muted">(N_eff = {cs.n_eff:.1f}, {cs.n_clusters} clusters, '
                f"{_esc(cs.status.value)})</span></h3>"
            )
            if cs.top_pairs:
                pairs = ", ".join(
                    f"{_esc(pp.a)}{_esc(pp.b)} (s = {pp.propensity.value:+.2f})"
                    for pp in cs.top_pairs[:5]
                )
                p.append(f'<p class="muted">Top harvested pairs (log-odds s): {pairs}</p>')

    p.append(_footer(stats))
    p.append("</body></html>")

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(p), encoding="utf-8")


def write_triage_report(
    doc: WeakRegionsDoc,
    path: str | Path,
    title: str = "Tessera stability-triage report",
) -> None:
    """Write the self-contained 7A triage report (§7A).

    A ranked weak-region map with converging evidence; every signal keeps its
    **native units** (frustration Z-score, count/fraction of stabilizing mutations,
    best per-mutation ΔΔG in kcal/mol, flexibility) and there is **no synthesized
    per-residue ΔG** column (§D0). Functional-exclusion flags and the proposed
    install-mode feature are surfaced per region.
    """
    prov = doc.provenance
    mode = "mock (external tools faked — not comparable to a real run)" if prov.mock_mode else "real"
    p: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_esc(title)}</title>",
        _STYLE,
        "</head><body>",
        f"<h1>{_esc(title)}</h1>",
        '<p class="units">This is a ranked <strong>weak-region map</strong> with converging '
        "evidence, not a single-number readout. Each signal keeps its native units: frustration "
        "is a <strong>Z-score</strong>, stabilizing-mutation density is a "
        "<strong>count/fraction</strong>, the best available substitution is a per-mutation "
        "<strong>ΔΔG (kcal/mol)</strong>, and flexibility is <strong>pLDDT/B-factor/RMSF</strong> "
        "(§D0). Only <strong>incidental</strong> regions (surviving the functional-frustration "
        "filter, §D5) are valid graft targets.</p>",
        "<h2>Ranked weak regions</h2>",
    ]
    if doc.regions:
        p.append("<table><thead><tr>")
        p.append(
            "<th>Region</th><th>Residues</th><th>Consensus</th>"
            "<th>Frustration (Z)</th><th>Stabilizing (n / frac / best ΔΔG)</th>"
            "<th>Flexibility</th><th>Functional</th><th>Proposed feature</th>"
        )
        p.append("</tr></thead><tbody>")
        for r in doc.regions:
            resids = _esc(", ".join(str(x) for x in r.residues))
            frus = "—" if r.frustration is None else (
                f"{r.frustration.index_z:+.2f} ({_esc(r.frustration.frustration_class.value)})"
            )
            if r.stabilizing_density is None:
                stab = "—"
            else:
                sd = r.stabilizing_density
                stab = f"{sd.n_stabilizing} / {sd.fraction_stabilizing:.2f} / {_fmt_ddg(sd.best_ddg)}"
            flex = "—" if r.flexibility is None else f"{r.flexibility.value:.1f} ({_esc(r.flexibility.source)})"
            if r.functional.excluded:
                func = f'<strong>excluded</strong>: {_esc(", ".join(r.functional.reasons))}'
            else:
                func = "incidental (valid target)"
            feat = "—" if r.proposed_feature is None else _esc(r.proposed_feature)
            p.append(
                "<tr>"
                f"<td>{_esc(r.id)}</td><td>{resids}</td><td>{r.consensus_score:.2f}</td>"
                f"<td>{_esc(frus)}</td><td>{_esc(stab)}</td><td>{flex}</td>"
                f"<td>{func}</td><td>{feat}</td>"
                "</tr>"
            )
        p.append("</tbody></table>")
    else:
        p.append("<p><em>No weak regions passed the consensus filter (§D4).</em></p>")

    p.append(
        '<footer class="muted">Provenance: '
        f"{_esc(mode)} · oracle: {_esc(prov.oracle_model)} · "
        f"feature mode: {_esc(prov.feature_mode.value)} · tessera {_esc(prov.tessera_version)}"
        "</footer>"
    )
    p.append("</body></html>")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(p), encoding="utf-8")
