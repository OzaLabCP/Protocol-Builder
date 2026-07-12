"""Render an emit_protocol payload to Markdown for export."""

from __future__ import annotations

from collections import Counter


def _safe_url(u: str) -> str:
    s = str(u or "").strip()
    return s if s.lower().startswith(("http://", "https://")) else ""


def _cite(entry: dict) -> str:
    c = entry.get("citation")
    if not c:
        return ""
    ident = c.get("identifier", "")
    url = _safe_url(c.get("url")) or (
        f"https://doi.org/{ident}" if ident.startswith("10.") else
        f"https://pubmed.ncbi.nlm.nih.gov/{ident}/" if ident.isdigit() else ""
    )
    label = f"{c.get('authors','')} {c.get('year','')} — {ident}".strip()
    link = f"[{label}]({url})" if url else label
    badge = " ✓ verified" if entry.get("citation_verified") else (
        " ⚠ unverified" if entry.get("provenance") == "literature_grounded" else ""
    )
    return f" ({link}{badge})"


def _tier(entry: dict) -> str:
    return f"`{entry.get('provenance', '')}`"


def _anchor(entry: dict) -> str:
    """A verified verbatim source quote, shown only when the host confirmed it appears
    in the source — never render an unverified model-supplied 'quote' as if from source."""
    q = entry.get("source_quote")
    if not q or not entry.get("quote_verified"):
        return ""
    # collapse newlines; neutralize Markdown table/code metachars so the exported
    # anchor displays the verbatim characters it claims to (web UI uses esc()).
    q = " ".join(str(q).split()).replace("|", r"\|").replace("`", "ˋ")
    return f' 📌 “{q}”'


def _flex_note(entry: dict) -> str:
    fx = entry.get("flexibility") if isinstance(entry, dict) else None
    return fx.strip() if isinstance(fx, str) and fx.strip() else ""


def _flag_suffix(entry: dict) -> str:
    """Inline readability flags for a bullet-rendered value: what needs a user decision
    and what has latitude. Rendered on its own indented line so it stands out."""
    bits = []
    if isinstance(entry, dict) and entry.get("needs_user_input"):
        bits.append("👤 needs your input")
    fx = _flex_note(entry)
    if fx:
        bits.append(f"🎛 flexible: {fx}")
    return ("  \n  " + " · ".join(bits)) if bits else ""


def _mat_flags(m: dict) -> str:
    """Compact single-line flags for a value inside a Markdown table cell."""
    s = ""
    if m.get("needs_user_input"):
        s += " 👤"
    fx = _flex_note(m)
    if fx:
        s += f" 🎛 {fx.replace('|', chr(92) + '|')}"
    return s


def protocol_to_markdown(p: dict) -> str:
    out: list[str] = []
    out.append(f"# {p.get('title', 'Protocol')}\n")
    if p.get("summary"):
        out.append(p["summary"] + "\n")
    if p.get("source_citation"):
        out.append(f"**Source:** {p['source_citation']}  ")
    if p.get("estimated_duration"):
        out.append(f"**Estimated duration:** {p['estimated_duration']}\n")

    # provenance summary
    tiers: Counter = Counter()
    for m in p.get("materials", []):
        if isinstance(m, dict):
            tiers[m.get("provenance")] += 1
    for s in p.get("steps", []):
        if not isinstance(s, dict):
            continue
        tiers[s.get("provenance")] += 1
        for cp in s.get("critical_parameters", []):
            if isinstance(cp, dict):
                tiers[cp.get("provenance")] += 1
    rep = p.get("validation_report") or {}
    verified = len(rep.get("verified", []))
    downgraded = len(rep.get("downgraded", []))
    summary_bits = ", ".join(f"{v} {k}" for k, v in tiers.items() if k)
    out.append(f"> **Provenance:** {summary_bits}.")
    n_user = n_flex = 0
    for m in p.get("materials", []):
        if isinstance(m, dict):
            n_user += bool(m.get("needs_user_input")); n_flex += bool(_flex_note(m))
    for s in p.get("steps", []):
        if not isinstance(s, dict):
            continue
        for e in list(s.get("critical_parameters", [])) + list(s.get("substeps", [])):
            if isinstance(e, dict):
                n_user += bool(e.get("needs_user_input")); n_flex += bool(_flex_note(e))
    if n_user or n_flex:
        out.append(f"> 👤 {n_user} value(s) need your input · 🎛 {n_flex} flexible.")
    if rep:
        out.append(f"> **Citations:** {verified} verified, {downgraded} downgraded (unverifiable).\n")
    else:
        out.append("")

    mats = p.get("materials", [])
    if mats:
        out.append("## Materials\n")
        out.append("| Reagent | Amount | Grade/vendor | Provenance |")
        out.append("| --- | --- | --- | --- |")
        for m in mats:
            if not isinstance(m, dict):
                continue
            amt = " ".join(str(x) for x in [m.get("amount"), m.get("unit")] if x not in (None, ""))
            out.append(
                f"| {m.get('name','')} | {amt or '—'} | {m.get('vendor_or_grade') or '—'} | "
                f"{_tier(m)}{_cite(m)}{_anchor(m)}{_mat_flags(m)} |"
            )
        out.append("")

    if p.get("equipment"):
        out.append("## Equipment\n")
        for e in p["equipment"]:
            out.append(f"- {e}")
        out.append("")

    out.append("## Procedure\n")
    for s in p.get("steps", []):
        if not isinstance(s, dict):
            continue
        out.append(f"### {s.get('number')}. {s.get('title','')} {_tier(s)}{_anchor(s)}\n")
        out.append(s.get("instruction", "") + "\n")
        meta = " · ".join(
            x for x in [
                f"⏱ {s.get('duration')}" if s.get("duration") else "",
                f"🌡 {s.get('temperature')}" if s.get("temperature") else "",
            ] if x
        )
        if meta:
            out.append(meta + "\n")
        for cp in s.get("critical_parameters", []):
            if not isinstance(cp, dict):
                continue
            val = " ".join(str(x) for x in [cp.get("value"), cp.get("unit")] if x not in (None, ""))
            note = f" — {cp['provenance_note']}" if cp.get("provenance_note") else ""
            out.append(f"- **{cp.get('name','')}:** {val} {_tier(cp)}{_cite(cp)}{_anchor(cp)}{note}{_flag_suffix(cp)}")
        for ss in s.get("substeps", []):
            if not isinstance(ss, dict):
                continue
            out.append(f"  - {ss.get('number','')} {ss.get('instruction','')} {_tier(ss)}{_anchor(ss)}{_flag_suffix(ss)}")
        for w in s.get("warnings", []):
            out.append(f"> ⚠ {w}")
        out.append("")

    ts = p.get("titration_series")
    if isinstance(ts, dict) and (ts.get("points") or []):
        var = ts.get("variable", "condition")
        unit = f" ({ts['unit']})" if ts.get("unit") else ""
        out.append(f"## Titration series — {var}{unit} {_tier(ts)}\n")
        meta = " · ".join(x for x in [
            f"spacing: {ts['spacing']}" if ts.get("spacing") else "",
            ts.get("rationale", ""),
        ] if x)
        if meta:
            out.append(meta + "\n")
        # Column set = label, target, then the union of component names in order seen.
        comp_names: list[str] = []
        for pt in ts["points"]:
            for c in pt.get("components", []):
                nm = c.get("name", "")
                if nm and nm not in comp_names:
                    comp_names.append(nm)
        header = ["Condition", "Target"] + comp_names
        out.append("| " + " | ".join(header) + " |")
        out.append("| " + " | ".join(["---"] * len(header)) + " |")
        for pt in ts["points"]:
            vols = {c.get("name", ""): c.get("volume") for c in pt.get("components", [])}
            row = [pt.get("label", ""), pt.get("target_concentration") or "—"]
            row += [str(vols.get(nm) or "—") for nm in comp_names]
            out.append("| " + " | ".join(row) + " |")
        out.append("")

    alog = p.get("assumptions_log", [])
    if alog:
        out.append("## Assumptions log — everything not stated in the source\n")
        out.append("| Parameter | Value | Provenance | Basis |")
        out.append("| --- | --- | --- | --- |")
        for a in alog:
            verify = " · **verify**" if a.get("verify") else ""
            out.append(
                f"| {a.get('parameter','')} | {a.get('value','')} | {_tier(a)}{_cite(a)} | "
                f"{a.get('basis','')}{verify} |"
            )
        out.append("")

    if p.get("open_questions"):
        out.append("## Open questions\n")
        for q in p["open_questions"]:
            out.append(f"- {q}")
        out.append("")

    out.append("\n---\n*Generated by Methods Gap-Filler. Every value is provenance-tagged; "
               "`default_verify` items must be confirmed before use.*")
    return "\n".join(out)


def assay_selection_to_markdown(chosen: dict, opts: dict) -> str:
    """Prepended to the export on the hypothesis-first path so the record shows which
    assay was chosen, why, and what alternatives were considered."""
    out: list[str] = ["# Assay selection\n"]
    if opts.get("hypothesis_restated"):
        out.append(f"**Hypothesis (falsifiable restatement):** {opts['hypothesis_restated']}\n")
    if chosen:
        out.append(f"**Chosen assay:** {chosen.get('name', '')} {_tier(chosen)}{_cite(chosen)}")
        rows = [
            ("Measures", chosen.get("measures")),
            ("Why it tests the hypothesis", chosen.get("why_tests_hypothesis")),
            ("Critical comparison", chosen.get("critical_comparison")),
            ("Throughput / difficulty", " / ".join(
                x for x in [chosen.get("throughput"), chosen.get("difficulty")] if x)),
            ("Turnaround", chosen.get("turnaround")),
            ("Materials burden", chosen.get("materials_burden")),
            ("Equipment burden", chosen.get("equipment_burden")),
            ("Key limitation", chosen.get("key_limitation")),
        ]
        for label, val in rows:
            if val:
                out.append(f"- **{label}:** {val}")
        out.append("")
    others = [a for a in (opts.get("assays") or []) if a is not chosen and a.get("id") != (chosen or {}).get("id")]
    if others:
        out.append("**Alternatives considered:**")
        for a in others:
            out.append(f"- {a.get('name', '')} — {a.get('key_limitation', '')}{_cite(a)}")
        out.append("")
    if opts.get("recommendation_rationale"):
        out.append(f"> **Why this pick:** {opts['recommendation_rationale']}")
    return "\n".join(out)


def grounding_log_to_markdown(log: list) -> str:
    """The raw grounding search trail, as an export appendix."""
    if not log:
        return ""
    out = ["# Appendix: grounding search trail\n",
           "The literature searches the tool ran while grounding values:\n"]
    for q in log:
        out.append(f"- `{q}`")
    return "\n".join(out)


def _csv_safe(v) -> str:
    """Neutralize spreadsheet formula injection: a cell starting with =,+,-,@ (or a
    control char Excel treats as a formula lead-in) is prefixed with a quote."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def materials_to_csv(p: dict) -> str:
    """Reproducibility-grade materials table as CSV (provenance travels as columns)."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reagent", "amount", "unit", "vendor_or_grade",
                "provenance", "citation_identifier", "citation_verified", "note"])
    for m in p.get("materials") or []:
        if not isinstance(m, dict):
            continue
        c = m.get("citation") or {}
        w.writerow([_csv_safe(x) for x in [
            m.get("name", ""),
            "" if m.get("amount") in (None, "") else m.get("amount"),
            m.get("unit") or "",
            m.get("vendor_or_grade") or "",
            m.get("provenance") or "",
            c.get("identifier", "") if c else "",
            "yes" if m.get("citation_verified") else "",
            m.get("provenance_note") or "",
        ]])
    return buf.getvalue()


_VERDICT_LABEL = {"yes": "✓ Directly tests the hypothesis",
                  "partial": "◐ Partially tests the hypothesis",
                  "no": "✗ Does not directly test the hypothesis"}


def design_alignment_to_markdown(a: dict) -> str:
    out: list[str] = ["# Does this test your hypothesis?\n"]
    h = a.get("hypothesis") or {}
    if h.get("statement"):
        tag = " *(inferred)*" if a.get("inferred") else ""
        out.append(f"**Hypothesis{tag}:** {h['statement']}")
        if h.get("prediction_if_true"):
            out.append(f"- If true → {h['prediction_if_true']}")
        if h.get("prediction_if_false"):
            out.append(f"- If false → {h['prediction_if_false']}")
        out.append("")

    dt = a.get("directly_tests") or {}
    if dt:
        out.append(f"**Verdict:** {_VERDICT_LABEL.get(dt.get('verdict'), dt.get('verdict',''))}")
        if dt.get("rationale"):
            out.append(f"> {dt['rationale']}")
        out.append("")

    if a.get("critical_comparison"):
        out.append(f"**The comparison that makes it a valid test:** {a['critical_comparison']}\n")

    gaps = a.get("alignment_gaps") or []
    if gaps:
        out.append("## Where it falls short of testing the hypothesis\n")
        for g in gaps:
            out.append(f"- **{g.get('gap','')}** — {g.get('why_it_breaks_the_test','')}")
        out.append("")

    conf = a.get("confounds") or []
    if conf:
        out.append("## Confounds that would make a positive result ambiguous\n")
        for c in conf:
            mit = f" *Mitigation:* {c['mitigation']}" if c.get("mitigation") else ""
            out.append(f"- **{c.get('confound','')}** — {c.get('makes_result_ambiguous','')}.{mit}")
        out.append("")

    rc = a.get("recommended_changes") or []
    if rc:
        out.append("## Recommended protocol changes (to make it a direct test)\n")
        for c in rc:
            out.append(f"- **[{c.get('type','change')}]** {c.get('change','')}  \n"
                       f"  *Addresses:* {c.get('addresses','')}")
        out.append("")

    if a.get("summary"):
        out.append(f"**Bottom line:** {a['summary']}")
    return "\n".join(out)


_CORRECTNESS_VERDICT = {
    "sound": "✅ Sound — no correctness defects found",
    "issues_found": "⚠️ Issues found",
    "serious_issues": "🛑 Serious issues",
}
_CORRECTNESS_SEV = {"critical": "🔴 Critical", "major": "🟠 Major", "minor": "🟡 Minor"}


def correctness_review_to_markdown(r: dict) -> str:
    out: list[str] = ["# Correctness & practicality review (adversarial)\n"]
    out.append("_Model-generated audit — a skeptical pass for logic, value, ordering, and "
               "control errors, and for practicality (missing detail, unclear or impractical "
               "steps). This is reasoning, not a host-verified guarantee; only the cited "
               "evidence below is checked against a database._\n")
    out.append(f"**Verdict:** {_CORRECTNESS_VERDICT.get(r.get('verdict'), r.get('verdict', ''))}")
    if r.get("summary"):
        out.append(f"> {r['summary']}")
    out.append("")

    findings = r.get("findings") or []
    if findings:
        out.append("## Findings (most severe first)\n")
        for f in findings:
            if not isinstance(f, dict):
                continue
            sev = _CORRECTNESS_SEV.get(f.get("severity"), f.get("severity", ""))
            loc = f.get("location")
            cat = f.get("category")
            head = f"### {sev}" + (f" — {loc}" if loc else "")
            if cat:
                head += f"  _({cat})_"
            out.append(head)
            out.append(f"- **Problem:** {f.get('problem','')}")
            out.append(f"- **Fix:** {f.get('fix','')}")
            c = f.get("citation")
            if c:
                badge = " ✓" if f.get("citation_verified") else ""
                out.append(f"- **Evidence:** {c.get('identifier','')}{badge}")
            out.append("")
    else:
        out.append("_No correctness defects found in this audit._\n")

    strengths = r.get("strengths") or []
    if strengths:
        out.append("## Strengths\n")
        for s in strengths:
            out.append(f"- {s}")
        out.append("")
    return "\n".join(out)


def design_review_to_markdown(d: dict) -> str:
    out: list[str] = ["# Experiment design review\n"]
    if d.get("question"):
        out.append(f"**Question:** {d['question']}\n")
    if d.get("hypothesis"):
        out.append(f"**Hypothesis:** {d['hypothesis']}\n")

    v = d.get("variables") or {}
    if v:
        out.append("## Variables\n")
        for k in ("independent", "dependent", "controlled"):
            vals = v.get(k) or []
            out.append(f"- **{k.capitalize()}:** {', '.join(vals) if vals else '—'}")
        out.append("")

    controls = d.get("controls") or []
    if controls:
        out.append("## Controls — and what each rules out\n")
        out.append("| Control | Type | Rules out | Provenance |")
        out.append("| --- | --- | --- | --- |")
        for c in controls:
            out.append(
                f"| {c.get('name','')} | {c.get('type','')} | {c.get('rules_out','')} | "
                f"{_tier(c)}{_cite(c)} |"
            )
        out.append("")

    r = d.get("readout") or {}
    if r:
        out.append("## Readout adequacy\n")
        ok = "✓ tests the hypothesis" if r.get("answers_question") else "⚠ may not test the hypothesis"
        out.append(f"Measures: {r.get('measures','')} — **{ok}**.")
        if r.get("caveat"):
            out.append(f"> Caveat: {r['caveat']}")
        out.append("")

    rep = d.get("replication") or {}
    if rep:
        out.append("## Replication\n")
        bio = rep.get("biological"); tech = rep.get("technical")
        if bio:
            out.append(f"- Biological replicates: {bio}")
        if tech:
            out.append(f"- Technical replicates: {tech}")
        out.append(f"- Rationale: {rep.get('rationale','')}\n")

    er = d.get("expected_results") or []
    if er:
        out.append("## Expected results — if you see X, it means Y\n")
        for e in er:
            out.append(f"- **{e.get('scenario','')}** → {e.get('interpretation','')}")
        out.append("")

    fm = d.get("failure_modes") or []
    if fm:
        out.append("## Failure modes\n")
        out.append("| Symptom | Likely cause | Check |")
        out.append("| --- | --- | --- |")
        for f in fm:
            out.append(f"| {f.get('symptom','')} | {f.get('likely_cause','')} | {f.get('check') or '—'} |")
        out.append("")

    lim = d.get("interpretation_limits") or []
    if lim:
        out.append("## What this experiment cannot conclude\n")
        for x in lim:
            out.append(f"- {x}")
        out.append("")

    gaps = d.get("design_gaps") or []
    if gaps:
        out.append("## Design gaps to fix in this plan\n")
        for g in gaps:
            out.append(f"- {g}")
        out.append("")

    return "\n".join(out)
