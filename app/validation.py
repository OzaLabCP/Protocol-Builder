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


def _iter_citation_entries(protocol: dict) -> Iterator[tuple[dict, str]]:
    """Yield (entry, human-readable location) for every value that carries a
    provenance tier and may carry a citation."""
    for i, mat in enumerate(protocol.get("materials") or []):
        yield mat, f"materials[{i}] '{mat.get('name', '?')}'"
    for step in protocol.get("steps") or []:
        snum = step.get("number", "?")
        for j, cp in enumerate(step.get("critical_parameters") or []):
            yield cp, f"step {snum} critical_parameters[{j}] '{cp.get('name', '?')}'"
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
    entry["citation_verified"] = False
    if "verify" in entry or "basis" in entry:  # assumptions_log shape
        entry["verify"] = True
    existing = entry.get("provenance_note") or ""
    entry["provenance_note"] = (existing + " " if existing else "") + f"[VALIDATION] {note}"


def validate_and_finalize(
    protocol: dict, resolver: Resolver = resolve_citation, *, allow_stated: bool = True
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
        "consistency": {"inline_missing_from_log": [], "log_missing_from_inline": []},
    }
    open_questions = list(protocol.get("open_questions") or [])

    if not allow_stated:
        for entry, location in _iter_all_provenance_entries(protocol):
            if entry.get("provenance") == "stated":
                _downgrade(entry, "tagged stated but this protocol has no source document.")
                report["stated_downgrades"].append(location)
                open_questions.append(
                    f"{location}: tagged 'stated' but there is no source document "
                    f"(hypothesis-first draft); downgraded to default_verify."
                )

    for entry, location in _iter_citation_entries(protocol):
        prov = entry.get("provenance")
        citation = entry.get("citation")
        selected = bool(entry.get("selected_by_user"))

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
            _downgrade(entry, reason + "; downgraded to default_verify.")
            report["downgraded"].append(
                {"location": location, "identifier": identifier, "reason": reason}
            )
            open_questions.append(
                f"{location}: citation '{identifier}' resolved but metadata "
                f"mismatched ({'; '.join(bits)}); downgraded to default_verify."
            )
            continue

        # Verified.
        entry["citation_verified"] = True
        entry["citation"]["url"] = entry["citation"].get("url") or _canonical_url(resolved)
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
