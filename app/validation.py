"""Host-side validation — the step that turns "the model says it grounded this"
into "the tool verified it." Runs after emit_protocol, before rendering.

Three checks from the spec:
  1. Citation resolution: every literature_grounded citation is resolved against a
     real source; unresolvable/mismatched ones are downgraded to default_verify.
  2. Provenance/citation invariant: literature_grounded with a null citation is a
     fabrication guard -> downgrade; a citation on a non-literature_grounded value
     that the user did not select is stripped.
  3. assumptions_log <-> inline consistency: parameters present in one but not the
     other are surfaced (not silently trusted).

The resolver is injected so this module is unit-testable without the network.
"""

from __future__ import annotations

import re
from typing import Callable, Iterator, Optional

from .resolvers import ResolvedCitation, resolve_citation

Resolver = Callable[[str], Optional[ResolvedCitation]]

_WORD_RE = re.compile(r"[a-z0-9]+")


def _title_overlap(a: str, b: str) -> float:
    ta = set(_WORD_RE.findall((a or "").lower()))
    tb = set(_WORD_RE.findall((b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _norm_param(name: str) -> str:
    return " ".join(_WORD_RE.findall((name or "").lower()))


_WS_RE = re.compile(r"\s+")
_HYPHEN_BREAK_RE = re.compile(r"-\s*\n\s*")


def _normalize_source(text: str) -> str:
    """Lowercase and collapse whitespace so a quote matches across line breaks and
    inconsistent spacing, and rejoin words hyphenated across a line break (common in
    extracted PDF text: 'incu-\\nbated' -> 'incubated')."""
    t = _HYPHEN_BREAK_RE.sub("", text or "")
    return _WS_RE.sub(" ", t.lower()).strip()


def _quote_matches(quote: str, normalized_source: str) -> bool:
    """True if the (verbatim-ish) quote occurs in the already-normalized source. The
    quote is normalized the same way and stripped of surrounding quote marks/ellipses;
    quotes shorter than 4 chars are rejected as too weak to anchor anything (the
    value-support check below supplies the real specificity)."""
    q = _normalize_source(quote).strip("\"'“”‘’ .…")
    if len(q) < 4:
        return False
    return q in normalized_source


def _num_str(v) -> str:
    """Render a numeric value without a spurious trailing '.0' so it matches how the
    source is likely to write it (50, not 50.0)."""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def _value_tokens_present(entry: dict, text: str) -> bool:
    """True iff every scalar value token on the entry (its `value`/`amount`) occurs as a
    whole token in the already-normalized `text`. Entries with no scalar value pass
    vacuously. Shared by `_quote_supports` (source quote) and `_evidence_relevant`
    (retrieved excerpt)."""
    for key in ("value", "amount"):
        v = entry.get(key)
        if v in (None, ""):
            continue
        token = _normalize_source(_num_str(v))
        if token and not re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text):
            return False
    return True


def _quote_supports(entry: dict, quote: str) -> bool:
    """A verified quote must actually CONTAIN the value it anchors — the quote merely
    occurring in the source is not enough (a real but unrelated sentence would otherwise
    launder a wrong number into a 'source-anchored' badge). Entries with no scalar value
    to check (prose steps/substeps) pass this gate on quote presence alone."""
    return _value_tokens_present(entry, _normalize_source(quote))


def _evidence_relevant(entry: dict, evidence) -> bool:
    """True iff the model-attached `evidence` excerpt actually addresses THIS value —
    the gate that separates a resolvable-but-unrelated citation (metadata_matched) from
    genuine excerpt support (claim_support_status == "supported"). Metadata-only evidence
    can never support a claim; a scalar value's number must appear in the excerpt; a prose
    value needs one of its significant name words to appear."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("evidence_type") == "metadata_only":
        return False
    excerpt = _normalize_source(evidence.get("excerpt") or "")
    if len(excerpt) < 8:
        return False
    if any(entry.get(k) not in (None, "") for k in ("value", "amount")):
        return _value_tokens_present(entry, excerpt)
    name = entry.get("name") or entry.get("parameter") or ""
    sig = [w for w in _WORD_RE.findall(name.lower()) if len(w) >= 4]
    if not sig:
        return True
    return any(w in excerpt for w in sig)


def _iter_citation_entries(protocol: dict) -> Iterator[tuple[dict, str]]:
    """Yield (entry, human-readable location) for every provenance-bearing value the
    citation invariants apply to. Steps/substeps/titration have no citation field in the
    schema, so including them here is what enforces the invariant that a step tagged
    'literature_grounded' with no citation gets downgraded (otherwise an unverifiable
    grounding badge would ship on a step, unchecked)."""
    for i, mat in enumerate(protocol.get("materials") or []):
        yield mat, f"materials[{i}] '{mat.get('name', '?')}'"
    for step in protocol.get("steps") or []:
        snum = step.get("number", "?")
        yield step, f"step {snum} '{step.get('title', '?')}'"
        for j, cp in enumerate(step.get("critical_parameters") or []):
            yield cp, f"step {snum} critical_parameters[{j}] '{cp.get('name', '?')}'"
        for k, ss in enumerate(step.get("substeps") or []):
            yield ss, f"step {snum} substeps[{k}] '{ss.get('number', '?')}'"
    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        yield ts, "titration_series"
    for k, a in enumerate(protocol.get("assumptions_log") or []):
        yield a, f"assumptions_log[{k}] '{a.get('parameter', '?')}'"


def _iter_all_provenance_entries(protocol: dict) -> Iterator[tuple[dict, str]]:
    """Yield (entry, location) for EVERY provenance-bearing location, including step
    and substep provenance that _iter_citation_entries deliberately omits. Used by the
    no-source pre-pass so a stray "stated" tier cannot hide in a step or substep."""
    for i, mat in enumerate(protocol.get("materials") or []):
        yield mat, f"materials[{i}] '{mat.get('name', '?')}'"
    for step in protocol.get("steps") or []:
        snum = step.get("number", "?")
        yield step, f"step {snum} '{step.get('title', '?')}'"
        for j, cp in enumerate(step.get("critical_parameters") or []):
            yield cp, f"step {snum} critical_parameters[{j}] '{cp.get('name', '?')}'"
        for k, ss in enumerate(step.get("substeps") or []):
            yield ss, f"step {snum} substeps[{k}] '{ss.get('number', '?')}'"
    ts = protocol.get("titration_series")
    if isinstance(ts, dict):
        yield ts, "titration_series"
    for m, a in enumerate(protocol.get("assumptions_log") or []):
        yield a, f"assumptions_log[{m}] '{a.get('parameter', '?')}'"


def _downgrade(entry: dict, note: str) -> None:
    entry["provenance"] = "default_verify"
    entry["citation"] = None
    # `citation_verified` is a DERIVED back-compat alias meaning "the cited identifier
    # resolved to the cited work". It is NOT a claim of semantic support — that is
    # `claim_support_status`. A downgraded entry has metadata_matched False (except the
    # mismatch case where the citation is nulled), so the alias renders no misleading badge.
    entry["citation_verified"] = bool(entry.get("identifier_verified") and entry.get("metadata_matched"))
    entry["quote_verified"] = False  # a downgraded value is not source-anchored
    if "verify" in entry or "basis" in entry:  # assumptions_log shape
        entry["verify"] = True
    existing = entry.get("provenance_note") or ""
    entry["provenance_note"] = (existing + " " if existing else "") + f"[VALIDATION] {note}"


def _sanitize_protocol(protocol: dict) -> int:
    """Drop non-dict entries the model may emit in list fields (materials, steps, each
    step's critical_parameters/substeps, assumptions_log) so downstream .get() calls
    can't crash. Also coerces a non-dict titration_series to None. Mutates in place;
    returns how many malformed items were removed."""
    dropped = 0
    for key in ("materials", "steps", "assumptions_log"):
        v = protocol.get(key)
        if isinstance(v, list):
            clean = [e for e in v if isinstance(e, dict)]
            dropped += len(v) - len(clean)
            protocol[key] = clean
        elif v is not None:
            dropped += 1
            protocol[key] = []
    for step in protocol.get("steps") or []:
        for sub in ("critical_parameters", "substeps"):
            v = step.get(sub)
            if isinstance(v, list):
                clean = [e for e in v if isinstance(e, dict)]
                dropped += len(v) - len(clean)
                step[sub] = clean
            elif v is not None:
                dropped += 1
                step[sub] = []
    if "titration_series" in protocol and not isinstance(protocol.get("titration_series"), dict):
        if protocol.get("titration_series") is not None:
            dropped += 1
        protocol["titration_series"] = None
    return dropped


def validate_and_finalize(
    protocol: dict,
    resolver: Resolver = resolve_citation,
    *,
    allow_stated: bool = True,
    source_text: Optional[str] = None,
    source_exact: bool = True,
) -> dict:
    """Validate and repair the protocol in place. Returns a report dict; the
    protocol argument is mutated (citations verified/nulled, tiers downgraded,
    open_questions appended).

    When allow_stated is False (hypothesis-first: no source document), a pre-pass
    downgrades any value tagged "stated" to default_verify — the "stated" tier is a
    claim about a source paper, and there is none. This is keyed by the caller on
    session.source_kind (set in code), not on any pasteable text, so it is paste-proof."""
    report = {
        "citations_checked": 0,
        "verified": [],
        "downgraded": [],
        "invariant_fixes": [],
        "stated_downgrades": [],
        "malformed_dropped": 0,
        "quotes": {"verified": [], "downgraded": [], "unverified": [], "source_checked": False},
        "consistency": {"inline_missing_from_log": [], "log_missing_from_inline": []},
        "support": {"supported": [], "evidence_unavailable": [], "mismatch": []},
    }
    open_questions = list(protocol.get("open_questions") or [])

    # A model can emit a non-object where a structured entry is expected (e.g. a bare
    # string in `materials`). Drop those first so the provenance iterators — and the
    # renderer — never call .get() on a str. This is the untrusted-emit boundary.
    dropped = _sanitize_protocol(protocol)
    if dropped:
        report["malformed_dropped"] = dropped
        open_questions.append(
            f"{dropped} malformed protocol item(s) (non-object entries in a list field) were "
            f"dropped from the model's output; regenerate if the protocol looks incomplete."
        )

    # SECURITY: quote_verified is a HOST-ONLY attestation. Never trust a model-supplied
    # value — strip it from every entry so only the verification pass below can set it.
    # (Item schemas are open, dict(block.input) is used verbatim, and the badge is the
    # tool's whole trust signal, so a forged quote_verified must not survive.)
    for entry, _loc in _iter_all_provenance_entries(protocol):
        for k in ("quote_verified", "identifier_verified", "metadata_matched",
                  "claim_support_status", "citation_verified"):
            entry.pop(k, None)

    if not allow_stated:
        for entry, location in _iter_all_provenance_entries(protocol):
            if entry.get("provenance") == "stated":
                _downgrade(entry, "tagged stated but this protocol has no source document.")
                report["stated_downgrades"].append(location)
                open_questions.append(
                    f"{location}: tagged 'stated' but there is no source document "
                    f"(hypothesis-first draft); downgraded to default_verify."
                )
        # source_citation is a free-string claim of a source paper; there is none here.
        if protocol.get("source_citation"):
            report["stated_downgrades"].append("source_citation")
            open_questions.append(
                "source_citation: cleared — this protocol was drafted from a hypothesis, "
                "not a source paper."
            )
            protocol["source_citation"] = None

    # Source-quote anchoring: verify that every "stated" value's quote actually appears
    # in the source AND contains the value it anchors. Only possible when we retained the
    # source text (paste, or a PDF we could extract); without it, quotes are never trusted.
    #
    # source_exact distinguishes an AUTHORITATIVE source (pasted text is exactly what the
    # model read -> a mismatch is a real paraphrase/fabrication, so downgrade) from a
    # LOSSY one (host-extracted PDF text != the model's native read: reflowed columns,
    # dropped units/ligatures -> a mismatch is inconclusive, so confirm-only, never
    # accuse). This keeps the teeth where they're reliable without libelling correct
    # PDF-sourced values.
    if source_text and str(source_text).strip():
        report["quotes"]["source_checked"] = True
        norm_source = _normalize_source(source_text)
        for entry, location in _iter_all_provenance_entries(protocol):
            if entry.get("provenance") != "stated":
                continue
            quote = (entry.get("source_quote") or "").strip()
            ok = bool(quote) and _quote_matches(quote, norm_source) and _quote_supports(entry, quote)
            if ok:
                entry["quote_verified"] = True
                report["quotes"]["verified"].append(location)
            elif source_exact:
                reason = ("no source_quote to anchor it" if not quote
                          else "its source_quote was not found verbatim in the source")
                _downgrade(entry, f"tagged stated but {reason}.")
                report["quotes"]["downgraded"].append(location)
                open_questions.append(
                    f"{location}: tagged 'stated' but {reason}; downgraded to default_verify."
                )
            else:
                # lossy source: cannot confirm, but a non-match is not proof of fabrication
                report["quotes"]["unverified"].append(location)

    for entry, location in _iter_citation_entries(protocol):
        prov = entry.get("provenance")
        citation = entry.get("citation")
        selected = bool(entry.get("selected_by_user"))

        # Host-owned attestations, initialized before branching so every entry carries a
        # defined value. `identifier_verified` = the DOI/PMID resolved; `metadata_matched`
        # = the resolved title/year align; `claim_support_status` (unchecked | mismatch |
        # evidence_unavailable | supported) = whether a retrieved excerpt actually supports
        # THIS value. A resolvable identifier with matching metadata is NOT support.
        entry["identifier_verified"] = False
        entry["metadata_matched"] = False
        entry["claim_support_status"] = "unchecked"

        # --- Invariant: literature_grounded MUST carry a citation. ---
        if prov == "literature_grounded" and not citation:
            _downgrade(entry, "claimed literature_grounded but carried no citation.")
            report["invariant_fixes"].append(
                {"location": location, "fix": "downgraded (no citation)"}
            )
            open_questions.append(
                f"{location}: claimed literature-grounded with no citation; "
                f"downgraded to default_verify."
            )
            continue

        # --- Invariant: a citation on a non-literature_grounded value the user
        # did not select is illegitimate -> strip it. ---
        if prov != "literature_grounded" and citation and not selected:
            entry["citation"] = None
            report["invariant_fixes"].append(
                {"location": location, "fix": "stripped stray citation"}
            )
            continue

        if not citation:
            continue

        # --- Resolution: verify the identifier against a real source. ---
        identifier = str(citation.get("identifier", "")).strip()
        report["citations_checked"] += 1
        try:
            resolved = resolver(identifier)
        except Exception as exc:  # network failure -> treat as unresolved
            resolved = None
            resolve_error = str(exc)
        else:
            resolve_error = None

        if resolved is None:
            reason = (
                "citation identifier did not resolve"
                + (f" ({resolve_error})" if resolve_error else "")
            )
            _downgrade(entry, reason + "; downgraded to default_verify.")
            report["downgraded"].append(
                {"location": location, "identifier": identifier, "reason": reason}
            )
            open_questions.append(
                f"{location}: citation '{identifier}' could not be verified "
                f"({reason}); value downgraded to default_verify."
            )
            continue

        # Resolved — confirm year and title are consistent.
        claimed_year = citation.get("year")
        year_mismatch = (
            claimed_year is not None
            and resolved.year is not None
            and int(claimed_year) != int(resolved.year)
        )
        overlap = _title_overlap(citation.get("title", ""), resolved.title)
        title_mismatch = overlap < 0.34

        if year_mismatch or title_mismatch:
            bits = []
            if year_mismatch:
                bits.append(f"year {claimed_year} vs source {resolved.year}")
            if title_mismatch:
                bits.append(f"title overlap {overlap:.0%} with source '{resolved.title}'")
            reason = "citation metadata mismatch (" + "; ".join(bits) + ")"
            # The identifier resolved to a real record, but to a clearly different work.
            entry["identifier_verified"] = True
            entry["metadata_matched"] = False
            _downgrade(entry, reason + "; downgraded to default_verify.")
            # Set AFTER the downgrade so the "resolves to a different work" diagnostic
            # survives even though provenance is now default_verify and citation is nulled.
            entry["claim_support_status"] = "mismatch"
            report["support"]["mismatch"].append(location)
            report["downgraded"].append(
                {"location": location, "identifier": identifier, "reason": reason}
            )
            open_questions.append(
                f"{location}: citation '{identifier}' resolved but metadata "
                f"mismatched ({'; '.join(bits)}); downgraded to default_verify."
            )
            continue

        # Verified: identifier resolved AND metadata matches. This earns the
        # `literature_grounded` tier and the derived `citation_verified` alias, but NOT
        # `supported` unless an attached excerpt actually addresses the value.
        entry["identifier_verified"] = True
        entry["metadata_matched"] = True
        entry["citation_verified"] = True
        entry["citation"]["url"] = entry["citation"].get("url") or _canonical_url(resolved)
        ev = (entry.get("citation") or {}).get("evidence")
        if _evidence_relevant(entry, ev):
            entry["claim_support_status"] = "supported"
            report["support"]["supported"].append(location)
        else:
            # Identifier + metadata are genuinely verified; the value simply carries no
            # excerpt-support badge. It STAYS literature_grounded.
            entry["claim_support_status"] = "evidence_unavailable"
            report["support"]["evidence_unavailable"].append(location)
        report["verified"].append(
            {
                "location": location,
                "identifier": identifier,
                "title": resolved.title,
                "year": resolved.year,
                "source": resolved.source,
            }
        )

    _consistency_check(protocol, report, open_questions)
    protocol["open_questions"] = open_questions
    protocol["validation_report"] = report
    return report


def _canonical_url(resolved: ResolvedCitation) -> Optional[str]:
    if resolved.kind == "pmid":
        return f"https://pubmed.ncbi.nlm.nih.gov/{resolved.identifier}/"
    if resolved.kind == "doi":
        return f"https://doi.org/{resolved.identifier}"
    return None


def _consistency_check(protocol: dict, report: dict, open_questions: list) -> None:
    """Surface parameters that appear inline but not in the assumptions_log (or
    vice versa). Informational — we flag, we don't silently trust either copy."""
    inline_names: set[str] = set()
    for mat in protocol.get("materials") or []:
        if mat.get("provenance") not in (None, "stated"):
            inline_names.add(_norm_param(mat.get("name", "")))
    for step in protocol.get("steps") or []:
        for cp in step.get("critical_parameters") or []:
            if cp.get("provenance") not in (None, "stated"):
                inline_names.add(_norm_param(cp.get("name", "")))

    log_names = {
        _norm_param(a.get("parameter", "")) for a in protocol.get("assumptions_log") or []
    }

    inline_names.discard("")
    log_names.discard("")

    inline_missing = sorted(inline_names - log_names)
    log_missing = sorted(log_names - inline_names)
    report["consistency"]["inline_missing_from_log"] = inline_missing
    report["consistency"]["log_missing_from_inline"] = log_missing

    for name in inline_missing:
        open_questions.append(
            f"Consistency: '{name}' is filled inline but missing from the "
            f"assumptions_log (the log must be exhaustive)."
        )
    # log-only entries are allowed (step-level assumptions) but still worth noting
    for name in log_missing:
        open_questions.append(
            f"Consistency: assumptions_log lists '{name}' with no matching inline "
            f"material or critical parameter — confirm it is applied somewhere."
        )


def check_citation(citation: dict, resolver: Resolver = resolve_citation):
    """Resolve one citation and confirm year/title. Returns (ok, resolved, reason)."""
    identifier = str(citation.get("identifier", "")).strip()
    try:
        resolved = resolver(identifier)
    except Exception as exc:  # network failure -> unresolved
        return False, None, f"did not resolve ({exc})"
    if resolved is None:
        return False, None, "citation identifier did not resolve"
    claimed_year = citation.get("year")
    year_mismatch = (
        claimed_year is not None and resolved.year is not None
        and int(claimed_year) != int(resolved.year)
    )
    overlap = _title_overlap(citation.get("title", ""), resolved.title)
    if year_mismatch or overlap < 0.34:
        bits = []
        if year_mismatch:
            bits.append(f"year {claimed_year} vs source {resolved.year}")
        if overlap < 0.34:
            bits.append(f"title overlap {overlap:.0%}")
        return False, resolved, "metadata mismatch (" + "; ".join(bits) + ")"
    return True, resolved, ""


def validate_design_review(review: dict, resolver: Resolver = resolve_citation) -> dict:
    """Verify grounded controls in a design review: resolve each control's citation,
    keep verified ones (with a canonical url), and downgrade unverifiable ones to
    best_practice. Mutates `review`; returns a small report."""
    report = {"citations_checked": 0, "verified": [], "downgraded": []}
    for ctrl in review.get("controls") or []:
        prov = ctrl.get("provenance")
        cit = ctrl.get("citation")
        name = ctrl.get("name", "?")

        if prov == "literature_grounded" and not cit:
            ctrl["provenance"] = "best_practice"
            ctrl["citation"] = None
            ctrl["citation_verified"] = False
            report["downgraded"].append({"control": name, "reason": "no citation"})
            continue
        if prov != "literature_grounded" and cit:
            ctrl["citation"] = None  # stray citation on a non-grounded control
            continue
        if not cit:
            continue

        report["citations_checked"] += 1
        ok, resolved, reason = check_citation(cit, resolver)
        if ok:
            ctrl["citation_verified"] = True
            ctrl["citation"]["url"] = cit.get("url") or (_canonical_url(resolved) if resolved else None)
            report["verified"].append({"control": name, "identifier": cit.get("identifier")})
        else:
            ctrl["provenance"] = "best_practice"
            ctrl["citation"] = None
            ctrl["citation_verified"] = False
            report["downgraded"].append(
                {"control": name, "identifier": cit.get("identifier"), "reason": reason}
            )

    review["validation_report"] = report
    return report


_ALIGN_VERDICTS = {"yes", "partial", "no"}
_CHANGE_TYPES = {
    "add_control", "add_condition", "add_comparison",
    "change_readout", "increase_replication", "other",
}


def validate_design_alignment(alignment: dict, *, hypothesis_supplied: bool = False) -> dict:
    """Host-side integrity pass for a hypothesis-alignment assessment.

    Unlike the protocol/review/assay passes there are no citations to resolve here, so
    this normalizes the model's structured claims instead of trusting them verbatim:
      - `inferred` is set from HOST truth (did the caller actually supply a hypothesis?),
        never from the model — it drives the "(inferred)" label the student sees, so the
        model must not be able to pass a student-stated hypothesis off as inferred, or
        vice versa.
      - enum fields (the verdict, each change's `type`) are coerced to allowed values so a
        stray token can't leak into the export or the one-click-apply control.
      - internal contradictions (a non-passing verdict that names no gaps; a "yes" verdict
        that still lists gaps or unmitigated confounds) are surfaced, not silently kept.

    Mutates `alignment`; returns a small report (also attached as validation_report)."""
    report = {"normalized": [], "inconsistencies": [], "inferred_corrected": False}

    # Host-truth `inferred`: the model doesn't get to decide this.
    truth = not hypothesis_supplied
    if bool(alignment.get("inferred")) != truth:
        report["inferred_corrected"] = True
    alignment["inferred"] = truth

    # Verdict enum -> coerce an out-of-range value to the conservative "partial".
    dt = alignment.get("directly_tests")
    if not isinstance(dt, dict):
        dt = {}
        alignment["directly_tests"] = dt
    verdict = dt.get("verdict")
    if verdict not in _ALIGN_VERDICTS:
        dt["verdict"] = "partial"
        report["normalized"].append(f"directly_tests.verdict '{verdict}' -> 'partial'")
        verdict = "partial"

    # Defensive list coercion + change-type enum.
    for key in ("alignment_gaps", "confounds", "recommended_changes"):
        if not isinstance(alignment.get(key), list):
            alignment[key] = []
    for i, ch in enumerate(alignment["recommended_changes"]):
        if not isinstance(ch, dict):
            continue
        t = ch.get("type")
        if t is not None and t not in _CHANGE_TYPES:
            ch["type"] = "other"
            report["normalized"].append(f"recommended_changes[{i}].type '{t}' -> 'other'")

    # Consistency: a non-passing verdict should name what's missing; a passing one
    # shouldn't simultaneously list unresolved problems.
    gaps = alignment["alignment_gaps"]
    unmitigated = [
        c for c in alignment["confounds"]
        if isinstance(c, dict) and not str(c.get("mitigation") or "").strip()
    ]
    if verdict in ("no", "partial") and not gaps:
        report["inconsistencies"].append(f"verdict '{verdict}' but no alignment_gaps are listed.")
    if verdict == "yes" and (gaps or unmitigated):
        bits = [
            f"{len(gaps)} alignment gap(s)" if gaps else "",
            f"{len(unmitigated)} unmitigated confound(s)" if unmitigated else "",
        ]
        report["inconsistencies"].append(
            "verdict 'yes' but the assessment still lists " + ", ".join(b for b in bits if b) + "."
        )

    alignment["validation_report"] = report
    return report


_CORRECTNESS_SEVERITY = {"critical": 0, "major": 1, "minor": 2}


def validate_correctness_review(review: dict, resolver: Resolver = resolve_citation) -> dict:
    """Normalize the adversarial correctness review and verify any citations its findings
    cite (a finding may back an "out-of-range value" claim with a paper). Coerces the
    verdict/severity enums, drops malformed findings, resolves cited evidence (unverifiable
    citations are nulled but the finding stays — the concern still stands), and sorts
    most-severe first. Mutates `review`; returns a small report. The review itself is
    model-generated reasoning, not a host guarantee."""
    report = {"citations_checked": 0, "verified": [], "downgraded": []}
    findings = review.get("findings")
    if not isinstance(findings, list):
        findings = []
    clean = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("severity") not in _CORRECTNESS_SEVERITY:
            f["severity"] = "major"
        cit = f.get("citation")
        if cit:
            report["citations_checked"] += 1
            ok, resolved, reason = check_citation(cit, resolver)
            loc = f.get("location") or (f.get("problem", "?")[:40])
            if ok:
                f["citation_verified"] = True
                f["citation"]["url"] = cit.get("url") or (_canonical_url(resolved) if resolved else None)
                report["verified"].append(loc)
            else:
                f["citation"] = None
                f["citation_verified"] = False
                report["downgraded"].append({"location": loc, "reason": reason})
        clean.append(f)
    clean.sort(key=lambda x: _CORRECTNESS_SEVERITY.get(x.get("severity"), 1))
    review["findings"] = clean
    # Keep the verdict honest against what actually survived.
    if review.get("verdict") not in ("sound", "issues_found", "serious_issues"):
        review["verdict"] = "issues_found" if clean else "sound"
    if not clean:
        review["verdict"] = "sound"
    elif any(f.get("severity") == "critical" for f in clean) and review["verdict"] == "sound":
        review["verdict"] = "serious_issues"
    review["validation_report"] = report
    return report


def validate_assay_options(opts: dict, resolver: Resolver = resolve_citation) -> dict:
    """Verify the citation on each candidate assay (hypothesis-first discovery). Keeps
    verified literature_grounded assays (with a canonical url), downgrades unverifiable
    ones to best_practice, strips stray citations, and repairs a dangling
    recommended_assay_id. Mutates `opts`; returns a small report."""
    report = {"citations_checked": 0, "verified": [], "downgraded": [], "recommended_repaired": False}
    assays = opts.get("assays") or []
    for assay in assays:
        prov = assay.get("provenance")
        cit = assay.get("citation")
        name = assay.get("name", "?")

        if prov == "literature_grounded" and not cit:
            assay["provenance"] = "best_practice"
            assay["citation"] = None
            assay["citation_verified"] = False
            report["downgraded"].append({"assay": name, "reason": "no citation"})
            continue
        if prov != "literature_grounded" and cit:
            assay["citation"] = None  # stray citation on a non-grounded assay
            continue
        if not cit:
            continue

        report["citations_checked"] += 1
        ok, resolved, reason = check_citation(cit, resolver)
        if ok:
            assay["citation_verified"] = True
            assay["citation"]["url"] = cit.get("url") or (_canonical_url(resolved) if resolved else None)
            report["verified"].append({"assay": name, "identifier": cit.get("identifier")})
        else:
            assay["provenance"] = "best_practice"
            assay["citation"] = None
            assay["citation_verified"] = False
            report["downgraded"].append(
                {"assay": name, "identifier": cit.get("identifier"), "reason": reason}
            )

    # Repair a recommended_assay_id that points at no assay.
    ids = [a.get("id") for a in assays]
    if assays and opts.get("recommended_assay_id") not in ids:
        opts["recommended_assay_id"] = ids[0]
        report["recommended_repaired"] = True

    opts["validation_report"] = report
    return report
